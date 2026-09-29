"""Config flow for AwoX Connect.Z."""

from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .advertisement import (
    AWOX_COMPANY_ID,
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
    CONF_AVAILABILITY_TIMEOUT,
    CONF_DEFAULT_TRANSITION,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_IDLE_DISCONNECT,
    CONF_MAX_CONCURRENT_COMMANDS,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
    CONF_OWNER_ID,
    DEFAULT_AVAILABILITY_TIMEOUT,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_MAX_CONCURRENT_COMMANDS,
    DEFAULT_TRANSITION,
    MAX_AVAILABILITY_TIMEOUT,
    MAX_CONCURRENT_COMMANDS,
    MIN_AVAILABILITY_TIMEOUT,
    MIN_CONCURRENT_COMMANDS,
    DOMAIN,
)


class AwoxConnectZConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up all AwoX Connect.Z lights from one account login."""

    VERSION = 2
    ADDITIONAL_DISCOVERY_TIMEOUT = 30

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_address: str | None = None
        self._discovered_mesh_id: int | None = None
        self._discovered_name: str | None = None

    def _configured_entries(self) -> list[ConfigEntry]:
        """Return existing AwoX account entries."""
        return list(self.hass.config_entries.async_entries(DOMAIN, include_ignore=False))

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
            self.ADDITIONAL_DISCOVERY_TIMEOUT,
        )

    async def _async_matching_account(
        self, address: str
    ) -> tuple[ConfigEntry | None, bool]:
        """Find the existing account whose local mesh credential authenticates.

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

                description_placeholders["count"] = str(
                    len(imported.devices)
                )

                return self.async_create_entry(
                    title="AwoX / EGLO HomeControl",
                    data={
                        CONF_EMAIL: email,
                        CONF_OWNER_ID: imported.owner_id,
                        CONF_MESH_NAME: imported.mesh_name,
                        CONF_MESH_PASSWORD: imported.mesh_password,
                        CONF_DEVICES: imported.devices,
                    },
                    description_placeholders=description_placeholders,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
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
        if state is None or not 1 <= state.mesh_id <= 0xFFFE:
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
        """Refresh account, mesh credentials and lamp metadata."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

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

                return self.async_update_reload_and_abort(
                    reconfigure_entry,
                    data_updates={
                        CONF_EMAIL: email,
                        CONF_OWNER_ID: imported.owner_id,
                        CONF_MESH_NAME: imported.mesh_name,
                        CONF_MESH_PASSWORD: imported.mesh_password,
                        CONF_DEVICES: imported.devices,
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


class AwoxConnectZOptionsFlow(OptionsFlowWithReload):
    """Runtime tuning options shared by all lamps in the account."""

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
                    vol.Range(min=0.0, max=10.0),
                ),
                vol.Required(
                    CONF_IDLE_DISCONNECT,
                    default=options.get(
                        CONF_IDLE_DISCONNECT,
                        DEFAULT_IDLE_DISCONNECT,
                    ),
                ): vol.All(
                    vol.Coerce(float),
                    vol.Range(min=5.0, max=300.0),
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
