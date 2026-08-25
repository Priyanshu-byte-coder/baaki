"""Token budgeting and key rotation for the tail stage.

Adapted from the rate-limiting layer of my HelioOps project, which learned
these lessons against Groq's free tier the hard way.

The provider meters tokens per minute per **(key, model)** pair, on a rolling
window rather than a calendar minute. So the budget is a sliding window of
``(timestamp, tokens)`` reservations, one bucket per pair, and a pool of keys
multiplies the ceiling rather than sharing it.

Baaki calls a model on a thin residue, not on every record, so this rarely
binds. It is here because when it does bind, the failure is a 429 in the middle
of a batch, and a reconciliation run that dies half way through is worse than
one that takes an extra thirty seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0


def estimate_tokens(text: str) -> int:
    """Rough token count. cl100k averages about four characters per token."""
    return max(1, len(text) // 4)


class TokenBucket:
    """A sliding sixty-second token window for one (key, model) pair."""

    def __init__(self, limit_per_minute: int) -> None:
        self.limit = limit_per_minute
        # Reservations are mutated in place rather than corrected with a
        # compensating negative entry. A correction appended later carries a
        # later timestamp, so it would outlive the reservation it cancels and
        # leave the window under-counted at the edges.
        self._events: dict[int, list] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()

    def _evict(self, now: float) -> None:
        cutoff = now - WINDOW_SECONDS
        for key in [k for k, (ts, _) in self._events.items() if ts < cutoff]:
            del self._events[key]

    def _in_window(self) -> int:
        return sum(t for _, t in self._events.values())

    async def try_acquire(self, tokens: int) -> int | None:
        """Reserve without blocking. Returns a reservation id, or None if full."""
        tokens = min(tokens, self.limit)
        async with self._lock:
            now = time.monotonic()
            self._evict(now)
            if self._in_window() + tokens <= self.limit:
                self._next_id += 1
                self._events[self._next_id] = [now, tokens]
                return self._next_id
            return None

    async def wait_hint(self, tokens: int) -> float:
        """Seconds until ``tokens`` could plausibly fit. 0.0 if they fit now."""
        tokens = min(tokens, self.limit)
        async with self._lock:
            now = time.monotonic()
            self._evict(now)
            if self._in_window() + tokens <= self.limit:
                return 0.0
            if not self._events:
                return 0.0
            oldest = min(ts for ts, _ in self._events.values())
            return max(0.05, (oldest + WINDOW_SECONDS) - now)

    async def headroom(self) -> int:
        async with self._lock:
            self._evict(time.monotonic())
            return self.limit - self._in_window()

    async def reconcile(self, reservation: int, actual: int) -> None:
        """Shrink or grow a reservation to what the API actually billed."""
        async with self._lock:
            entry = self._events.get(reservation)
            if entry is not None:
                entry[1] = min(actual, self.limit)

    async def release(self, reservation: int) -> None:
        """Drop a reservation for a call that never reached the API."""
        async with self._lock:
            self._events.pop(reservation, None)

    async def penalise(self, seconds: float) -> None:
        """Mark this bucket saturated for roughly ``seconds``.

        Called on a 429: the server's accounting disagrees with ours and the
        server is authoritative. Without this, releasing the failed reservation
        makes the bucket look *emptier*, so the router hands the same exhausted
        key straight back and the call 429s again. The retry loop then sleeps
        out all its attempts against one key while the rest of the pool sits
        idle.
        """
        async with self._lock:
            now = time.monotonic()
            self._evict(now)
            # Backdate the block so it expires roughly `seconds` from now.
            stamp = now - max(0.0, WINDOW_SECONDS - seconds)
            self._next_id += 1
            self._events[self._next_id] = [stamp, self.limit]


class KeyPool:
    """Routes each call to the key with the most headroom for a model.

    A pool is throughput, not capability. It buys a larger per-minute ceiling
    and nothing else -- not better answers, not a bigger context window.
    """

    def __init__(self, keys: list[str], tpm_limit: int) -> None:
        if not keys:
            raise ValueError("key pool is empty")
        self.keys = keys
        self.tpm_limit = tpm_limit
        self._buckets: dict[tuple[str, str], TokenBucket] = {}

    def bucket(self, key: str, model: str) -> TokenBucket:
        slot = (key, model)
        if slot not in self._buckets:
            self._buckets[slot] = TokenBucket(self.tpm_limit)
        return self._buckets[slot]

    async def acquire(self, model: str, tokens: int) -> tuple[str, int]:
        """Pick a key that can afford ``tokens`` now, else wait for the soonest.

        Returns ``(api_key, reservation_id)``.
        """
        while True:
            best_key, best_headroom = None, -1
            for key in self.keys:
                headroom = await self.bucket(key, model).headroom()
                if headroom > best_headroom:
                    best_key, best_headroom = key, headroom

            reservation = await self.bucket(best_key, model).try_acquire(tokens)
            if reservation is not None:
                return best_key, reservation

            waits = [await self.bucket(k, model).wait_hint(tokens) for k in self.keys]
            wait = min(waits)
            log.info(
                "all %d keys saturated for %s (need %d tokens) - waiting %.1fs",
                len(self.keys),
                model,
                min(tokens, self.tpm_limit),
                wait,
            )
            await asyncio.sleep(min(wait, WINDOW_SECONDS))

    def reset(self) -> None:
        """Drop all rate-limit state. Used by tests."""
        self._buckets.clear()
