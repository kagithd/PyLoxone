"""Regression tests for room-controller temperature command contracts."""

from types import SimpleNamespace

import pytest
from homeassistant.components.climate.const import ClimateEntityFeature

from custom_components.loxone.climate import ActiveMode, ActiveState, LoxoneRoomControllerV2, OperatingMode
from custom_components.loxone.const import CONF_HVAC_AUTO_MODE, SENDDOMAIN


def _controller(monkeypatch, *, single_comfort=False, mode=OperatingMode.AUTO_HEAT_COOL):
    commands = []
    hass = SimpleNamespace(bus=SimpleNamespace(fire=lambda event, data: commands.append((event, data))))
    values = {
        "comfortTemperature": 20.0,
        "comfortTemperatureCool": 24.0,
        "tempTarget": 21.0,
        "frostProtectTemperature": 8.0,
        "heatProtectTemperature": 30.0,
    }
    controller = LoxoneRoomControllerV2(
        hass=hass,
        uuidAction="controller-a",
        name="Synthetic controller",
        room="Office",
        states={key: key for key in values},
        details={"singleComfortTemperature": single_comfort, "possibleCapabilities": 3, "timerModes": []},
        **{CONF_HVAC_AUTO_MODE: 0},
    )
    controller.operating_mode = mode
    controller.active_state = ActiveState(ActiveMode.COMFORT, 0)
    controller._state_attr_values.update(values)
    monkeypatch.setattr(controller, "schedule_update_ha_state", lambda: None)
    return controller, commands


@pytest.mark.parametrize("mode", (OperatingMode.AUTO_HEAT_COOL, OperatingMode.MANUAL_HEAT_COOL))
@pytest.mark.parametrize("single_comfort", (True, False))
def test_dual_capability_temperature_contract_matches_advertised_features(monkeypatch, mode, single_comfort):
    controller, commands = _controller(monkeypatch, single_comfort=single_comfort, mode=mode)
    has_range = bool(controller.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE)
    assert has_range is not single_comfort
    if single_comfort:
        controller.set_temperature(temperature=22.0)
        command = "setComfortModeTemp/2.0" if mode is OperatingMode.AUTO_HEAT_COOL else "setManualTemperature/22.0"
        assert commands == [(SENDDOMAIN, {"uuid": "controller-a", "value": command})]
        assert controller.target_temperature == 21.0
        assert controller.target_temperature_low is None
        assert controller.target_temperature_high is None
    else:
        controller.set_temperature(target_temp_low=21.0, target_temp_high=25.0)
        assert commands == [
            (SENDDOMAIN, {"uuid": "controller-a", "value": "setComfortTemperatureCool/25.0"}),
            (SENDDOMAIN, {"uuid": "controller-a", "value": "setComfortTemperature/21.0"}),
        ]
        assert controller.target_temperature is None
        assert controller.target_temperature_low == 20.0
        assert controller.target_temperature_high == 24.0


@pytest.mark.parametrize(
    ("targets", "expected"),
    (
        ({"target_temp_low": 10.0}, ["setecoplusmintemperature/10.0"]),
        ({"target_temp_high": 32.0}, ["setecoplusmaxtemperature/32.0"]),
        (
            {"target_temp_high": 32.0, "target_temp_low": 10.0},
            ["setecoplusmaxtemperature/32.0", "setecoplusmintemperature/10.0"],
        ),
        ({"target_temp_low": 8.0}, []),
        ({"target_temp_high": 30.0, "target_temp_low": 8.0}, []),
    ),
)
@pytest.mark.parametrize("comfort_received", (True, False))
def test_building_protection_targets_are_independent_of_comfort_values(
    monkeypatch, targets, expected, comfort_received
):
    controller, commands = _controller(monkeypatch)
    controller.active_state = ActiveState(ActiveMode.BUILDING_PROTECT, 0)
    # Building protection must work before unrelated comfort states are received.
    if not comfort_received:
        controller._state_attr_values.pop("comfortTemperature")
        controller._state_attr_values.pop("comfortTemperatureCool")
    controller.set_temperature(**targets)
    assert [data["value"] for event, data in commands] == expected
