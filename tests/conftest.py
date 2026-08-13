"""Fixtures for the Huawei Solar integration tests."""

from __future__ import annotations

import atexit
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _expose_as_custom_component() -> None:
    """Make the integration importable as ``custom_components.huawei_solar``.

    Home Assistant's loader only finds custom integrations below a
    ``custom_components`` package. This repository is ``content_in_root``, and
    putting the repository root on ``sys.path`` is not an option either: its
    ``types.py`` would shadow the stdlib module of that name. So the package is
    built outside the repository, pointing back at it.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="huawei_solar_tests_"))
    atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)

    package = tmp_dir / "custom_components"
    package.mkdir()
    (package / "huawei_solar").symlink_to(REPO_ROOT, target_is_directory=True)

    sys.path.insert(0, str(tmp_dir))


_expose_as_custom_component()

from huawei_solar import REGISTER_LOCATIONS, register_names as rn  # noqa: E402
from modbus_connection.mock import (  # noqa: E402
    MockModbusConnection,
    MockModbusUnit,
)

from homeassistant.const import (  # noqa: E402
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant  # noqa: E402

from custom_components.huawei_solar.const import (  # noqa: E402
    CONF_ENABLE_PARAMETER_CONFIGURATION,
    CONF_SLAVE_IDS,
    DOMAIN,
)

from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

UNIT_ID = 1
SERIAL_NUMBER = "HV2140000001"

# What a SUN2000 answers with. Written by register name rather than by address,
# so the values below are the ones the sensors are expected to show.
MOCK_REGISTERS: dict[str, Any] = {
    rn.MODEL_NAME: "SUN2000-3KTL-L1",
    rn.SERIAL_NUMBER: SERIAL_NUMBER,
    rn.PN: "000000-000",
    rn.FIRMWARE_VERSION: "V100R001C00SPC108",
    rn.SOFTWARE_VERSION: "V200R001C00SPC108",
    rn.NB_PV_STRINGS: 2,
    rn.ACTIVE_POWER: 3210,
    rn.ACCUMULATED_YIELD_ENERGY: 1234.56,
    rn.DAILY_YIELD_ENERGY: 0.65,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading of this custom integration in every test."""


def write_registers(unit: MockModbusUnit, values: dict[str, Any]) -> None:
    """Encode values into the mock's holding registers, by register name."""
    for name, value in values.items():
        location = REGISTER_LOCATIONS[name]
        field = location.definition()
        start = field.address + field.stride * ((location.instance or 1) - 1)
        for offset, word in enumerate(field.encode(value)):
            unit.holding[start + offset] = word


@pytest.fixture
def mock_unit(mock_connection: MockModbusConnection) -> MockModbusUnit:
    """Return the mock's primary unit."""
    return mock_connection.for_unit(UNIT_ID)


@pytest.fixture
def mock_connection() -> MockModbusConnection:
    """Return an in-memory Modbus connection answering as a SUN2000."""
    connection = MockModbusConnection()
    write_registers(connection.for_unit(UNIT_ID), MOCK_REGISTERS)
    return connection


@pytest.fixture
async def init_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_connection: MockModbusConnection,
) -> MockConfigEntry:
    """Set up the integration against the mock inverter."""
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.huawei_solar.create_tcp_connection",
        return_value=mock_connection,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    return config_entry


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry for a TCP-connected inverter."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: "192.0.2.10",
            CONF_PORT: 502,
            CONF_SLAVE_IDS: [UNIT_ID],
            CONF_ENABLE_PARAMETER_CONFIGURATION: False,
            CONF_USERNAME: None,
            CONF_PASSWORD: None,
        },
        version=1,
        minor_version=2,
    )
