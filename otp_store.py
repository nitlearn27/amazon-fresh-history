"""In-process, single-slot OTP store for Amazon's 2-step verification.

Replaces the old Salesforce side-channel. The scrape runs in a background
thread inside the Flask process; when it hits the OTP screen it marks the store
as *waiting* and blocks until a code is pushed (or it times out). A client pushes
the code over HTTP via ``POST /api/otp``; the scrape thread consumes it within
~1 second.

Why a single global slot: the service is single-tenant (one Amazon account) and
only one scrape/cart run can be in flight at a time, so at most one OTP is ever
being waited on.

Why a TTL: a pushed code is short-lived. It is cleared the moment it is consumed,
when a new wait begins, and when a wait ends — and, as a backstop, it is treated
as expired once it is older than ``OTP_TTL_SECONDS`` (default 300s / 5 min). This
guarantees a stale code can never be fed into a later login.

Thread-safe: the scrape thread and Flask request handlers touch this from
different threads, so every field is guarded by a single lock.
"""

import os
import threading
import time
from datetime import datetime, timezone

_DEFAULT_TTL_SECONDS = 300  # 5 minutes


def ttl_seconds() -> int:
    """OTP lifetime in seconds, from OTP_TTL_SECONDS (default 300, min 1)."""
    raw = os.getenv("OTP_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TTL_SECONDS
    return value if value > 0 else _DEFAULT_TTL_SECONDS


class OtpStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._code: str | None = None
        self._stored_at_monotonic: float = 0.0
        self._waiting_since_epoch: float | None = None

    # --- scraper side ------------------------------------------------------

    def begin_wait(self) -> None:
        """Mark that the scraper is now waiting for an OTP. Clears any old code."""
        with self._lock:
            self._waiting_since_epoch = time.time()
            self._code = None
            self._stored_at_monotonic = 0.0
        print("[otp] Waiting for an OTP — POST it to /api/otp.")

    def end_wait(self) -> None:
        """Clear the waiting flag and any leftover code (call when done waiting)."""
        with self._lock:
            self._waiting_since_epoch = None
            self._code = None
            self._stored_at_monotonic = 0.0
        print("[otp] Stopped waiting for an OTP.")

    def consume(self) -> str | None:
        """Return a fresh (non-expired) pushed OTP and clear it, else None."""
        with self._lock:
            if self._code is None:
                return None
            age = time.monotonic() - self._stored_at_monotonic
            if age > ttl_seconds():
                self._code = None
                self._stored_at_monotonic = 0.0
                print(f"[otp] Discarded a stale OTP ({age:.0f}s old > {ttl_seconds()}s TTL).")
                return None
            code = self._code
            self._code = None
            self._stored_at_monotonic = 0.0
            print(f"[otp] Consumed a {len(code)}-digit OTP ({age:.0f}s after it was pushed).")
            return code

    # --- client (HTTP) side ------------------------------------------------

    def is_waiting(self) -> bool:
        with self._lock:
            return self._waiting_since_epoch is not None

    def submit(self, code: str) -> None:
        """Store a pushed OTP, stamped now for TTL purposes."""
        with self._lock:
            self._code = code
            self._stored_at_monotonic = time.monotonic()

    def status(self) -> dict:
        with self._lock:
            since = self._waiting_since_epoch
        return {
            "waiting": since is not None,
            "waiting_since": (
                datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
                if since is not None
                else None
            ),
            "ttl_seconds": ttl_seconds(),
        }


# Module-level singleton shared by the scraper and the Flask routes.
store = OtpStore()
