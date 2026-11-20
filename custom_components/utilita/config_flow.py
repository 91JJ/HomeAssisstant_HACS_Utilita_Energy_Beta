"""Config flow for the Utilita Energy integration."""
import logging

import voluptuous as vol
from aiohttp import CookieJar
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client
from yarl import URL

from .const import CONF_REFRESH_RATE, DOMAIN

_LOGGER = logging.getLogger(__name__)

_UTILITA_BASE = "https://my.utilita.co.uk"
_CONF_COOKIES_RAW = "cookies_raw"


def _parse_cookie_string(raw: str) -> dict:
    """Parse a semicolon-separated Cookie header string into a name->value dict."""
    result = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            result[name.strip()] = value.strip()
    return result


class UtilitaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Utilita."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        """Initialise the config flow."""
        self._cookies: dict = {}
        self._cache_session: str = ""
        self._reauth_entry = None

    @staticmethod
    def _user_schema(default_refresh_rate: int = 7200) -> vol.Schema:
        """Return the schema for the user step."""
        return vol.Schema(
            {
                vol.Required(_CONF_COOKIES_RAW): str,
                vol.Required(CONF_REFRESH_RATE, default=default_refresh_rate): vol.All(
                    vol.Coerce(int), vol.Range(min=300)
                ),
            }
        )

    async def _async_validate_cookies(self, cookies: dict):
        """
        Validate cookies by calling /user-data.

        Returns (customer_id, cache_session) on success,
        or (None, "") if the session is rejected.
        Raises on network/unexpected errors.
        """
        # Utilita multi-property session isolation: validate without touching any other account.
        session = aiohttp_client.async_create_clientsession(
            self.hass, cookie_jar=CookieJar()
        )
        session.cookie_jar.update_cookies(cookies, URL(_UTILITA_BASE))
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-GB,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "X-App-Name": "my.utilita",
            "X-App-Version": "production",
        }
        try:
            async with session.get(
                f"{_UTILITA_BASE}/user-data",
                timeout=15,
                headers=headers,
                allow_redirects=False,
            ) as resp:
                # A redirect (to /login) or 401/403 means the cookies are invalid/expired.
                if resp.status in (301, 302, 303, 307, 308, 401, 403):
                    return None, ""
                if resp.status != 200:
                    raise Exception(f"Unexpected HTTP {resp.status} from /user-data")
                data = await resp.json(content_type=None)
                customer_id = data.get("customer_id")
                if not customer_id:
                    return None, ""
                return str(customer_id), resp.headers.get("Cache-Session", "")
        finally:
            session.detach()

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial setup step."""
        errors = {}

        if user_input is not None:
            cookies_raw = user_input[_CONF_COOKIES_RAW].strip()
            self._cookies = _parse_cookie_string(cookies_raw)
            refresh_rate = user_input[CONF_REFRESH_RATE]

            try:
                customer_id, cache_session = await self._async_validate_cookies(self._cookies)
                if customer_id is None:
                    errors["base"] = "invalid_cookies"
                else:
                    await self.async_set_unique_id(customer_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Utilita Energy",
                        data={
                            CONF_REFRESH_RATE: refresh_rate,
                            "cookies": self._cookies,
                            "cache_session": cache_session,
                        },
                    )
            except Exception:
                _LOGGER.exception("Unexpected error validating Utilita cookies")
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return UtilitaOptionsFlow(config_entry)

    async def async_step_reauth(self, entry_data) -> FlowResult:
        """Initiate re-authentication when the stored session has expired."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None) -> FlowResult:
        """Re-authentication form — paste fresh cookies to restore the session."""
        errors = {}

        if user_input is not None:
            cookies_raw = user_input[_CONF_COOKIES_RAW].strip()
            self._cookies = _parse_cookie_string(cookies_raw)

            try:
                customer_id, cache_session = await self._async_validate_cookies(self._cookies)
                if customer_id is None:
                    errors["base"] = "invalid_cookies"
                elif (
                    self._reauth_entry.unique_id
                    and customer_id != self._reauth_entry.unique_id
                ):
                    # Prevent cross-property account reassignment.
                    _LOGGER.warning("Utilita reauth cookies belong to a different account")
                    errors["base"] = "invalid_cookies"
                else:
                    updated_data = {
                        **self._reauth_entry.data,
                        "cookies": self._cookies,
                        "cache_session": cache_session,
                    }
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry, data=updated_data
                    )
                    await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
            except Exception:
                _LOGGER.exception("Unexpected error during Utilita re-authentication")
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(_CONF_COOKIES_RAW): str}),
            errors=errors,
        )


class UtilitaOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_REFRESH_RATE,
                        default=self._config_entry.options.get(CONF_REFRESH_RATE, self._config_entry.data.get(CONF_REFRESH_RATE, 7200)),
                    ): vol.All(vol.Coerce(int), vol.Range(min=300)),
                }
            ),
        )
