"""Connection lifecycle and durable engineering refresh orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .config_impact import async_find_engineering_change_impacts, async_publish_engineering_impact_plan
from .const import CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL
from .engineering_capabilities import ExposureStatus, resolve_engineering_capabilities
from .engineering_changes import EngineeringImpactPlan
from .engineering_config import EngineeringInventory, download_engineering_inventory
from .engineering_entities import build_engineering_entity_specs, engineering_inventory_updated_signal
from .device_sync import async_sync_control_entity_devices
from .engineering_registry import (
    EngineeringRegistryMetadata,
    async_apply_engineering_registry_plan,
    async_filter_entity_identity_conflicts,
    async_plan_engineering_registry_sync,
    registry_metadata_from_snapshot,
    engineering_area_operation_lock,
)
from .engineering_runtime import (
    RUNTIME_PROBE_CONCURRENCY,
    EngineeringRuntimeInventory,
    RuntimeProbeClient,
    async_probe_engineering_runtime,
)
from .engineering_snapshot import (
    EngineeringSnapshot,
    EngineeringSnapshotError,
    EngineeringStoreCommitCancelledError,
    EngineeringStoreCommitOutcome,
    StoredEngineeringState,
    async_load_engineering_state,
    async_store_engineering_state,
    engineering_configuration_revision_id,
    engineering_generation_id,
    engineering_safe_content_digest,
    next_engineering_read_sequence,
    snapshot_from_dict,
    snapshot_to_dict,
)
from .engineering_topology import EngineeringSourceContext, resolve_engineering_topology
from .miniserver import MiniServer
from .pyloxone_api.connection import LoxoneConnection
from .registry_maintenance import async_run_registry_maintenance
from .repairs import (
    async_register_engineering_area_conflict_reconciler,
    async_sync_engineering_area_conflict_issues,
)

_LOGGER = logging.getLogger(__name__)
_SOURCE_MISMATCH = "engineering source does not match connected provider"
_REGISTRY_PENDING = "engineering registry application is pending"
_PROBE_FAILED = "engineering runtime verification failed"

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def extract_loxapp_last_modified(lox_config: Mapping) -> str | None:
    """Normalize finite scalar revisions into a private stable token."""
    value = lox_config.get("lastModified")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return f"source:{hashlib.sha256(normalized.encode()).hexdigest()}"


def _engineering_room_identity_incomplete(snapshot: EngineeringSnapshot) -> bool:
    """Detect migrated snapshots that retained room labels but predate room UUIDs."""
    return any(node.element.room is not None and node.element.room_uuid is None for node in snapshot.nodes)


class LoxoneCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Loxone Miniserver."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger=_LOGGER,
            name="PyLoxone Coordinator",
            config_entry=config_entry,
            update_method=None,  # Not polling!
        )
        self.config_entry = config_entry
        config_entry.async_on_unload(
            async_register_engineering_area_conflict_reconciler(
                hass,
                config_entry,
                self,
            )
        )
        self._username = config_entry.options[CONF_USERNAME]
        self._password = config_entry.options[CONF_PASSWORD]
        self._host = config_entry.options[CONF_HOST]
        self._port = config_entry.options[CONF_PORT]
        self._verify_ssl = config_entry.options.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)

        self.api: LoxoneConnection | None = None
        self.miniserver: MiniServer | None = None
        self.listeners = []
        self.engineering_inventory: EngineeringInventory | None = None
        self.engineering_runtime: EngineeringRuntimeInventory | None = None
        self.engineering_snapshot: EngineeringSnapshot | None = None
        self._engineering_refresh_task: asyncio.Task | None = None
        self._engineering_refresh_lock = asyncio.Lock()
        self._engineering_schedule_lock = asyncio.Lock()
        self._engineering_registry_verified_generation: str | None = None
        self._engineering_signaled_generation: str | None = None
        self._engineering_published_generation: str | None = None
        self._engineering_published_impact_plan: EngineeringImpactPlan | None = None
        self._listening_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._unloading = False
        self.engineering_refresh_stage = "idle"

    async def async_config_entry_first_refresh(self) -> None:
        """Open the connection and initialize the ordinary LoxAPP model."""
        _LOGGER.debug("async_config_entry_first_refresh")
        if self.api and self.api.connection:
            await self.api.close()
            self.api.connection = None

        if "token" in self.config_entry.data:
            self.api = LoxoneConnection(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                token=self.config_entry.data,
                verify_ssl=self._verify_ssl,
            )
        else:
            self.api = LoxoneConnection(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
                verify_ssl=self._verify_ssl,
            )
        try:
            session = async_get_clientsession(self.hass)
            self.api.connection = await self.api.open(session)
        except Exception:
            # Connection exception strings may contain private endpoint data.
            _LOGGER.error("Could not connect to Loxone Miniserver")  # noqa: TRY400
            raise

        self.miniserver = MiniServer(self.hass, self.api.structure_file, self.config_entry)

    async def _async_update_data(self) -> None:
        """Retain the non-polling coordinator contract."""

    async def _async_download_engineering_inventory(self) -> EngineeringInventory:
        """Download complete engineering data into a local candidate."""
        return await self.hass.async_add_executor_job(
            lambda: download_engineering_inventory(
                self._host,
                self._username,
                self._password,
                verify_ssl=self._verify_ssl,
            )
        )

    def _engineering_runtime_client(self) -> RuntimeProbeClient:
        """Build the bounded GET-only client without retaining endpoints in state."""
        return RuntimeProbeClient(
            session=async_get_clientsession(self.hass),
            base_url=f"{self.api.scheme}://{self.api.url}",
            auth=aiohttp.BasicAuth(self._username, self._password, encoding="utf-8"),
            verify_ssl=self._verify_ssl,
            semaphore=asyncio.Semaphore(RUNTIME_PROBE_CONCURRENCY),
        )

    def _adopt_engineering_snapshot(self, snapshot: EngineeringSnapshot) -> None:
        """Install only the safe reconstructable projection in coordinator memory."""
        if self.engineering_snapshot is not None and self.engineering_snapshot.generation_id == snapshot.generation_id:
            return
        self.engineering_runtime = None
        self.engineering_snapshot = snapshot
        self.engineering_inventory = EngineeringInventory(
            "",
            snapshot.source.config_version,
            snapshot.source.config_timestamp,
            snapshot.captured_at,
            0,
            tuple(node.element for node in snapshot.nodes),
        )

    def _validate_engineering_scope(self, snapshot: EngineeringSnapshot) -> None:
        if snapshot.source.entry_id != self.config_entry.entry_id or snapshot.source.provider_identifier != (
            self.miniserver.serial or self.config_entry.entry_id
        ):
            raise EngineeringSnapshotError(_SOURCE_MISMATCH)

    def _engineering_degraded_notification(self) -> None:
        persistent_notification.async_create(
            self.hass,
            "Engineering refresh is pending recovery. The last committed topology is retained; retry the refresh.",
            title="PyLoxone engineering status",
            notification_id=self._engineering_status_notification_id,
        )

    @property
    def _engineering_status_notification_id(self) -> str:
        provider = self.miniserver.serial or self.config_entry.entry_id
        return f"loxone_engineering_status_{self.config_entry.entry_id}_{provider}"

    def _signal_engineering_generation(self, generation: str) -> None:
        if self._engineering_signaled_generation == generation:
            return
        async_dispatcher_send(
            self.hass,
            engineering_inventory_updated_signal(self.config_entry.entry_id),
        )
        self._engineering_signaled_generation = generation

    async def async_drain_committed_engineering_state(self, *, startup: bool = False) -> None:
        """Finish one durable generation; callers hold the per-entry refresh lock."""
        self.engineering_refresh_stage = "recovery_load"
        state = await async_load_engineering_state(self.hass, self.config_entry.entry_id)
        if state.snapshot is None:
            return
        snapshot = state.snapshot
        self._validate_engineering_scope(snapshot)
        self._adopt_engineering_snapshot(snapshot)
        generation = snapshot.generation_id
        try:
            if (
                startup
                or state.registry_applied_generation != generation
                or self._engineering_registry_verified_generation != generation
            ):
                self.engineering_refresh_stage = "registry_apply"
                metadata = await self.hass.async_add_executor_job(
                    lambda: registry_metadata_from_snapshot(
                        snapshot,
                        managed_area_ids=state.managed_area_ids,
                        room_area_mappings=state.room_area_mappings,
                        device_area_fallbacks=state.device_area_fallbacks,
                        applied_generation=state.registry_applied_generation,
                    )
                )
                plan = await async_plan_engineering_registry_sync(
                    self.hass, self.config_entry.entry_id, snapshot, metadata
                )
                await async_apply_engineering_registry_plan(self.hass, plan, committed_state=state)
                state = await async_load_engineering_state(self.hass, self.config_entry.entry_id)
                if state.registry_applied_generation != generation:
                    raise EngineeringSnapshotError(_REGISTRY_PENDING)  # noqa: TRY301 -- checked inside recovery boundary.
                self._engineering_registry_verified_generation = generation
            self.engineering_refresh_stage = "repairs_sync"
            await async_sync_engineering_area_conflict_issues(
                self.hass,
                self.config_entry.entry_id,
                config_entry=self.config_entry,
                coordinator=self,
            )
            if not startup:
                self._signal_engineering_generation(generation)
            if startup or state.impact_published_generation != generation:
                self.engineering_refresh_stage = "impact_publish"
                if startup or self._engineering_published_generation != generation:
                    plan = state.pending_impact_plan or EngineeringImpactPlan(generation, ())
                    if (
                        startup
                        or self._engineering_published_impact_plan is None
                        or self._engineering_published_impact_plan.impacts != plan.impacts
                    ):
                        await async_publish_engineering_impact_plan(
                            self.hass,
                            self.config_entry,
                            snapshot.source.provider_identifier,
                            plan,
                            recheck=startup,
                        )
                    # A new read with unchanged evidence advances the cursor but
                    # does not undo a current-process user dismissal.
                    self._engineering_published_impact_plan = plan
                    self._engineering_published_generation = generation
                if state.impact_published_generation != generation:
                    async with engineering_area_operation_lock(self.hass, self.config_entry.entry_id):
                        state = await async_load_engineering_state(self.hass, self.config_entry.entry_id)
                        if state.snapshot is None or state.snapshot.generation_id != generation:
                            raise EngineeringSnapshotError(_REGISTRY_PENDING)  # noqa: TRY301 -- checked at recovery boundary.
                        state = replace(state, impact_published_generation=generation)
                        await async_store_engineering_state(self.hass, state)
            # Maintenance owns its separately persisted last-counted token. It
            # must still reevaluate elapsed-time eligibility on every drain.
            self.engineering_refresh_stage = "maintenance"
            await async_run_registry_maintenance(
                self.hass, self.config_entry, self.miniserver.lox_config.json, bounded_notification=True
            )
            linked_entities = async_sync_control_entity_devices(
                self.hass,
                self.config_entry,
                self.miniserver.lox_config.json,
                snapshot,
            )
            if linked_entities:
                _LOGGER.info(
                    "Linked %s Loxone control entity/entities to physical devices",
                    linked_entities,
                )
            if startup:
                self._signal_engineering_generation(generation)
            persistent_notification.async_dismiss(self.hass, self._engineering_status_notification_id)
            self.engineering_refresh_stage = "complete"
        except (Exception, asyncio.CancelledError):
            self._engineering_degraded_notification()
            raise

    async def async_restore_engineering_snapshot(self) -> None:
        """Reconcile process-local registries before exposing cached entities."""
        async with self._engineering_refresh_lock:
            await self.async_drain_committed_engineering_state(startup=True)
            if self.engineering_snapshot is not None:
                await self.async_rebind_engineering_runtime()

    async def async_refresh_engineering_inventory(self, *, force: bool = False) -> EngineeringSnapshot | None:
        """Commit a validated read before mutation, then drain replayable phases."""
        async with self._engineering_refresh_lock:
            self.engineering_refresh_stage = "recovery"
            await self.async_drain_committed_engineering_state()
            self.engineering_refresh_stage = "revision_check"
            revision = extract_loxapp_last_modified(self.miniserver.lox_config.json)
            previous = self.engineering_snapshot
            if (
                not force
                and previous is not None
                and not _engineering_room_identity_incomplete(previous)
                and revision is not None
                and revision == previous.source.loxapp_last_modified
            ):
                self.engineering_refresh_stage = "runtime_rebind"
                await self.async_rebind_engineering_runtime()
                self.engineering_refresh_stage = "complete"
                return None
            self.engineering_refresh_stage = "download"
            inventory = await self._async_download_engineering_inventory()
            self.engineering_refresh_stage = "topology"
            source = EngineeringSourceContext(
                self.config_entry.entry_id,
                self.miniserver.serial,
                None,
                "Miniserver",
                "",
                inventory.config_version,
                inventory.config_timestamp,
                revision,
            )
            resolved = resolve_engineering_topology(inventory, source)
            self.engineering_refresh_stage = "runtime_probe"
            runtime = await async_probe_engineering_runtime(resolved, client=self._engineering_runtime_client())
            if any(
                binding.status in {"auth_error", "transport_error", "malformed_response"}
                for binding in runtime.bindings
            ):
                raise EngineeringSnapshotError(_PROBE_FAILED)
            self.engineering_refresh_stage = "snapshot_validation"
            rows = resolve_engineering_capabilities(resolved, runtime)
            _, rejected = await async_filter_entity_identity_conflicts(
                self.hass,
                self.config_entry.entry_id,
                build_engineering_entity_specs(rows, runtime),
            )
            rejected_ids = {item.spec.unique_id for item in rejected}
            rows = tuple(
                replace(
                    row,
                    capability=replace(
                        row.capability,
                        exposure=ExposureStatus.INVENTORY_ONLY,
                        reason="entity_unique_id_owned_by_other_entry",
                    ),
                )
                if row.node.element.uuid in rejected_ids
                else row
                for row in rows
            )
            sequence = await self.hass.async_add_executor_job(next_engineering_read_sequence, previous)
            candidate = EngineeringSnapshot(
                source,
                resolved.nodes,
                rows,
                engineering_configuration_revision_id(source),
                engineering_safe_content_digest(source, resolved.nodes, rows),
                sequence,
                engineering_generation_id(source, resolved.nodes, rows, read_sequence=sequence),
                datetime.now(UTC),
            )
            candidate = await self.hass.async_add_executor_job(lambda: snapshot_from_dict(snapshot_to_dict(candidate)))
            state = await async_load_engineering_state(self.hass, self.config_entry.entry_id)
            metadata = (
                EngineeringRegistryMetadata.empty()
                if previous is None
                else await self.hass.async_add_executor_job(
                    lambda: registry_metadata_from_snapshot(
                        previous,
                        managed_area_ids=state.managed_area_ids,
                        room_area_mappings=state.room_area_mappings,
                        device_area_fallbacks=state.device_area_fallbacks,
                        applied_generation=state.registry_applied_generation,
                    )
                )
            )
            # Planning and consumer discovery must both finish before commit.
            self.engineering_refresh_stage = "registry_plan"
            registry_plan = await async_plan_engineering_registry_sync(
                self.hass,
                self.config_entry.entry_id,
                candidate,
                metadata,
            )
            self.engineering_refresh_stage = "impact_analysis"
            impacts = await async_find_engineering_change_impacts(
                self.hass,
                self.config_entry,
                previous,
                candidate,
                state.pending_impact_plan,
                registry_plan=registry_plan,
            )
            self.engineering_refresh_stage = "snapshot_store"
            try:
                async with engineering_area_operation_lock(self.hass, self.config_entry.entry_id):
                    state = await async_load_engineering_state(self.hass, self.config_entry.entry_id)
                    committed = StoredEngineeringState(
                        snapshot=candidate,
                        pending_impact_plan=impacts,
                        registry_applied_generation=state.registry_applied_generation,
                        impact_published_generation=state.impact_published_generation,
                        managed_area_ids=state.managed_area_ids,
                        room_area_mappings=state.room_area_mappings,
                        device_area_fallbacks=state.device_area_fallbacks,
                        room_area_mapping_scope=state.room_area_mapping_scope,
                        pending_area_batch=state.pending_area_batch,
                    )
                    await async_store_engineering_state(self.hass, committed)
            except EngineeringStoreCommitCancelledError as err:
                if err.settled_outcome == EngineeringStoreCommitOutcome.COMMITTED:
                    self._adopt_engineering_snapshot(candidate)
                    self._engineering_degraded_notification()
                raise
            self._adopt_engineering_snapshot(candidate)
            self.engineering_runtime = runtime
            self.engineering_refresh_stage = "registry_apply"
            await self.async_drain_committed_engineering_state()
            self.engineering_refresh_stage = "complete"
            return self.engineering_snapshot

    async def async_rebind_engineering_runtime(self) -> None:
        """Refresh safe cached live bindings independently of topology revision."""
        from .engineering_runtime import async_rebind_engineering_runtime  # noqa: PLC0415

        if self.engineering_snapshot is None:
            return
        try:
            self.engineering_runtime = await async_rebind_engineering_runtime(
                self.engineering_snapshot.rows,
                client=self._engineering_runtime_client(),
            )
        except (Exception, asyncio.CancelledError):
            self.engineering_runtime = None
            async_dispatcher_send(self.hass, engineering_inventory_updated_signal(self.config_entry.entry_id))
            raise
        async_dispatcher_send(self.hass, engineering_inventory_updated_signal(self.config_entry.entry_id))

    async def async_schedule_engineering_refresh(self, delay: float = 5.0) -> None:
        """Replace only this coordinator's cancellable debounce task."""
        async with self._engineering_schedule_lock:
            await self._async_cancel_engineering_refresh()
            self._engineering_refresh_task = self.hass.async_create_background_task(
                self._async_debounced_engineering_refresh(delay),
                "loxone engineering refresh",
            )

    async def _async_debounced_engineering_refresh(self, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            if self._unloading:
                return
            if self._engineering_registry_verified_generation is None:
                await self.async_restore_engineering_snapshot()
                snapshot = self.engineering_snapshot
                if (
                    snapshot is not None
                    and not _engineering_room_identity_incomplete(snapshot)
                    and snapshot.source.loxapp_last_modified is not None
                    and snapshot.source.loxapp_last_modified
                    == extract_loxapp_last_modified(self.miniserver.lox_config.json)
                ):
                    return
            await self.async_refresh_engineering_inventory()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- background failures expose bounded status only.
            self._engineering_degraded_notification()

    async def _async_cancel_engineering_refresh(self) -> None:
        task = self._engineering_refresh_task
        self._engineering_refresh_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def async_cleanup(self):
        """Clean up resources."""
        if self._engineering_refresh_task is not None:
            self._engineering_refresh_task.cancel()
        # Release transport independently of a finishing atomic metadata write.
        if self.api is not None:
            await self.api.close()
        async with self._engineering_schedule_lock:
            await self._async_cancel_engineering_refresh()
        if hasattr(self, "listeners"):
            # Clean up all event listeners
            for listener in self.listeners:
                if listener is not None:
                    listener()
            self.listeners = []
