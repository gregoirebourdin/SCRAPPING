"""Time-ordered UUIDv7 identifiers (RFC 9562) — index-friendly primary keys."""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7() -> uuid.UUID:
    """Return a monotonic UUIDv7 (48-bit unix ms, 12-bit counter, 62 random bits)."""
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            _counter += 1
            if _counter > 0xFFF:  # counter overflow: advance the logical clock
                _last_ms += 1
                _counter = 0
            ms = _last_ms
        else:
            _last_ms = ms
            _counter = int.from_bytes(os.urandom(2), "big") & 0x3FF
        counter = _counter
    rand = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (counter & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand
    return uuid.UUID(int=value)
