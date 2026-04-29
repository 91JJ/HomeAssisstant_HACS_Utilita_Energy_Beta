import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import re
import logging
import urllib.request
import urllib.parse
import json
from http.cookiejar import CookieJar
from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_REFRESH_RATE

_LOGGER = logging.getLogger(__name__)

class UtilitaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Utilita."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        """Initialize the config flow."""
        self._email = None
        self._password = None
        self._refresh_rate = None
        self._token = None
        self._xsrf_token = None
        self._cookie_jar = CookieJar()
        self._mfa_method = None          # "sms" or "email"
        self._mfa_target_display = None  # what to show in the OTP step ("admin@homeassistant.com or "your registered mobile number")

    @staticmethod
    def _user_schema(default_refresh_rate=7200):
        """Return the schema for the user step."""
        return vol.Schema(
            {
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_REFRESH_RATE, default=default_refresh_rate): vol.All(
                    vol.Coerce(int), vol.Range(min=300)
                ),
            }
        )

    async def _abort_if_email_configured(self) -> None:
        """Abort if the account email is already configured."""
        normalized_email = self._email.lower().strip()
        await self.async_set_unique_id(normalized_email)
        self._abort_if_unique_id_configured()

    def _make_request(self, url, method="GET", data=None, headers=None):
        """Execute a urllib.request in a thread pool to avoid blocking."""
        if headers is None:
            headers = {}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._cookie_jar))
        try:
            with opener.open(request, timeout=10) as response:
                body = response.read().decode("utf-8", errors="ignore")
                return response.status, body, response.url, dict(response.headers)
        except Exception as err:
            _LOGGER.error(f"Request failed for {url}: {err}")
            raise

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._password = user_input[CONF_PASSWORD]
            self._refresh_rate = user_input[CONF_REFRESH_RATE]

            await self._abort_if_email_configured()

            try:
                # GET login page
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                }
                status, login_page, url, response_headers = await self.hass.async_add_executor_job(
                    self._make_request, "https://my.utilita.co.uk/login", "GET", None, headers
                )
                if status != 200:
                    errors["base"] = "cannot_connect"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=self._user_schema(self._refresh_rate),
                        errors=errors,
                    )

                match = re.search(r'<input type="hidden" name="_token" value="([^"]+)"', login_page)
                if not match:
                    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', login_page, re.IGNORECASE)
                if not match:
                    _LOGGER.error("CSRF token not found in login page")
                    errors["base"] = "cannot_connect"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=self._user_schema(self._refresh_rate),
                        errors=errors,
                    )
                self._token = match.group(1)

                # Extract XSRF-TOKEN from cookies
                for cookie in self._cookie_jar:
                    if cookie.name == "XSRF-TOKEN" and "my.utilita.co.uk" in cookie.domain:
                        self._xsrf_token = cookie.value
                        break

                # Perform login
                login_data = urllib.parse.urlencode({
                    "_token": self._token,
                    "email": self._email,
                    "password": self._password,
                    "remember": "on",
                }).encode("utf-8")

                login_headers = {
                    "User-Agent": headers["User-Agent"],
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Referer": "https://my.utilita.co.uk/login",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Content-Type": "application/x-www-form-urlencoded",
                }

                status, response_text, url, response_headers = await self.hass.async_add_executor_job(
                    self._make_request, "https://my.utilita.co.uk/login", "POST", login_data, login_headers
                )

                if status != 200:
                    errors["base"] = "invalid_auth"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=self._user_schema(self._refresh_rate),
                        errors=errors,
                    )

                # Check if MFA/OTP is required
                if "otp-login" in str(url) or "otp-login" in response_text or "#OTP-form" in str(url):
                    return await self.async_step_mfa_method()

                # Successful login without MFA
                return self.async_create_entry(
                    title="Utilita Energy",
                    data={
                        CONF_EMAIL: self._email,
                        CONF_PASSWORD: self._password,
                        CONF_REFRESH_RATE: self._refresh_rate,
                        "cookies": {cookie.name: cookie.value for cookie in self._cookie_jar if "my.utilita.co.uk" in cookie.domain},
                    },
                )

            except Exception as err:
                _LOGGER.exception("Unexpected exception in async_step_user")
                errors["base"] = "unknown"
                return self.async_show_form(
                    step_id="user",
                    data_schema=self._user_schema(self._refresh_rate),
                    errors=errors,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(),
            errors=errors,
        )

    async def async_step_mfa_method(self, user_input=None):
        """Choose SMS or Email for the OTP."""
        errors = {}

        if user_input is not None:
            self._mfa_method = user_input["mfa_method"]

            payload = {"method": self._mfa_method.upper()}
            if self._mfa_method == "email":
                payload["email"] = self._email
                self._mfa_target_display = self._email
            else:
                self._mfa_target_display = "your registered mobile number"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json;charset=UTF-8",
                "Origin": "https://my.utilita.co.uk",
                "Referer": "https://my.utilita.co.uk/login",
                "x-app-name": "my.utilita",
                "x-app-version": "production",
                "x-csrf-token": self._token,
                "x-requested-with": "XMLHttpRequest",
                "x-xsrf-token": self._xsrf_token or "",
            }

            try:
                status, response_text, _, _ = await self.hass.async_add_executor_job(
                    self._make_request,
                    "https://my.utilita.co.uk/login/otp/resend",
                    "POST",
                    json.dumps(payload).encode("utf-8"),
                    headers,
                )

                if status == 200 and json.loads(response_text).get("success", False):
                    return await self.async_step_otp()
                else:
                    errors["base"] = "otp_failed"
            except Exception:
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="mfa_method",
            data_schema=vol.Schema({
                vol.Required("mfa_method", default="email"): vol.In({
                    "email": f"Email (to {self._email})",
                    "sms": "SMS (to your phone)",
                })
            }),
            errors=errors,
            description_placeholders={
                "message": "Please choose how you would like to receive your security code."
            }
        )

    async def async_step_otp(self, user_input=None):
        """Enter the OTP code."""
        errors = {}

        if user_input is not None:
            otp_code = user_input["otp_code"].strip()

            if len(otp_code) != 6 or not otp_code.isdigit():
                errors["base"] = "invalid_otp"
            else:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json;charset=UTF-8",
                    "Origin": "https://my.utilita.co.uk",
                    "Referer": "https://my.utilita.co.uk/login",
                    "x-app-name": "my.utilita",
                    "x-app-version": "production",
                    "x-csrf-token": self._token,
                    "x-requested-with": "XMLHttpRequest",
                    "x-xsrf-token": self._xsrf_token or "",
                }

                payload = json.dumps({
                    "OTP": otp_code,
                    "email": self._email,
                    "password": self._password
                }).encode("utf-8")

                try:
                    status, response_text, _, _ = await self.hass.async_add_executor_job(
                        self._make_request,
                        "https://my.utilita.co.uk/login/otp",
                        "POST",
                        payload,
                        headers,
                    )

                    if status == 200 and json.loads(response_text).get("success", False):
                        return self.async_create_entry(
                            title="Utilita Energy",
                            data={
                                CONF_EMAIL: self._email,
                                CONF_PASSWORD: self._password,
                                CONF_REFRESH_RATE: self._refresh_rate,
                                "cookies": {cookie.name: cookie.value for cookie in self._cookie_jar if "my.utilita.co.uk" in cookie.domain},
                            },
                        )
                except Exception:
                    pass

                errors["base"] = "invalid_otp"

        return self.async_show_form(
            step_id="otp",
            data_schema=vol.Schema({vol.Required("otp_code"): str}),
            errors=errors,
            description_placeholders={
                "target": self._mfa_target_display or self._email
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        return UtilitaOptionsFlow(config_entry)


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