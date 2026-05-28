import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_time_interval
import re
import logging
from datetime import timedelta, date, datetime
from .const import (
    CONF_REFRESH_RATE,
    DOMAIN,
)
import json
import async_timeout
import time
from yarl import URL

_LOGGER = logging.getLogger(__name__)
UTILITA_BASE_URL = URL("https://my.utilita.co.uk")
SESSION_KEEPALIVE_INTERVAL = timedelta(minutes=30)


def _log_session_state(cookies, cache_session):
    """Return a redacted description of the current session state."""
    return {
        "cookie_count": len(cookies),
        "cookie_names": sorted(cookies.keys()) if isinstance(cookies, dict) else [],
        "has_cache_session": bool(cache_session),
    }

class UtilitaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Utilita data with session persistence."""

    def __init__(self, hass, entry):
        """Initialize."""
        self.hass = hass
        self.config_entry = entry
        self.cookies = entry.data.get("cookies", {})
        self.cache_session = entry.data.get("cache_session", "")
        self.login_retry_after = None
        self.session_validated = False
        self.last_session_check = 0
        self.retry_attempts = 0
        self._keepalive_unsub = None
        self._keepalive_in_progress = False
        super().__init__(
            hass,
            _LOGGER,
            name=f"Utilita_{entry.entry_id}",
            update_interval=timedelta(seconds=entry.options.get(CONF_REFRESH_RATE, entry.data.get(CONF_REFRESH_RATE, 7200))),
        )

    @property
    def device_info(self):
        """Return device registry info for this integration."""
        from homeassistant.helpers.device_registry import DeviceInfo, DeviceEntryType
        return DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, self.config_entry.entry_id)},
            name="Utilita Energy",
            manufacturer="Utilita",
            model="Energy Monitor",
        )

    def _async_apply_cookies(self, session):
        """Apply stored session cookies to the shared HTTP session."""
        if self.cookies:
            session.cookie_jar.update_cookies(self.cookies, UTILITA_BASE_URL)

    def _get_session_cookies(self, session):
        """Return cookies currently stored for the Utilita domain."""
        return {
            name: morsel.value
            for name, morsel in session.cookie_jar.filter_cookies(UTILITA_BASE_URL).items()
        }

    def _async_store_session(self, session, cache_session=None):
        """Persist the latest session cookies and cache token."""
        cookies = self._get_session_cookies(session)
        if cookies:
            self.cookies = cookies
        if cache_session:
            self.cache_session = cache_session

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                "cookies": self.cookies,
                "cache_session": self.cache_session,
            },
        )

    def _should_enable_keepalive(self):
        """Return whether a lightweight keepalive is needed between refreshes."""
        return self.update_interval is not None and self.update_interval > timedelta(minutes=45)

    @callback
    def _async_keepalive_listener(self, _now):
        """Schedule a lightweight keepalive check."""
        if self._keepalive_in_progress or not self.cookies:
            return
        self.hass.async_create_task(self._async_keep_session_alive())

    @callback
    def _async_configure_keepalive(self):
        """Start or stop keepalive scheduling based on refresh interval."""
        if self._keepalive_unsub is not None:
            self._keepalive_unsub()
            self._keepalive_unsub = None

        if self._should_enable_keepalive():
            self._keepalive_unsub = async_track_time_interval(
                self.hass,
                self._async_keepalive_listener,
                SESSION_KEEPALIVE_INTERVAL,
            )

    async def _async_keep_session_alive(self):
        """Touch a lightweight endpoint to avoid idle-session expiry."""
        if self._keepalive_in_progress or not self.cookies:
            return

        if self.login_retry_after and time.time() < self.login_retry_after:
            return

        self._keepalive_in_progress = True
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            self._async_apply_cookies(session)
            headers = self._build_api_headers()
            async with async_timeout.timeout(15):
                async with session.get(
                    "https://my.utilita.co.uk/user-data",
                    timeout=10,
                    headers=headers,
                ) as response:
                    if response.status == 200:
                        self.session_validated = True
                        self.last_session_check = time.time()
                        self._async_store_session(
                            session,
                            response.headers.get("Cache-Session", self.cache_session),
                        )
                        _LOGGER.debug("Utilita keepalive succeeded for entry %s", self.config_entry.entry_id)
                    else:
                        self.session_validated = False
                        _LOGGER.debug(
                            "Utilita keepalive returned HTTP %s for entry %s",
                            response.status,
                            self.config_entry.entry_id,
                        )
        except Exception as err:
            _LOGGER.debug(
                "Utilita keepalive skipped after %s for entry %s",
                type(err).__name__,
                self.config_entry.entry_id,
            )
        finally:
            self._keepalive_in_progress = False

    def _build_api_headers(self, accept="application/json"):
        """Build standard authenticated headers for Utilita API requests."""
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "Accept": accept,
            "Accept-Language": "en-GB,en;q=0.9",
            "Referer": f"https://my.utilita.co.uk/energy?cache={self.cache_session}",
            "Connection": "keep-alive",
            "Cache-Session": self.cache_session,
            "X-App-Name": "my.utilita",
            "X-App-Version": "production",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-TOKEN": self.cookies.get("csrf_token", ""),
            "X-XSRF-TOKEN": self.cookies.get("XSRF-TOKEN", ""),
        }

    async def _async_ensure_authenticated(self, session):
        """Ensure the stored session is valid; trigger re-authentication if not."""
        if self.login_retry_after and time.time() < self.login_retry_after:
            expiry = datetime.fromtimestamp(self.login_retry_after).strftime('%Y-%m-%d %H:%M:%S')
            raise UpdateFailed(f"Re-authentication required. Retries paused until {expiry}.")

        self._async_apply_cookies(session)

        if not self.cookies:
            _LOGGER.debug("No session cookies stored.")
            await self._async_trigger_session_reauth()
            raise UpdateFailed("No session cookies stored. Re-authentication required.")

        _LOGGER.debug("Loaded stored session state: %s", _log_session_state(self.cookies, self.cache_session))
        if not self.session_validated or (time.time() - self.last_session_check) > 3600:
            self.session_validated = await self._async_validate_session(session)
            self.last_session_check = time.time()
            _LOGGER.debug("Session validation result: %s", self.session_validated)
            if not self.session_validated:
                await self._async_trigger_session_reauth()
                raise UpdateFailed("Session cookies expired. Re-authentication required.")

    async def _async_request(self, session, url, error_message, response_type="json"):
        """Perform an authenticated GET request, retrying once after re-login on 401."""
        headers = self._build_api_headers(
            accept="application/json" if response_type == "json" else "text/plain, */*"
        )

        async with session.get(url, timeout=10, headers=headers) as response:
            if response.status == 401:
                _LOGGER.debug("Session expired (401) during request to %s. Triggering re-authentication.", url)
                await self._async_trigger_session_reauth()
                raise UpdateFailed(f"{error_message}: session expired (HTTP 401). Re-authentication required.")

            if response.status != 200:
                raise UpdateFailed(f"{error_message}: HTTP {response.status}")

            self._async_store_session(
                session,
                response.headers.get("Cache-Session", self.cache_session),
            )
            return await response.json() if response_type == "json" else await response.text()

    async def _async_trigger_session_reauth(self):
        """Notify the user that session cookies have expired and trigger re-authentication."""
        self.session_validated = False
        self.retry_attempts += 1
        backoff = min(300 * 2 ** (self.retry_attempts - 1), 3600)
        self.login_retry_after = time.time() + backoff
        expiry = datetime.fromtimestamp(self.login_retry_after).strftime('%Y-%m-%d %H:%M:%S')
        _LOGGER.warning(
            "Utilita session cookies expired for entry %s. Re-authentication required. Retries paused until %s.",
            self.config_entry.entry_id,
            expiry,
        )
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": f"utilita_reauth_{self.config_entry.entry_id}",
                "title": "Utilita - Session Expired",
                "message": (
                    "Your Utilita session cookies have expired and data can no longer be fetched.\n\n"
                    "To restore the connection:\n"
                    "1. Open your browser and log into **https://my.utilita.co.uk**\n"
                    "2. Press **F12** to open DevTools → **Network** tab → reload the page → "
                    "click any request to `my.utilita.co.uk` → **Request Headers** → "
                    "copy the entire **Cookie** header value\n"
                    "3. Go to **Settings → Devices & Services → Utilita → Re-authenticate** "
                    "and paste the cookie string\n\n"
                    f"Automatic retries paused until {expiry}."
                ),
            },
        )
        self.config_entry.async_start_reauth(self.hass)


    async def _async_validate_session(self, session):
        """Validate session by checking a lightweight endpoint."""
        self._async_apply_cookies(session)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-GB,en;q=0.9",
            "Referer": "https://my.utilita.co.uk/energy",
            "Connection": "keep-alive",
            "Cache-Session": self.cache_session,
        }
        async with session.get("https://my.utilita.co.uk/user-data", timeout=10, headers=headers) as response:
            _LOGGER.debug(f"Session validation response: HTTP {response.status}, URL: {response.url}")
            if response.status == 200:
                self._async_store_session(
                    session,
                    response.headers.get("Cache-Session", self.cache_session),
                )
            return response.status == 200

    async def _async_update_data(self):
        """Fetch data from Utilita."""
        _LOGGER.debug(f"Starting data update for entry {self.config_entry.entry_id} at {date.today()} {self.update_interval}")
        try:
            async with async_timeout.timeout(30):
                session = aiohttp_client.async_get_clientsession(self.hass)
                await self._async_ensure_authenticated(session)

                balance = await self._async_request(
                    session,
                    "https://my.utilita.co.uk/json/balance",
                    "Failed to fetch balance",
                )
                _LOGGER.debug(f"Balance API response: {json.dumps(balance, indent=2)}")

                usage = await self._async_request(
                    session,
                    f"https://my.utilita.co.uk/json/usage?end_date={date.today()}",
                    "Failed to fetch usage",
                )

                user_data = await self._async_request(
                    session,
                    "https://my.utilita.co.uk/user-data",
                    "Failed to fetch user data",
                )

                payments = await self._async_request(
                    session,
                    "https://my.utilita.co.uk/json/payments?page=1&per_page=50",
                    "Failed to fetch payments",
                )

                messages_unread = await self._async_request(
                    session,
                    "https://my.utilita.co.uk/messages-unread",
                    "Failed to fetch unread messages",
                    response_type="text",
                )
                try:
                    messages_unread = int(messages_unread.strip())
                except ValueError:
                    _LOGGER.error(f"Invalid unread messages response: {messages_unread}")
                    messages_unread = 0

                _LOGGER.debug(f"Data update completed successfully for entry {self.config_entry.entry_id}")
                self.login_retry_after = None  # Reset retry delay on success
                self.session_validated = True
                self.last_session_check = time.time()
                self.retry_attempts = 0
                return {
                    "balance": balance,
                    "usage": usage,
                    "user_data": user_data,
                    "payments": payments,
                    "messages_unread": messages_unread
                }
        except TimeoutError as err:
            _LOGGER.warning(
                "Timed out fetching data for entry %s after 30 seconds",
                self.config_entry.entry_id,
            )
            raise UpdateFailed("Timed out fetching data from Utilita") from err
        except Exception as err:
            _LOGGER.exception(
                "Error fetching data for entry %s (%s)",
                self.config_entry.entry_id,
                type(err).__name__,
            )
            raise UpdateFailed(f"Error fetching data: {err}")

    async def async_unload(self):
        """Unload the coordinator."""
        if self._keepalive_unsub is not None:
            self._keepalive_unsub()
            self._keepalive_unsub = None
        await super().async_unload()

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Utilita from a config entry."""
    refresh_rate = entry.options.get(CONF_REFRESH_RATE, entry.data.get(CONF_REFRESH_RATE, 7200))
    _LOGGER.debug("Setting up entry %s with refresh_rate: %s seconds", entry.entry_id, refresh_rate)

    coordinator = UtilitaDataUpdateCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator, "config": entry}
    await coordinator.async_config_entry_first_refresh()
    if not coordinator.last_update_success:
        _LOGGER.error(f"Initial refresh failed for entry {entry.entry_id}")
        return False
    coordinator._async_configure_keepalive()
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if await hass.config_entries.async_unload_platforms(entry, ["sensor"]):
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        await coordinator.async_unload()
        hass.data[DOMAIN].pop(entry.entry_id)
        return True
    return False

async def async_options_updated(hass, entry):
    """Handle options update."""
    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        if coordinator:
            new_refresh_rate = entry.options.get(CONF_REFRESH_RATE, 7200)
            coordinator.update_interval = timedelta(seconds=new_refresh_rate)
            coordinator._async_configure_keepalive()
            await coordinator.async_request_refresh()
    return True
