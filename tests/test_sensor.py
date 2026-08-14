"""Test how sensors behave when the inverter stops answering."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from huawei_solar import REGISTER_LOCATIONS, register_names as rn
from modbus_connection.exceptions import ModbusTimeoutError, ServerDeviceBusyError
from modbus_connection.mock import MockModbusConnection, MockModbusUnit

from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import async_get as async_get_restore_state
import homeassistant.util.dt as dt_util

from custom_components.huawei_solar.const import DOMAIN, INVERTER_UPDATE_INTERVAL
from custom_components.huawei_solar.diagnostics import (
    async_get_config_entry_diagnostics,
)

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from conftest import MOCK_REGISTERS, SERIAL_NUMBER, UNIT_ID, write_registers

# A running total, and an instantaneous reading from the same component.
TOTAL_SENSOR = rn.ACCUMULATED_YIELD_ENERGY
INSTANTANEOUS_SENSOR = rn.ACTIVE_POWER
# A counter Home Assistant watches for resets, unlike TOTAL_SENSOR.
TOTAL_INCREASING_SENSOR = rn.DAILY_YIELD_ENERGY
# On a component polled after the inverter's own, so failing it is contained
# rather than taken for a device that never answered at all.
LATER_COMPONENT_SENSOR = rn.PV_01_VOLTAGE


def _entity_id(entity_registry: er.EntityRegistry, register_name: str) -> str:
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL_NUMBER}_{register_name}"
    )
    assert entity_id, f"no entity for {register_name}"
    return entity_id


def _state(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, register_name: str
) -> str:
    state = hass.states.get(_entity_id(entity_registry, register_name))
    assert state is not None
    return state.state


async def _poll(hass: HomeAssistant, times: int) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + times * INVERTER_UPDATE_INTERVAL)
    await hass.async_block_till_done()


async def test_totals_survive_a_silent_device(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_unit: MockModbusUnit,
    entity_registry: er.EntityRegistry,
) -> None:
    """A total holds its last value; an instantaneous reading goes unavailable.

    Both of the ways a poll can come up short are covered: the whole device
    falling silent, as an inverter does overnight, and a single component
    failing while the rest of the device keeps updating.
    """
    await _poll(hass, 1)
    assert _state(hass, entity_registry, TOTAL_SENSOR) == "1234.56"
    assert _state(hass, entity_registry, INSTANTANEOUS_SENSOR) == "3210"

    mock_unit.fail_requests(ModbusTimeoutError("device asleep"))
    await _poll(hass, 2)

    assert _state(hass, entity_registry, TOTAL_SENSOR) == "1234.56"
    assert _state(hass, entity_registry, INSTANTANEOUS_SENSOR) == "unavailable"

    mock_unit.fail_requests(None)
    # Not a timeout: the total sits on the first component polled, and a
    # timeout there means the whole device is silent, which is the case above.
    mock_unit.fail_read(
        REGISTER_LOCATIONS[TOTAL_SENSOR].definition().address,
        ServerDeviceBusyError(),
    )
    await _poll(hass, 3)

    assert _state(hass, entity_registry, TOTAL_SENSOR) == "1234.56"
    assert _state(hass, entity_registry, INSTANTANEOUS_SENSOR) == "unavailable"


async def test_a_counter_ignores_a_torn_read(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_unit: MockModbusUnit,
    entity_registry: er.EntityRegistry,
) -> None:
    """A counter served mid-update reads a hair low, which reads as a meter reset.

    Only a hair is ignored: a genuine reset drops much further than 1%, and is
    published as the reset it is.
    """
    write_registers(mock_unit, {TOTAL_INCREASING_SENSOR: 100.0})
    await _poll(hass, 1)
    assert _state(hass, entity_registry, TOTAL_INCREASING_SENSOR) == "100.0"

    write_registers(mock_unit, {TOTAL_INCREASING_SENSOR: 99.5})
    await _poll(hass, 2)
    assert _state(hass, entity_registry, TOTAL_INCREASING_SENSOR) == "100.0"

    write_registers(mock_unit, {TOTAL_INCREASING_SENSOR: 50.0})
    await _poll(hass, 3)
    assert _state(hass, entity_registry, TOTAL_INCREASING_SENSOR) == "50.0"


async def test_a_plain_total_is_allowed_to_fall(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_unit: MockModbusUnit,
    entity_registry: er.EntityRegistry,
) -> None:
    """Home Assistant reads no reset into a TOTAL, so nothing is held back."""
    await _poll(hass, 1)
    assert _state(hass, entity_registry, TOTAL_SENSOR) == "1234.56"

    write_registers(mock_unit, {TOTAL_SENSOR: 1230.0})
    await _poll(hass, 2)
    assert _state(hass, entity_registry, TOTAL_SENSOR) == "1230.0"


async def test_a_total_restores_across_a_restart(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A total comes back with the value it had, even if the device stays quiet.

    Holding the value only lasts as long as the process does, so an inverter
    that is asleep when Home Assistant starts would otherwise leave a gap in
    the energy dashboard exactly where the restart was.
    """
    entity_id = _entity_id(entity_registry, TOTAL_SENSOR)

    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    mock_restore_cache_with_extra_data(
        hass,
        (
            (
                State(entity_id, "999.99"),
                {
                    "native_value": 999.99,
                    "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
                },
            ),
        ),
    )
    # Closing the connection is permanent, so a restart builds a fresh one -
    # against an inverter whose component behind the total stays silent.
    restarted = MockModbusConnection()
    write_registers(restarted.for_unit(UNIT_ID), MOCK_REGISTERS)
    restarted.for_unit(UNIT_ID).fail_read(
        REGISTER_LOCATIONS[TOTAL_SENSOR].definition().address,
        ModbusTimeoutError("device asleep"),
    )

    with patch(
        "custom_components.huawei_solar.create_tcp_connection",
        return_value=restarted,
    ):
        assert await hass.config_entries.async_setup(init_integration.entry_id)
        await hass.async_block_till_done()

    assert _state(hass, entity_registry, TOTAL_SENSOR) == "999.99"
    assert _state(hass, entity_registry, INSTANTANEOUS_SENSOR) == "unavailable"


async def test_only_a_total_registers_for_restoring(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Only the sensors that need their value back ask to be restored.

    Home Assistant writes every registered entity to disk on a timer, and the
    readings that gap harmlessly - a power, a voltage - are the large majority.
    """
    totals = {
        entry.entity_id
        for entry in er.async_entries_for_config_entry(
            entity_registry, init_integration.entry_id
        )
        if (state := hass.states.get(entry.entity_id))
        and state.attributes.get("state_class")
        in (SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING)
    }

    assert totals
    assert set(async_get_restore_state(hass).entities) == totals


async def test_a_failing_component_is_logged_once(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_unit: MockModbusUnit,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A component can stay quiet for hours; only the onset is worth a line."""
    mock_unit.fail_read(
        REGISTER_LOCATIONS[LATER_COMPONENT_SENSOR].definition().address,
        ModbusTimeoutError("component gone"),
    )

    await _poll(hass, 1)
    assert caplog.text.count("Failed to fetch PVString[1]") == 1

    await _poll(hass, 2)
    assert caplog.text.count("Failed to fetch PVString[1]") == 1


async def test_diagnostics_name_the_component_that_stopped_answering(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    mock_unit: MockModbusUnit,
) -> None:
    """Values alone do not say why a register is missing; the poll outcome does."""
    mock_unit.fail_read(
        REGISTER_LOCATIONS[LATER_COMPONENT_SENSOR].definition().address,
        ModbusTimeoutError("component gone"),
    )
    await _poll(hass, 1)

    diagnostics = await async_get_config_entry_diagnostics(hass, init_integration)
    poll = diagnostics[f"device_{UNIT_ID}_poll"]

    assert "component gone" in poll["failed"]["PVString[1]"]
    assert "PVString[1]" not in poll["updated"]
