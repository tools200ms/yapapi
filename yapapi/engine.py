import asyncio
import aiohttp
from asyncio import CancelledError
from collections import defaultdict
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import itertools
import logging
import os
import sys
from typing import (
    AsyncContextManager,
    Awaitable,
    Callable,
    cast,
    Dict,
    Iterator,
    List,
    Optional,
    Set,
    Type,
    Union,
)
from typing_extensions import Final, AsyncGenerator

if sys.version_info >= (3, 7):
    from contextlib import AsyncExitStack
else:
    from async_exit_stack import AsyncExitStack  # type: ignore

from yapapi import rest, events
from yapapi.agreements_pool import AgreementsPool
from yapapi.ctx import WorkContext
from yapapi.payload import Payload
from yapapi import props
from yapapi.props import com
from yapapi.props.builder import DemandBuilder, DemandDecorator
from yapapi.rest.activity import Activity
from yapapi.rest.market import Agreement, OfferProposal, Subscription
from yapapi.script import Script
from yapapi.script.command import BatchCommand
from yapapi.storage import gftp
from yapapi.strategy import (
    DecreaseScoreForUnconfirmedAgreement,
    LeastExpensiveLinearPayuMS,
    MarketStrategy,
    SCORE_NEUTRAL,
)
from yapapi.utils import AsyncWrapper


DEBIT_NOTE_MIN_TIMEOUT: Final[int] = 30  # in seconds
"Shortest debit note acceptance timeout the requestor will accept."

DEBIT_NOTE_ACCEPTANCE_TIMEOUT_PROP: Final[str] = "golem.com.payment.debit-notes.accept-timeout?"

DEFAULT_DRIVER: str = os.getenv("YAGNA_PAYMENT_DRIVER", "erc20").lower()
DEFAULT_NETWORK: str = os.getenv("YAGNA_PAYMENT_NETWORK", "rinkeby").lower()
DEFAULT_SUBNET: Optional[str] = os.getenv("YAGNA_SUBNET", "devnet-beta")


logger = logging.getLogger("yapapi.executor")


class NoPaymentAccountError(Exception):
    """The error raised if no payment account for the required driver/network is available."""

    required_driver: str
    """Payment driver required for the account."""

    required_network: str
    """Network required for the account."""

    def __init__(self, required_driver: str, required_network: str):
        """Initialize `NoPaymentAccountError`.

        :param required_driver: payment driver for which initialization was required
        :param required_network: payment network for which initialization was required
        """
        self.required_driver: str = required_driver
        self.required_network: str = required_network

    def __str__(self) -> str:
        return (
            f"No payment account available for driver `{self.required_driver}`"
            f" and network `{self.required_network}`"
        )


# Type aliases to make some type annotations more meaningful
JobId = str
AgreementId = str


class _Engine:
    """Base execution engine containing functions common to all modes of operation."""

    def __init__(
        self,
        *,
        budget: Union[float, Decimal],
        strategy: Optional[MarketStrategy] = None,
        subnet_tag: Optional[str] = None,
        payment_driver: Optional[str] = None,
        payment_network: Optional[str] = None,
        stream_output: bool = False,
        app_key: Optional[str] = None,
    ):
        """Initialize the engine.

        :param budget: maximum budget for payments
        :param strategy: market strategy used to select providers from the market
            (e.g. LeastExpensiveLinearPayuMS or DummyMS)
        :param subnet_tag: use only providers in the subnet with the subnet_tag name.
            Uses `YAGNA_SUBNET` environment variable, defaults to `None`
        :param payment_driver: name of the payment driver to use. Uses `YAGNA_PAYMENT_DRIVER`
            environment variable, defaults to `erc20`. Only payment platforms with
            the specified driver will be used
        :param payment_network: name of the payment network to use. Uses `YAGNA_PAYMENT_NETWORK`
        environment variable, defaults to `rinkeby`. Only payment platforms with the specified
            network will be used
        :param stream_output: stream computation output from providers
        :param app_key: optional Yagna application key. If not provided, the default is to
                        get the value from `YAGNA_APPKEY` environment variable
        """
        self._api_config = rest.Configuration(app_key)
        self._budget_amount = Decimal(budget)
        self._budget_allocations: List[rest.payment.Allocation] = []
        self._wrapped_consumers: List[AsyncWrapper] = []

        if not strategy:
            strategy = LeastExpensiveLinearPayuMS(
                max_fixed_price=Decimal("1.0"),
                max_price_for={com.Counter.CPU: Decimal("0.2"), com.Counter.TIME: Decimal("0.1")},
            )
            # The factor 0.5 below means that an offer for a provider that failed to confirm
            # the last agreement proposed to them will have it's score multiplied by 0.5.
            strategy = DecreaseScoreForUnconfirmedAgreement(strategy, 0.5)
        self._strategy = strategy

        self._subnet: Optional[str] = subnet_tag or DEFAULT_SUBNET
        self._payment_driver: str = payment_driver.lower() if payment_driver else DEFAULT_DRIVER
        self._payment_network: str = payment_network.lower() if payment_network else DEFAULT_NETWORK
        self._stream_output = stream_output

        # a set of `Job` instances used to track jobs - computations or services - started
        # it can be used to wait until all jobs are finished
        self._jobs: Set[Job] = set()

        # initialize the payment structures
        self._agreements_to_pay: Dict[JobId, Set[AgreementId]] = defaultdict(set)
        self._agreements_accepting_debit_notes: Dict[JobId, Set[AgreementId]] = defaultdict(set)
        self._invoices: Dict[AgreementId, rest.payment.Invoice] = dict()
        self._payment_closing: bool = False

        self._process_invoices_job: Optional[asyncio.Task] = None

        # a set of async generators created by executors that use this engine
        self._generators: Set[AsyncGenerator] = set()
        self._services: Set[asyncio.Task] = set()
        self._stack = AsyncExitStack()

        #   Separate stack for event consumers - because we want to ensure that they are the last
        #   thing to exit no matter when add_event_consumer was called.
        self._wrapped_consumer_stack = AsyncExitStack()
        self._stack.push_async_exit(self._wrapped_consumer_stack.__aexit__)

        self._started = False

        #   All agreements ever used within this Engine will be stored here
        self._all_agreements: Dict[str, Agreement] = {}

    async def create_demand_builder(
        self, expiration_time: datetime, payload: Payload
    ) -> DemandBuilder:
        """Create a `DemandBuilder` for given `payload` and `expiration_time`."""
        builder = DemandBuilder()
        builder.add(props.Activity(expiration=expiration_time, multi_activity=True))
        builder.add(props.NodeInfo(subnet_tag=self._subnet))
        if self._subnet:
            builder.ensure(f"({props.NodeInfoKeys.subnet_tag}={self._subnet})")
        await builder.decorate(self.payment_decorator, self.strategy, payload)
        return builder

    @property
    def payment_driver(self) -> str:
        """Return the name of the payment driver used by this engine."""
        return self._payment_driver

    @property
    def payment_network(self) -> str:
        """Return the name of the payment payment network used by this engine."""
        return self._payment_network

    @property
    def storage_manager(self):
        """Return the storage manager used by this engine."""
        return self._storage_manager

    @property
    def strategy(self) -> MarketStrategy:
        """Return the instance of `MarketStrategy` used by this engine."""
        return self._strategy

    @property
    def subnet_tag(self) -> Optional[str]:
        """Return the name of the subnet used by this engine, or `None` if it is not set."""
        return self._subnet

    @property
    def started(self) -> bool:
        """Return `True` if this instance is initialized, `False` otherwise."""
        return self._started

    async def add_event_consumer(self, event_consumer: Callable[[events.Event], None]) -> None:
        """All events emited via `self.emit` will be passed to this callable

        NOTE: after this method was called (either on an already started Engine or not),
              stop() is required for a clean shutdown.
        """
        # Add buffering to the provided event emitter to make sure
        # that emitting events will not block
        wrapped_consumer = AsyncWrapper(event_consumer)

        await self._wrapped_consumer_stack.enter_async_context(wrapped_consumer)

        self._wrapped_consumers.append(wrapped_consumer)

    def emit(self, event_class: Type[events.Event], **kwargs) -> events.Event:
        """Emit an event to be consumed by this engine's event consumer."""
        event = self._resolve_emit_args(event_class, **kwargs)

        for wrapped_consumer in self._wrapped_consumers:
            wrapped_consumer.async_call(event)

        return event

    @staticmethod
    def _resolve_emit_args(event_class, **kwargs) -> events.Event:
        #   NOTE: This is a temporary function that will disappear once the new events are ready.
        #         Purpose of all these ugly hacks here is to allow gradual changes of
        #         events interface (so that everything works as before after only some parts changed).
        #         IOW, to have a dual old-new interface while the new interface is replacing the old one.
        import attr

        event_class_fields = [f.name for f in attr.fields(event_class)]

        #   Set all fields that are not just ids of the passed objects
        if "invoice" in kwargs and "amount" in event_class_fields:
            kwargs["amount"] = kwargs["invoice"].amount

        if "debit_note" in kwargs and "amount" in event_class_fields:
            kwargs["amount"] = kwargs["debit_note"].total_amount_due

        if "job" in kwargs and "expires" in event_class_fields:
            kwargs["expires"] = kwargs["job"].expiration_time

        if "job" in kwargs and "num_offers" in event_class_fields:
            kwargs["num_offers"] = kwargs["job"].offers_collected

        if "script" in kwargs and "cmds" in event_class_fields:
            kwargs["cmds"] = kwargs["script"]._evaluate()

        if "proposal" in kwargs and "provider_id" in event_class_fields:
            kwargs["provider_id"] = kwargs["proposal"].issuer

        if "command" in kwargs and "path" in event_class_fields:
            if event_class.__name__ == "DownloadStarted":
                path = kwargs["command"]._src_path
            else:
                assert event_class.__name__ == "DownloadFinished"
                path = str(kwargs["command"]._dst_path)
            kwargs["path"] = path

        #   Set id fields and remove old objects
        for row in (
            ("job", "job_id"),
            ("script", "script_id"),
            ("proposal", "prop_id"),
            ("subscription", "sub_id"),
            ("invoice", "inv_id", "invoice_id"),
            ("debit_note", "debit_note_id", "debit_note_id"),
        ):
            object_name = row[0]
            event_field_name = row[1]
            object_id_field_name = "id" if len(row) < 3 else row[2]

            obj = kwargs.pop(object_name, None)
            if obj is not None and event_field_name in event_class_fields:
                kwargs[event_field_name] = getattr(obj, object_id_field_name)

        #   Command is different because we sometimes have "command" and never "command_id"
        if "command" in kwargs and "command" not in event_class_fields:
            del kwargs["command"]

        return event_class(**kwargs)  # type: ignore

    async def stop(self, *exc_info) -> Optional[bool]:
        """Stop the engine.

        This *must* be called at the end of the work, by the Engine user.
        """
        if exc_info[0] is not None:
            self.emit(events.ExecutionInterrupted, exc_info=exc_info)  # type: ignore
        return await self._stack.__aexit__(None, None, None)

    async def start(self):
        """Start the engine.

        This is supposed to be called exactly once. Repeated or interrupted call
        will leave the engine in an unrecoverable state.
        """

        stack = self._stack

        def report_shutdown(*exc_info):
            if any(item for item in exc_info):
                self.emit(events.ShutdownFinished, exc_info=exc_info)  # noqa
            else:
                self.emit(events.ShutdownFinished)

        stack.push(report_shutdown)

        market_client = await stack.enter_async_context(self._api_config.market())
        self._market_api = rest.Market(market_client)

        activity_client = await stack.enter_async_context(self._api_config.activity())
        self._activity_api = rest.Activity(activity_client)

        payment_client = await stack.enter_async_context(self._api_config.payment())
        self._payment_api = rest.Payment(payment_client)

        net_client = await stack.enter_async_context(self._api_config.net())
        self._net_api = rest.Net(net_client)

        # TODO replace with a proper REST API client once ya-client and ya-aioclient are updated
        # https://github.com/golemfactory/yapapi/issues/636
        self._root_api_session = await stack.enter_async_context(
            aiohttp.ClientSession(
                headers=net_client.default_headers,
            )
        )

        self.payment_decorator = _Engine.PaymentDecorator(await self._create_allocations())

        # TODO: make the method starting the process_invoices() task an async context manager
        # to simplify code in __aexit__()
        loop = asyncio.get_event_loop()
        self._process_invoices_job = loop.create_task(self._process_invoices())
        self._services.add(self._process_invoices_job)
        self._services.add(loop.create_task(self._process_debit_notes()))

        self._storage_manager = await stack.enter_async_context(gftp.provider())

        stack.push_async_exit(self._shutdown)

        self._started = True

    async def add_to_async_context(self, async_context_manager: AsyncContextManager) -> None:
        await self._stack.enter_async_context(async_context_manager)

    def _unpaid_agreement_ids(self) -> Set[AgreementId]:
        """Return the set of all yet unpaid agreement ids."""

        unpaid_agreement_ids = set()
        for job_id, agreement_ids in self._agreements_to_pay.items():
            unpaid_agreement_ids.update(agreement_ids)
        return unpaid_agreement_ids

    async def _shutdown(self, *exc_info):
        """Shutdown this Golem instance."""

        # Importing this at the beginning would cause circular dependencies
        from yapapi.log import pluralize

        logger.info("Golem is shutting down...")

        # Some generators created by `execute_tasks` may still have elements;
        # if we don't close them now, their jobs will never be marked as finished.
        for gen in self._generators:
            await gen.aclose()

        # Wait until all computations are finished
        logger.debug("Waiting for the jobs to finish...")
        await asyncio.gather(*[job.finished.wait() for job in self._jobs])
        logger.info("All jobs have finished")

        self._payment_closing = True

        # Cancel all services except the one that processes invoices
        for task in self._services:
            if task is not self._process_invoices_job:
                task.cancel()

        # Wait for some time for invoices for unpaid agreements,
        # then cancel the invoices service
        if self._process_invoices_job:

            unpaid_agreements = self._unpaid_agreement_ids()
            if unpaid_agreements:
                logger.info(
                    "%s still unpaid, waiting for invoices...",
                    pluralize(len(unpaid_agreements), "agreement"),
                )
                try:
                    await asyncio.wait_for(self._process_invoices_job, timeout=30)
                except asyncio.TimeoutError:
                    logger.debug("process_invoices_job cancelled")
                unpaid_agreements = self._unpaid_agreement_ids()
                if unpaid_agreements:
                    logger.warning("Unpaid agreements: %s", unpaid_agreements)

            self._process_invoices_job.cancel()

        try:
            logger.info("Waiting for Golem services to finish...")
            _, pending = await asyncio.wait(
                self._services, timeout=10, return_when=asyncio.ALL_COMPLETED
            )
            if pending:
                logger.debug("%s still running: %s", pluralize(len(pending), "service"), pending)
        except Exception:
            logger.debug("Got error when waiting for services to finish", exc_info=True)

    async def _create_allocations(self) -> rest.payment.MarketDecoration:

        if not self._budget_allocations:
            async for account in self._payment_api.accounts():
                driver = account.driver.lower()
                network = account.network.lower()
                if (driver, network) != (self._payment_driver, self._payment_network):
                    logger.debug(
                        "Not using payment platform `%s`, platform's driver/network "
                        "`%s`/`%s` is different than requested driver/network `%s`/`%s`",
                        account.platform,
                        driver,
                        network,
                        self._payment_driver,
                        self._payment_network,
                    )
                    continue
                logger.debug("Creating allocation using payment platform `%s`", account.platform)
                allocation = cast(
                    rest.payment.Allocation,
                    await self._stack.enter_async_context(
                        self._payment_api.new_allocation(
                            self._budget_amount,
                            payment_platform=account.platform,
                            payment_address=account.address,
                            #   TODO what do to with this?
                            #   expires=self._expires + CFG_INVOICE_TIMEOUT,
                        )
                    ),
                )
                self._budget_allocations.append(allocation)

            if not self._budget_allocations:
                raise NoPaymentAccountError(self._payment_driver, self._payment_network)

        allocation_ids = [allocation.id for allocation in self._budget_allocations]
        return await self._payment_api.decorate_demand(allocation_ids)

    def _get_allocation(
        self, item: Union[rest.payment.DebitNote, rest.payment.Invoice]
    ) -> rest.payment.Allocation:
        try:
            return next(
                allocation
                for allocation in self._budget_allocations
                if allocation.payment_address == item.payer_addr
                and allocation.payment_platform == item.payment_platform
            )
        except:
            raise ValueError(f"No allocation for {item.payment_platform} {item.payer_addr}.")

    async def _process_invoices(self) -> None:
        """Process incoming invoices."""

        async for invoice in self._payment_api.incoming_invoices():
            job_id = next(
                (
                    id
                    for id in self._agreements_to_pay
                    if invoice.agreement_id in self._agreements_to_pay[id]
                ),
                None,
            )
            if job_id is not None:
                job = self._get_job_by_id(job_id)
                agreement = self._get_agreement_by_id(invoice.agreement_id)
                job.emit(
                    events.InvoiceReceived,
                    agreement=agreement,
                    invoice=invoice,
                )
                try:
                    allocation = self._get_allocation(invoice)
                    await invoice.accept(amount=invoice.amount, allocation=allocation)
                    job.emit(
                        events.InvoiceAccepted,
                        agreement=agreement,
                        invoice=invoice,
                    )
                except CancelledError:
                    raise
                except Exception:
                    job.emit(
                        events.PaymentFailed,
                        agreement=agreement,
                        exc_info=sys.exc_info(),  # type: ignore
                    )
                else:
                    self._agreements_to_pay[job_id].remove(invoice.agreement_id)
                    assert invoice.agreement_id in self._agreements_accepting_debit_notes[job_id]
                    self._agreements_accepting_debit_notes[job_id].remove(invoice.agreement_id)
            else:
                self._invoices[invoice.agreement_id] = invoice
            if self._payment_closing and not any(
                agr_ids for agr_ids in self._agreements_to_pay.values()
            ):
                break

    async def _process_debit_notes(self) -> None:
        """Process incoming debit notes."""

        async for debit_note in self._payment_api.incoming_debit_notes():
            job_id = next(
                (
                    id
                    for id in self._agreements_accepting_debit_notes
                    if debit_note.agreement_id in self._agreements_accepting_debit_notes[id]
                ),
                None,
            )
            if job_id is not None:
                job = self._get_job_by_id(job_id)
                agreement = self._get_agreement_by_id(debit_note.agreement_id)
                job.emit(
                    events.DebitNoteReceived,
                    agreement=agreement,
                    debit_note=debit_note,
                )
                try:
                    allocation = self._get_allocation(debit_note)
                    await debit_note.accept(
                        amount=debit_note.total_amount_due, allocation=allocation
                    )
                    job.emit(
                        events.DebitNoteAccepted,
                        agreement=agreement,
                        debit_note=debit_note,
                    )
                except CancelledError:
                    raise
                except Exception:
                    job.emit(
                        events.PaymentFailed, agreement=agreement, exc_info=sys.exc_info()  # type: ignore
                    )
            if self._payment_closing and not self._agreements_to_pay:
                break

    async def accept_payments_for_agreement(
        self, job_id: str, agreement_id: str, *, partial: bool = False
    ) -> None:
        """Add given agreement to the set of agreements for which invoices should be accepted."""
        job = self._get_job_by_id(job_id)
        agreement = self._get_agreement_by_id(agreement_id)
        job.emit(events.PaymentPrepared, agreement=agreement)
        inv = self._invoices.get(agreement_id)
        if inv is None:
            self._agreements_to_pay[job_id].add(agreement_id)
            job.emit(events.PaymentQueued, agreement=agreement)
            return
        del self._invoices[agreement_id]
        allocation = self._get_allocation(inv)
        await inv.accept(amount=inv.amount, allocation=allocation)
        job.emit(events.InvoiceAccepted, agreement=agreement, invoice=inv)

    def accept_debit_notes_for_agreement(self, job_id: str, agreement_id: str) -> None:
        """Add given agreement to the set of agreements for which debit notes should be accepted."""
        self._agreements_accepting_debit_notes[job_id].add(agreement_id)

    def add_job(self, job: "Job"):
        """Register a job with this engine."""
        self._jobs.add(job)
        job.emit(events.ComputationStarted)

    def finalize_job(self, job: "Job"):
        """Mark a job as finished."""
        job.finished.set()
        job.emit(events.ComputationFinished, exc_info=job._exc_info)  # type: ignore

    def register_generator(self, generator: AsyncGenerator) -> None:
        """Register a generator with this engine."""
        self._generators.add(generator)

    @dataclass
    class PaymentDecorator(DemandDecorator):
        """A `DemandDecorator` that adds payment-related constraints and properties to a Demand."""

        market_decoration: rest.payment.MarketDecoration

        async def decorate_demand(self, demand: DemandBuilder):
            """Add properties and constraints to a Demand."""
            for constraint in self.market_decoration.constraints:
                demand.ensure(constraint)
            demand.properties.update({p.key: p.value for p in self.market_decoration.properties})

    async def create_activity(self, agreement_id: str) -> Activity:
        """Create an activity for given `agreement_id`."""
        return await self._activity_api.new_activity(
            agreement_id, stream_events=self._stream_output
        )

    async def start_worker(
        self, job: "Job", run_worker: Callable[[WorkContext], Awaitable]
    ) -> Optional[asyncio.Task]:
        loop = asyncio.get_event_loop()

        async def worker_task(agreement: Agreement):
            """A coroutine run by every worker task.

            It creates an Activity for a given Agreement, then creates a WorkContext for this Activity
            and then executes `run_worker` with this WorkContext.
            """
            self._all_agreements[agreement.id] = agreement

            job.emit(events.WorkerStarted, agreement=agreement)

            try:
                activity = await self.create_activity(agreement.id)
            except Exception:
                job.emit(events.ActivityCreateFailed, agreement=agreement, exc_info=sys.exc_info())  # type: ignore
                raise

            work_context = WorkContext(activity, agreement, self.storage_manager, emitter=job.emit)
            work_context.emit(events.ActivityCreated)

            async with activity:
                self.accept_debit_notes_for_agreement(job.id, agreement.id)
                await run_worker(work_context)

        return await job.agreements_pool.use_agreement(
            lambda agreement: loop.create_task(worker_task(agreement))
        )

    async def process_batches(
        self,
        job_id: str,
        agreement_id: str,
        activity: rest.activity.Activity,
        batch_generator: AsyncGenerator[Script, Awaitable[List[events.CommandEvent]]],
    ) -> None:
        """Send command batches produced by `batch_generator` to `activity`."""

        script: Script = await batch_generator.__anext__()

        while True:
            batch_deadline = (
                datetime.now(timezone.utc) + script.timeout if script.timeout is not None else None
            )

            try:
                await script._before()
                batch: List[BatchCommand] = script._evaluate()
                remote = await activity.send(batch, deadline=batch_deadline)
            except Exception:
                script = await batch_generator.athrow(*sys.exc_info())
                continue

            script.emit(events.ScriptSent)

            async def get_batch_results() -> List[events.CommandEvent]:
                results = []
                async for event_class, event_kwargs in remote:
                    event = script.process_batch_event(event_class, event_kwargs)
                    results.append(event)

                script.emit(events.GettingResults)
                await script._after()
                script.emit(events.ScriptFinished)
                await self.accept_payments_for_agreement(job_id, agreement_id, partial=True)
                return results

            loop = asyncio.get_event_loop()

            if script.wait_for_results:
                # Block until the results are available
                try:
                    future_results = loop.create_future()
                    results = await get_batch_results()
                    future_results.set_result(results)
                except Exception:
                    # Raise the exception in `batch_generator` (the `worker` coroutine).
                    # If the client code is able to handle it then we'll proceed with
                    # subsequent batches. Otherwise the worker finishes with error.
                    script = await batch_generator.athrow(*sys.exc_info())
                else:
                    script = await batch_generator.asend(future_results)

            else:
                # Schedule the coroutine in a separate asyncio task
                future_results = loop.create_task(get_batch_results())
                script = await batch_generator.asend(future_results)

    def recycle_offer(self, offer: OfferProposal) -> None:
        """This offer was already processed, but something happened and we should treat it as a fresh one.

        Currently this "something" is always "we couldn't confirm the agreement with the provider",
        but someday this might be useful also in other scenarios.

        Purpose:
        *   we want to rescore the offer (score might change because of the event that caused recycling)
        *   if this is a draft offer (and in the current usecase it always is), we want to return to the
            negotiations and get a new draft - e.g. because this draft already has an Agreement

        We don't care which Job initiated recycling - it should be recycled by all unfinished Jobs.
        """
        unfinished_jobs = (job for job in self._jobs if not job.finished.is_set())
        for job in unfinished_jobs:
            asyncio.get_event_loop().create_task(job._handle_proposal(offer, ignore_draft=True))

    def _get_job_by_id(self, job_id) -> "Job":
        try:
            return next(job for job in self._jobs if job.id == job_id)
        except StopIteration:
            raise KeyError(f"This _Engine doesn't know job with id {job_id}")

    def _get_agreement_by_id(self, agreement_id) -> Agreement:
        try:
            return self._all_agreements[agreement_id]
        except KeyError:
            raise KeyError(f"This _Engine never used agreement with id {agreement_id}")


class Job:
    """Functionality related to a single job.

    Responsible for posting a Demand to market and collecting Offer proposals for the Demand.
    """

    # Infinite supply of generated job ids: "1", "2", ...
    # We prefer short ids since they would make log messages more readable.
    _generated_job_ids: Iterator[str] = (str(n) for n in itertools.count(1))

    # Job ids already used, tracked to avoid duplicates
    _used_job_ids: Set[str] = set()

    def __init__(
        self,
        engine: _Engine,
        expiration_time: datetime,
        payload: Payload,
        id: Optional[str] = None,
    ):
        """Initialize a `Job` instance.

        param engine: a `Golem` engine which will run this job
        param expiration_time: expiration time for the job; all agreements created for this job
            must expire before this date
        param payload: definition of a service runtime or a runtime package that needs to
            be deployed on providers for executing this job
        param id: an optional string to be used to identify this job in events emitted
            by the engine. ValueError is raised if a job with the same id has already been created.
            If not specified, a unique identifier will be generated.
        """

        if id:
            if id in Job._used_job_ids:
                raise ValueError(f"Non unique job id {id}")
            self.id = id
        else:
            self.id = next(id for id in Job._generated_job_ids if id not in Job._used_job_ids)
        Job._used_job_ids.add(self.id)

        self.engine = engine
        self.offers_collected: int = 0
        self.proposals_confirmed: int = 0
        self.expiration_time: datetime = expiration_time
        self.payload: Payload = payload

        self.agreements_pool = AgreementsPool(self.id, self.emit, self.engine.recycle_offer)
        self.finished = asyncio.Event()

        self._demand_builder: Optional[DemandBuilder] = None

        #   Exception that ended the job
        self._exc_info = None

    def emit(self, event_class: Type[events.Event], **kwargs) -> events.Event:
        return self.engine.emit(event_class, job=self, **kwargs)

    def set_exc_info(self, exc_info):
        assert self._exc_info is None, "We can't have more than one exc_info ending a job"
        self._exc_info = exc_info

    async def _handle_proposal(
        self,
        proposal: OfferProposal,
        ignore_draft: bool = False,
    ) -> events.Event:
        """Handle a single `OfferProposal`.

        A `proposal` is scored and then can be rejected, responded with
        a counter-proposal or stored in an agreements pool to be used
        for negotiating an agreement.

        If `ignore_draft` is True, we either reject, or respond with a counter-proposal,
        but never pass the proposal to the agreements pool.
        """

        async def reject_proposal(reason: str) -> events.Event:
            """Reject `proposal` due to given `reason`."""
            await proposal.reject(reason)
            return self.emit(events.ProposalRejected, proposal=proposal, reason=reason)

        score = await self.engine._strategy.score_offer(proposal, self.agreements_pool)
        logger.debug(
            "Scored offer %s, provider: %s, strategy: %s, score: %f",
            proposal.id,
            proposal.props.get("golem.node.id.name"),
            type(self.engine._strategy).__name__,
            score,
        )

        if score < SCORE_NEUTRAL:
            return await reject_proposal("Score too low")

        if ignore_draft or not proposal.is_draft:
            demand_builder = self._demand_builder
            assert demand_builder is not None

            # Check if any of the supported payment platforms matches the proposal
            common_platforms = self._get_common_payment_platforms(proposal)
            if common_platforms:
                demand_builder.properties["golem.com.payment.chosen-platform"] = next(
                    iter(common_platforms)
                )
            else:
                # reject proposal if there are no common payment platforms
                return await reject_proposal("No common payment platform")

            # Check if the timeout for debit note acceptance is not too low
            timeout = proposal.props.get(DEBIT_NOTE_ACCEPTANCE_TIMEOUT_PROP)
            if timeout:
                if timeout < DEBIT_NOTE_MIN_TIMEOUT:
                    return await reject_proposal("Debit note acceptance timeout is too short")
                else:
                    demand_builder.properties[DEBIT_NOTE_ACCEPTANCE_TIMEOUT_PROP] = timeout

            await proposal.respond(demand_builder.properties, demand_builder.constraints)
            return self.emit(events.ProposalResponded, proposal=proposal)

        else:
            await self.agreements_pool.add_proposal(score, proposal)
            return self.emit(events.ProposalConfirmed, proposal=proposal)

    async def _find_offers_for_subscription(
        self,
        subscription: Subscription,
    ) -> None:
        """Create a market subscription and repeatedly collect offer proposals for it.

        Collected proposals are processed concurrently using a bounded number
        of asyncio tasks.

        :param state: A state related to a call to `Executor.submit()`
        """
        max_number_of_tasks = 5

        try:
            proposals = subscription.events()
        except Exception as ex:
            self.emit(events.CollectFailed, subscription=subscription, reason=str(ex))
            raise

        # A semaphore is used to limit the number of handler tasks
        semaphore = asyncio.Semaphore(max_number_of_tasks)

        async for proposal in proposals:

            self.emit(events.ProposalReceived, proposal=proposal)
            self.offers_collected += 1

            async def handler(proposal_):
                """Wrap `_handle_proposal()` method with error handling."""
                try:
                    event = await self._handle_proposal(proposal_)
                    assert isinstance(event, events.ProposalEvent)
                    if isinstance(event, events.ProposalConfirmed):
                        self.proposals_confirmed += 1
                except CancelledError:
                    raise
                except Exception:
                    with contextlib.suppress(Exception):
                        self.emit(events.ProposalFailed, proposal=proposal_, exc_info=sys.exc_info())  # type: ignore
                finally:
                    semaphore.release()

            # Create a new handler task
            await semaphore.acquire()
            asyncio.get_event_loop().create_task(handler(proposal))

    async def find_offers(self) -> None:
        """Create demand subscription and process offers.

        When the subscription expires, create a new one. And so on...
        """
        if self._demand_builder is None:
            self._demand_builder = await self.engine.create_demand_builder(
                self.expiration_time, self.payload
            )

        while True:
            try:
                subscription = await self._demand_builder.subscribe(self.engine._market_api)
                self.emit(events.SubscriptionCreated, subscription=subscription)
            except Exception as ex:
                self.emit(events.SubscriptionFailed, reason=str(ex))
                raise
            async with subscription:
                await self._find_offers_for_subscription(subscription)

    # TODO: move to Golem
    def _get_common_payment_platforms(self, proposal: rest.market.OfferProposal) -> Set[str]:
        prov_platforms = {
            property.split(".")[4]
            for property in proposal.props
            if property.startswith("golem.com.payment.platform.") and property is not None
        }
        if not prov_platforms:
            prov_platforms = {"NGNT"}
        req_platforms = {
            allocation.payment_platform
            for allocation in self.engine._budget_allocations
            if allocation.payment_platform is not None
        }
        return req_platforms.intersection(prov_platforms)
