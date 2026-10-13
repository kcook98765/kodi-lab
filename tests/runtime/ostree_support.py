"""Integrity-checked signed metadata fixtures; no Kodi executable payload."""

import base64
import hashlib
import json
from pathlib import Path


def signed_fixture(role: str) -> tuple[bytes, bytes]:
    raw = (
        Path(__file__)
        .with_name("fixtures")
        .joinpath("rc1-signed-metadata.json")
        .read_bytes()
    )
    assert hashlib.sha256(raw).hexdigest() == (
        "e9be691244b402597ad386eb953790caa2efe222ee313e45ffea01d08482d120"
    )
    value = json.loads(raw)["components"][role]
    return (
        base64.b64decode(value["commit_base64"], validate=True),
        base64.b64decode(value["detached_base64"], validate=True),
    )
