from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers import aiohttp_client
import re
import logging
from datetime import timedelta, date, datetime
from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_REFRESH_RATE
import json
import async_timeout
import time
from yarl import URL
import asyncio

_LOGGER = logging.getLogger(__name__)

class UtilitaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Utilita data with session persistence."""

    def __init__(self, hass, entry):
        """Initialize."""
        self.hass = hass
        self.email = entry.data[CONF_EMAIL]
        self.password = entry.data[CONF_PASSWORD]
        self.cookies = entry.data.get("cookies", {})
        self.cache_session = entry.data.get("cache_session", "")
        self.login_retry_after = None
        self.session_validated = False
        self.last_session_check = 0
        self.retry_attempts = 0
        super().__init__(
            hass,
            _LOGGER,
            name=f"Utilita_{entry.entry_id}",
            update_interval=timedelta(seconds=entry.options.get(CONF_REFRESH_RATE, entry.data.get(CONF_REFRESH_RATE, 7200))),
        )
        self._ping_task = self.hass.async_create_task(self._async_keep_alive())

    async def _async_login(self, session):
        """Perform login to refresh session."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        async with session.get("https://my.utilita.co.uk/login", timeout=10, headers=headers, allow_redirects=True) as response:
            if response.status != 200:
                raise UpdateFailed(f"Failed to load login page: HTTP {response.status}, URL: {response.url}")
            login_page = await response.text()
            _LOGGER.debug(f"Login page URL: {response.url}, Headers: {response.headers}")
            match = re.search(r'<input type="hidden" name="_token" value="([^"]+)"', login_page)
            if not match:
                match = re.search(r'<meta name="csrf-token" content="([^"]+)"', login_page, re.IGNORECASE)
            if not match:
                snippet = login_page[:1000]
                _LOGGER.error(f"CSRF token not found. Login page snippet: {snippet}")
                raise UpdateFailed("CSRF token not found")
            token = match.group(1)
            _LOGGER.debug(f"CSRF token found: {token[:10]}...")
            xsrf_token = None
            for cookie in response.cookies.values():
                if cookie.key == "XSRF-TOKEN" and "my.utilita.co.uk" in cookie["domain"]:
                    xsrf_token = cookie.value
                    _LOGGER.debug(f"XSRF token found: {xsrf_token[:10]}...")
                    break
            cookies = [f"{cookie.key}={cookie.value}" for cookie in response.cookies.values()]
            _LOGGER.debug(f"Cookies after login page: {cookies}")

        async with session.post(
            "https://my.utilita.co.uk/login",
            data={"_token": token, "email": self.email, "password": self.password, "remember": "on"},
            timeout=10,
            headers={
                "User-Agent": headers["User-Agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
                "Referer": "https://my.utilita.co.uk/login",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Content-Type": "application/x-www-form-urlencoded",
            }
        ) as response:
            response_text = await response.text()
            if response.status != 200:
                raise UpdateFailed(f"Login failed: HTTP {response.status}, URL: {response.url}")
            cookies = [f"{cookie.key}={cookie.value}" for cookie in response.cookies.values()]
            _LOGGER.debug(f"Cookies after login: {cookies}")
            if "otp-login" in str(response.url) or "otp-login" in response_text or "#OTP-form" in str(response.url):
                self.retry_attempts += 1
                # Exponential backoff: 5min, 15min, 30min, 60min
                backoff = min(300 * 2 ** (self.retry_attempts - 1), 3600)  # Cap at 1 hour
                self.login_retry_after = time.time() + backoff
                expiry = datetime.fromtimestamp(self.login_retry_after).strftime('%Y-%m-%d %H:%M:%S')
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Utilita Login Requires OTP",
                        "message": f"OTP required for {self.email}. Please re-authenticate the Utilita integration in Home Assistant. Delaying retries until {expiry}.",
                    },
                )
                _LOGGER.warning(f"OTP required during login. Delaying retries until {expiry}.")
                raise UpdateFailed("OTP required, but background tasks cannot prompt for OTP")
            # Reset retry attempts on successful login
            self.retry_attempts = 0
            # Update cookies and cache session
            self.cookies = {cookie.key: cookie.value for cookie in response.cookies.values() if "my.utilita.co.uk" in cookie["domain"]}
            self.cache_session = response.headers.get("Cache-Session", self.cache_session)
            _LOGGER.debug(f"Updated session cookies: {self.cookies}, Cache-Session: {self.cache_session}")
            self.session_validated = True
            self.last_session_check = time.time()
            # Save updated cookies and cache session to config entry
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={
                    **self.config_entry.data,
                    "cookies": self.cookies,
                    "cache_session": self.cache_session,
                }
            )

    async def _async_keep_alive(self):
        """Periodically ping /json/scroller to keep session alive."""
        while True:
            try:
                if not self.session_validated or not self.cookies:
                    _LOGGER.debug("Skipping keep-alive ping: session not validated or no cookies")
                    await asyncio.sleep(300)  # Wait 5 minutes
                    continue
                session = aiohttp_client.async_get_clientsession(self.hass)
                session.cookie_jar.update_cookies(self.cookies, URL("https://my.utilita.co.uk"))
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Referer": f"https://my.utilita.co.uk/energy?cache={self.cache_session}",
                    "X-App-Name": "my.utilita",
                    "X-App-Version": "production",
                    "X-Requested-With": "XMLHttpRequest",
                    "Cache-Session": self.cache_session,
                    "X-CSRF-TOKEN": self.cookies.get("csrf_token", ""),
                    "X-XSRF-TOKEN": self.cookies.get("XSRF-TOKEN", ""),
                    "Sec-Ch-Ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                }
                async with session.get("https://my.utilita.co.uk/json/scroller", timeout=10, headers=headers) as response:
                    response_text = await response.text()
                    _LOGGER.debug(f"Keep-alive ping response: HTTP {response.status}, URL: {response.url}, Response: {response_text[:1000]}")
                    if response.status == 401:
                        _LOGGER.debug("Session invalid during keep-alive, marking for re-authentication")
                        self.session_validated = False
                    elif response.status != 200:
                        _LOGGER.warning(f"Keep-alive ping failed: HTTP {response.status}")
                    self.cache_session = response.headers.get("Cache-Session", self.cache_session)
            except Exception as err:
                _LOGGER.error(f"Error during keep-alive ping: {err}")
            await asyncio.sleep(300)  # Ping every 5 minutes

    async def _async_validate_session(self, session):
        """Validate session by checking a lightweight endpoint."""
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
            return response.status == 200

    async def _async_update_data(self):
        """Fetch data from Utilita."""
        _LOGGER.debug(f"Starting data update for entry {self.config_entry.entry_id} at {date.today()} {self.update_interval}")
        try:
            async with async_timeout.timeout(10):
                if self.login_retry_after and time.time() < self.login_retry_after:
                    expiry = datetime.fromtimestamp(self.login_retry_after).strftime('%Y-%m-%d %H:%M:%S')
                    _LOGGER.warning(f"Login retry delay in effect until {expiry}. Skipping login.")
                    raise UpdateFailed(f"Login retry delay in effect until {expiry}. Please try again later.")

                session = aiohttp_client.async_get_clientsession(self.hass)
                if self.cookies:
                    session.cookie_jar.update_cookies(self.cookies, URL("https://my.utilita.co.uk"))
                    _LOGGER.debug(f"Loaded cookies for session: {self.cookies}, Cache-Session: {self.cache_session}")
                    if not self.session_validated or (time.time() - self.last_session_check) > 3600:
                        self.session_validated = await self._async_validate_session(session)
                        self.last_session_check = time.time()
                        _LOGGER.debug(f"Session validation result: {self.session_validated}")
                        if not self.session_validated:
                            _LOGGER.debug("Session invalid, attempting to log in again.")
                            await self._async_login(session)
                else:
                    _LOGGER.debug("No stored cookies, proceeding with login.")
                    await self._async_login(session)

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                    "Accept": "application/json",
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

                # Fetch balance data
                async with session.get("https://my.utilita.co.uk/json/balance", timeout=10, headers=headers) as response:
                    if response.status == 401:
                        _LOGGER.debug("Session invalid, attempting to log in again.")
                        self.session_validated = False
                        await self._async_login(session)
                        async with session.get("https://my.utilita.co.uk/json/balance", timeout=10, headers=headers) as retry_response:
                            if retry_response.status != 200:
                                raise UpdateFailed(f"Failed to fetch balance after login: HTTP {retry_response.status}")
                            balance = await retry_response.json()
                            _LOGGER.debug(f"Balance API response: {json.dumps(balance, indent=2)}")
                    elif response.status != 200:
                        raise UpdateFailed(f"Failed to fetch balance: HTTP grs{response.status}")
                    else:
                        balance = await response.json()
                        _LOGGER.debug(f"Balance API response: {json.dumps(balance, indent=2)}")

                # Fetch usage data
                async with session.get(f"https://my.utilita.co.uk/json/usage?end_date={date.today()}", timeout=10, headers=headers) as response:
                    if response.status != 200:
                        raise UpdateFailed(f"Failed to fetch usage: HTTP {response.status}")
                    usage = await response.json()

                # Fetch user data
                async with session.get("https://my.utilita.co.uk/user-data", timeout=10, headers=headers) as response:
                    if response.status != 200:
                        raise UpdateFailed(f"Failed to fetch user data: HTTP {response.status}")
                    user_data = await response.json()

                # Fetch payments data
                async with session.get("https://my.utilita.co.uk/json/payments?page=1&per_page=50", timeout=10, headers=headers) as response:
                    if response.status != 200:
                        raise UpdateFailed(f"Failed to fetch payments: HTTP {response.status}")
                    payments = await response.json()

                # Fetch unread messages count
                async with session.get("https://my.utilita.co.uk/messages-unread", timeout=10, headers=headers) as response:
                    if response.status != 200:
                        raise UpdateFailed(f"Failed to fetch unread messages: HTTP {response.status}")
                    messages_unread = await response.text()
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
        except Exception as err:
            _LOGGER.error(f"Error fetching data for entry {self.config_entry.entry_id}: {err}")
            raise UpdateFailed(f"Error fetching data: {err}")

    async def async_unload(self):
        """Unload the coordinator and cancel the ping task."""
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        await super().async_unload()

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Utilita from a config entry."""
    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]
    refresh_rate = entry.options.get(CONF_REFRESH_RATE, entry.data.get(CONF_REFRESH_RATE, 7200))
    _LOGGER.debug(f"Setting up entry {entry.entry_id} with refresh_rate: {refresh_rate} seconds")

    coordinator = UtilitaDataUpdateCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator, "config": entry}
    await coordinator.async_config_entry_first_refresh()
    if not coordinator.last_update_success:
        _LOGGER.error(f"Initial refresh failed for entry {entry.entry_id}")
        return False

    # Explicitly create the device (fixes the missing device issue in HA 2025.x)
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers.device_registry import DeviceEntryType
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"utilita_{entry.entry_id}")},
        name="Utilita Energy",
        manufacturer="Utilita",
        model="Energy Monitor",
        entry_type=DeviceEntryType.SERVICE,
    )

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
            await coordinator.async_request_refresh()
    return True