# Utilita multi-account session resilience
import asyncio
from aiohttp import CookieJar, ClientError

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
KEEPALIVE_RETRY_SECONDS = 120


def _log_session_state(cookies, cache_session):
    """Return a redacted description of the current session state."""
    return {
        "cookie_count": len(cookies),
        "cookie_names": sorted(cookies.keys()) if isinstance(cookies, dict) else [],
        "has_cache_session": bool(cache_session),
    }

class UtilitaAuthenticationError(UpdateFailed):
    """The server rejected the currently supplied authentication credentials."""


class UtilitaAccountIdentityError(UtilitaAuthenticationError):
    """The response belongs to a different account (or has no account identity)."""


class UtilitaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Utilita data with session persistence."""

    def __init__(self, hass, entry):
        """Initialize."""
        self.hass = hass
        # Utilita multi-property session isolation: one cookie jar/session per Utilita property.
        self._session = aiohttp_client.async_create_clientsession(
            hass, cookie_jar=CookieJar()
        )
        self.config_entry = entry
        self.cookies = entry.data.get("cookies", {})
        self.cache_session = entry.data.get("cache_session", "")
        self.login_retry_after = None
        self.session_validated = False
        self.last_session_check = 0
        self.retry_attempts = 0
        self._keepalive_unsub = None
        self._keepalive_in_progress = False
        self._keepalive_retry_task = None
        self._keepalive_failure_count = 0
        self._keepalive_auth_rejections = 0
        self._reauth_requested = False
        self._cookies_seeded = False
        self._unloading = False
        self._api_lock = asyncio.Lock()
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
        user_data = (self.data or {}).get("user_data") or {}
        premises = user_data.get("premises") or []
        address = (
            premises[0].get("addr_full")
            if premises and isinstance(premises[0], dict)
            else None
        )
        device_name = f"Utilita Energy - {address}" if address else self.config_entry.title
        return DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, self.config_entry.entry_id)},
            name=device_name,
            manufacturer="Utilita",
            model="Energy Monitor",
        )

    def _async_apply_cookies(self, session):
        """Seed the private jar ONCE; never resurrect expired/deleted cookies.

        The previous version re-applied the saved cookies on every validation.
        That could restore a server-deleted or already-expired session cookie.
        Subsequent requests must use the private jar as the source of truth.
        """
        if self._cookies_seeded:
            return
        self._cookies_seeded = True
        if self.cookies:
            session.cookie_jar.update_cookies(self.cookies, UTILITA_BASE_URL)

    def _get_session_cookies(self, session):
        """Snapshot only cookies still valid for this Utilita domain."""
        return {
            name: morsel.value
            for name, morsel in session.cookie_jar.filter_cookies(UTILITA_BASE_URL).items()
        }

    def _async_store_session(self, session, cache_session=None):
        """Persist the live jar, including removals; never merge stale cookies.

        Only call after a successful response that has passed account checks.
        aiohttp honours Set-Cookie deletion and expiry while requests run.
        """
        current = self._get_session_cookies(session)
        new_cache = cache_session or self.cache_session
        if current == self.cookies and new_cache == self.cache_session:
            return
        self.cookies = current
        self.cache_session = new_cache
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                "cookies": current,
                "cache_session": new_cache,
            },
        )
        _LOGGER.debug(
            "Utilita [%s] SESSION STORE cookie_count=%d auth_cookie_present=%s",
            self.config_entry.entry_id, len(current), bool(current.get("myutilita_session")),
        )

    def _should_enable_keepalive(self):
        """Return whether a lightweight keepalive is needed between refreshes."""
        return self.update_interval is not None and self.update_interval > timedelta(minutes=45)

    @callback
    def _async_keepalive_listener(self, _now):
        """Kick off a keepalive, unless a recovery attempt is already pending."""
        if (
            self._unloading or self._reauth_requested or self._keepalive_in_progress
            or not self.cookies
            or (self._keepalive_retry_task and not self._keepalive_retry_task.done())
        ):
            return
        self.hass.async_create_task(self._async_keep_session_alive())

    @callback
    def _async_configure_keepalive(self):
        """Start/stop keepalive scheduling based on the chosen polling interval."""
        if self._keepalive_unsub is not None:
            self._keepalive_unsub()
            self._keepalive_unsub = None

        if self._should_enable_keepalive():
            self._keepalive_unsub = async_track_time_interval(
                self.hass, self._async_keepalive_listener,
                SESSION_KEEPALIVE_INTERVAL,
            )
        else:
            self._cancel_keepalive_retry()

    def _cancel_keepalive_retry(self):
        task = self._keepalive_retry_task
        self._keepalive_retry_task = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_keepalive_retry(self, delay):
        """Retry transient failures promptly, without creating task storms."""
        if self._unloading or self._reauth_requested:
            return
        if self._keepalive_retry_task is not None and not self._keepalive_retry_task.done():
            return
        _LOGGER.warning(
            "Utilita [%s] KEEPALIVE RETRY scheduled in %ss",
            self.config_entry.entry_id, delay,
        )
        self._keepalive_retry_task = self.hass.async_create_task(
            self._async_delayed_keepalive_retry(delay),
        )

    async def _async_delayed_keepalive_retry(self, delay):
        try:
            await asyncio.sleep(delay)
            # Clear our own handle before trying: another failure may schedule
            # a fresh retry. Never cancel the task that's currently running.
            self._keepalive_retry_task = None
            if not self._unloading and not self._reauth_requested:
                await self._async_keep_session_alive()
        except asyncio.CancelledError:
            return

    async def _async_keep_session_alive(self):
        """Validate the session; retry outages, confirm 401 before reauth.

        A successful /user-data request proves *current* validity. It does NOT
        prove that the server has extended the login expiry. Cookie metadata is
        logged separately to help identify server-side session expiry.
        """
        if self._unloading or self._reauth_requested or self._keepalive_in_progress or not self.cookies:
            return
        self._keepalive_in_progress = True
        try:
            # Do not let keepalive overlap the normal account refresh.
            async with self._api_lock:
                async with async_timeout.timeout(15):
                    await self._async_validate_session(self._session)
            self.session_validated = True
            self.last_session_check = time.time()
            self._keepalive_failure_count = 0
            self._keepalive_auth_rejections = 0
            self._cancel_keepalive_retry()
            _LOGGER.debug(
                "Utilita [%s] KEEPALIVE SUCCESS",
                self.config_entry.entry_id,
            )
        except UtilitaAccountIdentityError:
            _LOGGER.error(
                "Utilita [%s] KEEPALIVE identity mismatch: refusing cross-account data",
                self.config_entry.entry_id,
            )
            self.session_validated = False
            await self._async_trigger_session_reauth()
        except UtilitaAuthenticationError:
            self.session_validated = False
            self._keepalive_auth_rejections += 1
            _LOGGER.warning(
                "Utilita [%s] KEEPALIVE auth rejection %s/2",
                self.config_entry.entry_id, self._keepalive_auth_rejections,
            )
            if self._keepalive_auth_rejections >= 2:
                await self._async_trigger_session_reauth()
            else:
                self._schedule_keepalive_retry(KEEPALIVE_RETRY_SECONDS)
        except (TimeoutError, ClientError, UpdateFailed) as err:
            self._keepalive_failure_count += 1
            delay = min(
                KEEPALIVE_RETRY_SECONDS * 2 ** min(self._keepalive_failure_count - 1, 2),
                600,
            )
            _LOGGER.warning(
                "Utilita [%s] KEEPALIVE transient failure type=%s attempt=%s",
                self.config_entry.entry_id, type(err).__name__, self._keepalive_failure_count,
            )
            self._schedule_keepalive_retry(delay)
        except Exception:
            _LOGGER.exception(
                "Utilita [%s] KEEPALIVE unexpected failure", self.config_entry.entry_id,
            )
            self._schedule_keepalive_retry(KEEPALIVE_RETRY_SECONDS)
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
        """Validate once per hour; confirm rejection before opening reauth."""
        if self._reauth_requested:
            raise UpdateFailed("Re-authentication already requested for this account.")
        if self.login_retry_after and time.time() < self.login_retry_after:
            raise UpdateFailed("Re-authentication required; retry is paused.")

        self._async_apply_cookies(session)
        if not self.cookies:
            await self._async_trigger_session_reauth()
            raise UpdateFailed("No saved session cookies; re-authentication required.")

        _LOGGER.debug(
            "Utilita [%s] SESSION STATE %s",
            self.config_entry.entry_id, _log_session_state(self.cookies, self.cache_session),
        )
        if not self.session_validated or time.time() - self.last_session_check > 3600:
            try:
                await self._async_validate_session(session)
            except UtilitaAccountIdentityError:
                await self._async_trigger_session_reauth()
                raise
            except UtilitaAuthenticationError:
                # Do not declare expiry on a lone 401; confirm on /user-data.
                _LOGGER.warning(
                    "Utilita [%s] SESSION RECHECK after auth rejection",
                    self.config_entry.entry_id,
                )
                await asyncio.sleep(2)
                try:
                    await self._async_validate_session(session)
                except UtilitaAuthenticationError:
                    await self._async_trigger_session_reauth()
                    raise UpdateFailed(
                        "Session rejected twice by Utilita; re-authentication required."
                    ) from None
            # HTTP 5xx, malformed JSON, and timeouts propagate as transient
            # failures. They never start a re-authentication flow.
            self.session_validated = True
            self.last_session_check = time.time()
            self._keepalive_auth_rejections = 0
            _LOGGER.debug(
                "Utilita [%s] SESSION VALIDATION accepted", self.config_entry.entry_id,
            )

    async def _async_request(self, session, url, error_message, response_type="json", timeout=10):
        """Fetch one API endpoint and log its duration without exposing account data."""
        endpoint = URL(url).path  # Deliberately omit query strings and credentials.
        entry_id = self.config_entry.entry_id
        started = time.monotonic()
        headers = self._build_api_headers(
            accept="application/json" if response_type == "json" else "text/plain, */*"
        )
        _LOGGER.debug("Utilita [%s] API START %s (request timeout %ss)", entry_id, endpoint, timeout)
        try:
            async with session.get(
                url, timeout=timeout, headers=headers, allow_redirects=False,
            ) as response:
                status = response.status
                elapsed = time.monotonic() - started
                _LOGGER.debug(
                    "Utilita [%s] API RESPONSE %s HTTP %s after %.2fs",
                    entry_id, endpoint, status, elapsed,
                )
                if status in (301, 302, 303, 307, 308, 401, 403):
                    _LOGGER.warning(
                        "Utilita [%s] API AUTH REJECTED %s HTTP %s after %.2fs",
                        entry_id, endpoint, status, elapsed,
                    )
                    # Some endpoints can fail independently of the login.
                    # Verify account identity before treating the whole session as expired.
                    try:
                        await self._async_validate_session(session)
                    except UtilitaAccountIdentityError:
                        await self._async_trigger_session_reauth()
                        raise
                    except UtilitaAuthenticationError:
                        await self._async_trigger_session_reauth()
                        raise UtilitaAuthenticationError(
                            f"{error_message}: authentication rejected (HTTP {status})"
                        ) from None
                    else:
                        raise UpdateFailed(
                            f"{error_message}: HTTP {status} on endpoint while session valid"
                        )
                if status != 200:
                    _LOGGER.warning(
                        "Utilita [%s] API HTTP ERROR %s HTTP %s after %.2fs",
                        entry_id, endpoint, status, elapsed,
                    )
                    raise UpdateFailed(f"{error_message}: HTTP {status}")

                result = (
                    await response.json()
                    if response_type == "json"
                    else await response.text()
                )

                if endpoint.rstrip("/").endswith("/user-data"):
                    returned_id = (
                        str(result.get("customer_id") or "")
                        if isinstance(result, dict) else ""
                    )
                    expected_id = str(self.config_entry.unique_id or "")
                    if not returned_id or (expected_id and returned_id != expected_id):
                        _LOGGER.error(
                            "Utilita [%s] API account identity mismatch on %s",
                            entry_id, endpoint,
                        )
                        await self._async_trigger_session_reauth()
                        raise UtilitaAccountIdentityError(
                            "Utilita account identity mismatch; refusing to display another account"
                        )

                self._async_store_session(
                    session, response.headers.get("Cache-Session"),
                )
                _LOGGER.debug(
                    "Utilita [%s] API SUCCESS %s in %.2fs",
                    entry_id, endpoint, time.monotonic() - started,
                )
                return result
        except TimeoutError:
            _LOGGER.warning(
                "Utilita [%s] API TIMEOUT %s after %.2fs (request limit %ss; "
                "overall refresh limit may also have interrupted it)",
                entry_id, endpoint, time.monotonic() - started, timeout,
            )
            raise
        except ClientError as err:
            _LOGGER.warning(
                "Utilita [%s] API NETWORK ERROR %s type=%s after %.2fs",
                entry_id, endpoint, type(err).__name__, time.monotonic() - started,
            )
            raise
        except ValueError as err:
            _LOGGER.warning(
                "Utilita [%s] API PARSE ERROR %s type=%s after %.2fs",
                entry_id, endpoint, type(err).__name__, time.monotonic() - started,
            )
            raise


    async def _async_optional_request(
        self, session, url, error_message, *, fallback=None,
        response_type="json", timeout=6,
    ):
        """Fetch a nonessential endpoint without invalidating balance/usage data.

        Reuse the last value on a transient failure; return None if no previous
        value exists so that the sensor doesn't invent a zero or empty history.
        Authentication rejection is never suppressed.
        """
        endpoint = URL(url).path
        try:
            result = await self._async_request(
                session, url, error_message,
                response_type=response_type, timeout=timeout,
            )
            return result, True
        except UtilitaAuthenticationError:
            raise
        except (TimeoutError, ClientError, ValueError, UpdateFailed) as err:
            _LOGGER.warning(
                "Utilita [%s] OPTIONAL API FAILED %s error=%s; %s",
                self.config_entry.entry_id,
                endpoint,
                type(err).__name__,
                "keeping last known value" if fallback is not None
                else "no previous value available",
            )
            return fallback, False


    async def _async_trigger_session_reauth(self):
        """Notify the user that session cookies have expired and trigger re-authentication."""
        if self._reauth_requested:
            return
        self._reauth_requested = True
        self.session_validated = False
        self._cancel_keepalive_retry()
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
        """Validate the expected account without conflating 5xx with expiry."""
        self._async_apply_cookies(session)
        entry_id = self.config_entry.entry_id
        started = time.monotonic()
        headers = self._build_api_headers()
        old_auth = self._get_session_cookies(session).get("myutilita_session")
        _LOGGER.debug("Utilita [%s] SESSION VALIDATION START /user-data", entry_id)
        try:
            async with session.get(
                "https://my.utilita.co.uk/user-data",
                timeout=10,
                headers=headers,
                allow_redirects=False,
            ) as response:
                _LOGGER.debug(
                    "Utilita [%s] SESSION VALIDATION HTTP %s after %.2fs",
                    entry_id, response.status, time.monotonic() - started,
                )
                if response.status in (301, 302, 303, 307, 308, 401, 403):
                    _LOGGER.warning(
                        "Utilita [%s] SESSION VALIDATION rejected HTTP %s",
                        entry_id, response.status,
                    )
                    raise UtilitaAuthenticationError(
                        f"Session validation rejected HTTP {response.status}"
                    )
                if response.status != 200:
                    # 5xx, 429, and other server errors must not request reauth.
                    raise UpdateFailed(f"Session validation HTTP {response.status}")
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, TypeError) as err:
                    raise UpdateFailed("Session validation returned invalid JSON") from err
                if not isinstance(payload, dict):
                    raise UpdateFailed("Session validation returned non-object JSON")
                returned_id = str(payload.get("customer_id") or "")
                expected_id = str(self.config_entry.unique_id or "")
                if not returned_id or (expected_id and returned_id != expected_id):
                    _LOGGER.error(
                        "Utilita [%s] SESSION VALIDATION identity mismatch", entry_id,
                    )
                    raise UtilitaAccountIdentityError(
                        "Session validation account identity mismatch"
                    )
                new_auth = self._get_session_cookies(session).get("myutilita_session")
                # Set-Cookie indicates server sent a cookie (potential renewal).
                # A 200 alone is NOT evidence that expiry has been extended.
                set_auth_cookie = any(
                    item.lstrip().lower().startswith("myutilita_session=")
                    for item in response.headers.getall("Set-Cookie", [])
                )
                _LOGGER.debug(
                    "Utilita [%s] SESSION COOKIE META auth_set_cookie=%s "
                    "auth_changed=%s auth_present=%s",
                    entry_id, set_auth_cookie,
                    old_auth != new_auth, bool(new_auth),
                )
                self._async_store_session(
                    session, response.headers.get("Cache-Session"),
                )
                _LOGGER.debug(
                    "Utilita [%s] SESSION VALIDATION SUCCESS in %.2fs",
                    entry_id, time.monotonic() - started,
                )
                return True
        except TimeoutError:
            _LOGGER.warning(
                "Utilita [%s] SESSION VALIDATION TIMEOUT after %.2fs",
                entry_id, time.monotonic() - started,
            )
            raise
        except ClientError as err:
            _LOGGER.warning(
                "Utilita [%s] SESSION VALIDATION NETWORK ERROR type=%s after %.2fs",
                entry_id, type(err).__name__, time.monotonic() - started,
            )
            raise

    async def _async_update_data(self):
        """Fetch account data, pinpointing the failing stage and elapsed time."""
        entry_id = self.config_entry.entry_id
        started = time.monotonic()
        stage = "session_validation"
        _LOGGER.debug(
            "Utilita [%s] REFRESH START interval=%s overall_timeout=30s",
            entry_id, self.update_interval,
        )
        try:
            # Keep the existing limit while diagnosing; increasing it now
            # might hide which request is slow.
            async with self._api_lock:
                async with async_timeout.timeout(30):
                    session = self._session
                    await self._async_ensure_authenticated(session)

                    stage = "/user-data"
                    user_data = await self._async_request(
                        session,
                        "https://my.utilita.co.uk/user-data",
                        "Failed to fetch user data",
                    )

                    stage = "/json/balance"
                    balance = await self._async_request(
                        session,
                        "https://my.utilita.co.uk/json/balance",
                        "Failed to fetch balance",
                    )

                    stage = "/json/usage"
                    usage = await self._async_request(
                        session,
                        f"https://my.utilita.co.uk/json/usage?end_date={date.today()}",
                        "Failed to fetch usage",
                    )

                    # Unread messages are optional; core balance/usage still updates.
                    previous = self.data if isinstance(self.data, dict) else {}

                    stage = "/messages-unread"
                    messages_text, messages_ok = await self._async_optional_request(
                        session,
                        "https://my.utilita.co.uk/messages-unread",
                        "Failed to fetch unread messages",
                        response_type="text",
                        fallback=None,
                        timeout=6,
                    )
                    if messages_ok:
                        try:
                            messages_unread = int(messages_text.strip())
                        except (ValueError, AttributeError):
                            messages_ok = False
                            messages_unread = previous.get("messages_unread")
                            _LOGGER.warning(
                                "Utilita [%s] OPTIONAL API FAILED /messages-unread "
                                "invalid count; keeping last known value",
                                entry_id,
                            )
                    else:
                        messages_unread = previous.get("messages_unread")

                    if not messages_ok:
                        _LOGGER.warning(
                            "Utilita [%s] REFRESH PARTIAL: core account/balance/usage "
                            "updated; unread_messages=stale_or_unavailable",
                            entry_id,
                        )

                    _LOGGER.debug(
                        "Utilita [%s] REFRESH COMPLETE core endpoints in %.2fs",
                        entry_id, time.monotonic() - started,
                    )
                    self.login_retry_after = None
                    self._reauth_requested = False
                    self.session_validated = True
                    self.last_session_check = time.time()
                    self.retry_attempts = 0
                    return {
                        "balance": balance,
                        "usage": usage,
                        "user_data": user_data,
                        "messages_unread": messages_unread,
                        "api_status": {
                            "messages_unread": "ok" if messages_ok else (
                                "stale" if messages_unread is not None else "unavailable"
                            ),
                        },
                    }
        except TimeoutError as err:
            elapsed = time.monotonic() - started
            _LOGGER.warning(
                "Utilita [%s] REFRESH TIMEOUT stage=%s elapsed=%.2fs "
                "(overall_limit=30s, individual_request_limit=10s)",
                entry_id, stage, elapsed,
            )
            raise UpdateFailed(
                f"Utilita refresh timed out at {stage} after {elapsed:.1f}s"
            ) from err
        except UpdateFailed as err:
            _LOGGER.warning(
                "Utilita [%s] REFRESH FAILED stage=%s elapsed=%.2fs reason=%s",
                entry_id, stage, time.monotonic() - started, str(err),
            )
            raise
        except ClientError as err:
            _LOGGER.warning(
                "Utilita [%s] REFRESH NETWORK FAILURE stage=%s elapsed=%.2fs type=%s",
                entry_id, stage, time.monotonic() - started, type(err).__name__,
            )
            raise UpdateFailed(
                f"Utilita network error at {stage}: {type(err).__name__}"
            ) from err
        except Exception as err:
            _LOGGER.exception(
                "Utilita [%s] REFRESH ERROR stage=%s elapsed=%.2fs type=%s",
                entry_id, stage, time.monotonic() - started, type(err).__name__,
            )
            raise UpdateFailed(
                f"Utilita error at {stage}: {type(err).__name__}"
            ) from err


    async def async_unload(self):
        """Cancel keepalive/retries and close this account's private session."""
        self._unloading = True
        if self._keepalive_unsub is not None:
            self._keepalive_unsub()
            self._keepalive_unsub = None
        self._cancel_keepalive_retry()
        await self._session.close()


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
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
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
            new_refresh_rate = entry.options.get(CONF_REFRESH_RATE, entry.data.get(CONF_REFRESH_RATE, 7200))
            # Saving refreshed cookies also updates ConfigEntry.data. Never
            # start a new refresh for those writes; only react to options.
            old_rate = coordinator.update_interval.total_seconds() if coordinator.update_interval else None
            if old_rate == new_refresh_rate:
                return True
            coordinator.update_interval = timedelta(seconds=new_refresh_rate)
            coordinator._async_configure_keepalive()
            await coordinator.async_request_refresh()
    return True
