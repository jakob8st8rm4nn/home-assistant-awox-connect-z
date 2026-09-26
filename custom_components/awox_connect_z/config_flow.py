"""Config flow for AwoX Connect.Z."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import callback

from .cloud import (
    AwoxCloudError,
    AwoxInvalidAuth,
    AwoxNoDevices,
    async_import_account,
)
from .const import (
    CONF_DEFAULT_TRANSITION,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_IDLE_DISCONNECT,
    CONF_MESH_NAME,
    CONF_MESH_PASSWORD,
    CONF_OWNER_ID,
    DEFAULT_IDLE_DISCONNECT,
    DEFAULT_TRANSITION,
    DOMAIN,
)


class AwoxConnectZConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up all AwoX Connect.Z lights from one account login."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
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


class AwoxConnectZOptionsFlow(OptionsFlow):
    """Runtime tuning options shared by all lamps in the account."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            result = self.async_create_entry(
                title="", data=user_input
            )
            await self.hass.config_entries.async_reload(
                self.config_entry.entry_id
            )
            return result

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
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema
        )
