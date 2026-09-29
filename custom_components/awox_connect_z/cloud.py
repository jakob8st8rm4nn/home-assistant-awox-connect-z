"""One-shot import of AwoX / EGLO HomeControl account data.

The account password is used only during setup or manual reconfiguration. The password
and Parse session token are deliberately not stored. We persist only the local
service=zigbee mesh credential and lamp metadata needed for local BLE control.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import uuid
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import PARSE_APP_ID, PARSE_CLIENT_KEY, PARSE_URLS

_MAC_HEX_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


class AwoxCloudError(Exception):
    """Base cloud error."""


class AwoxInvalidAuth(AwoxCloudError):
    """Invalid AwoX account credentials."""


class AwoxNoDevices(AwoxCloudError):
    """No usable Connect.Z light records were found."""


@dataclass(slots=True)
class AwoxAccountImport:
    """Data imported once from the AwoX account."""

    owner_id: str
    mesh_name: str
    mesh_password: str
    devices: list[dict[str, Any]]
    raw_device_count: int


def _headers(
    installation_id: str, session_token: str | None = None
) -> dict[str, str]:
    headers = {
        "X-Parse-Application-Id": PARSE_APP_ID,
        "X-Parse-Client-Key": PARSE_CLIENT_KEY,
        "X-Parse-Installation-Id": installation_id,
        "Content-Type": "application/json",
    }
    if session_token:
        headers["X-Parse-Session-Token"] = session_token
    return headers


async def _post_json(
    hass: HomeAssistant,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    session = async_get_clientsession(hass)
    async with asyncio.timeout(20):
        async with session.post(url, headers=headers, json=payload) as response:
            try:
                data = await response.json(content_type=None)
            except Exception as err:
                raise AwoxCloudError(
                    f"Invalid AwoX cloud response: {err}"
                ) from err
            return response.status, data


def normalize_mac(value: str) -> str:
    """Normalize a cloud BLE address to AA:BB:CC:DD:EE:FF."""
    compact = (
        value.strip()
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
    )
    if not _MAC_HEX_RE.fullmatch(compact):
        raise ValueError(f"Invalid Bluetooth MAC: {value!r}")
    compact = compact.upper()
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2))


def _first_nonempty(device: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = device.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _parse_mesh_id(value: Any) -> int:
    """Preserve the cloud's signed 16-bit Connect.Z address for diagnostics."""
    if value is None or str(value).strip() == "":
        return 0
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip()
        parsed = (
            int(text, 0)
            if text.lower().startswith("0x")
            else int(text, 10)
        )
    return parsed & 0xFFFF


def _looks_like_light(device: dict[str, Any]) -> bool:
    """Identify BLE light records while excluding explicit Wi-Fi devices."""
    dtype = str(device.get("type") or "").lower()
    provider = str(device.get("provider") or "").lower()
    model = str(
        device.get("modelName")
        or device.get("model")
        or ""
    ).lower()

    if dtype.startswith(".wifi.") or "wifi" in provider:
        return False

    if "light" in dtype:
        return True

    return any(
        token in model
        for token in (
            "light",
            "lamp",
            "bulb",
            "eglo",
            "awox",
            "spot",
            "panel",
        )
    )


def _extract_mac(device: dict[str, Any]) -> str:
    raw = _first_nonempty(
        device,
        "macAddress",
        "mac",
        "bleAddress",
        "bluetoothAddress",
        "bluetoothMacAddress",
    )
    if raw is None:
        raise ValueError("missing Bluetooth MAC")
    return normalize_mac(str(raw))


def _convert_devices(
    raw_devices: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert HomeControl Device rows into JSON-safe light metadata."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for device in raw_devices:
        if not _looks_like_light(device):
            continue

        try:
            mac = _extract_mac(device)
            mesh_id = _parse_mesh_id(
                _first_nonempty(
                    device,
                    "address",
                    "meshAddress",
                    "meshId",
                    "shortAddress",
                )
            )
        except (TypeError, ValueError):
            continue

        if mac in seen:
            continue
        seen.add(mac)

        name = str(
            _first_nonempty(
                device,
                "displayName",
                "friendlyName",
                "name",
                "modelName",
            )
            or f"AwoX {mac[-8:]}"
        )

        result.append(
            {
                "name": name,
                "mac": mac,
                "mesh_id": mesh_id,
                "model": str(
                    _first_nonempty(device, "modelName", "model")
                    or "Connect.Z"
                ),
                "manufacturer": str(
                    _first_nonempty(
                        device, "vendor", "manufacturer"
                    )
                    or "EGLO / AwoX"
                ),
                "firmware": (
                    str(
                        _first_nonempty(
                            device, "version", "firmwareVersion"
                        )
                        or ""
                    )
                    or None
                ),
                "hardware": (
                    str(
                        _first_nonempty(
                            device, "hardwareVersion", "hardware"
                        )
                        or ""
                    )
                    or None
                ),
                "device_type": str(device.get("type") or ""),
                "cloud_object_id": str(device.get("objectId") or ""),
            }
        )

    return result


async def _fetch_class(
    hass: HomeAssistant,
    base_url: str,
    installation_id: str,
    session_token: str,
    owner_id: str,
    class_name: str,
    *,
    owner_filtered: bool,
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"_method": "GET"}
    if owner_filtered:
        payload["where"] = {
            "owner": {
                "__type": "Pointer",
                "className": "_User",
                "objectId": owner_id,
            }
        }

    status, data = await _post_json(
        hass,
        base_url + "classes/" + class_name,
        headers=_headers(installation_id, session_token),
        payload=payload,
    )
    if status != 200:
        raise AwoxCloudError(
            str(data.get("error") or f"HTTP {status}")
        )
    return list(data.get("results", []))


def _merge_rows(
    primary: list[dict[str, Any]],
    secondary: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge owner-filtered and ACL-visible rows without duplicating objects."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in [*primary, *secondary]:
        object_id = str(row.get("objectId") or "")
        if object_id:
            key = f"id:{object_id}"
        else:
            key = repr(sorted(row.items(), key=lambda item: item[0]))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


async def async_import_account(
    hass: HomeAssistant,
    email: str,
    password: str,
) -> AwoxAccountImport:
    """Import all usable BLE lights and the local Connect.Z credential."""
    installation_id = str(uuid.uuid4())

    user: dict[str, Any] | None = None
    base_url: str | None = None
    last_error = "unknown"

    for candidate_url in PARSE_URLS:
        try:
            status, data = await _post_json(
                hass,
                candidate_url + "login",
                headers=_headers(installation_id),
                payload={
                    "username": email.strip().lower(),
                    "password": password,
                    "_method": "GET",
                },
            )
        except (TimeoutError, AwoxCloudError) as err:
            last_error = str(err)
            continue

        if (
            status == 200
            and data.get("objectId")
            and data.get("sessionToken")
        ):
            user = data
            base_url = candidate_url
            break

        last_error = str(
            data.get("error") or f"HTTP {status}"
        )

    if user is None or base_url is None:
        if (
            "invalid" in last_error.lower()
            or "password" in last_error.lower()
            or "credential" in last_error.lower()
        ):
            raise AwoxInvalidAuth(last_error)
        raise AwoxCloudError(last_error)

    owner_id = str(user["objectId"])
    session_token = str(user["sessionToken"])

    credentials = await _fetch_class(
        hass,
        base_url,
        installation_id,
        session_token,
        owner_id,
        "Credential",
        owner_filtered=True,
    )

    owner_devices = await _fetch_class(
        hass,
        base_url,
        installation_id,
        session_token,
        owner_id,
        "Device",
        owner_filtered=True,
    )
    acl_devices = await _fetch_class(
        hass,
        base_url,
        installation_id,
        session_token,
        owner_id,
        "Device",
        owner_filtered=False,
    )
    raw_devices = _merge_rows(owner_devices, acl_devices)
    devices = _convert_devices(raw_devices)

    mesh_name = ""
    mesh_password = ""
    for item in credentials:
        if str(item.get("service") or "").lower() != "zigbee":
            continue
        candidate_name = str(item.get("client_id") or "")
        candidate_password = str(item.get("access_token") or "")
        if not candidate_name or not candidate_password:
            continue
        if (
            len(candidate_name.encode("utf-8")) > 16
            or len(candidate_password.encode("utf-8")) > 16
        ):
            continue
        mesh_name = candidate_name
        mesh_password = candidate_password
        break

    if not mesh_name or not mesh_password:
        raise AwoxCloudError(
            "No usable service=zigbee credential found"
        )

    if not devices:
        raise AwoxNoDevices(
            f"AwoX account returned {len(raw_devices)} Device rows, "
            "but none were usable BLE lights"
        )

    return AwoxAccountImport(
        owner_id=owner_id,
        mesh_name=mesh_name,
        mesh_password=mesh_password,
        devices=devices,
        raw_device_count=len(raw_devices),
    )
