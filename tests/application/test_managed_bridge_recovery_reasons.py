"""Recovery reasons originate at the failed managed Bridge trust check."""

from dataclasses import replace
from typing import Any

import pytest

from kodi_lab.application.managed_bridge_trust import invalidate_managed_bridge_trust
from kodi_lab.kodi.logs import KodiLogRequest
from kodi_lab.kodi.ui_state_native import UiObservationError
from kodi_lab.storage.models import ManagedBridgeTrustRow
from tests.application.test_managed_bridge_trust import row
from tests.application.test_managed_bridge_trust import setup as setup
from tests.application.test_managed_observation_bridge import consumer
from tests.application.test_mcp_kodi_logs import excerpt
from tests.web.conftest import INSTANCE_ID
from tests.web.conftest import database as database


@pytest.mark.parametrize("state", ["prepared", "invalidated", "revoked"])
@pytest.mark.parametrize("operation", ["ui", "log"])
def test_nonactive_trust_reports_recovery_without_native_fallback_or_repair(
    setup: Any, state: str, operation: str
) -> None:
    owner, broker, client, _ = setup
    owner.prepare(INSTANCE_ID)
    with owner.sessions.begin() as session:
        current = session.get(ManagedBridgeTrustRow, str(INSTANCE_ID))
        assert current is not None
        if state == "prepared":
            current.state = state
        else:
            invalidate_managed_bridge_trust(
                session, INSTANCE_ID, secrets=owner.secrets, revoked=state == "revoked"
            )
    service, observed = consumer(setup)
    calls, establishments = client.calls, broker.establishments
    with pytest.raises(UiObservationError) as caught:
        if operation == "ui":
            service.collect(INSTANCE_ID, observed, "21.3")
        else:
            service.read_log(INSTANCE_ID, observed, "21.3", KodiLogRequest())
    assert caught.value.code == "identity_mismatch"
    assert caught.value.reason == "managed_bridge_recovery_required"
    assert client.calls == calls and broker.establishments == establishments
    assert row(owner).state == state


@pytest.mark.parametrize("operation", ["ui", "log"])
def test_changed_runtime_reports_binding_mismatch_without_repair(
    setup: Any, operation: str
) -> None:
    owner, broker, client, _ = setup
    owner.prepare(INSTANCE_ID)
    service, observed = consumer(setup)
    calls = client.calls
    with pytest.raises(UiObservationError) as caught:
        changed = observed.model_copy(update={"container_id": "b" * 64})
        if operation == "ui":
            service.collect(INSTANCE_ID, changed, "21.3")
        else:
            service.read_log(INSTANCE_ID, changed, "21.3", KodiLogRequest())
    assert caught.value.code == "identity_mismatch"
    assert caught.value.reason == "managed_bridge_runtime_mismatch"
    assert client.calls == calls and broker.establishments == 1


@pytest.mark.parametrize("operation", ["ui", "log"])
def test_revocation_during_read_reports_changed_trust_and_rejects_result(
    setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    owner, _, client, _ = setup
    client.value = replace(client.value, capabilities=("ui_observation", "log_read"))
    owner.prepare(INSTANCE_ID)
    service, observed = consumer(setup)
    client.before_return = lambda: owner.revoke(INSTANCE_ID)
    request = KodiLogRequest()
    monkeypatch.setattr(
        client, "read_log", lambda *a, **k: (client.identity(), excerpt(request))
    )
    with pytest.raises(UiObservationError) as caught:
        if operation == "ui":
            service.collect(INSTANCE_ID, observed, "21.3")
        else:
            service.read_log(INSTANCE_ID, observed, "21.3", request)
    assert caught.value.code == "identity_mismatch"
    assert caught.value.reason == "managed_bridge_trust_changed"
