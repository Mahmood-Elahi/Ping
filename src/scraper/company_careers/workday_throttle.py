"""Scan-local backpressure for Workday's tenant-scoped CXS listing POSTs."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
import threading
import time
from urllib.parse import urlsplit

import requests


HOST = re.compile(r"[a-z0-9-]+\.wd[0-9]+\.myworkdayjobs\.com")
LISTING = re.compile(r"/wday/cxs/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/jobs")
WINDOW = 60
SPACING = 1.0
CORRELATED_SPACING = 3.0


def retry_seconds(value, now):
    value = (value or "").strip()
    if value.isascii() and value.isdecimal():
        digits = value.lstrip("0") or "0"
        return int(digits) if len(digits) <= 12 else float("inf")
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, date.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


class WorkdayRateLimit(requests.HTTPError):
    """A physical 429 or a scan-local admission held by an earlier 429."""

    def __init__(self, message, *, retry_at=None, physical_response=None, request=None):
        super().__init__(message, response=physical_response, request=request)
        self.physical = physical_response is not None
        self.reason = "rate_limit_backpressure"
        self.retry_at = None
        if retry_at is not None:
            self.reason = "rate_limited" if self.physical else "rate_limit_wait"
            try:
                deadline = datetime.fromtimestamp(retry_at, timezone.utc)
            except (OverflowError, OSError, ValueError):
                # An out-of-range provider delay requires operator review.
                deadline = datetime.max.replace(tzinfo=timezone.utc)
            self.retry_at = deadline.isoformat()


class WorkdayRedirect(requests.HTTPError):
    """An unreviewed redirect on the identity-bound CXS POST."""


class WorkdayThrottle:
    """Share pacing decisions across source workers, never a requests.Session."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep, wall_clock=time.time):
        self.clock, self.sleep, self.wall_clock = clock, sleep, wall_clock
        self._lock = threading.Lock()
        self._retry_at = {}
        self._signals = {}
        self._paused_until = self._paced_until = self._next_start = 0.0

    def session(self):
        return WorkdayListingSession(self)

    def throttled(self, host, retry_after):
        with self._lock:
            now, wall_now = self.clock(), self.wall_clock()
            delay = retry_seconds(retry_after, wall_now)
            wait = WINDOW if delay is None else delay
            deadline, retry_at = self._retry_at.get(host, (float("-inf"), 0))
            if now + wait > deadline:
                retry_at = wall_now + wait
                self._retry_at[host] = now + wait, retry_at
            self._signals = {h: t for h, t in self._signals.items() if t > now - WINDOW}
            self._signals[host] = now
            if len(self._signals) >= 2:
                self._paused_until = now + WINDOW
                self._paced_until = self._paused_until + WINDOW
        return delay, retry_at

    def admit(self, host):
        while True:
            with self._lock:
                now = self.clock()
                deadline, retry_at = self._retry_at.get(host, (0, 0))
                if now < deadline:
                    raise WorkdayRateLimit(
                        "Workday HTTP 429 cooldown for " + host, retry_at=retry_at,
                    )
                if now < self._paused_until:
                    raise WorkdayRateLimit("Workday listing request deferred after 429s across hosts")
                wait = self._next_start - now
                if wait <= 0:
                    spacing = CORRELATED_SPACING if now < self._paced_until else SPACING
                    self._next_start = now + spacing
                    return
            self.sleep(wait)


class WorkdayListingSession(requests.Session):
    def __init__(self, throttle):
        super().__init__()
        self.throttle = throttle
        self.rejection_deadline = None

    def send_admitted(self, request, **kwargs):
        """Dispatch one admitted request, including redirect hops re-entering send()."""
        return super().send(request, **kwargs)

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        host = url.hostname or ""
        if not (request.method == "POST" and url.scheme == "https" and HOST.fullmatch(host)
                and LISTING.fullmatch(url.path)):
            return self.send_admitted(request, **kwargs)
        if self.rejection_deadline is not None:
            raise WorkdayRateLimit("Workday HTTP 429 on this source", retry_at=self.rejection_deadline)
        self.throttle.admit(host)
        kwargs["allow_redirects"] = False
        response = self.send_admitted(request, **kwargs)
        if 300 <= response.status_code < 400:
            response.close()
            raise WorkdayRedirect("Unreviewed Workday CXS redirect for " + host, response=response, request=request)
        if response.status_code == 429:
            delay, self.rejection_deadline = self.throttle.throttled(
                host, response.headers.get("Retry-After")
            )
            response.close()
            hint = "" if delay is None else f" (Retry-After {delay:.0f}s)"
            raise WorkdayRateLimit("Workday HTTP 429 for " + host + hint,
                                   retry_at=self.rejection_deadline, physical_response=response, request=request)
        return response
