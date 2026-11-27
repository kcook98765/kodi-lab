from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from kodi_lab.managed import runtime as module
from kodi_lab.managed.runtime import AUDIO_LABEL, AUDIO_OWNER_LABEL, Refusal, Runtime

WORKER_IMAGE = "sha256:" + "b" * 64
WORKER_ID = "a" * 64


@pytest.mark.parametrize(
    ("generation", "worker_running_at_upgrade"), [("omega", True), ("piers", False)]
)
def test_silent_audio_uses_only_a_private_socket_and_can_survive_broker_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generation: str,
    worker_running_at_upgrade: bool,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    native = Runtime(root, audio_image=WORKER_IMAGE, generation=generation)
    socket_path = native.credentials / "audio/native"
    calls: list[tuple[str, ...]] = []
    labels = {AUDIO_LABEL: str(native.target)}
    state = {"Running": worker_running_at_upgrade}
    child = {
        "Id": WORKER_ID,
        "Name": "/" + native.audio_name,
        "Image": WORKER_IMAGE,
        "Config": {"Labels": labels},
        "State": state,
    }
    running = False

    with socket.socket(socket.AF_UNIX) as endpoint:
        # Use the existing directory as the bind cwd to keep the pathname short.
        monkeypatch.chdir(root)

        def docker(*args: str) -> str:
            nonlocal running
            calls.append(args)
            if args[0] == "ps":
                return WORKER_ID if running else ""
            if args[0] == "create":
                assert args[args.index("--network") + 1] == "none"
                assert args[args.index("--user") + 1] == "1000:1000"
                assert args[args.index("--cap-drop") + 1] == "ALL"
                assert "--read-only" in args and "no-new-privileges" in args
                assert "--device" not in args and "--publish" not in args
                owner = next(
                    arg.removeprefix(f"{AUDIO_OWNER_LABEL}=")
                    for arg in args
                    if arg.startswith(f"{AUDIO_OWNER_LABEL}=")
                )
                labels[AUDIO_OWNER_LABEL] = owner
                assert owner == native.audio_owner(WORKER_IMAGE)
                assert args.count("--mount") == 1
                assert args[args.index("--mount") + 1] == (
                    f"type=bind,src={socket_path.parent},dst=/audio"
                )
                assert args[args.index("--entrypoint") + 1 :][:3] == (
                    "/usr/bin/pulseaudio",
                    WORKER_IMAGE,
                    "-n",
                )
                assert [arg for arg in args if arg.startswith("module-")] == [
                    "module-null-sink sink_name=kodi_lab rate=48000",
                    "module-native-protocol-unix socket=/audio/native "
                    "auth-cookie-enabled=no",
                ]
                running = True
                return WORKER_ID
            if args[0] == "start":
                assert args == ("start", WORKER_ID)
                endpoint.bind(str(socket_path.relative_to(root)))
                return WORKER_ID
            if args[0] == "inspect":
                return json.dumps([child])
            return ""

        monkeypatch.setattr(module, "docker", docker)
        environment = native.start_audio()
        assert "PULSE_SERVER=unix:/managed-v2-credentials/audio/native" in environment
        alsa_config = native.credentials / "asound.conf"
        if generation == "piers":
            assert "ALSA_CONFIG_PATH=/managed-v2-credentials/asound.conf" in environment
            assert (
                "ALSA_PLUGIN_DIR=/opt/fdo/lib/x86_64-linux-gnu/alsa-lib" in environment
            )
            assert b"type pulse" in alsa_config.read_bytes()
        else:
            assert len(environment) == 2 and not alsa_config.exists()
        assert socket_path.is_socket()
        assert socket_path.parent.stat().st_mode & 0o777 == 0o700

        # An exact-name collision from a different image must survive Stop.
        child["Image"] = "sha256:" + "c" * 64
        with pytest.raises(Refusal, match="managed_audio_container_identity"):
            native.stop_audio()
        assert not any(call[0] in {"stop", "rm"} for call in calls)
        assert socket_path.is_socket()

        child["Image"] = WORKER_IMAGE
        successor = Runtime(root, audio_image="sha256:" + "d" * 64)
        successor.stop_audio()
        assert calls[-1] == ("rm", "-f", WORKER_ID)
        if worker_running_at_upgrade:
            assert calls[-2] == ("stop", "-t", "3", WORKER_ID)
        else:
            assert not any(call[0] == "stop" for call in calls)
        assert not list(native.credentials.iterdir())


def test_recovery_can_remove_a_late_audio_create_without_starting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    native = Runtime(root, audio_image=WORKER_IMAGE)
    found = False
    calls: list[tuple[str, ...]] = []

    def docker(*args: str) -> str:
        calls.append(args)
        if args[0] == "ps":
            return WORKER_ID if found else ""
        if args[0] == "create":
            raise subprocess.TimeoutExpired("docker", 35)
        if args[0] == "inspect":
            return json.dumps(
                [
                    {
                        "Id": WORKER_ID,
                        "Name": "/" + native.audio_name,
                        "Image": WORKER_IMAGE,
                        "Config": {
                            "Labels": {
                                AUDIO_LABEL: str(native.target),
                                AUDIO_OWNER_LABEL: native.audio_owner(WORKER_IMAGE),
                            }
                        },
                        "State": {"Running": False},
                    }
                ]
            )
        assert args == ("rm", "-f", WORKER_ID)
        return WORKER_ID

    monkeypatch.setattr(module, "docker", docker)
    with pytest.raises(subprocess.TimeoutExpired):
        native.start_audio()
    successor = Runtime(root, audio_image="sha256:" + "d" * 64)
    successor.stop_audio()
    assert not list(native.credentials.iterdir())
    found = True
    successor.stop_audio()
    assert calls[-1] == ("rm", "-f", WORKER_ID)
    assert not any(call[0] in {"start", "stop"} for call in calls)
