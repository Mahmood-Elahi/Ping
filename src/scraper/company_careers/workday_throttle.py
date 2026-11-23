"""Scan-local backpressure for Workday's tenant-scoped CXS listing POSTs."""
from datetime import timezone
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


def retry_seconds(value, now):
    value = (value or "").strip()
    if re.fullmatch(r"[0-9]{1,12}", value):
        return int(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return max(0, date.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


class WorkdayThrottle:
    """Share pacing decisions across source workers, never a requests.Session."""
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep, wall_clock=time.time):
        self.clock, self.sleep, self.wall_clock = clock, sleep, wall_clock
        self._lock = threading.Lock()
        self._retry_at = {}
        self._signals = {}
        self._paced_until = self._next_start = 0.0

    def session(self):
        return _ListingSession(self)

    def throttled(self, host, retry_after):
        delay = retry_seconds(retry_after, self.wall_clock())
        with self._lock:
            now = self.clock()
            self._retry_at[host] = max(self._retry_at.get(host, 0), now + (WINDOW if delay is None else delay))
            self._signals = {h: t for h, t in self._signals.items() if t > now - WINDOW}
            self._signals[host] = now
            if len(self._signals) >= 2:
                self._paced_until = now + WINDOW
                self._next_start = max(self._next_start, now + SPACING)
        return delay

    def admit(self, host):
        while True:
            with self._lock:
                now = self.clock()
                if now < self._retry_at.get(host, 0):
                    raise requests.HTTPError("Workday HTTP 429 cooldown for " + host)
                if now >= self._paced_until:
                    return
                wait = min(self._next_start, self._paced_until) - now
                if wait <= 0:
                    self._next_start = now + SPACING
                    return
            self.sleep(wait)


class _ListingSession(requests.Session):
    def __init__(self, throttle):
        super().__init__()
        self.throttle = throttle
        self.rejected = False

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        host = url.hostname or ""
        if not (request.method == "POST" and url.scheme == "https" and HOST.fullmatch(host)
                and LISTING.fullmatch(url.path)):
            return super().send(request, **kwargs)
        if self.rejected:
            raise requests.HTTPError("Workday HTTP 429 on this source")
        self.throttle.admit(host)
        response = super().send(request, **kwargs)
        if response.status_code == 429:
            self.rejected = True
            delay = self.throttle.throttled(host, response.headers.get("Retry-After"))
            response.close()
            hint = "" if delay is None else f" (Retry-After {delay:.0f}s)"
            raise requests.HTTPError("Workday HTTP 429 for " + host + hint, response=response, request=request)
        return response
