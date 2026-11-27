"""One leased display recorder and bounded private artifacts for managed Kodi."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from selectors import EVENT_READ, DefaultSelector
from threading import Event, Lock, Thread
from typing import BinaryIO
from uuid import uuid4

from .protocol import Document, Refusal
from .runtime import DOCKER, atomic
from .x11_recording import HEIGHT, LEASE_SECONDS, WIDTH

MAX_BYTES = 64 * 1024**2
MAX_ARTIFACTS = 4
RETAIN_SECONDS = 24 * 60 * 60
FREE_RESERVE = 2 * 1024**3
AUTHORITY_SECONDS = 6
ENCODER_ID = 10002
RECORDING_ID = re.compile(r"[0-9a-f]{32}")
SAMPLE_SOURCE = Path(__file__).with_name("x11_recording.py")


@dataclass(frozen=True, slots=True)
class CaptureBinding:
    generation: str
    container: str
    started: str
    image: str
    profile: str
    current_profile: Callable[[], str]


@dataclass(slots=True)
class Capture:
    identity: tuple[str, str]
    binding: CaptureBinding
    data: Document
    authority_until: float
    stop: Event
    reason: str = "stopped"


class Recordings:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.active: Capture | None = None
        self.latest: Capture | None = None
        self.worker: Thread | None = None
        self.lock = Lock()
        self.closed = False
        self.next_cleanup = 0.0
        self.root.mkdir(mode=0o710, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_uid != os.getuid():
            raise Refusal("recording_storage_identity")
        if os.getuid() == 0:
            os.chown(self.root, 0, ENCODER_ID)
        self.root.chmod(0o710)
        # A process restart invalidates all running leases. Recover only
        # artifacts with a committed receipt and their original checksum.
        for entry in self.root.iterdir():
            if (
                entry.name.startswith(".stage-")
                or entry.suffix == ".part"
                or (entry.suffix == ".mp4" and not entry.with_suffix(".json").is_file())
            ):
                entry.unlink()
        self.cleanup()

    def cleanup(self) -> None:
        with self.lock:
            self._cleanup()

    def _cleanup(self) -> None:
        now = time.time()
        if now < self.next_cleanup:
            return
        for receipt in self.root.glob("*.json"):
            if now - receipt.stat().st_mtime >= RETAIN_SECONDS:
                receipt.unlink()
                receipt.with_suffix(".mp4").unlink(missing_ok=True)
        self.next_cleanup = now + 60

    def start(
        self,
        identity: tuple[str, str],
        rate: int,
        seconds: int,
        bind: Callable[[], CaptureBinding],
    ) -> Document:
        with self.lock:
            if self.closed or self.active is not None:
                raise Refusal("managed_recording_busy")
            self._cleanup()
            if len(list(self.root.glob("*.json"))) >= MAX_ARTIFACTS:
                raise Refusal("managed_recording_artifact_limit")
            if shutil.disk_usage(self.root).free < FREE_RESERVE + MAX_BYTES:
                raise Refusal("managed_recording_storage_limit")
            binding = bind()
            capture = Capture(
                identity,
                binding,
                {
                    "recording": uuid4().hex,
                    "generation": binding.generation,
                    "incarnation": hashlib.sha256(
                        json.dumps(
                            [
                                binding.container,
                                binding.started,
                                binding.image,
                                binding.profile,
                            ]
                        ).encode()
                    ).hexdigest(),
                    "state": "starting",
                    "fps": rate,
                    "limit_seconds": seconds,
                },
                time.monotonic() + AUTHORITY_SECONDS,
                Event(),
            )
            self.active = capture
            self.latest = capture
            self.worker = Thread(
                target=self._run, args=(capture,), name="managed-recording", daemon=True
            )
            self.worker.start()
            return dict(capture.data)

    def request(
        self,
        identity: tuple[str, str],
        recording: str | None,
        operation: str,
    ) -> Document:
        with self.lock:
            if recording is None:
                if self.latest is None or self.latest.identity != identity:
                    raise Refusal("managed_recording_not_found")
                recording = self.latest.data["recording"]
            capture = self.active
            if operation == "renew":
                if (
                    capture is None
                    or capture.identity != identity
                    or capture.data["recording"] != recording
                    or capture.stop.is_set()
                    or capture.authority_until <= time.monotonic()
                ):
                    raise Refusal("managed_recording_not_active")
                capture.authority_until = time.monotonic() + AUTHORITY_SECONDS
                return {"state": "active"}
            if operation == "delete":
                path, _ = self._artifact(identity, recording)
                path.with_suffix(".json").unlink()
                path.unlink(missing_ok=True)
                return {"state": "deleted"}
            latest = self.latest
            if (
                latest is not None
                and latest.identity == identity
                and latest.data["recording"] == recording
            ):
                if latest is capture and operation == "stop":
                    latest.stop.set()
                    latest.data["state"] = "finalizing"
                if latest.data["state"] != "ready":
                    return dict(latest.data)
            return self._artifact(identity, recording)[1]

    def _artifact(
        self, identity: tuple[str, str], recording: str
    ) -> tuple[Path, Document]:
        if not RECORDING_ID.fullmatch(recording):
            raise Refusal("invalid_arguments")
        receipt = self.root / (recording + ".json")
        try:
            if time.time() - receipt.stat().st_mtime >= RETAIN_SECONDS:
                raise ValueError
            data = json.loads(receipt.read_bytes())
            if tuple(data["identity"]) != identity:
                raise ValueError
            result: Document = data["result"]
            path = receipt.with_suffix(".mp4")
            if path.stat().st_size != result["bytes"]:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise Refusal("managed_recording_not_found") from error
        return path, result

    def open_artifact(
        self,
        identity: tuple[str, str],
        recording: str,
    ) -> tuple[BinaryIO, Document]:
        with self.lock:
            path, result = self._artifact(identity, recording)
            return path.open("rb"), result

    def close(self) -> None:
        with self.lock:
            self.closed = True
            if self.active is not None:
                self.active.reason = "shutdown"
                self.active.stop.set()
            worker = self.worker
        if worker is not None:
            worker.join(12)

    def _run(self, capture: Capture) -> None:
        recording = capture.data["recording"]
        path = self.root / (recording + ".part")
        result: Document = {
            "state": "failed",
            "error": "managed_recording_encoder_failed",
        }
        try:
            result = self._encode(capture, path)
            if result["reason"] == "authority_lost":
                raise Refusal("managed_recording_authority_lost")
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
                os.fsync(file.fileno())
            size = path.stat().st_size
            if not 0 < size <= MAX_BYTES:
                raise Refusal("managed_recording_storage_limit")
            result = {"state": "ready", **result, "sha256": digest, "bytes": size}
            if os.getuid() == 0:
                os.chown(path, 0, 0)
            path.replace(path.with_suffix(".mp4"))
            try:
                atomic(
                    path.with_suffix(".json"),
                    json.dumps(
                        {
                            "identity": capture.identity,
                            "result": {**capture.data, **result},
                        }
                    ).encode(),
                )
            except OSError:
                path.with_suffix(".mp4").unlink(missing_ok=True)
                raise
        except Refusal as error:
            result = {"state": "failed", "error": str(error)}
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            result = {"state": "failed", "error": "managed_recording_encoder_failed"}
        finally:
            with suppress(OSError):
                path.unlink(missing_ok=True)
            with self.lock:
                capture.data.update(result)
                self.active = None

    def _encode(self, capture: Capture, path: Path) -> Document:
        binding = capture.binding
        rate, seconds = capture.data["fps"], capture.data["limit_seconds"]
        use_uid = os.getuid() == 0
        with path.open("xb"):
            pass
        path.chmod(0o600)
        if use_uid:
            os.chown(path, ENCODER_ID, ENCODER_ID)
        sampler = subprocess.Popen(
            [
                *DOCKER,
                "exec",
                "-i",
                "--user",
                "1000:1000",
                binding.container,
                "/usr/bin/python3",
                "-I",
                "-B",
                "-c",
                SAMPLE_SOURCE.read_text(),
                str(rate),
                str(seconds),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        assert sampler.stdin is not None and sampler.stdout is not None
        encoder: subprocess.Popen[bytes] | None = None
        try:
            with DefaultSelector() as selector:
                selector.register(sampler.stdout, EVENT_READ)
                if not selector.select(timeout=3):
                    raise Refusal("managed_recording_display_unavailable")
            started = datetime.now(UTC).isoformat()
            start_time = time.monotonic()
            if not current(binding):
                raise Refusal("managed_recording_runtime_changed")
            encoder = subprocess.Popen(
                [
                    "/usr/bin/ffmpeg",
                    "-y",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-f",
                    "rawvideo",
                    "-pixel_format",
                    "bgr0",
                    "-video_size",
                    f"{WIDTH}x{HEIGHT}",
                    "-framerate",
                    str(rate),
                    "-i",
                    "pipe:0",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-threads",
                    "1",
                    "-pix_fmt",
                    "yuv420p",
                    "-crf",
                    "21",
                    "-maxrate",
                    "240k",
                    "-bufsize",
                    "480k",
                    "-fs",
                    str(MAX_BYTES - 1048576),
                    "-movflags",
                    "+faststart",
                    "-f",
                    "mp4",
                    path.name,
                ],
                stdin=sampler.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=self.root,
                user=ENCODER_ID if use_uid else None,
                group=ENCODER_ID if use_uid else None,
                extra_groups=[] if use_uid else None,
            )
            sampler.stdout.close()
            os.set_blocking(sampler.stdin.fileno(), False)
            with self.lock:
                capture.data["started_at"] = started
                if not capture.stop.is_set():
                    capture.data["state"] = "recording"
            reason = self._monitor(capture, sampler, encoder, start_time)
            if reason == "authority_lost":
                return {"reason": reason}
            with suppress(BrokenPipeError):
                sampler.stdin.close()
            if encoder.wait(5) != 0:
                raise Refusal("managed_recording_encoder_failed")
            if not current(binding):
                reason = "runtime_stopped"
            probe = subprocess.run(
                [
                    "/usr/bin/ffprobe",
                    "-v",
                    "error",
                    "-f",
                    "mov",
                    "-show_entries",
                    "stream=codec_type,codec_name,width,height,avg_frame_rate:format=duration",
                    "-of",
                    "json",
                    path.name,
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.root,
                timeout=3,
                user=ENCODER_ID if use_uid else None,
                group=ENCODER_ID if use_uid else None,
                extra_groups=[] if use_uid else None,
            )
            video = json.loads(probe.stdout)
            streams = video["streams"]
            duration = float(video["format"]["duration"])
            if (
                len(streams) != 1
                or any(
                    streams[0].get(key) != value
                    for key, value in {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": WIDTH,
                        "height": HEIGHT,
                        "avg_frame_rate": f"{rate}/1",
                    }.items()
                )
                or not 0 < duration <= seconds + 1 / rate
            ):
                raise Refusal("managed_recording_encoder_failed")
            return {
                "started_at": started,
                "duration_seconds": duration,
                "reason": reason,
                "width": WIDTH,
                "height": HEIGHT,
            }
        finally:
            if encoder is not None and encoder.poll() is None:
                encoder.kill()
            if encoder is not None:
                encoder.wait()
            sampler.stdin.close()
            sampler.stdout.close()
            try:
                sampler.wait(LEASE_SECONDS + 1)
            except subprocess.TimeoutExpired:
                # The independent in-runtime watchdog expires even if X11 or
                # the attached Docker transport stalled.
                sampler.kill()
                sampler.wait()

    def _monitor(
        self,
        capture: Capture,
        sampler: subprocess.Popen[bytes],
        encoder: subprocess.Popen[bytes],
        started: float,
    ) -> str:
        assert sampler.stdin is not None
        check_at = started
        while True:
            now = time.monotonic()
            if now >= capture.authority_until:
                return "authority_lost"
            if capture.stop.wait(0.2):
                return capture.reason
            if now >= started + capture.data["limit_seconds"] - 0.1:
                return "duration_limit"
            if sampler.poll() is not None:
                return "capture_ended"
            if encoder.poll() is not None:
                return "encoder_ended"
            if now >= check_at:
                if shutil.disk_usage(self.root).free < FREE_RESERVE:
                    return "storage_limit"
                if not current(capture.binding):
                    return "runtime_stopped"
                with suppress(BlockingIOError, BrokenPipeError):
                    sampler.stdin.write(b"+")
                check_at = now + 2


def current(binding: CaptureBinding) -> bool:
    """Compare the exact container incarnation without taking the MCP lock."""
    try:
        if binding.current_profile() != binding.profile:
            return False
        result = subprocess.run(
            [
                *DOCKER,
                "inspect",
                "--type",
                "container",
                "--format",
                "{{.Id}} {{.Image}} {{.State.StartedAt}} {{.State.Running}}",
                binding.container,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        )
        return result.stdout.split() == [
            binding.container,
            binding.image,
            binding.started,
            "true",
        ]
    except (OSError, Refusal, subprocess.SubprocessError):
        return False
