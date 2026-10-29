"""In-memory throttle for the audit endpoint.

Why this exists
---------------
One `/api/audit` request in live mode makes **three** Qloo calls: `/search` to
resolve the seed, `/v2/insights` for the seed record, and `/v2/insights` again
for the recommendation set. The endpoint is unauthenticated by design, so
without a limit any visitor — or any stray browser tab retrying in a loop — can
spend the operator's API quota. `/api/seeds` is hand-authored and costs nothing;
only the audit endpoint is throttled.

Two bounds, both plain Python and both process-local:

* `RateLimiter` — a token bucket per client key. A bucket holds `capacity`
  tokens and refills continuously at `refill_per_minute / 60` tokens per second,
  so a burst of a few clicks is free but a sustained loop is refused with HTTP
  429 before any Qloo request is made.
* `ConcurrencyGate` — how many audits may be in flight at once. This is the
  ceiling on simultaneous upstream calls (`gate x 3`) and it keeps a handful of
  slow Qloo responses from piling threads onto a small single instance.

Known limitations, stated for operators
--------------------------------------
1. State is in memory. A restart, an idle spin-down, or a redeploy empties every
   bucket, so the burst allowance is available again immediately.
2. It is per instance. Running more than one instance multiplies the allowance.
3. The client key comes from `X-Forwarded-For`, which a client can populate with
   anything. Only the value appended by the fronting proxy is trustworthy, so the
   rightmost entry is used — see `client_key`.
4. It is a quota guard, not an identity or access-control mechanism.

Nothing here echoes a credential: buckets are keyed by an opaque digest of the
client address, never the raw header, and no upstream response body is retained.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Callable, Optional, Tuple

# Audits per minute, per client, at steady state.
DEFAULT_PER_MINUTE = 6
# Audits a single client may fire in a burst before the refill starts to bind.
DEFAULT_BURST = 3
# Audits in flight across the whole instance at one time.
DEFAULT_CONCURRENT = 2
# Cap on distinct clients tracked, so a flood of spoofed addresses cannot grow
# the bucket table without bound.
DEFAULT_MAX_KEYS = 2048

# The longest seed string passed to Qloo. Real artist names are far shorter; this
# only bounds what one request can put on the wire.
MAX_SEED_LENGTH = 120

# Ceiling for the environment overrides below.
HARD_MAX_PER_MINUTE = 600
HARD_MAX_BURST = 100
HARD_MAX_CONCURRENT = 16


def _env_int(name: str, default: int, maximum: int) -> int:
    """Read a positive integer setting, clamped; an unusable value keeps the default."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value < 1:
        return default
    return min(value, maximum)


def settings_from_env() -> dict:
    """Effective throttle settings, reading the environment at call time.

    `set_default_throttle(None)` rebuilds the active throttle from these values;
    a test or an operator can also install an explicit `AuditThrottle` instead.
    The point is that a deployment can raise or lower the limits without touching
    the code.
    """
    per_minute = _env_int("SO_AUDIT_PER_MINUTE", DEFAULT_PER_MINUTE, HARD_MAX_PER_MINUTE)
    burst = _env_int("SO_AUDIT_BURST", DEFAULT_BURST, HARD_MAX_BURST)
    return {
        "per_minute": per_minute,
        # A burst larger than the steady rate would let a client bank requests
        # it is never allowed to make.
        "burst": min(burst, per_minute),
        "concurrent": _env_int("SO_MAX_CONCURRENT_AUDITS", DEFAULT_CONCURRENT, HARD_MAX_CONCURRENT),
        "max_keys": DEFAULT_MAX_KEYS,
    }


class RateLimiter:
    """Token bucket per key. Refill is continuous; state is process-local."""

    def __init__(
        self,
        capacity: int = DEFAULT_BURST,
        refill_per_minute: int = DEFAULT_PER_MINUTE,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if refill_per_minute < 1:
            raise ValueError("refill_per_minute must be at least 1")
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        self.capacity = int(capacity)
        self.refill_per_minute = int(refill_per_minute)
        self._refill_per_second = refill_per_minute / 60.0
        self.max_keys = int(max_keys)
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict = {}

    def allow(self, key: str, cost: float = 1.0) -> Tuple[bool, float]:
        """Take `cost` tokens from `key`'s bucket.

        Returns `(allowed, retry_after_seconds)`. When refused, no token is
        consumed, so the caller reports how long until a full request fits.
        """
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self._refill_per_second)
            allowed = tokens >= cost
            self._buckets[key] = (tokens - cost if allowed else tokens, now)
            self._prune_if_needed()
            if allowed:
                return True, 0.0
            wait = (cost - tokens) / self._refill_per_second
            return False, min(max(wait, 0.0), 3600.0)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _prune_if_needed(self) -> None:
        """Forget the least-recently-seen buckets when the table outgrows `max_keys`.

        Called with the lock held. It shrinks to 80% of the cap rather than to
        exactly the cap, so a sustained flood of new addresses pays one sort
        instead of sorting on every request.

        Oldest-last is a deliberate choice over "drop the emptiest": a refused
        client that keeps hammering still gets a real `Retry-After`, while an
        idle client is forgotten.
        """
        if len(self._buckets) <= self.max_keys:
            return
        target = max(1, (self.max_keys * 4) // 5)
        for key, _ in sorted(self._buckets.items(), key=lambda kv: kv[1][1])[: len(self._buckets) - target]:
            self._buckets.pop(key, None)


class ConcurrencyGate:
    """Non-blocking semaphore over the number of audits in flight."""

    def __init__(self, limit: int = DEFAULT_CONCURRENT) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = int(limit)
        self._sem = threading.BoundedSemaphore(self.limit)

    def try_acquire(self) -> bool:
        return self._sem.acquire(blocking=False)

    def release(self) -> None:
        self._sem.release()


class AuditThrottle:
    """The two bounds together, plus the opaque client keying."""

    def __init__(
        self,
        per_minute: int = DEFAULT_PER_MINUTE,
        burst: int = DEFAULT_BURST,
        concurrent: int = DEFAULT_CONCURRENT,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        burst = max(1, min(int(burst), int(per_minute)))
        self.per_minute = int(per_minute)
        self.burst = burst
        self.limiter = RateLimiter(
            capacity=burst, refill_per_minute=per_minute, max_keys=max_keys, clock=clock
        )
        self.gate = ConcurrencyGate(concurrent)
        self.concurrent = int(concurrent)

    def budget(self, raw_client: str) -> Tuple[bool, float]:
        """Consume one audit token for a client. Never call this before validation."""
        return self.limiter.allow(client_key(raw_client))

    def as_dict(self) -> dict:
        return {
            "per_minute": self.per_minute,
            "burst": self.burst,
            "concurrent": self.concurrent,
            "storage": "in-memory",
            "resets_on_restart": True,
        }


def client_key(raw: Optional[str]) -> str:
    """Stable, opaque bucket key for a client address.

    The address is hashed rather than stored: logs and bucket tables then carry
    no personal IP, and a hostile header value cannot inflate the key table with
    arbitrary text. Empty or absent input collapses to a single `anonymous`
    bucket, which is the safe direction to fail.
    """
    value = (raw or "").strip().split(",")[-1].strip()
    if not value:
        return "anonymous"
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()
    return digest[:16]


def rightmost_forwarded_client(forwarded_for: Optional[str]) -> str:
    """The client address as recorded by the fronting proxy.

    A proxy that *appends* to `X-Forwarded-For` leaves every earlier entry under
    the client's control, so trusting the leftmost one lets a caller pick its own
    bucket and walk past the limit. A proxy that *replaces* the header leaves only
    one entry. Either way the real client is the last entry, and that is the only
    one used here — the rule holds without needing to know how a given host fills
    the header in.
    """
    header = (forwarded_for or "").strip()
    if not header:
        return ""
    return header.split(",")[-1].strip()


# -- the instance the server uses ------------------------------------------
#
# Built once, at import, from the environment. A handler looks it up per request
# so tests can replace it without restarting the process.

_ACTIVE = AuditThrottle(**settings_from_env())


def default_throttle() -> AuditThrottle:
    """The process-wide throttle the HTTP handlers share."""
    return _ACTIVE


def set_default_throttle(throttle: Optional[AuditThrottle]) -> AuditThrottle:
    """Install `throttle`, or rebuild from the environment when given None.

    Used by tests, and by an operator who wants a different budget at runtime.
    Returns what is now active.
    """
    global _ACTIVE
    _ACTIVE = throttle if throttle is not None else AuditThrottle(**settings_from_env())
    return _ACTIVE

