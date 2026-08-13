"""Smoke test: set up the integration in Home Assistant against a mock inverter."""

from __future__ import annotations

from unittest.mock import patch

from huawei_solar import register_names as rn
from modbus_connection.mock import MockModbusConnection

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import homeassistant.util.dt as dt_util

from custom_components.huawei_solar.const import DOMAIN, INVERTER_UPDATE_INTERVAL

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

# Not a relative import: ``tests`` is deliberately not a package, so that pytest
# does not put the repository root (and its ``types.py``) on ``sys.path``.
from conftest import SERIAL_NUMBER


async def test_setup_entry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
    entity_registry: er.EntityRegistry,
) -> None:
    """The entry loads, creates entities, and a sensor decodes a polled value."""
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.huawei_solar.create_tcp_connection",
        return_value=mock_connection,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        assert config_entry.state is ConfigEntryState.LOADED

        entities = er.async_entries_for_config_entry(
            entity_registry, config_entry.entry_id
        )
        assert entities

        async_fire_time_changed(hass, dt_util.utcnow() + INVERTER_UPDATE_INTERVAL)
        await hass.async_block_till_done()

    for register_name, expected in (
        (rn.ACCUMULATED_YIELD_ENERGY, 1234.56),
        (rn.ACTIVE_POWER, 3210),
        (rn.DAILY_YIELD_ENERGY, 0.65),
    ):
        entity_id = entity_registry.async_get_entity_id(
            "sensor", DOMAIN, f"{SERIAL_NUMBER}_{register_name}"
        )
        assert entity_id, f"no entity for {register_name}"
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == expected
