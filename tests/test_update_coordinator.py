"""Test how the readings and settings polls divide the registers and the link."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from conftest import SERIAL_NUMBER
from custom_components.huawei_solar.update_coordinator import (
    TIMEOUTS_BEFORE_RECONNECT,
    HuaweiSolarUpdateCoordinator,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from huawei_solar import SETTING_REGISTERS, register_names as rn


def _silent_device() -> MagicMock:
    """A device that never answers a batch update."""
    device = MagicMock()
    device.serial_number = SERIAL_NUMBER
    device.batch_update_readings = AsyncMock(side_effect=TimeoutError)
    device.batch_update_settings = AsyncMock(side_effect=TimeoutError)
    return device


def _coordinator(
    hass: HomeAssistant,
    device: MagicMock,
    connection: MagicMock | None,
    settings: bool = False,
) -> HuaweiSolarUpdateCoordinator:
    return HuaweiSolarUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        device=device,
        connection=connection,
        name=f"{SERIAL_NUMBER}_test_coordinator",
        settings=settings,
    )


async def _time_out(coordinator: HuaweiSolarUpdateCoordinator, times: int) -> None:
    for _ in range(times):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


async def test_the_device_coordinator_recycles_a_wedged_link(
    hass: HomeAssistant,
) -> None:
    """The coordinator holding the connection drops it once polls keep timing out."""
    connection = MagicMock()
    connection.disconnect = AsyncMock()
    coordinator = _coordinator(hass, _silent_device(), connection)

    await _time_out(coordinator, TIMEOUTS_BEFORE_RECONNECT - 1)
    connection.disconnect.assert_not_called()

    await _time_out(coordinator, 1)
    connection.disconnect.assert_awaited_once()


async def test_a_second_coordinator_leaves_the_link_alone(
    hass: HomeAssistant,
) -> None:
    """A coordinator without the connection fails its own poll and nothing else.

    Every coordinator on a device shares one link, so only the device's own gets
    to diagnose it; a settings poll timing out must not tear the link down under
    a poll the fast one is still running.
    """
    connection = MagicMock()
    connection.disconnect = AsyncMock()
    device = _silent_device()
    settings = _coordinator(hass, device, None, settings=True)

    await _time_out(settings, TIMEOUTS_BEFORE_RECONNECT * 2)

    connection.disconnect.assert_not_called()
    assert device.batch_update_settings.await_count == TIMEOUTS_BEFORE_RECONNECT * 2


async def test_only_one_coordinator_counts_a_silent_device(
    hass: HomeAssistant,
) -> None:
    """Two coordinators over one device recycle the link once, not twice."""
    connection = MagicMock()
    connection.disconnect = AsyncMock()
    device = _silent_device()
    fast = _coordinator(hass, device, connection)
    settings = _coordinator(hass, device, None, settings=True)

    for _ in range(TIMEOUTS_BEFORE_RECONNECT):
        await _time_out(fast, 1)
        await _time_out(settings, 1)

    connection.disconnect.assert_awaited_once()


# A register of each kind that an entity of this integration actually reads.
_READINGS = [rn.ACTIVE_POWER, rn.DEVICE_STATUS, rn.STORAGE_STATE_OF_CAPACITY]
_SETTINGS = [
    rn.STORAGE_MAXIMUM_CHARGING_POWER,
    rn.STORAGE_HUAWEI_LUNA2000_TIME_OF_USE_CHARGING_AND_DISCHARGING_PERIODS,
    rn.STORAGE_CAPACITY_CONTROL_PERIODS,
]


async def test_the_two_polls_divide_the_registers_between_them(
    hass: HomeAssistant,
) -> None:
    """Each half reads its own registers, and the library says which those are."""
    device = MagicMock()
    device.serial_number = SERIAL_NUMBER
    device.batch_update_readings = AsyncMock(return_value=MagicMock(failed={}))
    device.batch_update_settings = AsyncMock(return_value=MagicMock(failed={}))

    readings = _coordinator(hass, device, MagicMock())
    settings = _coordinator(hass, device, None, settings=True)
    readings.counterpart = settings
    settings.counterpart = readings

    # Every entity attaches through for_registers, whichever half it was built
    # with: the register map decides, not the caller.
    for name in _READINGS + _SETTINGS:
        for built_with in (readings, settings):
            built_with.for_registers([name]).async_add_listener(
                lambda: None, {"register_names": [name]}
            )

    await readings._async_update_data()
    await settings._async_update_data()

    polled_readings = set(device.batch_update_readings.await_args.args[0])
    polled_settings = set(device.batch_update_settings.await_args.args[0])

    assert not polled_readings & polled_settings
    assert polled_readings | polled_settings == set(_READINGS + _SETTINGS)
    assert polled_settings == {
        name for name in _READINGS + _SETTINGS if name in SETTING_REGISTERS
    }
