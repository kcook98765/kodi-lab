from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier, Event
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.external_bridges import (
    ExternalBridgeService,
    ExternalBridgeServiceError,
)
from kodi_lab.application.mcp_administration import (
    READ_ONLY_INSPECTION_PRESET_ID,
    McpAdministrationService,
    McpAuthenticationService,
    McpPrincipal,
)
from kodi_lab.application.mcp_target_adapters import KodiTargetAdapterService
from kodi_lab.application.production_inspection import PRODUCTION_VISUAL_PRESET_ID
from kodi_lab.application.production_screenshots import (
    ProductionScreenshotError,
    ProductionScreenshotService,
)
from kodi_lab.application.screenshot_preferences import (
    ScreenshotPreferenceError,
    ScreenshotPreferenceService,
)
from kodi_lab.kodi.bridge import ExternalBridgeError
from kodi_lab.kodi.external import ExternalEndpointPolicy
from kodi_lab.kodi.remote import BrowserRemoteAction
from kodi_lab.mcp.authorization import McpAuthorizationError, McpAuthorizationService
from kodi_lab.mcp.registry import build_mcp_tool_registry
from kodi_lab.mcp.tools import McpTargetToolService
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.metadata import Base
from kodi_lab.storage.models import ExternalBridgeRequestReceiptRow
from tests.application.test_external_bridges import _decode_token, _seed, _service
from tests.application.test_production_bridges import (
    _pair_production_bridge,
    _production_target,
)
from tests.application.test_production_capability_amendments import (
    CURRENT_TARGET_ID,
    _approval,
    _current_state,
    _seed_paired_bridge_and_downstream_authority,
)
from tests.application.test_production_navigation import _authority_environment
from tests.kodi.fake_external_bridge import FakeExternalBridge


@dataclass
class _Environment:
    sessions: sessionmaker[Session]
    bridges: ExternalBridgeService
    preference: ScreenshotPreferenceService
    fake: FakeExternalBridge
    target_id: UUID

    def set_enabled(self, enabled: bool) -> None:
        current = self.preference.read(self.target_id)
        self.preference.update(
            self.target_id,
            enabled=enabled,
            expected_revision=current.revision,
            administrator_reference="test_admin",
        )


@contextmanager
def _paired(tmp_path: Path, production: bool) -> Iterator[_Environment]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'preferences.db'}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    if production:
        target_id, _ = _production_target(sessions, tmp_path)
    else:
        target_id = uuid4()
        _seed(sessions, target_id)
    try:
        with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
            if production:
                bridges = _pair_production_bridge(sessions, tmp_path, target_id, fake)
            else:
                bridges = _service(sessions, tmp_path / "bridge-secrets", fake.port)
                token = bridges.generate_pairing_token(target_id, bridge_port=fake.port)
                fake.token = _decode_token(token.token)
                bridges.attempt_pairing(target_id)
                bridges.confirm_pairing(target_id)
            yield _Environment(
                sessions,
                bridges,
                ScreenshotPreferenceService(sessions),
                fake,
                target_id,
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("production", [False, True])
def test_off_blocks_capture_and_on_preserves_authenticated_pairing(
    tmp_path: Path, production: bool
) -> None:
    with _paired(tmp_path, production) as env:
        before = env.bridges.status(env.target_id)
        assert env.preference.read(env.target_id).enabled
        env.set_enabled(False)
        # Technical availability and trust do not depend on capture preference.
        assert env.bridges.screenshot_available(env.target_id)
        with pytest.raises(ExternalBridgeServiceError, match="screenshots_disabled"):
            env.bridges.capture_screenshot(env.target_id, request_id=uuid4())
        assert env.fake.capture_count == 0
        env.set_enabled(True)
        assert env.bridges.status(env.target_id) == before
        assert env.bridges.capture_screenshot(env.target_id, request_id=uuid4()).png
        assert env.fake.capture_count == 1
        assert env.preference.read(env.target_id).revision == 3


@pytest.mark.parametrize("production", [False, True])
@pytest.mark.parametrize("reenable", [False, True])
def test_concurrent_change_suppresses_in_flight_pixels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, production: bool, reenable: bool
) -> None:
    with _paired(tmp_path, production) as env:
        arrived, release = Event(), Event()
        client = env.bridges._client
        original = client.capture_screenshot

        def paused(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            arrived.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(client, "capture_screenshot", paused)
        request_id = uuid4()
        before = env.bridges.status(env.target_id)
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(
                env.bridges.capture_screenshot, env.target_id, request_id=request_id
            )
            try:
                assert arrived.wait(5)
                env.set_enabled(False)
                if reenable:
                    env.set_enabled(True)
            finally:
                release.set()
            with pytest.raises(
                ExternalBridgeServiceError,
                match="screenshot_preference_changed"
                if reenable
                else "screenshots_disabled",
            ):
                future.result(timeout=5)
        assert env.fake.capture_count == 1
        assert env.bridges.status(env.target_id) == before
        if production:
            assert (
                env.target_id,
                request_id,
            ) not in env.bridges._production_pixel_cache


def test_disabled_and_old_revision_replay_never_returns_cached_pixels(
    tmp_path: Path,
) -> None:
    with _paired(tmp_path, True) as env:
        service = ProductionScreenshotService(env.sessions, env.bridges)
        client = McpAdministrationService(env.sessions).create_client(
            display_name="Receipt reader", preset_id=PRODUCTION_VISUAL_PRESET_ID
        )
        request: dict[str, Any] = dict(
            client_id=client.id,
            target_id=env.target_id,
            request_id=uuid4(),
            identity_generation=1,
            bridge_trust_revision=1,
        )
        assert service.capture(**request).png
        env.set_enabled(False)
        with pytest.raises(ProductionScreenshotError, match="screenshots_disabled"):
            service.capture(**request)
        env.set_enabled(True)
        with pytest.raises(
            ProductionScreenshotError, match="screenshot_preference_changed"
        ):
            service.capture(**request)
        with pytest.raises(
            ProductionScreenshotError, match="screenshot_replay_unavailable"
        ):
            service.capture(**request)
        assert env.fake.capture_count == 1


def test_policy_change_during_mcp_receipt_completion_suppresses_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _paired(tmp_path, True) as env:
        service = ProductionScreenshotService(env.sessions, env.bridges)
        client = McpAdministrationService(env.sessions).create_client(
            display_name="Receipt reader", preset_id=PRODUCTION_VISUAL_PRESET_ID
        )
        complete = service._complete

        def toggle_after_completion(*args: Any, **kwargs: Any) -> None:
            complete(*args, **kwargs)
            env.set_enabled(False)
            env.set_enabled(True)

        monkeypatch.setattr(service, "_complete", toggle_after_completion)
        with pytest.raises(
            ProductionScreenshotError, match="screenshot_preference_changed"
        ):
            service.capture(
                client_id=client.id,
                target_id=env.target_id,
                request_id=uuid4(),
                identity_generation=1,
                bridge_trust_revision=1,
            )
        assert not env.bridges._production_pixel_cache


@pytest.mark.parametrize("production", [False, True])
def test_mcp_authorization_and_final_publication_require_current_preference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, production: bool
) -> None:
    with _paired(tmp_path, production) as env:
        admin = McpAdministrationService(env.sessions)
        preset = (
            PRODUCTION_VISUAL_PRESET_ID
            if production
            else READ_ONLY_INSPECTION_PRESET_ID
        )
        client = admin.create_client(display_name="Reader", preset_id=preset)
        token = admin.issue_credential(client.id)
        principal = McpAuthenticationService(env.sessions).authenticate(token.token)
        assert isinstance(principal, McpPrincipal)
        adapters = KodiTargetAdapterService(
            env.sessions,
            managed_status=cast(Any, None),
            managed_inventory=cast(Any, None),
            managed_remote=cast(Any, None),
            managed_authority=cast(Any, None),
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(8080,), test_loopback_enabled=True
            ),
            credential_store=cast(Any, None),
            external_bridges=env.bridges,
        )
        auth = McpAuthorizationService(
            env.sessions, build_mcp_tool_registry(), targets=adapters
        )
        tools = McpTargetToolService(
            env.sessions,
            auth,
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            target_adapters=adapters,
            production_screenshots=ProductionScreenshotService(
                env.sessions, env.bridges
            ),
        )
        # Enabled preference supplies neither a grant nor production scopes.
        with pytest.raises(McpAuthorizationError):
            tools.screenshot_capture(
                principal, target_id=env.target_id, request_id=uuid4()
            )
        admin.assign_target_preset(env.target_id, preset)
        admin.grant_target(client.id, env.target_id)
        if production:
            with pytest.raises(McpAuthorizationError):
                tools.screenshot_capture(
                    principal, target_id=env.target_id, request_id=uuid4()
                )
            admin.grant_production_inspection_scope(
                client.id, env.target_id, identity_generation=1
            )
            admin.grant_production_visual_scope(
                client.id, env.target_id, identity_generation=1, bridge_trust_revision=1
            )
        env.set_enabled(False)
        with pytest.raises(McpAuthorizationError, match="screenshots_disabled"):
            tools.screenshot_capture(
                principal, target_id=env.target_id, request_id=uuid4()
            )
        env.set_enabled(True)
        completed_request = uuid4()
        assert tools.screenshot_capture(
            principal, target_id=env.target_id, request_id=completed_request
        ).png

        def completed_then_toggled() -> bytes:
            env.set_enabled(False)
            env.set_enabled(True)
            return b"old pixels"

        with pytest.raises(
            McpAuthorizationError, match="screenshot_preference_changed"
        ):
            auth.invoke_target(
                principal, "screenshot_capture", env.target_id, completed_then_toggled
            )
        # Exercise actual tool publication and the production cache discard callback.
        screenshots = tools._production_screenshots
        if screenshots is not None:
            monkeypatch.setattr(screenshots, "_monotonic", lambda: 1000000000000.0)
        authorized = tools._screenshot_capture_authorized

        def toggle_before_publication(*args: Any, **kwargs: Any) -> Any:
            result = authorized(*args, **kwargs)
            env.set_enabled(False)
            env.set_enabled(True)
            return result

        monkeypatch.setattr(
            tools, "_screenshot_capture_authorized", toggle_before_publication
        )
        with pytest.raises(
            McpAuthorizationError, match="screenshot_preference_changed"
        ):
            tools.screenshot_capture(
                principal, target_id=env.target_id, request_id=uuid4()
            )
        if production:
            # Older cache entries cannot publish through replay after an off/on cycle.
            assert screenshots is not None
            count = env.fake.capture_count
            with pytest.raises(ProductionScreenshotError):
                screenshots.capture(
                    client_id=client.id,
                    target_id=env.target_id,
                    request_id=completed_request,
                    identity_generation=1,
                    bridge_trust_revision=1,
                )
            assert env.fake.capture_count == count
        admin.remove_target_grant(client.id, env.target_id)
        before = _rows(env.sessions)
        env.set_enabled(False)
        env.set_enabled(True)
        assert _rows(env.sessions) == before
        with pytest.raises(McpAuthorizationError):
            tools.screenshot_capture(
                principal, target_id=env.target_id, request_id=uuid4()
            )


def _rows(sessions: sessionmaker[Session]) -> dict[str, list[tuple[Any, ...]]]:
    with sessions() as session:
        return {
            name: sorted(
                (
                    tuple(row)
                    for row in session.execute(text(f'SELECT * FROM "{name}"'))
                ),
                key=repr,
            )
            for name in inspect(session.get_bind()).get_table_names()
            if name != "external_screenshot_preferences"
        }


def test_off_on_preserves_credentials_scopes_acknowledgements_and_approvals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, amendments, administration = _current_state(monkeypatch, tmp_path)
    amendments.amend_production_capability_maximum(
        CURRENT_TARGET_ID,
        operation="add",
        capability="screenshot",
        expected_configured_capabilities=frozenset(
            {"target_inspect", "addon_inventory"}
        ),
        expected_policy_revision=8,
    )
    secret, scopes, client = _seed_paired_bridge_and_downstream_authority(
        sessions, amendments, administration
    )
    with sessions.begin() as session:
        for state in ("pending", "approved"):
            session.add(
                _approval(
                    approval_id=uuid4(),
                    client_id=client,
                    transient_scope_id=scopes[2],
                    state=state,
                )
            )
    preference = ScreenshotPreferenceService(sessions)
    before = _rows(sessions)
    for enabled in (True, False, True):
        preference.update(
            CURRENT_TARGET_ID,
            enabled=enabled,
            expected_revision=preference.read(CURRENT_TARGET_ID).revision,
            administrator_reference="test",
        )
        assert _rows(sessions) == before
        assert (
            amendments._bridge_secrets.read(
                CURRENT_TARGET_ID, secret, "request_authentication"
            )
            == b"s" * 32
        )

    # Valid standing navigation and its exact native HTTP acknowledgement remain
    # effective, rather than merely retaining their persisted records.
    nav = _authority_environment(tmp_path / "navigation")
    preference = ScreenshotPreferenceService(nav.factory)
    authority = nav.authorization.authorize_production_navigation(
        nav.principal, nav.target_id, BrowserRemoteAction.HOME
    )
    before = _rows(nav.factory)
    for enabled in (False, True):
        preference.update(
            nav.target_id,
            enabled=enabled,
            expected_revision=preference.read(nav.target_id).revision,
            administrator_reference="test",
        )
        assert _rows(nav.factory) == before
        assert (
            nav.authorization.authorize_production_navigation(
                nav.principal, nav.target_id, BrowserRemoteAction.HOME
            )
            == authority
        )


def test_cas_and_database_guards_prevent_revision_reset(tmp_path: Path) -> None:
    with _paired(tmp_path, False) as env:
        barrier = Barrier(2)

        def update() -> str:
            barrier.wait(timeout=5)
            try:
                env.preference.update(
                    env.target_id,
                    enabled=False,
                    expected_revision=1,
                    administrator_reference="test",
                )
                return "updated"
            except ScreenshotPreferenceError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: update(), range(2)))
        assert sorted(results) == ["screenshot_preference_conflict", "updated"]
        for sql in (
            "UPDATE external_screenshot_preferences SET revision=1",
            "DELETE FROM external_screenshot_preferences",
        ):
            with pytest.raises(IntegrityError), env.sessions.begin() as session:
                session.execute(text(sql))
        assert env.preference.read(env.target_id).revision == 2


@pytest.mark.parametrize("production", [False, True])
def test_reenable_does_not_restore_revoked_bridge_trust(
    tmp_path: Path, production: bool
) -> None:
    with _paired(tmp_path, production) as env:
        env.bridges.revoke(env.target_id)
        before = env.bridges.status(env.target_id)
        env.set_enabled(False)
        env.set_enabled(True)
        assert env.bridges.status(env.target_id) == before
        with pytest.raises(ExternalBridgeServiceError, match="bridge_not_paired"):
            env.bridges.capture_screenshot(env.target_id, request_id=uuid4())
        assert env.fake.capture_count == 0


@pytest.mark.parametrize("production", [False, True])
def test_pending_receipt_after_process_loss_cannot_adopt_new_preference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, production: bool
) -> None:
    with _paired(tmp_path, production) as env:
        request_id = uuid4()

        def process_loss(*args: Any, **kwargs: Any) -> None:
            raise SystemExit("simulated process loss before receipt completion")

        monkeypatch.setattr(env.bridges, "_complete_receipt", process_loss)
        with pytest.raises(SystemExit):
            env.bridges.capture_screenshot(env.target_id, request_id=request_id)
        assert env.fake.capture_count == 1
        env.set_enabled(False)
        env.set_enabled(True)
        restarted = ExternalBridgeService(
            env.sessions,
            endpoint_policy=ExternalEndpointPolicy(
                allowed_ports=(env.fake.port,), test_loopback_enabled=True
            ),
            secret_store=env.bridges._secrets,
        )
        with pytest.raises(
            ExternalBridgeServiceError, match="screenshot_replay_unavailable"
        ):
            restarted.capture_screenshot(env.target_id, request_id=request_id)
        assert env.fake.capture_count == 1
        key = {"target_id": str(env.target_id), "request_id": str(request_id)}
        with env.sessions() as session:
            pending = session.get(ExternalBridgeRequestReceiptRow, key)
            assert pending is not None
            assert pending.state == "pending"
            assert pending.capture_preference_revision == 1
        with pytest.raises(IntegrityError), env.sessions.begin() as session:
            session.execute(
                text(
                    "UPDATE external_bridge_request_receipts "
                    "SET capture_preference_revision=3"
                )
            )
        fresh = restarted.capture_screenshot(env.target_id, request_id=uuid4())
        assert fresh.preference_revision == 3
        with env.sessions() as session:
            receipt = session.get(
                ExternalBridgeRequestReceiptRow,
                {"target_id": str(env.target_id), "request_id": str(fresh.capture_id)},
            )
            assert receipt is not None and receipt.capture_preference_revision == 3


def test_disable_after_uncertain_dispatch_prevents_development_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _paired(tmp_path, False) as env:
        attempts = 0

        def uncertain(*args: Any, **kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            env.set_enabled(False)
            raise ExternalBridgeError("bridge_unreachable")

        monkeypatch.setattr(env.bridges._client, "capture_screenshot", uncertain)
        with pytest.raises(ExternalBridgeServiceError, match="screenshots_disabled"):
            env.bridges.capture_screenshot(env.target_id)
        assert attempts == 1
        with env.sessions() as session:
            assert (
                session.scalar(
                    text("SELECT state FROM external_bridge_request_receipts")
                )
                == "ambiguous"
            )
