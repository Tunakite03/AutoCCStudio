"""A rotating pool of provider API keys.

With one key, a per-minute quota stops the job. With several, it only stops the
job if every key is limited *at the same moment* — which is rarer than it
sounds, because the keys are used one after another and their windows roll over
one after another too.

That is the reason a key is never retired here, only made unavailable until a
point in time. By the time a long translation has cycled to the last key, the
first one's window has usually already reopened, and treating it as spent would
throw away a working credential.
"""

from __future__ import annotations

import threading
import time


class CredentialPool:
    """Hands out keys, remembers which ones are cooling down, and rotates.

    Shared across workers, so every method holds the lock: two jobs translating
    at once must not both decide the same key is the free one.
    """

    def __init__(
        self,
        keys,
        *,
        header: str = "Authorization",
        scheme: str = "Bearer",
    ):
        self._keys = tuple(keys)
        self._header = header
        self._scheme = scheme
        self._lock = threading.Lock()
        self._available_at: dict[str, float] = {key: 0.0 for key in self._keys}
        self._strikes: dict[str, int] = {key: 0 for key in self._keys}
        # Position, never the secret: this is what goes in the logs.
        self._labels = {
            key: f"{index + 1}/{len(self._keys)}" for index, key in enumerate(self._keys)
        }
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._keys)

    def acquire(self) -> tuple[str | None, float]:
        """The next usable key, or `(None, seconds until the earliest one frees)`.

        Round-robin rather than always-the-first: two workers should spread
        across the pool instead of queueing on one key while the rest idle.
        """

        if not self._keys:
            return None, 0.0
        now = time.monotonic()
        with self._lock:
            for offset in range(len(self._keys)):
                index = (self._cursor + offset) % len(self._keys)
                key = self._keys[index]
                if self._available_at[key] <= now:
                    self._cursor = (index + 1) % len(self._keys)
                    return key, 0.0
            return None, max(0.0, min(self._available_at.values()) - now)

    def penalise(self, key: str, delay: float) -> None:
        """Take a rate-limited key out of rotation for `delay` seconds."""

        with self._lock:
            self._strikes[key] = self._strikes.get(key, 0) + 1
            self._available_at[key] = time.monotonic() + max(0.0, delay)

    def reject(self, key: str, delay: float) -> None:
        """Shelve a key the provider refused — a typo, a revoked credential.

        Separate from `penalise` because it is not a strike: being rejected says
        nothing about how busy the key is, and folding it into the rate-limit
        backoff would punish the key later for a problem that is not a quota.
        """

        with self._lock:
            self._available_at[key] = time.monotonic() + max(0.0, delay)

    def release(self, key: str) -> None:
        """A key that answered is a working key: forget its backoff history.

        Without this, one bad minute would keep lengthening that key's cooldown
        for the rest of a job it is now serving perfectly well.
        """

        with self._lock:
            if self._strikes.get(key):
                self._strikes[key] = 0

    def strikes(self, key: str) -> int:
        """Consecutive rate limits on this key, for the caller's backoff maths."""

        with self._lock:
            return self._strikes.get(key, 0)

    def label(self, key: str) -> str:
        return self._labels.get(key, "?")

    def authorization(self, key: str) -> dict[str, str]:
        return {self._header: f"{self._scheme} {key}".strip()}
