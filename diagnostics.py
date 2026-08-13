"""Diagnostics support for Huawei Solar."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DATA_DEVICE_DATAS
from .types import (
    HuaweiSolarConfigEntry,
    HuaweiSolarDeviceData,
    HuaweiSolarInverterData,
)
from .update_coordinator import HuaweiSolarUpdateCoordinator

TO_REDACT = {CONF_PASSWORD}


def _poll_outcome(coordinator: HuaweiSolarUpdateCoordinator) -> dict[str, Any] | None:
    """Return what the last poll refreshed, and what it did not.

    Values alone do not say whether a register is missing because the device
    does not have it or because its component stopped answering.
    """
    report = coordinator.last_report
    if report is None:
        return None
    return {
        "updated": sorted(report.updated),
        "failed": {name: str(err) for name, err in report.failed.items()},
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: HuaweiSolarConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    device_datas: list[HuaweiSolarDeviceData] = entry.runtime_data[DATA_DEVICE_DATAS]

    diagnostics_data = {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entities": {
            entity_entry.entity_id: entity_entry.extended_dict
            for entity_entry in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        },
    }
    for dd in device_datas:
        if isinstance(dd, HuaweiSolarInverterData):
            diagnostics_data[f"device_{dd.device.unit_id}"] = {
                "_type": "SUN2000",
                "model_name": dd.device.model_name,
                "firmware_version": dd.device.firmware_version,
                "software_version": dd.device.software_version,
                "pv_string_count": dd.device.pv_string_count,
                "has_optimizers": dd.device.has_optimizers,
                "battery_type": dd.device.battery_type,
                "battery_1_type": dd.device.battery_1_type,
                "battery_2_type": dd.device.battery_2_type,
                "power_meter_type": dd.device.power_meter_type,
                "supports_capacity_control": dd.device.supports_capacity_control,
            }

            if dd.power_meter_update_coordinator:
                diagnostics_data[
                    f"device_{dd.device.unit_id}_power_meter_data"
                ] = dd.power_meter_update_coordinator.data
                diagnostics_data[f"device_{dd.device.unit_id}_power_meter_poll"] = (
                    _poll_outcome(dd.power_meter_update_coordinator)
                )

            if dd.energy_storage_update_coordinator:
                diagnostics_data[f"device_{dd.device.unit_id}_battery_data"] = (
                    dd.energy_storage_update_coordinator.data
                )
                diagnostics_data[f"device_{dd.device.unit_id}_battery_poll"] = (
                    _poll_outcome(dd.energy_storage_update_coordinator)
                )

            if dd.optimizer_update_coordinator:
                diagnostics_data[
                    f"device_{dd.device.unit_id}_optimizer_data"
                ] = dd.optimizer_update_coordinator.data
        else:
            diagnostics_data[f"device_{dd.device.unit_id}"] = {
                "_type": type(dd.device).__name__,
                "model_name": dd.device.model_name,
                "serial_number": dd.device.serial_number,
            }

        diagnostics_data[f"device_{dd.device.unit_id}_data"] = (
            dd.update_coordinator.data
        )
        diagnostics_data[f"device_{dd.device.unit_id}_poll"] = _poll_outcome(
            dd.update_coordinator
        )

        if dd.configuration_update_coordinator:
            diagnostics_data[f"device_{dd.device.unit_id}_config_data"] = (
                dd.configuration_update_coordinator.data
            )
            diagnostics_data[f"device_{dd.device.unit_id}_config_poll"] = _poll_outcome(
                dd.configuration_update_coordinator
            )

    return diagnostics_data
