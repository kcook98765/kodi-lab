from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from kodi_lab.managed import runtime as module
from kodi_lab.managed.runtime import AUDIO_LABEL, AUDIO_RUN_LABEL, Refusal, Runtime

WORKER_IMAGE = "sha256:" + "b" * 64
WORKER_ID = "a" * 64


@pytest.mark.parametrize("worker_running_at_upgrade", [True, False])
def test_silent_audio_uses_only_a_private_socket_and_can_survive_broker_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker_running_at_upgrade: bool
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    native = Runtime(root, audio_image=WORKER_IMAGE)
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
            if args[0] == "run":
                assert args[args.index("--network") + 1] == "none"
                assert args[args.index("--user") + 1] == "1000:1000"
                assert args[args.index("--cap-drop") + 1] == "ALL"
                assert "--read-only" in args and "no-new-privileges" in args
                assert "--device" not in args and "--publish" not in args
                launch = next(
                    arg.removeprefix(f"{AUDIO_RUN_LABEL}=")
                    for arg in args
                    if arg.startswith(f"{AUDIO_RUN_LABEL}=")
                )
                labels[AUDIO_RUN_LABEL] = launch
                assert (native.credentials / "audio-worker").read_text() == (
                    f"{WORKER_IMAGE} {launch}"
                )
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
                endpoint.bind(str(socket_path.relative_to(root)))
                running = True
                return WORKER_ID
            if args[0] == "inspect":
                return json.dumps([child])
            return ""

        monkeypatch.setattr(module, "docker", docker)
        assert native.start_audio() == [
            "--env",
            "PULSE_SERVER=unix:/managed-v2-credentials/audio/native",
        ]
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
        assert calls[-1] == ("rm", WORKER_ID)
        if worker_running_at_upgrade:
            assert calls[-2] == ("stop", "-t", "3", WORKER_ID)
        else:
            assert not any(call[0] == "stop" for call in calls)
        assert not list(native.credentials.iterdir())
