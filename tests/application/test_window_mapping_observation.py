"""Window semantics retain provider freshness, authorization, and call budgets."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import ExternalBridgeServiceError
from kodi_lab.application.mcp_administration import McpAdministrationService
from kodi_lab.application.mcp_target_adapters import (
    KodiTargetAdapterError,
    KodiTargetAdapterService,
)
from kodi_lab.application.ui_state import ManagedUiObservationService
from kodi_lab.kodi.external import (
    ExternalEndpointPolicy,
    ExternalProbeError,
    NativeExternalKodiAdapter,
)
from kodi_lab.kodi.ui_state import UiCollection, UiObservation, normalize_collection
from kodi_lab.kodi.ui_state_native import UiObservationError
from kodi_lab.kodi.window_resolver import WindowResolver
from kodi_lab.storage.secrets import ExternalCredentialStore
from tests.application.test_mcp_external_targets import TARGET_ID
from tests.application.test_mcp_external_targets import (
    external_authority as external_authority,
)
from tests.kodi.test_ui_state import movie_collection, native_fixture

type Authority = tuple[
    sessionmaker[Session], McpAdministrationService, KodiTargetAdapterService
]
CUSTOM_ID = 19123


@pytest.fixture
def configured_resolver(tmp_path: Path) -> WindowResolver:
    local = tmp_path / "window-mappings"
    local.mkdir()
    for skin_id, version, name, role in (
        ("skin.test.a", "1.0.0", "Custom search", "navigation"),
        ("skin.test.a", "2.0.0", "Skin settings", "settings"),
        ("skin.test.b", "1.0.0", "Power options", "dialog"),
    ):
        document = {
            "schema_version": 1,
            "kind": "skin",
            "skin_id": skin_id,
            "skin_versions": [version],
            "kodi_versions": [21],
            "source": {
                "repository": "https://example.org/reviewed-skin",
                "revision": "fixture-revision",
                "version": version,
                "urls": ["https://example.org/reviewed-skin/Custom.xml"],
            },
            "entries": [
                {
                    "window_id": CUSTOM_ID,
                    "name": name,
                    "role": role,
                    "verification": "verified",
                }
            ],
        }
        (local / f"{skin_id}-{version}.json").write_text(json.dumps(document))
    resolver = WindowResolver(local_root=local)
    assert not resolver.errors
    return resolver


def _collection(
    *, window_id: int = CUSTOM_ID, name: str | None = None, fresh: bool = False
) -> UiCollection:
    raw = movie_collection().model_dump()
    for key in ("before", "after"):
        raw[key]["window_id"] = window_id
    raw["labels"]["window"] = name
    if fresh:
        raw["labels"].update(control="Fresh result", label="Fresh result", dbid="456")
    return UiCollection.model_validate(raw)


class _Bridge:
    def __init__(
        self,
        initial: UiCollection,
        refreshed: UiCollection | None = None,
        *,
        recollect_error: ExternalBridgeServiceError | None = None,
    ) -> None:
        self.initial = initial
        self.refreshed = refreshed or initial
        self.recollect_error = recollect_error
        self.calls: list[UUID] = []
        self.events: list[str] = []

    def inspect_ui_state(self, target: UUID) -> UiCollection:
        self.events.append("Bridge.inspect_ui_state")
        self.calls.append(target)
        if len(self.calls) > 1:
            if self.recollect_error:
                raise self.recollect_error
            return self.refreshed
        return self.initial


class _Rpc:
    def __init__(
        self,
        *,
        skin_id: str = "skin.test.a",
        version: str = "1.0.0",
        fail_at: int | None = None,
        changed_skin: bool = False,
        changed_version: bool = False,
        foreground_id: int = CUSTOM_ID,
    ) -> None:
        self.skin_id, self.version = skin_id, version
        self.fail_at = fail_at
        self.changed_skin, self.changed_version = changed_skin, changed_version
        self.foreground_id = foreground_id
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.context_calls = 0
        self.events: list[str] = []

    def result(self, method: str, params: dict[str, Any] | None) -> object:
        self.events.append(method)
        self.calls.append((method, params))
        if method == "GUI.GetProperties" and params and "skin" in params["properties"]:
            self.context_calls += 1
            if self.context_calls == self.fail_at:
                raise httpx.ConnectError("optional context unavailable")
            identity = (
                "skin.test.b"
                if self.changed_skin and self.context_calls > 1
                else self.skin_id
            )
            return {
                "skin": {"id": identity},
                "currentwindow": {"id": self.foreground_id},
            }
        if method == "Addons.GetAddonDetails":
            self.context_calls += 1
            if self.context_calls == self.fail_at:
                raise httpx.ReadTimeout("optional context timed out")
            return {
                "addon": {
                    "addonid": self.skin_id,
                    "version": "9.9.9"
                    if self.changed_version and self.context_calls > 2
                    else self.version,
                    "enabled": True,
                }
            }
        if method == "JSONRPC.Ping":
            return "pong"
        if method == "Application.GetProperties":
            return {
                "name": "Kodi",
                "version": {"major": 21, "minor": 3, "tag": "stable"},
            }
        return native_fixture(method, params)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = self.result(body["method"], body.get("params"))
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )


def _service(
    authority: Authority,
    resolver: WindowResolver,
    tmp_path: Path,
    rpc: _Rpc,
    bridge: _Bridge | None,
) -> KodiTargetAdapterService:
    factory, _, _ = authority
    return KodiTargetAdapterService(
        factory,
        managed_status=cast(Any, None),
        managed_inventory=cast(Any, None),
        managed_remote=cast(Any, None),
        managed_authority=cast(Any, None),
        endpoint_policy=ExternalEndpointPolicy(
            allowed_ports=(8080,), test_loopback_enabled=True
        ),
        credential_store=ExternalCredentialStore(
            tmp_path / "mapping-secrets", master_key=None, key_version=1
        ),
        external_bridges=cast(Any, bridge),
        external_transport=httpx.MockTransport(rpc.handle),
        window_resolver=resolver,
    )


@pytest.mark.parametrize("name", [None, "Movies"])
def test_bridge_core_and_reported_names_add_no_context_calls(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
    name: str | None,
) -> None:
    bridge = _Bridge(_collection(window_id=10025, name=name))
    rpc = _Rpc()
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    result = service.inspect_ui_state(TARGET_ID)
    assert bridge.calls == [TARGET_ID]
    assert rpc.calls == []
    assert result.window.id == 10025 and result.window.label == name
    assert result.window.resolution
    assert result.window.resolution.source == ("reported" if name else "core_mapping")
    assert result.selected_item.associated and result.selected_item.dbid == 123


def test_native_core_keeps_existing_identity_checks_and_adds_no_skin_calls(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
) -> None:
    rpc = _Rpc()
    service = _service(external_authority, configured_resolver, tmp_path, rpc, None)
    result = service.inspect_ui_state(TARGET_ID)
    assert [method for method, _ in rpc.calls[:2]] == [
        "JSONRPC.Ping",
        "Application.GetProperties",
    ]
    assert rpc.context_calls == 0
    assert result.window.id == 10025 and result.window.label == "Movies"
    assert result.window.resolution and result.window.resolution.source == "reported"
    assert not result.selected_item.associated


@pytest.mark.parametrize(
    ("skin_id", "version", "name", "role"),
    [
        ("skin.test.a", "1.0.0", "Custom search", "navigation"),
        ("skin.test.a", "2.0.0", "Skin settings", "settings"),
        ("skin.test.b", "1.0.0", "Power options", "dialog"),
    ],
)
def test_active_skin_and_exact_version_select_semantics_on_fresh_collection(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
    skin_id: str,
    version: str,
    name: str,
    role: str,
) -> None:
    bridge = _Bridge(_collection(), _collection(fresh=True))
    rpc = _Rpc(skin_id=skin_id, version=version)
    bridge.events = rpc.events
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    result = service.inspect_ui_state(TARGET_ID)
    assert bridge.calls == [TARGET_ID, TARGET_ID]
    assert [method for method, _ in rpc.calls] == [
        "GUI.GetProperties",
        "Addons.GetAddonDetails",
        "Addons.GetAddonDetails",
        "GUI.GetProperties",
    ]
    assert rpc.events == [
        "Bridge.inspect_ui_state",
        "GUI.GetProperties",
        "Addons.GetAddonDetails",
        "Bridge.inspect_ui_state",
        "Addons.GetAddonDetails",
        "GUI.GetProperties",
    ]
    assert result.window.id == CUSTOM_ID and result.window.label is None
    resolution = result.window.resolution
    assert resolution and resolution.source == "skin_mapping"
    assert resolution.status == "verified" and resolution.name == name
    assert resolution.role == role
    assert (resolution.skin_id, resolution.skin_version) == (skin_id, version)
    assert result.focus.label == "Fresh result"
    assert result.selected_item.label == "Fresh result"
    assert result.selected_item.dbid == 456 and result.selected_item.associated
    assert result.summary.startswith(name + ". Focus is on Fresh result")
    assert UiObservation.model_validate_json(result.model_dump_json()) == result


def test_custom_foreground_dialog_keeps_base_window_separate(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
) -> None:
    raw = _collection(window_id=10000).model_dump()
    for key in ("before", "after"):
        raw[key]["dialog_id"] = CUSTOM_ID
        raw[key]["is_container"] = False
    bridge = _Bridge(UiCollection.model_validate(raw))
    rpc = _Rpc(skin_id="skin.test.b")
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    result = service.inspect_ui_state(TARGET_ID)
    assert result.window.id == 10000 and result.window.label is None
    assert result.window.resolution and result.window.resolution.name == "Home"
    assert result.dialog.id == CUSTOM_ID and result.dialog.label is None
    assert result.dialog.active is True
    assert result.dialog.resolution and result.dialog.resolution.name == "Power options"
    assert result.dialog.resolution.role == "dialog"
    assert result.summary.startswith("Power options dialog open.")
    assert not result.selected_item.associated


@pytest.mark.parametrize(
    ("window_id", "label"),
    [(CUSTOM_ID, "Native custom name"), (98765, None)],
)
def test_useful_custom_name_and_no_candidate_skip_optional_remote_calls(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
    window_id: int,
    label: str | None,
) -> None:
    bridge = _Bridge(_collection(window_id=window_id, name=label))
    rpc = _Rpc()
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    result = service.inspect_ui_state(TARGET_ID)
    assert bridge.calls == [TARGET_ID] and rpc.calls == []
    assert result.window.id == window_id and result.window.label == label
    assert result.window.resolution
    assert result.window.resolution.source == ("reported" if label else "unknown")


@pytest.mark.parametrize(
    ("skin_id", "version"),
    [("skin.test.unrelated", "1.0.0"), ("skin.test.a", "9.9.9")],
)
def test_unmapped_skin_identity_or_version_skips_recollection(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
    skin_id: str,
    version: str,
) -> None:
    bridge = _Bridge(_collection(), _collection(fresh=True))
    rpc = _Rpc(skin_id=skin_id, version=version)
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    result = service.inspect_ui_state(TARGET_ID)
    assert bridge.calls == [TARGET_ID]
    assert rpc.context_calls == (1 if skin_id == "skin.test.unrelated" else 2)
    assert result.focus.label == "Blade Runner" and result.selected_item.dbid == 123
    assert result.window.resolution and result.window.resolution.source == "unknown"


@pytest.mark.parametrize("fail_at", [1, 3, 4])
def test_optional_http_context_failure_preserves_initial_bridge_observation(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
    fail_at: int,
) -> None:
    bridge = _Bridge(_collection(), _collection(fresh=True))
    rpc = _Rpc(fail_at=fail_at)
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    result = service.inspect_ui_state(TARGET_ID)
    assert len(bridge.calls) == (1 if fail_at == 1 else 2)
    assert result.window.id == CUSTOM_ID and result.window.label is None
    assert result.focus.label == "Blade Runner"
    assert result.selected_item.dbid == 123 and result.selected_item.associated
    assert result.window.resolution and result.window.resolution.source == "unknown"
    assert result.window.resolution.skin_id is None


@pytest.mark.parametrize("change", ["skin", "version", "foreground"])
def test_unstable_context_does_not_attach_semantics_to_old_collection(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
    change: str,
) -> None:
    refreshed = _collection(
        window_id=CUSTOM_ID + 1 if change == "foreground" else CUSTOM_ID, fresh=True
    )
    bridge = _Bridge(_collection(), refreshed)
    rpc = _Rpc(changed_skin=change == "skin", changed_version=change == "version")
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    result = service.inspect_ui_state(TARGET_ID)
    assert len(bridge.calls) == 2
    assert result.window.id == CUSTOM_ID
    assert result.focus.label == "Blade Runner" and result.selected_item.dbid == 123
    assert result.window.resolution and result.window.resolution.source == "unknown"


@pytest.mark.parametrize("code", ["bridge_identity_mismatch", "permission_denied"])
def test_bridge_recollection_authority_failures_propagate(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
    code: str,
) -> None:
    bridge = _Bridge(_collection(), recollect_error=ExternalBridgeServiceError(code))
    rpc = _Rpc()
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    with pytest.raises(KodiTargetAdapterError, match=code):
        service.inspect_ui_state(TARGET_ID)
    assert bridge.calls == [TARGET_ID, TARGET_ID]
    assert rpc.context_calls == 2


@pytest.mark.parametrize("code", ["authentication_failed", "identity_mismatch"])
@pytest.mark.parametrize("error_type", [UiObservationError, ExternalProbeError])
def test_native_recollection_authority_failures_propagate(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    error_type: type[RuntimeError],
) -> None:
    calls = []

    def collect(_self: NativeExternalKodiAdapter) -> UiObservation:
        calls.append(True)
        if len(calls) > 1:
            raise error_type(code)
        return normalize_collection(_collection())

    monkeypatch.setattr(NativeExternalKodiAdapter, "inspect_ui_state", collect)
    rpc = _Rpc()
    service = _service(external_authority, configured_resolver, tmp_path, rpc, None)
    with pytest.raises(KodiTargetAdapterError, match=code):
        service.inspect_ui_state(TARGET_ID)
    assert len(calls) == 2 and rpc.context_calls == 2


def test_managed_bridge_uses_same_configured_resolver_and_freshness_bracket(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _, _ = external_authority
    bridge = _Bridge(_collection(), _collection(fresh=True))
    rpc = _Rpc()
    service = ManagedUiObservationService(
        factory,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        bridges=cast(
            Any,
            SimpleNamespace(collect=lambda *args: bridge.inspect_ui_state(TARGET_ID)),
        ),
        window_resolver=configured_resolver,
    )
    profile = SimpleNamespace(kodi_release=SimpleNamespace(canonical="21.3"))
    observed = SimpleNamespace(
        container_id="a" * 64,
        runtime_revision_id="runtime-1",
        ipv4_address="127.0.0.1",
        jsonrpc_port=8080,
    )
    monkeypatch.setattr(service, "_admit", lambda target: (observed, profile))
    monkeypatch.setattr(service, "_native_call", lambda runtime, execution: rpc.result)
    verified = []
    monkeypatch.setattr(
        service,
        "_verify_current",
        lambda target, runtime: verified.append((target, runtime)),
    )
    result = service.inspect(TARGET_ID)
    assert result.window.resolution and result.window.resolution.name == "Custom search"
    assert result.focus.label == "Fresh result" and result.selected_item.associated
    assert bridge.calls == [TARGET_ID, TARGET_ID] and rpc.context_calls == 4
    assert verified == [(TARGET_ID, observed)]


def test_repeated_bridge_observation_brackets_first_collection(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
) -> None:
    bridge = _Bridge(_collection(), _collection(fresh=True))
    rpc = _Rpc()
    bridge.events = rpc.events
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    service.inspect_ui_state(TARGET_ID)
    rpc.events.clear()
    result = service.inspect_ui_state(TARGET_ID)
    assert len(bridge.calls) == 3 and rpc.context_calls == 8
    assert rpc.events == [
        "GUI.GetProperties",
        "Addons.GetAddonDetails",
        "Bridge.inspect_ui_state",
        "Addons.GetAddonDetails",
        "GUI.GetProperties",
    ]
    assert result.window.resolution and result.window.resolution.name == "Custom search"
    assert result.selected_item.associated and result.selected_item.dbid == 456


def test_warm_bridge_hint_cannot_reuse_previous_skin_or_version(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
) -> None:
    bridge = _Bridge(_collection())
    rpc = _Rpc()
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    service.inspect_ui_state(TARGET_ID)
    rpc.skin_id = "skin.test.b"
    result = service.inspect_ui_state(TARGET_ID)
    assert len(bridge.calls) == 3
    assert result.window.resolution and result.window.resolution.name == "Power options"
    rpc.version = "9.9.9"
    result = service.inspect_ui_state(TARGET_ID)
    assert result.window.resolution and result.window.resolution.name is None
    assert not service._window_hints.enabled(
        TARGET_ID,
        service._skin_hint_identity(
            service._external_snapshot(TARGET_ID, load_secret=True)
        ),
    )


def test_warm_context_failure_collects_once_and_forgets_hint(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
) -> None:
    bridge = _Bridge(_collection(), _collection(fresh=True))
    rpc = _Rpc()
    service = _service(external_authority, configured_resolver, tmp_path, rpc, bridge)
    service.inspect_ui_state(TARGET_ID)
    rpc.fail_at = 5  # first optional context request on the warm observation
    result = service.inspect_ui_state(TARGET_ID)
    assert len(bridge.calls) == 3 and rpc.context_calls == 5
    assert result.selected_item.associated and result.selected_item.dbid == 456
    assert result.window.resolution and result.window.resolution.name is None


@pytest.mark.parametrize("provider", ["managed", "external"])
def test_custom_base_behind_core_dialog_stays_resolved_across_warm_requests(
    external_authority: Authority,
    configured_resolver: WindowResolver,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    raw = _collection().model_dump()
    for key in ("before", "after"):
        raw[key]["dialog_id"] = 10100
        raw[key]["is_container"] = False
    bridge = _Bridge(UiCollection.model_validate(raw))
    rpc = _Rpc(foreground_id=10100)
    observe: Callable[[], UiObservation]
    if provider == "external":
        external = _service(
            external_authority, configured_resolver, tmp_path, rpc, bridge
        )
        observe = partial(external.inspect_ui_state, TARGET_ID)
    else:
        factory, _, _ = external_authority
        managed = ManagedUiObservationService(
            factory,
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            bridges=cast(
                Any,
                SimpleNamespace(
                    collect=lambda *args: bridge.inspect_ui_state(TARGET_ID)
                ),
            ),
            window_resolver=configured_resolver,
        )
        profile = SimpleNamespace(kodi_release=SimpleNamespace(canonical="21.3"))
        observed = SimpleNamespace(
            container_id="a" * 64,
            runtime_revision_id="runtime-1",
            ipv4_address="127.0.0.1",
            jsonrpc_port=8080,
        )
        monkeypatch.setattr(managed, "_admit", lambda target: (observed, profile))
        monkeypatch.setattr(
            managed, "_native_call", lambda runtime, execution: rpc.result
        )
        monkeypatch.setattr(managed, "_verify_current", lambda target, runtime: None)
        observe = partial(managed.inspect, TARGET_ID)

    results = [observe(), observe(), observe()]
    assert results[0] == results[1] == results[2]
    assert len(bridge.calls) == 4 and rpc.context_calls == 12
    for result in results:
        assert result.window.id == CUSTOM_ID and result.window.label is None
        assert result.window.resolution
        assert result.window.resolution.name == "Custom search"
        assert result.window.resolution.source == "skin_mapping"
        assert result.dialog.id == 10100 and result.dialog.active is True
        assert result.dialog.resolution
        assert result.dialog.resolution.source == "core_mapping"
        assert result.dialog.resolution.status == "verified"
        assert not result.selected_item.associated

    # A scheduling hint does not authorize a different active skin or version.
    rpc.skin_id = "skin.test.unrelated"
    unrelated = observe()
    assert len(bridge.calls) == 5 and rpc.context_calls == 13
    assert unrelated.window.resolution and unrelated.window.resolution.name is None
    assert unrelated.dialog.resolution
    assert unrelated.dialog.resolution.source == "core_mapping"
    rpc.skin_id, rpc.version = "skin.test.a", "9.9.9"
    incompatible = observe()
    assert len(bridge.calls) == 6 and rpc.context_calls == 15
    assert (
        incompatible.window.resolution and incompatible.window.resolution.name is None
    )
    assert incompatible.dialog.resolution
    assert incompatible.dialog.resolution.source == "core_mapping"
