import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest

from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import (
    ProcessObservationKind,
    PythonBytecodePolicy,
    ReadinessProbeKind,
    RuntimeExecutionProfile,
    RuntimeKind,
)
from kodi_lab.kodi.remote import (
    BrowserRemoteAction,
    KodiBrowserRemoteAdapter,
    RemoteActionAmbiguousError,
    RemoteKodiRejectedError,
    RemoteNoActivePlayerError,
    fixed_request_mapping,
)

INVOCATION_ID = UUID("00000000-0000-4000-8000-000000000501")
REVISION_ID = UUID("3a000000-0000-4000-8000-000000000006")


@dataclass
class RecordingTransport:
    active_players: list[dict[str, object]] = field(
        default_factory=lambda: [{"playerid": 1, "type": "video"}]
    )
    reject_action: bool = False
    ambiguous_action: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)

    def post(self, **kwargs: Any) -> bytes:
        document = json.loads(kwargs["body"])
        self.requests.append(document)
        if document["method"] == "Player.GetActivePlayers":
            result: object = self.active_players
        elif self.ambiguous_action:
            raise RemoteActionAmbiguousError("uncertain")
        elif self.reject_action:
            return json.dumps(
                {"jsonrpc": "2.0", "id": document["id"], "error": {"code": -1}}
            ).encode()
        else:
            result = {
                "Player.PlayPause": {"speed": 1},
                "Application.SetVolume": 50,
                "Application.SetMute": True,
            }.get(document["method"], "OK")
        return json.dumps(
            {"jsonrpc": "2.0", "id": document["id"], "result": result}
        ).encode()


def adapter(transport: RecordingTransport) -> KodiBrowserRemoteAdapter:
    observation = ObserveManagedKodiRuntimeResult(
        managed_instance_id="00000000-0000-4000-8000-000000000101",
        runtime_revision_id=str(REVISION_ID),
        container_id="a" * 64,
        runtime_state=ContainerRuntimeState.RUNNING,
        process_state=KodiProcessState.RUNNING,
        ipv4_address="10.250.0.10",
        jsonrpc_port=8080,
    )
    profile = RuntimeExecutionProfile(
        runtime_revision_id=REVISION_ID,
        runtime_kind=RuntimeKind.KODI,
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
        expected_application_name="Kodi",
        expected_kodi_version="21.3",
        expected_version_tag="stable",
        expected_prerelease_ordinal=None,
        jsonrpc_port=8080,
        jsonrpc_path="/jsonrpc",
        process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
        readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
        readiness_timeout_seconds=90,
        poll_interval_seconds=1,
        http_timeout_seconds=3,
    )
    return KodiBrowserRemoteAdapter(observation, profile, transport=transport)


@pytest.mark.parametrize("action", list(BrowserRemoteAction))
def test_every_closed_action_uses_only_its_fixed_mapping(
    action: BrowserRemoteAction,
) -> None:
    transport = RecordingTransport()
    adapter(transport).dispatch(action, INVOCATION_ID)
    action_request = transport.requests[-1]
    method, mapping = fixed_request_mapping()[action]
    assert action_request["method"] == method
    assert set(action_request) <= {"jsonrpc", "id", "method", "params"}
    if mapping is None:
        assert "params" not in action_request
    elif mapping == "derived_playerid":
        assert action_request["params"] == {"playerid": 1}
    elif mapping == "derived_playerid_previous":
        assert action_request["params"] == {"playerid": 1, "to": "previous"}
    elif mapping == "derived_playerid_next":
        assert action_request["params"] == {"playerid": 1, "to": "next"}
    else:
        assert action_request["params"] == mapping


def test_player_selection_is_server_derived_and_deterministic() -> None:
    transport = RecordingTransport(
        active_players=[
            {"playerid": 0, "type": "audio"},
            {"playerid": 1, "type": "video"},
            {"playerid": 2, "type": "picture"},
        ]
    )
    adapter(transport).dispatch(BrowserRemoteAction.STOP, INVOCATION_ID)
    assert transport.requests[-1]["params"] == {"playerid": 1}


def test_no_active_player_sends_no_control_action() -> None:
    transport = RecordingTransport(active_players=[])
    with pytest.raises(RemoteNoActivePlayerError):
        adapter(transport).dispatch(BrowserRemoteAction.PLAY_PAUSE, INVOCATION_ID)
    assert [item["method"] for item in transport.requests] == [
        "Player.GetActivePlayers"
    ]


def test_kodi_rejection_and_ambiguous_timeout_never_retry() -> None:
    rejected = RecordingTransport(reject_action=True)
    with pytest.raises(RemoteKodiRejectedError):
        adapter(rejected).dispatch(BrowserRemoteAction.SELECT, INVOCATION_ID)
    assert len(rejected.requests) == 1

    ambiguous = RecordingTransport(ambiguous_action=True)
    with pytest.raises(RemoteActionAmbiguousError):
        adapter(ambiguous).dispatch(BrowserRemoteAction.SELECT, INVOCATION_ID)
    assert len(ambiguous.requests) == 1
