"""UUIDv7 request identities for viewer protocol fixtures."""

import time
from uuid import UUID, uuid4


def view_attempt(stamp: int | None = None) -> UUID:
    stamp = time.time_ns() // 1_000_000 if stamp is None else stamp
    random = uuid4().int & ((1 << 80) - 1)
    value = (stamp << 80) | random
    value = (value & ~(15 << 76)) | (7 << 76)
    value = (value & ~(3 << 62)) | (2 << 62)
    return UUID(int=value)
