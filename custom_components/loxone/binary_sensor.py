"""Support for Loxone binary sensors."""

from __future__ import annotations

import logging
from typing import Literal, final

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_VALUE_TEMPLATE,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import LoxoneEntity
from .const import DOMAIN
from .engineering_entities import (
    EngineeringEntitySpec,
    EngineeringPlatformReconciler,
    build_engineering_entity_specs,
    engineering_event_value,
    engineering_inventory_updated_signal,
    engineering_state_updated_signal,
)
from .engineering_snapshot import async_load_engineering_state
from .helpers import add_room_and_cat_to_value_values, get_all, get_or_create_device
from .miniserver import get_miniserver_from_hass

_LOGGER = logging.getLogger(__name__)
NEW_SENSOR = "binairy_sensors"
DEFAULT_NAME = "Loxone Binary Sensor"

LOXONE_DEVICE_CLASS_MAP: dict[str, BinarySensorDeviceClass] = {
    "presence": BinarySensorDeviceClass.PRESENCE,
    "smoke": BinarySensorDeviceClass.SMOKE,
}


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_devices: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up Loxone Sensor from yaml"""
    value_template = config.get(CONF_VALUE_TEMPLATE)
    if value_template is not None:
        value_template.hass = hass

    # Devices from yaml
    if config != {}:
        # Here setup all Sensors in Yaml-File
        new_sensor = LoxoneCustomBinarySensor(**config)
        async_add_devices([new_sensor])
        return True
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up entry."""
    miniserver = get_miniserver_from_hass(hass, config_entry)
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    loxconfig = miniserver.lox_config.json
    entities = []

    for sensor in get_all(loxconfig, "InfoOnlyDigital"):
        sensor = add_room_and_cat_to_value_values(loxconfig, sensor)
        sensor.update({"type": "digital"})
        entities.append(LoxoneDigitalSensor(**sensor))

    for sensor in get_all(loxconfig, "PresenceDetector"):
        sensor = add_room_and_cat_to_value_values(loxconfig, sensor)
        sensor.update({"type": "presence"})
        entities.append(LoxoneDigitalSensor(**sensor))

    for sensor in get_all(loxconfig, "SmokeAlarm"):
        sensor = add_room_and_cat_to_value_values(loxconfig, sensor)
        sensor.update({"type": "smoke"})
        entities.append(LoxoneDigitalSensor(**sensor))

    @callback
    def async_add_binary_sensors(_):
        async_add_entities(_, True)

    miniserver.listeners.append(
        async_dispatcher_connect(
            hass,
            miniserver.async_signal_new_device("sensors"),
            async_add_binary_sensors,
        )
    )
    async_add_entities(entities)
    standard_binary_sensor_uuids = frozenset(
        unique_id for entity in entities if isinstance((unique_id := entity.unique_id), str) and unique_id
    )
    engineering_platform = EngineeringPlatformReconciler(
        config_entry.entry_id,
        "binary_sensor",
        standard_binary_sensor_uuids,
        lambda spec: LoxoneEngineeringBinarySensor(spec, config_entry.entry_id),
        async_add_entities,
    )

    async def async_refresh_engineering_binary_sensors(*, use_runtime: bool = True) -> None:
        try:
            stored = await async_load_engineering_state(hass, config_entry.entry_id)
        except Exception:
            _LOGGER.warning("Unable to restore the safe engineering binary-sensor snapshot", exc_info=True)
            specs = ()
        else:
            runtime = coordinator.engineering_runtime if use_runtime else None
            snapshot = stored.snapshot
            specs = (
                ()
                if snapshot is None or stored.registry_applied_generation != snapshot.generation_id
                else build_engineering_entity_specs(snapshot.rows, runtime)
            )
        await engineering_platform.async_reconcile(
            hass,
            specs,
        )

    miniserver.listeners.append(
        async_dispatcher_connect(
            hass,
            engineering_inventory_updated_signal(config_entry.entry_id),
            async_refresh_engineering_binary_sensors,
        )
    )
    await async_refresh_engineering_binary_sensors(use_runtime=False)


class LoxoneEngineeringBinarySensor(BinarySensorEntity):
    """A disabled-by-default read-only engineering binary sensor."""

    _attr_should_poll = False

    def __init__(self, spec: EngineeringEntitySpec, config_entry_id: str | None = None) -> None:
        self._spec = spec
        self._config_entry_id = config_entry_id
        self._event_unsub = None
        self._event_token = None
        self._lifecycle_active = False
        self._live_eligible = True
        self._attr_unique_id = spec.unique_id
        self._attr_is_on = engineering_event_value("binary_sensor", spec.native_value)
        self._attr_available = spec.available and self._attr_is_on is not None
        self._apply_spec_metadata(spec)

    def _apply_spec_metadata(self, spec: EngineeringEntitySpec) -> None:
        """Refresh all metadata derived from the current accepted specification."""
        self._attr_name = spec.name
        self._attr_entity_registry_enabled_default = spec.enabled_by_default
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, spec.owner_identifier)})
        self._update_attributes(spec)

    def _update_attributes(self, spec: EngineeringEntitySpec) -> None:
        self._attr_extra_state_attributes = {
            "uuid": spec.unique_id,
            "io_name": spec.io_name,
            "loxone_type": spec.loxone_type,
            "engineering_config_version": spec.config_version,
            "runtime_binding": spec.runtime_binding,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe only to the source-scoped proven state mapping."""
        await super().async_added_to_hass()
        self._lifecycle_active = True
        if self._live_eligible:
            self._subscribe_state_updates()
        self.async_on_remove(self._deactivate_state_updates)

    @callback
    def _subscribe_state_updates(self) -> None:
        self._unsubscribe_state_updates()
        if not self._lifecycle_active or not self._live_eligible or self.hass is None or self._config_entry_id is None:
            return
        token = object()
        self._event_token = token

        @callback
        def async_handle_engineering_value(value: object) -> None:
            self._handle_engineering_value(value, token)

        self._event_unsub = async_dispatcher_connect(
            self.hass,
            engineering_state_updated_signal(self._config_entry_id, self._spec.state_uuid),
            async_handle_engineering_value,
        )

    @callback
    def _unsubscribe_state_updates(self) -> None:
        self._event_token = None
        if self._event_unsub is not None:
            self._event_unsub()
            self._event_unsub = None

    @callback
    def _deactivate_state_updates(self) -> None:
        self._lifecycle_active = False
        self._live_eligible = False
        self._unsubscribe_state_updates()

    @callback
    def _handle_engineering_value(self, value: object, token: object) -> None:
        if not self._live_eligible or token is not self._event_token:
            return
        is_on = engineering_event_value("binary_sensor", value)
        if is_on is None:
            return
        self._attr_is_on = is_on
        self._attr_available = True
        if self.hass is not None:
            self.async_write_ha_state()

    @callback
    def update_spec(self, spec: EngineeringEntitySpec) -> None:
        """Apply a current compatible prepared specification."""
        if spec.unique_id != self.unique_id or spec.platform != "binary_sensor":
            raise ValueError("engineering binary sensor specification identity changed")
        state_uuid_changed = spec.state_uuid != self._spec.state_uuid
        self._spec = spec
        self._live_eligible = True
        if (is_on := engineering_event_value("binary_sensor", spec.native_value)) is not None:
            self._attr_is_on = is_on
        self._attr_available = spec.available and is_on is not None
        self._apply_spec_metadata(spec)
        if self._lifecycle_active and (state_uuid_changed or self._event_unsub is None):
            self._subscribe_state_updates()
        if self.hass is not None:
            self.async_write_ha_state()

    @callback
    def mark_unavailable(self) -> None:
        """Revoke a channel until an authoritative specification accepts it."""
        self._live_eligible = False
        self._unsubscribe_state_updates()
        self._attr_available = False
        if self.hass is not None:
            self.async_write_ha_state()


class LoxoneDigitalSensor(LoxoneEntity, BinarySensorEntity):
    """Representation of a binary Loxone device."""

    _attr_is_on: bool | None = None
    _attr_state: None = None
    _attr_available = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._attr_state = STATE_UNKNOWN
        self._attr_is_on = STATE_UNKNOWN
        self._from_loxone_config = False

        if "type" in kwargs and "room" in kwargs and "cat" in kwargs and hasattr(self, "states"):
            self._from_loxone_config = True
            if self.type == "smoke":
                self._state_uuid = self.states["areAlarmSignalsOff"]
            if self.type == "presence":
                self._state_uuid = self.states["active"]
            elif "active" in self.states:
                self._state_uuid = self.uuidAction
        else:
            self._state_uuid = self.uuidAction

        self._state = STATE_UNKNOWN
        self._format = self._get_format(kwargs.get("details", {}).get("format", ""))
        self._parent_id = kwargs.get("parent_id", None)
        self._on_state = STATE_ON
        self._off_state = STATE_OFF
        self._attr_available = True
        if self.type in LOXONE_DEVICE_CLASS_MAP:
            self._attr_device_class = LOXONE_DEVICE_CLASS_MAP[self.type]
        else:
            self._attr_device_class = None

        if self._parent_id:
            self.uuidAction = self._parent_id

        if self._from_loxone_config:
            self._attr_device_info = get_or_create_device(self.unique_id, self.name, self.type, self.room)
        else:
            self._attr_device_info = get_or_create_device(self.unique_id, self.name, self.type, "")

        if self._from_loxone_config:
            self._attr_extra_state_attributes.update(
                {
                    "state_uuid": self._state_uuid,
                    "device_type": self.type,
                }
            )
        else:
            self._attr_extra_state_attributes.update(
                {
                    "device_type": self._attr_device_class,
                }
            )

    async def event_handler(self, e):
        if self._state_uuid in e.data:
            self._state = e.data[self._state_uuid]
            if self._state == 1.0:
                self._state = self._on_state
            else:
                self._state = self._off_state
            if not self._attr_available:
                self._attr_available = True
            self.async_schedule_update_ha_state()

    @final
    @property
    def state(self) -> Literal["on", "off"] | None:
        """Return the state of the binary sensor."""
        if (is_on := self.is_on) is None:
            return None
        return STATE_ON if is_on else STATE_OFF

    @property
    def is_on(self) -> bool | None:
        """Return true if sensor is on."""
        return self._state == self._on_state


class LoxoneCustomBinarySensor(LoxoneEntity, BinarySensorEntity):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._name = kwargs["name"]
        self._state = STATE_UNKNOWN
        self._on_state = STATE_ON
        self._off_state = STATE_OFF

        if "uuidAction" in kwargs:
            self.uuidAction = kwargs["uuidAction"]
        else:
            self.uuidAction = ""

    @property
    def is_on(self) -> bool | None:
        """Return true if sensor is on."""
        return self._state == self._on_state

    @property
    def state(self) -> Literal["on", "off"] | None:
        """Return the state of the binary sensor."""
        if (is_on := self.is_on) is None:
            return None
        return STATE_ON if is_on else STATE_OFF

    async def event_handler(self, e):
        if self.uuidAction in e.data:
            data = e.data[self.uuidAction]
            if data == 1.0:
                self._state = self._on_state
            else:
                self._state = self._off_state
            self.async_schedule_update_ha_state()

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name
