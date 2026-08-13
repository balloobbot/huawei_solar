"""Test how sensors behave when the inverter stops answering."""

from __future__ import annotations

from unittest.mock import patch

from huawei_solar import REGISTER_LOCATIONS, register_names as rn
from modbus_connection.exceptions import ModbusTimeoutError
from modbus_connection.mock import MockModbusConnection, MockModbusUnit

from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util

from custom_components.huawei_solar.const import DOMAIN, INVERTER_UPDATE_INTERVAL

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from conftest import MOCK_REGISTERS, SERIAL_NUMBER, UNIT_ID, write_registers

# A running total, and an instantaneous reading from the same component.
TOTAL_SENSOR = rn.ACCUMULATED_YIELD_ENERGY
INSTANTANEOUS_SENSOR = rn.ACTIVE_POWER


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
