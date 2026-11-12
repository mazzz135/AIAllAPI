from __future__ import annotations

import hashlib


def seeded_stream(seed: str, salt: str, nbytes: int) -> bytes:
    """Deterministic bytes: SHA-256(salt:seed || counter_be32), concatenated."""
    data = f"{salt}:{seed}".encode("utf-8")
    out = bytearray()
    counter = 0
    while len(out) < nbytes:
        out.extend(hashlib.sha256(data + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:nbytes])


def device_id_for(uid: str) -> str:
    """15-digit numeric x-device-id. Format matters: upstream treats a bad shape as no device."""
    raw = seeded_stream(uid, "devid", 16)
    return "".join(str(b % 10) for b in raw[:15])


def session_id_for(uid: str) -> str:
    return seeded_stream(uid, "sess", 32).hex()


def market_user_id_for(uid: str) -> str:
    raw = bytearray(seeded_stream(uid, "market", 16))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    h = raw.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
