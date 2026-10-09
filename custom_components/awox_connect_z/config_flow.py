"""Config flow for AwoX Connect.Z."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    SOURCE_BLUETOOTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .advertisement import (
    advertisement_matches_address,
    parse_awox_advertisement,
)
from .client import (
    AwoxAuthenticationError,
    AwoxConnectZClient,
)
from .cloud import (
    AwoxCloudError,
    AwoxInvalidAuth,
    AwoxNoDevices,
    async_import_account,
)
from .const import (
    AWOX_COMPANY_ID,
    ADDITIONAL_DISCOVERY_TIMEOUT_SECONDS,
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEFAULT_TRANSITION,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_IDLE_DISCONNECT,
    CONF_MAC,
    CONF_MAX_CONCURRENT_COMMANDS,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
    CONF_NAME,
    CONF_OWNER_ID,
    CONF_SETUP_METHOD,
    DEFAULT_AVAILABILITY_TIMEOUT,
    LOCAL_DISCOVERY_SCAN_SECONDS,
    LOCAL_VERIFICATION_CONCURRENCY,
    LOCAL_VERIFICATION_TIMEOUT_SECONDS,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_MAX_CONCURRENT_COMMANDS,
    DEFAULT_TRANSITION,
    MAX_AVAILABILITY_TIMEOUT,
    MAX_IDLE_DISCONNECT,
    MAX_TRANSITION,
    MAX_CONCURRENT_COMMANDS,
    MESH_CREDENTIAL_MAX_BYTES,
    MIN_AVAILABILITY_TIMEOUT,
    MIN_IDLE_DISCONNECT,
    MIN_TRANSITION,
    MIN_CONCURRENT_COMMANDS,
    SETUP_METHOD_CLOUD,
    SETUP_METHOD_LOCAL,
    DOMAIN,
)
from .protocol import is_valid_device_mesh_id


class AwoxConnectZConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up AwoX Connect.Z lights from cloud import or local mesh data."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_address: str | None = None
        self._discovered_mesh_id: int | None = None
        self._discovered_name: str | None = None
        self._local_mesh_name: str | None = None
        self._local_mesh_password: str | None = None
        self._local_candidates: dict[str, dict[str, Any]] = {}
        self._local_selected_addresses: set[str] = set()
        self._local_verified_addresses: set[str] = set()
        self._local_scan_done = False
        self._local_reconfigure_mode = False
        self._local_failed_device = ""
        self._local_mesh_id_conflicts: dict[str, tuple[int, int]] = {}
        self._local_observed_mesh_ids: dict[str, int] = {}
        self._local_conflict_entered_id = ""
        self._local_conflict_advertised_id = ""
        self._local_unavailable_devices = ""
        self._local_rejected_devices = ""
        self._local_scan_error = False
        self._pending_cloud_data: dict[str, Any] | None = None
        self._pending_cloud_review_counts: tuple[int, int] | None = None

    def _configured_entries(self) -> list[ConfigEntry]:
        """Return existing AwoX Connect.Z config entries."""
        return list(self.hass.config_entries.async_entries(DOMAIN, include_ignore=False))

    @staticmethod
    def _entry_setup_method(entry: ConfigEntry) -> str:
        """Return cloud/local setup type, including older entries."""
        configured = str(entry.data.get(CONF_SETUP_METHOD) or "")
        if configured in {SETUP_METHOD_CLOUD, SETUP_METHOD_LOCAL}:
            return configured
        return (
            SETUP_METHOD_CLOUD
            if entry.data.get(CONF_OWNER_ID)
            else SETUP_METHOD_LOCAL
        )

    @staticmethod
    def _normalize_mac(value: Any) -> str | None:
        """Normalize a user-entered Bluetooth MAC address."""
        raw = str(value or "").strip().upper().replace("-", ":")
        if re.fullmatch(r"[0-9A-F]{12}", raw):
            raw = ":".join(raw[index : index + 2] for index in range(0, 12, 2))
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", raw):
            return None
        return raw

    @staticmethod
    def _mesh_credential_error(value: str) -> str | None:
        """Validate one AwoX mesh credential before protocol encoding."""
        if not value:
            return "mesh_credential_required"
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            return "mesh_credential_invalid"
        if len(encoded) > MESH_CREDENTIAL_MAX_BYTES:
            return "mesh_credential_too_long"
        return None

    @staticmethod
    def _parse_mesh_id(value: Any) -> int | None:
        """Parse decimal or 0x-prefixed per-lamp mesh destination."""
        raw = str(value or "").strip()
        try:
            mesh_id = int(raw, 16) if raw.lower().startswith("0x") else int(raw, 10)
        except (TypeError, ValueError):
            return None
        return mesh_id if is_valid_device_mesh_id(mesh_id) else None

    @staticmethod
    def _local_device_record(
        *, name: str, address: str, mesh_id: int, device_type: str
    ) -> dict[str, Any]:
        """Build a local-only device record compatible with normal setup."""
        return {
            "name": name,
            "mac": address,
            "mesh_id": mesh_id,
            "model": "Connect.Z",
            "manufacturer": "EGLO / AwoX",
            "firmware": None,
            "hardware": None,
            "device_type": device_type,
            "cloud_object_id": "",
        }

    def _configured_addresses(
        self, *, exclude_entry: ConfigEntry | None = None
    ) -> set[str]:
        """Return Bluetooth addresses already present in config entries."""
        return {
            str(device.get("mac") or "").upper()
            for entry in self._configured_entries()
            if exclude_entry is None or entry.entry_id != exclude_entry.entry_id
            for device in list(entry.data.get(CONF_DEVICES) or [])
            if device.get("mac")
        }

    @staticmethod
    def _device_address(device: dict[str, Any]) -> str:
        """Return a normalized device MAC from a stored/imported record."""
        return str(device.get("mac") or "").upper()

    @classmethod
    def _filter_conflicting_devices(
        cls,
        devices: list[dict[str, Any]],
        configured_addresses: set[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split imported devices into usable and already-configured records."""
        usable: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for device in devices:
            target = (
                conflicts
                if cls._device_address(device) in configured_addresses
                else usable
            )
            target.append(dict(device))
        return usable, conflicts

    def _set_local_credentials(self, mesh_name: str, mesh_password: str) -> None:
        """Store flow credentials and invalidate auth cache when they change."""
        credentials_changed = (
            self._local_mesh_name != mesh_name
            or self._local_mesh_password != mesh_password
        )
        self._local_mesh_name = mesh_name
        self._local_mesh_password = mesh_password
        if credentials_changed:
            self._local_verified_addresses.clear()

    def _device_label(self, address: str) -> str:
        """Return a useful name/MAC label for setup errors."""
        device = self._local_candidates.get(address)
        if device is None:
            return address
        name = str(device.get("name") or "").strip()
        return f"{name} ({address})" if name and address not in name else (name or address)

    def _current_reconfigure_device(
        self, address: str
    ) -> dict[str, Any] | None:
        """Return a stored device from the local entry currently being reconfigured."""
        if not self._local_reconfigure_mode:
            return None
        entry = self._get_reconfigure_entry()
        for device in list(entry.data.get(CONF_DEVICES) or []):
            if self._device_address(device) == address:
                return dict(device)
        return None

    def _remember_local_candidate(
        self, address: str, discovered: dict[str, Any]
    ) -> None:
        """Remember discovery data without hiding a mesh-ID contradiction."""
        discovered_mesh_id = int(discovered["mesh_id"])
        self._local_observed_mesh_ids[address] = discovered_mesh_id

        existing = self._local_candidates.get(address)
        if existing is not None and str(existing.get("device_type") or "") == "local_manual":
            entered_mesh_id = int(existing["mesh_id"])
            if entered_mesh_id != discovered_mesh_id:
                self._local_mesh_id_conflicts[address] = (
                    entered_mesh_id,
                    discovered_mesh_id,
                )
            else:
                self._local_mesh_id_conflicts.pop(address, None)

            # The user's name is deliberate. Keep it. Also keep the entered mesh
            # ID until the contradiction is explicitly corrected in the manual form.
            merged = dict(existing)
            for key, value in discovered.items():
                if key in {"name", "mesh_id", "device_type"}:
                    continue
                if merged.get(key) in (None, "") and value not in (None, ""):
                    merged[key] = value
            self._local_candidates[address] = merged
            return

        stored = self._current_reconfigure_device(address)
        if stored is not None:
            stored_mesh_id = int(stored.get("mesh_id") or 0)
            if stored_mesh_id == discovered_mesh_id:
                # Already-correct devices from the current hub are not "new"
                # candidates and do not need to clutter the reconfigure list.
                self._local_mesh_id_conflicts.pop(address, None)
                return

            # Surface an already-configured lamp only when the live advertisement
            # contradicts its stored target address. Keep its stored name/ID so the
            # user sees what must be corrected.
            candidate = dict(stored)
            candidate["device_type"] = "local_existing_mesh_id_conflict"
            self._local_candidates[address] = candidate
            self._local_mesh_id_conflicts[address] = (
                stored_mesh_id,
                discovered_mesh_id,
            )
            self._local_selected_addresses.add(address)
            return

        self._local_mesh_id_conflicts.pop(address, None)
        self._local_candidates[address] = dict(discovered)

    def _local_candidate_from_service_info(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        configured_addresses: set[str],
    ) -> tuple[str, dict[str, Any]] | None:
        """Build one local candidate from a complete Connect.Z advertisement."""
        address = str(service_info.address or "").upper()
        if not address or address in configured_addresses:
            return None

        raw = service_info.manufacturer_data.get(AWOX_COMPANY_ID)
        if raw is None:
            return None

        data = bytes(raw)
        if not advertisement_matches_address(data, address):
            return None

        state = parse_awox_advertisement(data)
        if state is None or not is_valid_device_mesh_id(state.mesh_id):
            return None

        name = self._discovery_display_name(service_info, address)
        return (
            address,
            self._local_device_record(
                name=name,
                address=address,
                mesh_id=state.mesh_id,
                device_type="local_bluetooth_discovered",
            ),
        )

    def _local_discovery_candidates(
        self,
        *,
        exclude_entry: ConfigEntry | None = None,
        min_seen_time: float | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return complete Connect.Z advertisements eligible for this flow."""
        configured_addresses = self._configured_addresses(
            exclude_entry=exclude_entry
        )
        candidates: dict[str, dict[str, Any]] = {}

        for service_info in bluetooth.async_discovered_service_info(
            self.hass, connectable=True
        ):
            if min_seen_time is not None:
                seen_time = float(getattr(service_info, "time", 0.0) or 0.0)
                if seen_time < min_seen_time:
                    continue

            candidate = self._local_candidate_from_service_info(
                service_info, configured_addresses
            )
            if candidate is not None:
                address, device = candidate
                candidates[address] = device

        return candidates

    async def _async_collect_local_candidates(
        self, *, exclude_entry: ConfigEntry | None = None
    ) -> None:
        """Collect only advertisements freshly seen during this scan window."""
        configured_addresses = self._configured_addresses(
            exclude_entry=exclude_entry
        )

        # "Search again" refreshes automatic results. Keep deliberate manual
        # records and previously detected existing-device mesh-ID conflicts.
        for address, device in tuple(self._local_candidates.items()):
            if str(device.get("device_type") or "") == "local_bluetooth_discovered":
                self._local_candidates.pop(address, None)
                self._local_mesh_id_conflicts.pop(address, None)

        scan_started = time.monotonic()

        @callback
        def _async_collect(
            service_info: bluetooth.BluetoothServiceInfoBleak,
            _change: bluetooth.BluetoothChange,
        ) -> None:
            # HA may replay a cached service-info record when the callback is
            # registered. Only a timestamp refreshed after this scan began is
            # considered a discovery from this scan.
            seen_time = float(getattr(service_info, "time", 0.0) or 0.0)
            if seen_time < scan_started:
                return

            candidate = self._local_candidate_from_service_info(
                service_info, configured_addresses
            )
            if candidate is None:
                return
            address, device = candidate
            self._remember_local_candidate(address, device)

        unregister = bluetooth.async_register_callback(
            self.hass,
            _async_collect,
            {
                "manufacturer_id": AWOX_COMPANY_ID,
                "connectable": True,
            },
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
        try:
            request_active_scan = getattr(
                bluetooth, "async_request_active_scan", None
            )
            wait_started = time.monotonic()

            if request_active_scan is not None:
                try:
                    await request_active_scan(
                        self.hass, LOCAL_DISCOVERY_SCAN_SECONDS
                    )
                except Exception:
                    # Keep collecting advertisements through the already
                    # registered callback for the rest of the scan window.
                    # asyncio.CancelledError is intentionally not swallowed.
                    self._local_scan_error = True

            remaining = LOCAL_DISCOVERY_SCAN_SECONDS - (
                time.monotonic() - wait_started
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
        finally:
            unregister()

        # Home Assistant can deduplicate byte-identical callbacks while still
        # refreshing the latest service-info timestamp. Pick up those packets,
        # but only when the timestamp proves they were seen during this scan.
        for address, device in self._local_discovery_candidates(
            exclude_entry=exclude_entry,
            min_seen_time=scan_started,
        ).items():
            self._remember_local_candidate(address, device)

    @staticmethod
    def _new_local_mesh_unique_id() -> str:
        """Create a stable unique id independent of mutable mesh credentials."""
        return f"awox-local-mesh-{uuid4().hex}"

    def _matching_local_mesh_entry(self) -> ConfigEntry | None:
        """Return an existing local entry with the same mesh credentials."""
        if self._local_mesh_name is None or self._local_mesh_password is None:
            return None

        for entry in self._configured_entries():
            if self._entry_setup_method(entry) != SETUP_METHOD_LOCAL:
                continue
            if (
                str(entry.data.get(CONF_MESH_NAME) or "") == self._local_mesh_name
                and str(entry.data.get(CONF_MESH_PASSWORD) or "")
                == self._local_mesh_password
            ):
                return entry
        return None

    @staticmethod
    def _merge_devices(
        existing: list[dict[str, Any]],
        additional: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge device records by Bluetooth address."""
        merged = {
            str(device.get("mac") or "").upper(): dict(device)
            for device in existing
            if device.get("mac")
        }
        for device in additional:
            address = str(device.get("mac") or "").upper()
            if address:
                merged[address] = dict(device)
        return list(merged.values())

    def _local_device_options(self) -> list[SelectOptionDict]:
        """Build selector options, including any detected mesh-ID contradiction."""
        options: list[SelectOptionDict] = []
        for address, device in sorted(self._local_candidates.items()):
            label = f"{device['name']} — 0x{int(device['mesh_id']):04X}"
            conflict = self._local_mesh_id_conflicts.get(address)
            if conflict is not None:
                entered_id, advertised_id = conflict
                label += f" ⚠ Advertisement 0x{advertised_id:04X}"
            options.append(SelectOptionDict(value=address, label=label))
        return options

    async def _async_verify_local_selection(
        self,
        addresses: list[str],
        *,
        exclude_entry: ConfigEntry | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str]:
        """Verify selected devices in parallel and report all connection failures."""
        if self._local_mesh_name is None or self._local_mesh_password is None:
            return [], "local_setup_incomplete", ""

        devices: list[dict[str, Any]] = []
        to_verify: list[str] = []

        # Validate non-I/O conditions first.
        for address in addresses:
            device = self._local_candidates.get(address)
            if device is None:
                return [], "device_unavailable_named", self._device_label(address)

            mesh_id_conflict = self._local_mesh_id_conflicts.get(address)
            if mesh_id_conflict is not None:
                entered_id, advertised_id = mesh_id_conflict
                self._local_conflict_entered_id = f"0x{entered_id:04X}"
                self._local_conflict_advertised_id = f"0x{advertised_id:04X}"
                return [], "mesh_id_mismatch_named", self._device_label(address)

            if address in self._configured_addresses(
                exclude_entry=exclude_entry
            ):
                return (
                    [],
                    "device_already_configured_named",
                    self._device_label(address),
                )

            devices.append(dict(device))
            if address not in self._local_verified_addresses:
                to_verify.append(address)

        if not to_verify:
            return devices, None, ""

        limiter = asyncio.Semaphore(LOCAL_VERIFICATION_CONCURRENCY)

        async def _verify_one(address: str) -> tuple[str, str | None]:
            async with limiter:
                return (
                    address,
                    await self._async_verify_credentials(
                        address,
                        self._local_mesh_name,
                        self._local_mesh_password,
                    ),
                )

        results = await asyncio.gather(
            *(_verify_one(address) for address in to_verify)
        )

        unavailable: list[str] = []
        rejected: list[str] = []

        for address, verify_error in results:
            if verify_error is None:
                self._local_verified_addresses.add(address)
            elif verify_error == "invalid_mesh_credentials":
                rejected.append(self._device_label(address))
            else:
                unavailable.append(self._device_label(address))

        if unavailable and rejected:
            self._local_unavailable_devices = ", ".join(unavailable)
            self._local_rejected_devices = ", ".join(rejected)
            return [], "local_verification_failed_named", ""

        if unavailable:
            return [], "devices_unavailable_named", ", ".join(unavailable)

        if rejected:
            return [], "mesh_credentials_rejected_named", ", ".join(rejected)

        return devices, None, ""

    def _first_device_conflict(
        self,
        devices: list[dict[str, Any]],
        *,
        exclude_entry: ConfigEntry | None = None,
    ) -> str | None:
        """Return the first MAC that became configured by another flow."""
        configured = self._configured_addresses(exclude_entry=exclude_entry)
        for device in devices:
            address = self._device_address(device)
            if address and address in configured:
                return address
        return None

    def _cloud_review_devices(
        self, *, exclude_entry: ConfigEntry | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Re-evaluate pending cloud devices against the current config entries."""
        if self._pending_cloud_data is None:
            return [], []
        return self._filter_conflicting_devices(
            list(self._pending_cloud_data.get(CONF_DEVICES) or []),
            self._configured_addresses(exclude_entry=exclude_entry),
        )

    async def _async_verify_credentials(
        self, address: str, mesh_name: str, mesh_password: str
    ) -> str | None:
        """Verify local mesh credentials with an interactive-flow time limit."""
        verifier = AwoxConnectZClient(
            self.hass,
            address,
            mesh_name,
            mesh_password,
            default_transition=DEFAULT_TRANSITION,
            idle_disconnect=DEFAULT_IDLE_DISCONNECT,
            command_semaphore=asyncio.Semaphore(1),
            max_concurrent_commands=1,
            availability_timeout=DEFAULT_AVAILABILITY_TIMEOUT,
        )
        try:
            async with asyncio.timeout(LOCAL_VERIFICATION_TIMEOUT_SECONDS):
                await verifier.async_verify_mesh_credentials()
        except AwoxAuthenticationError:
            return "invalid_mesh_credentials"
        except Exception:
            return "device_unavailable"
        finally:
            await verifier.async_close()
        return None

    async def _async_create_local_entry(
        self, devices: list[dict[str, Any]]
    ) -> ConfigFlowResult:
        """Create or extend one local mesh entry with a final duplicate check."""
        if self._local_mesh_name is None or self._local_mesh_password is None:
            return self.async_abort(reason="local_setup_incomplete")
        if not devices:
            return self.async_abort(reason="local_setup_incomplete")

        existing_entry = self._matching_local_mesh_entry()

        if existing_entry is None:
            await self.async_set_unique_id(self._new_local_mesh_unique_id())
            self._abort_if_unique_id_configured()

            # Another setup flow may have created the same local mesh while this
            # flow was authenticating or awaiting unique-id handling.
            existing_entry = self._matching_local_mesh_entry()

        conflict = self._first_device_conflict(
            devices, exclude_entry=existing_entry
        )
        if conflict is not None:
            return self.async_abort(
                reason="device_configured_during_setup",
                description_placeholders={
                    "device": self._device_label(conflict),
                },
            )

        if existing_entry is not None:
            existing_devices = [
                dict(device)
                for device in list(existing_entry.data.get(CONF_DEVICES) or [])
            ]
            old_addresses = {
                self._device_address(device) for device in existing_devices
            }
            actually_new = [
                device
                for device in devices
                if self._device_address(device) not in old_addresses
            ]
            merged = self._merge_devices(existing_devices, devices)
            new_data = dict(existing_entry.data)
            new_data[CONF_DEVICES] = merged

            # Persistence happens immediately after the final cross-entry check.
            self.hass.config_entries.async_update_entry(
                existing_entry, data=new_data
            )
            self._async_abort_bluetooth_discovery_flows(
                {
                    self._device_address(device)
                    for device in actually_new
                    if self._device_address(device)
                }
            )
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(
                reason="local_devices_added",
                description_placeholders={"count": str(len(actually_new))},
            )

        # No await between this final duplicate check and async_create_entry().
        conflict = self._first_device_conflict(devices)
        if conflict is not None:
            return self.async_abort(
                reason="device_configured_during_setup",
                description_placeholders={
                    "device": self._device_label(conflict),
                },
            )

        self._async_abort_bluetooth_discovery_flows(
            {
                self._device_address(device)
                for device in devices
                if self._device_address(device)
            }
        )
        return self.async_create_entry(
            title=f"AwoX Connect.Z Local ({self._local_mesh_name})",
            data={
                CONF_SETUP_METHOD: SETUP_METHOD_LOCAL,
                CONF_MESH_NAME: self._local_mesh_name,
                CONF_MESH_PASSWORD: self._local_mesh_password,
                CONF_DEVICES: devices,
            },
        )

    @callback
    def _async_abort_bluetooth_discovery_flows(
        self, addresses: set[str]
    ) -> None:
        """Close stale Bluetooth discovery cards for newly configured lamps."""
        normalized = {address.upper() for address in addresses}
        for flow in self._async_in_progress(include_uninitialized=True):
            if flow["flow_id"] == self.flow_id:
                continue
            context = flow.get("context") or {}
            if context.get("source") != SOURCE_BLUETOOTH:
                continue
            unique_id = str(context.get("unique_id") or "").upper()
            if unique_id not in normalized:
                continue
            self.hass.config_entries.flow.async_abort(flow["flow_id"])

    @staticmethod
    def _entry_contains_address(entry: ConfigEntry, address: str) -> bool:
        """Return whether an account entry already contains this BLE address."""
        normalized = address.upper()
        return any(
            str(device.get("mac") or "").upper() == normalized
            for device in list(entry.data.get(CONF_DEVICES) or [])
        )

    @staticmethod
    def _discovery_display_name(
        discovery_info: bluetooth.BluetoothServiceInfoBleak,
        address: str,
    ) -> str:
        """Build a provisional name from the BLE local name and full MAC."""
        candidate = str(discovery_info.name or "").strip()
        if candidate and candidate.upper() != address.upper():
            return f"{candidate} ({address})"
        return address

    async def _async_full_advertisement(
        self,
        discovery_info: bluetooth.BluetoothServiceInfoBleak,
        address: str,
    ) -> bluetooth.BluetoothServiceInfoBleak:
        """Wait for the long Connect.Z advertisement that contains the mesh ID."""
        raw = discovery_info.manufacturer_data.get(AWOX_COMPANY_ID)
        if raw is not None:
            data = bytes(raw)
            if (
                advertisement_matches_address(data, address)
                and parse_awox_advertisement(data) is not None
            ):
                return discovery_info

        def _complete(
            service_info: bluetooth.BluetoothServiceInfoBleak,
        ) -> bool:
            raw_data = service_info.manufacturer_data.get(AWOX_COMPANY_ID)
            if raw_data is None:
                return False
            data = bytes(raw_data)
            return (
                advertisement_matches_address(data, address)
                and parse_awox_advertisement(data) is not None
            )

        return await bluetooth.async_process_advertisements(
            self.hass,
            _complete,
            {
                "address": address,
                "manufacturer_id": AWOX_COMPANY_ID,
                "connectable": True,
            },
            bluetooth.BluetoothScanningMode.ACTIVE,
            ADDITIONAL_DISCOVERY_TIMEOUT_SECONDS,
        )

    async def _async_matching_account(
        self, address: str
    ) -> tuple[ConfigEntry | None, bool]:
        """Find the existing entry whose local mesh credential authenticates.

        Returns (matching_entry, had_transient_error). A rejected credential is
        a conclusive non-match; connection/transport failures are transient.
        """
        had_transient_error = False

        for entry in self._configured_entries():
            if self._entry_contains_address(entry, address):
                return entry, False

            mesh_name = str(entry.data.get(CONF_MESH_NAME) or "")
            mesh_password = str(entry.data.get(CONF_MESH_PASSWORD) or "")
            if not mesh_name or not mesh_password:
                continue

            verifier = AwoxConnectZClient(
                self.hass,
                address,
                mesh_name,
                mesh_password,
                default_transition=DEFAULT_TRANSITION,
                idle_disconnect=DEFAULT_IDLE_DISCONNECT,
                command_semaphore=asyncio.Semaphore(1),
                max_concurrent_commands=1,
                availability_timeout=DEFAULT_AVAILABILITY_TIMEOUT,
            )
            try:
                await verifier.async_verify_mesh_credentials()
            except AwoxAuthenticationError:
                continue
            except Exception:
                had_transient_error = True
                continue
            finally:
                await verifier.async_close()

            return entry, False

        return None, had_transient_error

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlowWithReload:
        return AwoxConnectZOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose cloud import or fully local mesh setup."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["cloud", "local"],
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Import compatible lamps from AwoX / EGLO HomeControl."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            email = str(user_input[CONF_EMAIL]).strip().lower()
            password = str(user_input[CONF_PASSWORD])

            try:
                imported = await async_import_account(
                    self.hass, email, password
                )
            except AwoxInvalidAuth:
                errors["base"] = "invalid_auth"
            except AwoxNoDevices:
                errors["base"] = "no_devices"
            except AwoxCloudError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    f"awox-cloud-{imported.owner_id}"
                )
                self._abort_if_unique_id_configured()

                self._pending_cloud_data = {
                    CONF_SETUP_METHOD: SETUP_METHOD_CLOUD,
                    CONF_EMAIL: email,
                    CONF_OWNER_ID: imported.owner_id,
                    CONF_MESH_NAME: imported.mesh_name,
                    CONF_MESH_PASSWORD: imported.mesh_password,
                    CONF_DEVICES: list(imported.devices),
                }
                imported_devices, conflicts = self._cloud_review_devices()

                if not imported_devices:
                    errors["base"] = "cloud_devices_already_configured"
                    description_placeholders["conflict_count"] = str(
                        len(conflicts)
                    )
                    self._pending_cloud_data = None
                elif conflicts:
                    self._pending_cloud_review_counts = (
                        len(imported_devices),
                        len(conflicts),
                    )
                    return await self.async_step_cloud_review()
                else:
                    self._pending_cloud_data[CONF_DEVICES] = imported_devices
                    pending = self._pending_cloud_data
                    self._pending_cloud_data = None
                    self._pending_cloud_review_counts = None
                    return self.async_create_entry(
                        title="AwoX / EGLO HomeControl",
                        data=pending,
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="cloud",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_cloud_review(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain and confirm a cloud import that skips existing lamps."""
        if self._pending_cloud_data is None:
            return self.async_abort(reason="local_setup_incomplete")

        imported_devices, conflicts = self._cloud_review_devices()
        current_counts = (len(imported_devices), len(conflicts))
        errors: dict[str, str] = {}

        if user_input is not None:
            if not imported_devices:
                errors["base"] = "cloud_devices_already_configured"
            elif (
                self._pending_cloud_review_counts is not None
                and current_counts != self._pending_cloud_review_counts
            ):
                # Another config flow changed ownership while the review page
                # was open. Show the updated counts once more before saving.
                self._pending_cloud_review_counts = current_counts
            else:
                self._abort_if_unique_id_configured()
                data = dict(self._pending_cloud_data)
                data[CONF_DEVICES] = imported_devices

                # No await between this last duplicate evaluation and creation.
                self._pending_cloud_data = None
                self._pending_cloud_review_counts = None
                return self.async_create_entry(
                    title="AwoX / EGLO HomeControl",
                    data=data,
                )

        self._pending_cloud_review_counts = current_counts
        self._set_confirm_only()
        return self.async_show_form(
            step_id="cloud_review",
            errors=errors,
            description_placeholders={
                "import_count": str(len(imported_devices)),
                "skipped_count": str(len(conflicts)),
            },
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the local Connect.Z mesh credential."""
        errors: dict[str, str] = {}
        if user_input is not None:
            mesh_name = str(user_input[CONF_MESH_NAME]).strip()
            mesh_password = str(user_input[CONF_MESH_PASSWORD])

            if error := self._mesh_credential_error(mesh_name):
                errors[CONF_MESH_NAME] = error
            if error := self._mesh_credential_error(mesh_password):
                errors[CONF_MESH_PASSWORD] = error

            if not errors:
                self._set_local_credentials(mesh_name, mesh_password)
                self._local_candidates = {}
                self._local_selected_addresses = set()
                self._local_mesh_id_conflicts = {}
                self._local_observed_mesh_ids = {}
                self._local_scan_error = False
                self._local_scan_done = False
                self._local_reconfigure_mode = False
                self._local_failed_device = ""
                return await self.async_step_local_devices()

        schema = vol.Schema(
            {
                vol.Required(CONF_MESH_NAME): TextSelector(),
                vol.Required(CONF_MESH_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="local",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_local_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Automatically scan, then select devices or stage a manual device."""
        if self._local_mesh_name is None or self._local_mesh_password is None:
            return self.async_abort(reason="local_setup_incomplete")

        if not self._local_scan_done:
            known_before = set(self._local_candidates)
            selected_before = set(self._local_selected_addresses)
            await self._async_collect_local_candidates()
            self._local_scan_done = True

            current = set(self._local_candidates)
            newly_found = current - known_before

            # Keep the user's previous choice for devices that are still here.
            # Anything genuinely new is selected automatically.
            self._local_selected_addresses = (
                (selected_before & current) | newly_found
            )

        errors: dict[str, str] = {}
        if self._local_scan_error:
            errors["base"] = "active_scan_failed"
            self._local_scan_error = False

        self._local_failed_device = ""
        self._local_conflict_entered_id = ""
        self._local_conflict_advertised_id = ""
        self._local_unavailable_devices = ""
        self._local_rejected_devices = ""

        if user_input is not None:
            selected = user_input.get(CONF_DEVICES) or []
            if isinstance(selected, str):
                selected = [selected]
            self._local_selected_addresses = {
                str(address).upper()
                for address in selected
                if str(address).upper() in self._local_candidates
            }

            if bool(user_input.get("rescan")):
                known_before = set(self._local_candidates)
                selected_before = set(self._local_selected_addresses)

                await self._async_collect_local_candidates()

                current = set(self._local_candidates)
                newly_found = current - known_before

                # Preserve checked/unchecked state for devices still present.
                # Automatically check only devices that did not exist before
                # this rescan. Devices not freshly seen disappear from both the
                # candidate list and the selected set.
                self._local_selected_addresses = (
                    (selected_before & current) | newly_found
                )
                return await self.async_step_local_devices()

            if bool(user_input.get("manual_add")):
                return await self.async_step_local_manual()

            if not self._local_selected_addresses:
                errors["base"] = "no_selection"
            else:
                addresses = sorted(self._local_selected_addresses)
                devices, verify_error, failed_device = (
                    await self._async_verify_local_selection(addresses)
                )
                if verify_error is not None:
                    errors["base"] = verify_error
                    self._local_failed_device = failed_device
                else:
                    return await self._async_create_local_entry(devices)

        options = self._local_device_options()
        default_selected = [
            address
            for address in sorted(self._local_selected_addresses)
            if address in self._local_candidates
        ]

        schema_fields: dict[Any, Any] = {}
        if options:
            schema_fields[
                vol.Optional(CONF_DEVICES, default=default_selected)
            ] = SelectSelector(
                SelectSelectorConfig(options=options, multiple=True)
            )
        schema_fields[vol.Optional("rescan", default=False)] = BooleanSelector()
        schema_fields[
            vol.Optional("manual_add", default=False)
        ] = BooleanSelector()

        return self.async_show_form(
            step_id="local_devices" if options else "local_devices_empty",
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "count": str(len(self._local_candidates)),
                "seconds": str(LOCAL_DISCOVERY_SCAN_SECONDS),
                "failed_device": self._local_failed_device,
                "entered_mesh_id": self._local_conflict_entered_id,
                "advertised_mesh_id": self._local_conflict_advertised_id,
                "unavailable_devices": self._local_unavailable_devices,
                "rejected_devices": self._local_rejected_devices,
            },
        )

    async def async_step_local_devices_empty(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the zero-result local device form."""
        return await self.async_step_local_devices(user_input)

    async def async_step_local_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stage one manual lamp, then return to the shared device selection."""
        if self._local_mesh_name is None or self._local_mesh_password is None:
            return self.async_abort(reason="local_setup_incomplete")

        errors: dict[str, str] = {}
        if user_input is not None:
            address = self._normalize_mac(user_input.get(CONF_MAC))
            mesh_id = self._parse_mesh_id(user_input.get("mesh_id"))
            name = str(user_input.get(CONF_NAME) or "").strip()

            if address is None:
                errors["mac"] = "invalid_mac"
            if mesh_id is None:
                errors["mesh_id"] = "invalid_mesh_id"

            if address is not None and mesh_id is not None:
                observed_mesh_id = self._local_observed_mesh_ids.get(address)

                service_info = bluetooth.async_last_service_info(
                    self.hass, address, connectable=True
                )
                if service_info is not None:
                    raw = service_info.manufacturer_data.get(AWOX_COMPANY_ID)
                    if raw is not None:
                        data = bytes(raw)
                        if advertisement_matches_address(data, address):
                            advertised = parse_awox_advertisement(data)
                            if (
                                advertised is not None
                                and is_valid_device_mesh_id(advertised.mesh_id)
                            ):
                                observed_mesh_id = advertised.mesh_id
                                self._local_observed_mesh_ids[address] = (
                                    advertised.mesh_id
                                )

                if (
                    observed_mesh_id is not None
                    and observed_mesh_id != mesh_id
                ):
                    self._local_mesh_id_conflicts[address] = (
                        mesh_id,
                        observed_mesh_id,
                    )
                    self._local_conflict_entered_id = f"0x{mesh_id:04X}"
                    self._local_conflict_advertised_id = (
                        f"0x{observed_mesh_id:04X}"
                    )
                    errors["mesh_id"] = "mesh_id_mismatch"

            if address is not None and mesh_id is not None and not errors:
                reconfigure_entry = (
                    self._get_reconfigure_entry()
                    if self._local_reconfigure_mode
                    else None
                )
                if address in self._configured_addresses(
                    exclude_entry=reconfigure_entry
                ):
                    errors["base"] = "device_already_configured"
                else:
                    verify_error = await self._async_verify_credentials(
                        address,
                        self._local_mesh_name,
                        self._local_mesh_password,
                    )
                    if verify_error is not None:
                        errors["base"] = (
                            "invalid_mesh_credentials_named"
                            if verify_error == "invalid_mesh_credentials"
                            else "device_unavailable_named"
                        )
                        self._local_failed_device = address
                    else:
                        self._local_verified_addresses.add(address)
                        if not name and self._local_reconfigure_mode:
                            stored = self._current_reconfigure_device(address)
                            if stored is not None:
                                name = str(stored.get("name") or "").strip()
                        if not name:
                            name = f"AwoX Connect.Z ({address})"
                        observed_mesh_id = self._local_observed_mesh_ids.get(
                            address
                        )
                        if (
                            observed_mesh_id is None
                            or observed_mesh_id == mesh_id
                        ):
                            self._local_mesh_id_conflicts.pop(address, None)
                        self._local_candidates[address] = (
                            self._local_device_record(
                                name=name,
                                address=address,
                                mesh_id=mesh_id,
                                device_type="local_manual",
                            )
                        )
                        self._local_selected_addresses.add(address)
                        if self._local_reconfigure_mode:
                            return await self.async_step_reconfigure_local_devices()
                        return await self.async_step_local_devices()

        schema = vol.Schema(
            {
                vol.Optional(CONF_NAME, default=""): TextSelector(),
                vol.Required(CONF_MAC): TextSelector(),
                vol.Required("mesh_id"): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="local_manual",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "failed_device": self._local_failed_device,
                "entered_mesh_id": self._local_conflict_entered_id,
                "advertised_mesh_id": self._local_conflict_advertised_id,
                "unavailable_devices": self._local_unavailable_devices,
                "rejected_devices": self._local_rejected_devices,
            },
        )

    async def async_step_bluetooth(
        self,
        discovery_info: bluetooth.BluetoothServiceInfoBleak,
    ) -> ConfigFlowResult:
        """Handle a Connect.Z lamp discovered through Bluetooth."""
        address = str(discovery_info.address or "").upper()
        if not address:
            return self.async_abort(reason="incomplete_advertisement")

        await self.async_set_unique_id(address)
        # Respect Home Assistant's built-in "Ignore" entry for this device.
        self._abort_if_unique_id_configured()

        entries = self._configured_entries()
        if not entries:
            clear_match_history = getattr(
                bluetooth, "async_clear_address_from_match_history", None
            )
            if clear_match_history is not None:
                clear_match_history(self.hass, address)
            return self.async_abort(reason="account_required")

        if any(
            self._entry_contains_address(entry, address)
            for entry in entries
        ):
            return self.async_abort(reason="device_already_configured")

        try:
            complete_info = await self._async_full_advertisement(
                discovery_info, address
            )
        except TimeoutError:
            # Allow a later complete advertisement to trigger discovery again.
            clear_match_history = getattr(
                bluetooth, "async_clear_address_from_match_history", None
            )
            if clear_match_history is not None:
                clear_match_history(self.hass, address)
            return self.async_abort(reason="incomplete_advertisement")

        raw = complete_info.manufacturer_data.get(AWOX_COMPANY_ID)
        if raw is None:
            return self.async_abort(reason="incomplete_advertisement")

        data = bytes(raw)
        if not advertisement_matches_address(data, address):
            return self.async_abort(reason="identity_mismatch")

        state = parse_awox_advertisement(data)
        if state is None or not is_valid_device_mesh_id(state.mesh_id):
            return self.async_abort(reason="incomplete_advertisement")

        self._discovered_address = address
        self._discovered_mesh_id = state.mesh_id
        self._discovered_name = self._discovery_display_name(
            complete_info, address
        )
        self.context["title_placeholders"] = {
            "name": self._discovered_name,
        }

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm and locally verify a discovered lamp before adding it."""
        if (
            self._discovered_address is None
            or self._discovered_mesh_id is None
            or self._discovered_name is None
        ):
            return self.async_abort(reason="incomplete_advertisement")

        address = self._discovered_address
        mesh_id = self._discovered_mesh_id
        errors: dict[str, str] = {}

        if user_input is not None:
            entries = self._configured_entries()
            if any(
                self._entry_contains_address(entry, address)
                for entry in entries
            ):
                return self.async_abort(reason="device_already_configured")

            matching_entry, had_transient_error = (
                await self._async_matching_account(address)
            )
            if matching_entry is None:
                errors["base"] = (
                    "device_unavailable"
                    if had_transient_error
                    else "not_same_mesh"
                )
            else:
                devices = [
                    dict(device)
                    for device in list(
                        matching_entry.data.get(CONF_DEVICES) or []
                    )
                ]
                devices.append(
                    {
                        "name": self._discovered_name,
                        "mac": address,
                        "mesh_id": mesh_id,
                        "model": "Connect.Z",
                        "manufacturer": "EGLO / AwoX",
                        "firmware": None,
                        "hardware": None,
                        "device_type": "bluetooth_discovered",
                        "cloud_object_id": "",
                    }
                )
                new_data = dict(matching_entry.data)
                new_data[CONF_DEVICES] = devices
                self.hass.config_entries.async_update_entry(
                    matching_entry, data=new_data
                )
                await self.hass.config_entries.async_reload(
                    matching_entry.entry_id
                )
                return self.async_abort(
                    reason="device_added",
                    description_placeholders={
                        "name": self._discovered_name,
                    },
                )

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            errors=errors,
            description_placeholders={
                "name": self._discovered_name,
                "mesh_id": f"0x{mesh_id:04X}",
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Refresh cloud data or update local mesh credentials."""
        reconfigure_entry = self._get_reconfigure_entry()
        if self._entry_setup_method(reconfigure_entry) == SETUP_METHOD_LOCAL:
            return await self.async_step_reconfigure_local(user_input)

        errors: dict[str, str] = {}

        if user_input is not None:
            email = str(user_input[CONF_EMAIL]).strip().lower()
            password = str(user_input[CONF_PASSWORD])

            try:
                imported = await async_import_account(
                    self.hass, email, password
                )
            except AwoxInvalidAuth:
                errors["base"] = "invalid_auth"
            except AwoxNoDevices:
                errors["base"] = "no_devices"
            except AwoxCloudError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    f"awox-cloud-{imported.owner_id}"
                )
                self._abort_if_unique_id_mismatch(
                    reason="wrong_account"
                )

                self._pending_cloud_data = {
                    CONF_SETUP_METHOD: SETUP_METHOD_CLOUD,
                    CONF_EMAIL: email,
                    CONF_OWNER_ID: imported.owner_id,
                    CONF_MESH_NAME: imported.mesh_name,
                    CONF_MESH_PASSWORD: imported.mesh_password,
                    CONF_DEVICES: list(imported.devices),
                }
                imported_devices, conflicts = self._cloud_review_devices(
                    exclude_entry=reconfigure_entry
                )
                if not imported_devices:
                    errors["base"] = "cloud_devices_already_configured"
                    self._pending_cloud_data = None
                elif conflicts:
                    self._pending_cloud_review_counts = (
                        len(imported_devices),
                        len(conflicts),
                    )
                    return await self.async_step_reconfigure_cloud_review()
                else:
                    self._pending_cloud_data = None
                    self._pending_cloud_review_counts = None
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        data_updates={
                            CONF_SETUP_METHOD: SETUP_METHOD_CLOUD,
                            CONF_EMAIL: email,
                            CONF_OWNER_ID: imported.owner_id,
                            CONF_MESH_NAME: imported.mesh_name,
                            CONF_MESH_PASSWORD: imported.mesh_password,
                            CONF_DEVICES: imported_devices,
                        },
                    )

        email_default = (
            str(user_input.get(CONF_EMAIL, "")).strip().lower()
            if user_input is not None
            else str(
                reconfigure_entry.data.get(CONF_EMAIL, "")
            )
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_EMAIL, default=email_default
                ): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reconfigure_cloud_review(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm cloud reconfigure when some lamps are owned elsewhere."""
        if self._pending_cloud_data is None:
            return self.async_abort(reason="local_setup_incomplete")

        reconfigure_entry = self._get_reconfigure_entry()
        imported_devices, conflicts = self._cloud_review_devices(
            exclude_entry=reconfigure_entry
        )
        current_counts = (len(imported_devices), len(conflicts))
        errors: dict[str, str] = {}

        if user_input is not None:
            if not imported_devices:
                errors["base"] = "cloud_devices_already_configured"
            elif (
                self._pending_cloud_review_counts is not None
                and current_counts != self._pending_cloud_review_counts
            ):
                self._pending_cloud_review_counts = current_counts
            else:
                data_updates = dict(self._pending_cloud_data)
                data_updates[CONF_DEVICES] = imported_devices
                self._pending_cloud_data = None
                self._pending_cloud_review_counts = None

                # No await between the last ownership check and entry update.
                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data_updates=data_updates,
                )

        self._pending_cloud_review_counts = current_counts
        self._set_confirm_only()
        return self.async_show_form(
            step_id="reconfigure_cloud_review",
            errors=errors,
            description_placeholders={
                "import_count": str(len(imported_devices)),
                "skipped_count": str(len(conflicts)),
            },
        )

    async def async_step_reconfigure_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Verify local credentials, then optionally add more mesh devices."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            mesh_name = str(user_input[CONF_MESH_NAME]).strip()
            mesh_password = str(user_input[CONF_MESH_PASSWORD])

            if error := self._mesh_credential_error(mesh_name):
                errors[CONF_MESH_NAME] = error
            if error := self._mesh_credential_error(mesh_password):
                errors[CONF_MESH_PASSWORD] = error

            if errors:
                devices = []
            else:
                devices = list(reconfigure_entry.data.get(CONF_DEVICES) or [])

            had_transport_error = False
            had_auth_error = False
            verified = False
            for device in devices:
                address = str(device.get("mac") or "").upper()
                if not address:
                    continue
                verify_error = await self._async_verify_credentials(
                    address, mesh_name, mesh_password
                )
                if verify_error is None:
                    verified = True
                    break
                if verify_error == "invalid_mesh_credentials":
                    had_auth_error = True
                else:
                    had_transport_error = True

            if verified:
                self._set_local_credentials(mesh_name, mesh_password)
                self._local_candidates = {}
                self._local_selected_addresses = set()
                self._local_mesh_id_conflicts = {}
                self._local_observed_mesh_ids = {}
                self._local_scan_error = False
                self._local_scan_done = False
                self._local_reconfigure_mode = True
                self._local_failed_device = ""
                return await self.async_step_reconfigure_local_devices()

            if not errors:
                errors["base"] = (
                    "invalid_mesh_credentials"
                    if had_auth_error
                    else "device_unavailable"
                    if had_transport_error
                    else "local_setup_incomplete"
                )

        mesh_name_default = (
            str(user_input.get(CONF_MESH_NAME, "")).strip()
            if user_input is not None
            else str(reconfigure_entry.data.get(CONF_MESH_NAME, ""))
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MESH_NAME, default=mesh_name_default
                ): TextSelector(),
                vol.Required(CONF_MESH_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure_local",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reconfigure_local_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Search for and optionally add devices while reconfiguring a local mesh."""
        if self._local_mesh_name is None or self._local_mesh_password is None:
            return self.async_abort(reason="local_setup_incomplete")

        reconfigure_entry = self._get_reconfigure_entry()

        if not self._local_scan_done:
            known_before = set(self._local_candidates)
            selected_before = set(self._local_selected_addresses)
            await self._async_collect_local_candidates(
                exclude_entry=reconfigure_entry
            )
            self._local_scan_done = True

            current = set(self._local_candidates)
            newly_found = current - known_before
            self._local_selected_addresses = (
                (selected_before & current) | newly_found
            )

        errors: dict[str, str] = {}
        if self._local_scan_error:
            errors["base"] = "active_scan_failed"
            self._local_scan_error = False

        self._local_failed_device = ""
        self._local_conflict_entered_id = ""
        self._local_conflict_advertised_id = ""
        self._local_unavailable_devices = ""
        self._local_rejected_devices = ""

        if user_input is not None:
            selected = user_input.get(CONF_DEVICES) or []
            if isinstance(selected, str):
                selected = [selected]
            self._local_selected_addresses = {
                str(address).upper()
                for address in selected
                if str(address).upper() in self._local_candidates
            }

            if bool(user_input.get("rescan")):
                known_before = set(self._local_candidates)
                selected_before = set(self._local_selected_addresses)

                await self._async_collect_local_candidates(
                    exclude_entry=reconfigure_entry
                )

                current = set(self._local_candidates)
                newly_found = current - known_before
                self._local_selected_addresses = (
                    (selected_before & current) | newly_found
                )
                return await self.async_step_reconfigure_local_devices()

            if bool(user_input.get("manual_add")):
                return await self.async_step_local_manual()

            addresses = sorted(self._local_selected_addresses)
            new_devices: list[dict[str, Any]] = []
            if addresses:
                new_devices, verify_error, failed_device = (
                    await self._async_verify_local_selection(
                        addresses, exclude_entry=reconfigure_entry
                    )
                )
                if verify_error is not None:
                    errors["base"] = verify_error
                    self._local_failed_device = failed_device

            if not errors:
                conflict = self._first_device_conflict(
                    new_devices, exclude_entry=reconfigure_entry
                )
                if conflict is not None:
                    errors["base"] = "device_already_configured_named"
                    self._local_failed_device = self._device_label(conflict)
                else:
                    existing_devices = [
                        dict(device)
                        for device in list(
                            reconfigure_entry.data.get(CONF_DEVICES) or []
                        )
                    ]
                    merged = self._merge_devices(existing_devices, new_devices)

                    self._async_abort_bluetooth_discovery_flows(
                        {
                            self._device_address(device)
                            for device in new_devices
                            if self._device_address(device)
                        }
                    )

                    # No await between the final duplicate check and update.
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        data_updates={
                            CONF_SETUP_METHOD: SETUP_METHOD_LOCAL,
                            CONF_MESH_NAME: self._local_mesh_name,
                            CONF_MESH_PASSWORD: self._local_mesh_password,
                            CONF_DEVICES: merged,
                        },
                    )

        options = self._local_device_options()
        default_selected = [
            address
            for address in sorted(self._local_selected_addresses)
            if address in self._local_candidates
        ]

        schema_fields: dict[Any, Any] = {}
        if options:
            schema_fields[
                vol.Optional(CONF_DEVICES, default=default_selected)
            ] = SelectSelector(
                SelectSelectorConfig(options=options, multiple=True)
            )
        schema_fields[vol.Optional("rescan", default=False)] = BooleanSelector()
        schema_fields[
            vol.Optional("manual_add", default=False)
        ] = BooleanSelector()

        return self.async_show_form(
            step_id=(
                "reconfigure_local_devices"
                if options
                else "reconfigure_local_devices_empty"
            ),
            data_schema=vol.Schema(schema_fields),
            errors=errors,
            description_placeholders={
                "count": str(len(self._local_candidates)),
                "seconds": str(LOCAL_DISCOVERY_SCAN_SECONDS),
                "existing_count": str(
                    len(list(reconfigure_entry.data.get(CONF_DEVICES) or []))
                ),
                "failed_device": self._local_failed_device,
                "entered_mesh_id": self._local_conflict_entered_id,
                "advertised_mesh_id": self._local_conflict_advertised_id,
                "unavailable_devices": self._local_unavailable_devices,
                "rejected_devices": self._local_rejected_devices,
            },
        )

    async def async_step_reconfigure_local_devices_empty(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the zero-result local reconfigure device form."""
        return await self.async_step_reconfigure_local_devices(user_input)


class AwoxConnectZOptionsFlow(OptionsFlowWithReload):
    """Runtime tuning options shared by all lamps in the config entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DEFAULT_TRANSITION,
                    default=options.get(
                        CONF_DEFAULT_TRANSITION,
                        DEFAULT_TRANSITION,
                    ),
                ): vol.All(
                    vol.Coerce(float),
                    vol.Range(min=MIN_TRANSITION, max=MAX_TRANSITION),
                ),
                vol.Required(
                    CONF_IDLE_DISCONNECT,
                    default=options.get(
                        CONF_IDLE_DISCONNECT,
                        DEFAULT_IDLE_DISCONNECT,
                    ),
                ): vol.All(
                    vol.Coerce(float),
                    vol.Range(min=MIN_IDLE_DISCONNECT, max=MAX_IDLE_DISCONNECT),
                ),
                vol.Required(
                    CONF_AVAILABILITY_TIMEOUT,
                    default=options.get(
                        CONF_AVAILABILITY_TIMEOUT,
                        DEFAULT_AVAILABILITY_TIMEOUT,
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_AVAILABILITY_TIMEOUT,
                        max=MAX_AVAILABILITY_TIMEOUT,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_MAX_CONCURRENT_COMMANDS,
                    default=options.get(
                        CONF_MAX_CONCURRENT_COMMANDS,
                        DEFAULT_MAX_CONCURRENT_COMMANDS,
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_CONCURRENT_COMMANDS,
                        max=MAX_CONCURRENT_COMMANDS,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema
        )
