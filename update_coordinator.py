"""Specialized DataUpdateCoordinators for Huawei Solar entities."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from itertools import chain
import logging
from typing import Any

from huawei_solar import (
    SETTING_REGISTERS,
    ConnectionInterruptedException,
    DecodeError,
    HuaweiModbusConnection,
    HuaweiSolarException,
    ReadException,
    RegisterName,
    SUN2000Device,
    UpdateReport,
)
from huawei_solar.device.base import HuaweiSolarDevice
from huawei_solar.files import OptimizerRealTimeData
from modbus_connection import (
    ExceptionCode,
    IllegalDataAddressError,
    ModbusConnectionError,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import OPTIMIZER_UPDATE_TIMEOUT, UPDATE_TIMEOUT

_LOGGER = logging.getLogger(__name__)

# The library already retries a timed-out request three times, so this many
# updates in a row failing means the link is wedged rather than the device slow.
TIMEOUTS_BEFORE_RECONNECT = 3

ILLEGAL_ADDRESS_HELP = (
    "Device %s reported an illegal address error during a batch update. "
    "This likely means the library is querying a register that is not "
    "supported by your specific device. "
    "To find the culprit: systematically disable sensors one by one in "
    "Home Assistant, wait at least 30 seconds after each change, and "
    "check whether the error disappears. Please report the offending "
    "sensor to the integration maintainers."
)


class HuaweiSolarUpdateCoordinator(
    DataUpdateCoordinator[dict[RegisterName, Any]]
):
    """A specialised DataUpdateCoordinator for Huawei Solar entities."""

    device: HuaweiSolarDevice

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        device: HuaweiSolarDevice,
        connection: HuaweiModbusConnection | None,
        name: str,
        update_interval: timedelta | None = None,
        update_method: Callable[[], Awaitable[dict[RegisterName, Any]]]
        | None = None,
        request_refresh_debouncer: Debouncer | None = None,
        update_timeout: timedelta = UPDATE_TIMEOUT,
        settings: bool = False,
    ) -> None:
        """Create a HuaweiSolarUpdateCoordinator."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            update_method=update_method,
            request_refresh_debouncer=request_refresh_debouncer,
        )
        self.device = device
        self.connection = connection
        self.update_timeout = update_timeout
        self.settings = settings
        # The other half of the split poll, so an entity attaches to the
        # coordinator that reads its registers rather than to the one that
        # happened to build it. None while there is no settings poll.
        self.counterpart: HuaweiSolarUpdateCoordinator | None = None
        self._consecutive_timeouts = 0
        self._failed_components: set[str] = set()
        self.last_report: UpdateReport | None = None

    def for_registers(
        self, register_names: list[RegisterName]
    ) -> "HuaweiSolarUpdateCoordinator":
        """Return the coordinator polling ``register_names``.

        Which half a register belongs to is the library's SETTING_REGISTERS,
        derived from its register map: what can be written only changes when
        something writes it. The integration keeps no list of its own to drift.
        """
        settings = all(name in SETTING_REGISTERS for name in register_names)
        if settings == self.settings:
            return self
        return self.counterpart or self

    def _report_failed_components(self, report: UpdateReport) -> None:
        """Log the components that newly stopped answering, one line each.

        A component stays quiet for hours at a time, and at one poll every 30
        seconds saying so on every one of them buries the log. Only the onset
        is worth a line; that the entities behind it went unavailable is
        already visible without one.
        """
        newly_failed = sorted(set(report.failed) - self._failed_components)
        self._failed_components = set(report.failed)

        for name in newly_failed:
            _LOGGER.warning(
                "Failed to fetch %s from %s: %s",
                name,
                self.device.serial_number,
                report.failed[name],
            )

        # A refused address used to surface as a failed update. Now that it
        # only costs one component, this is the only place it is said.
        if any(
            isinstance(report.failed[name], IllegalDataAddressError)
            for name in newly_failed
        ):
            _LOGGER.error(ILLEGAL_ADDRESS_HELP, self.device.serial_number)

    async def _recycle_link(self) -> None:
        """Drop a link that is up but no longer answering.

        Reconnecting is otherwise automatic: every request connects first, so a
        dropped link heals within one update interval. The one case that cannot
        heal itself is a link the transport still considers open while the
        device behind it has stopped answering - which is what an inverter
        behind an SDongle or a serial-to-network bridge does. Dropping the link
        makes the next poll open a fresh one, without reloading the entry.

        Only the device's own coordinator is handed the connection. Every
        coordinator on a device shares one link, so counting timeouts on each of
        them would let a silent device be diagnosed several times over and have
        the link torn down from under a poll another coordinator is still
        running. The fastest one notices soonest anyway.
        """
        if self.connection is None:
            return

        self._consecutive_timeouts += 1
        if self._consecutive_timeouts < TIMEOUTS_BEFORE_RECONNECT:
            return

        self._consecutive_timeouts = 0
        _LOGGER.warning(
            "No response from %s for %s updates in a row. Dropping the Modbus "
            "link so the next update establishes a fresh one",
            self.device.serial_number,
            TIMEOUTS_BEFORE_RECONNECT,
        )
        try:
            await self.connection.disconnect()
        except ModbusConnectionError:
            # The link is dropped regardless; the next request opens a new one.
            _LOGGER.debug("Tearing down the Modbus link failed", exc_info=True)

    async def _async_update_data(self) -> dict[RegisterName, Any]:
        register_names_set = set(
            chain.from_iterable(ctx["register_names"] for ctx in self.async_contexts())
        )
        poll = (
            self.device.batch_update_settings
            if self.settings
            else self.device.batch_update_readings
        )
        try:
            async with asyncio.timeout(self.update_timeout.total_seconds()):
                report = await poll(list(register_names_set))
        except TimeoutError as err:
            await self._recycle_link()
            raise UpdateFailed(
                f"Timeout communicating with {self.device.serial_number}: "
                "the device did not respond in time"
            ) from err
        except ReadException as err:
            # Only reached when no component answered at all: a single component
            # refusing an address is contained and reported below instead.
            if err.modbus_exception_code == ExceptionCode.ILLEGAL_DATA_ADDRESS:
                _LOGGER.error(ILLEGAL_ADDRESS_HELP, self.device.serial_number)
            raise UpdateFailed(
                f"Could not update {self.device.serial_number} values: {err}"
            ) from err
        except ConnectionInterruptedException as err:
            _LOGGER.warning(
                "Connection to %s was interrupted during update. "
                "The inverter only supports one Modbus connection at a time - "
                "check whether another device has connected to the inverter",
                self.device.serial_number,
            )
            raise UpdateFailed(
                f"Connection to {self.device.serial_number} was interrupted, probably by another device. "
                "The inverter only supports one Modbus connection at a time."
            ) from err
        except HuaweiSolarException as err:
            raise UpdateFailed(
                f"Could not update {self.device.serial_number} values: {err}"
            ) from err

        # Something answered, so the link is not the problem.
        self._consecutive_timeouts = 0
        self._report_failed_components(report)
        self.last_report = report
        return report.values


class HuaweiSolarOptimizerUpdateCoordinator(
    DataUpdateCoordinator[dict[int, OptimizerRealTimeData]]
):
    """A specialised DataUpdateCoordinator for Huawei Solar optimizers."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        device: SUN2000Device,
        optimizer_device_infos: dict[int, DeviceInfo],
        name: str,
        update_interval: timedelta | None = None,
        request_refresh_debouncer: Debouncer | None = None,
    ) -> None:
        """Create a HuaweiSolarRegisterUpdateCoordinator."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            request_refresh_debouncer=request_refresh_debouncer,
        )
        self.device = device
        self.optimizer_device_infos = optimizer_device_infos

    async def _async_update_data(self) -> dict[int, OptimizerRealTimeData]:
        """Retrieve the latest values from the optimizers."""
        try:
            async with asyncio.timeout(OPTIMIZER_UPDATE_TIMEOUT.total_seconds()):
                return await self.device.get_latest_optimizer_history_data()
        except TimeoutError as err:
            raise UpdateFailed(
                f"Timeout communicating with {self.device.serial_number} optimizers: "
                "the device did not respond in time"
            ) from err
        except ConnectionInterruptedException as err:
            _LOGGER.warning(
                "Connection to %s was interrupted during optimizer update. "
                "The inverter only supports one Modbus connection at a time - "
                "check whether another device has connected to the inverter",
                self.device.serial_number,
            )
            raise UpdateFailed(
                f"Connection to {self.device.serial_number} was interrupted, probably by another device. "
                "The inverter only supports one Modbus connection at a time."
            ) from err
        except DecodeError as err:
            raise UpdateFailed(
                f"Could not decode optimizer data from {self.device.serial_number}: {err}. "
                "This is typically a transient problem that will resolve itself when the device generates a "
                "new optimizer data file.",
                retry_after=15 * 60,  # optimizer files are generated every 15 minutes
            ) from err
        except HuaweiSolarException as err:
            raise UpdateFailed(
                f"Could not update {self.device.serial_number} optimizer values: {err}"
            ) from err


async def create_optimizer_update_coordinator(
    hass: HomeAssistant,
    device: SUN2000Device,
    optimizer_device_infos: dict[int, DeviceInfo],
    update_interval: timedelta | None,
) -> HuaweiSolarOptimizerUpdateCoordinator:
    """Create and refresh entities of an HuaweiSolarOptimizerUpdateCoordinator."""

    coordinator = HuaweiSolarOptimizerUpdateCoordinator(
        hass,
        _LOGGER,
        device=device,
        optimizer_device_infos=optimizer_device_infos,
        name=f"{device.serial_number}_optimizer_data_update_coordinator",
        update_interval=update_interval,
    )

    await coordinator.async_config_entry_first_refresh()

    return coordinator
