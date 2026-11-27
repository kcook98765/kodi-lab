from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Thread
from uuid import uuid4

import pytest

from kodi_lab.managed.broker import Broker, BrokerClient
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.protocol import Document, Refusal
from kodi_lab.managed.recording import Capture, CaptureBinding, Recordings

ACTOR = str(uuid4())
CREDENTIAL = str(uuid4())
IDENTITY = (ACTOR, CREDENTIAL)
PAYLOAD = b"private recording fixture"


@pytest.fixture
def recordings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Recordings]:
    def encode(_self: Recordings, capture: Capture, path: Path) -> Document:
        with path.open("xb") as output:
            output.write(PAYLOAD)
        while not capture.stop.wait(0.01):
            if time.monotonic() > capture.authority_until:
                return {"reason": "authority_lost"}
        return {"reason": capture.reason, "duration_seconds": 1}

    monkeypatch.setattr(Recordings, "_encode", encode)
    store = Recordings(tmp_path / "video")
    try:
        yield store
    finally:
        store.close()


def binding() -> CaptureBinding:
    return CaptureBinding("omega", "id", "start", "image", "profile", lambda: "profile")


def test_recording_exact_authority_and_private_storage(recordings: Recordings) -> None:
    recording = recordings.start(IDENTITY, 2, 10, binding)["recording"]
    wrong_credential = (ACTOR, str(uuid4()))
    for operation in ("status", "stop", "delete"):
        with pytest.raises(Refusal, match="managed_recording_not_found"):
            recordings.request(wrong_credential, recording, operation)
    with pytest.raises(Refusal, match="managed_recording_busy"):
        recordings.start(wrong_credential, 2, 10, binding)

    assert recordings.request(IDENTITY, recording, "renew") == {"state": "active"}
    assert recordings.request(IDENTITY, recording, "stop")["state"] == "finalizing"
    assert recordings.worker is not None
    recordings.worker.join(5)
    with pytest.raises(Refusal, match="managed_recording_not_found"):
        recordings.open_artifact(wrong_credential, recording)
    result = recordings.request(IDENTITY, recording, "status")
    assert result["state"] == "ready"
    assert result["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    reopened = Recordings(recordings.root)
    stream, metadata = reopened.open_artifact(IDENTITY, recording)
    with stream:
        assert stream.read() == PAYLOAD
    assert metadata == result
    assert reopened.request(IDENTITY, recording, "delete") == {"state": "deleted"}
    assert not list(reopened.root.iterdir())


def test_expired_authority_stops_before_export(recordings: Recordings) -> None:
    recording = recordings.start(IDENTITY, 2, 10, binding)["recording"]
    assert recordings.active is not None and recordings.worker is not None
    recordings.active.authority_until = time.monotonic() - 1
    with pytest.raises(Refusal, match="managed_recording_not_active"):
        recordings.request(IDENTITY, recording, "renew")
    recordings.worker.join(5)
    result = recordings.request(IDENTITY, recording, "status")
    assert result["error"] == "managed_recording_authority_lost"
    assert not list(recordings.root.iterdir())


def test_broker_stream_is_bounded_and_requires_exact_credential(
    tmp_path: Path, recordings: Recordings
) -> None:
    recording = recordings.start(IDENTITY, 2, 10, binding)["recording"]
    recordings.request(IDENTITY, recording, "stop")
    assert recordings.worker is not None
    recordings.worker.join(5)
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    socket = tmp_path / "recording.sock"
    with Broker(socket, Controller(root), os.getuid(), recordings) as broker:
        server = Thread(target=broker.serve_forever, daemon=True)
        server.start()

        async def download() -> None:
            client = BrokerClient(socket)
            with pytest.raises(Refusal, match="managed_recording_not_found"):
                async with client.download(ACTOR, str(uuid4()), recording):
                    pytest.fail("A different credential downloaded the recording")
            async with client.download(ACTOR, CREDENTIAL, recording) as (reader, size):
                assert await reader.readexactly(size) == PAYLOAD

        try:
            asyncio.run(download())
        finally:
            broker.shutdown()
            server.join(5)
