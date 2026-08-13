"""Test how sensors behave when the inverter stops answering."""

from __future__ import annotations

from huawei_solar import REGISTER_LOCATIONS, register_names as rn
from modbus_connection.exceptions import ModbusTimeoutError
from modbus_connection.mock import MockModbusUnit

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util

from custom_components.huawei_solar.const import DOMAIN, INVERTER_UPDATE_INTERVAL

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from conftest import SERIAL_NUMBER

# A running total, and an instantaneous reading from the same component.
TOTAL_SENSOR = rn.ACCUMULATED_YIELD_ENERGY
INSTANTANEOUS_SENSOR = rn.ACTIVE_POWER


def _state(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, register_name: str
) -> str:
    entity_id = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{SERIAL_NUMBER}_{register_name}"
    )
    assert entity_id, f"no entity for {register_name}"
    state = hass.states.get(entity_id)
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
    falling silent, as an inverter does overnight, and a single component that
    stops answering while the rest of the device keeps updating.
    """
    await _poll(hass, 1)
    assert _state(hass, entity_registry, TOTAL_SENSOR) == "1234.56"
    assert _state(hass, entity_registry, INSTANTANEOUS_SENSOR) == "3210"

    mock_unit.fail_requests(ModbusTimeoutError("device asleep"))
    await _poll(hass, 2)

    assert _state(hass, entity_registry, TOTAL_SENSOR) == "1234.56"
    assert _state(hass, entity_registry, INSTANTANEOUS_SENSOR) == "unavailable"

    mock_unit.fail_requests(None)
    mock_unit.fail_read(
        REGISTER_LOCATIONS[TOTAL_SENSOR].definition().address,
        ModbusTimeoutError("component gone"),
    )
    await _poll(hass, 3)

    assert _state(hass, entity_registry, TOTAL_SENSOR) == "1234.56"
    assert _state(hass, entity_registry, INSTANTANEOUS_SENSOR) == "unavailable"
