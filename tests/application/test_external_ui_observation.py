"""Rich UI observations require pairing authority through response publication."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from kodi_lab.application.external_bridges import ExternalBridgeServiceError
from kodi_lab.kodi.ui_state import UiCollection, normalize_collection
from kodi_lab.storage.models import ExternalBridgePairingRow
from tests.application.test_production_otp_pairing import _code
from tests.application.test_production_otp_pairing import (
    production_otp as production_otp,
)
from tests.kodi.test_ui_state import movie_collection


@pytest.mark.parametrize("change", [None, "revoke", "replace_credential"])
def test_observation_rechecks_established_pairing_without_otp(
    production_otp: Any,
    monkeypatch: pytest.MonkeyPatch,
    change: str | None,
) -> None:
    factory, service, bridge, port, store, target = production_otp
    service.begin_otp_pairing(target, bridge_port=port, code=_code(bridge))
    service.commit_otp_pairing(target)
    snapshot = service._active_snapshot(
        target, allow_no_configured_bridge_capability=True
    )
    identity = service._client.identity(
        snapshot.endpoint,
        bridge_port=port,
        target_id=target,
        bridge_id=snapshot.bridge_id,
        secret=service._read_secret(snapshot),
    )

    def no_otp(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("observation attempted OTP or pairing")

    monkeypatch.setattr(service._client, "bootstrap_otp", no_otp)
    monkeypatch.setattr(service._client, "commit_pairing", no_otp)

    def collect(*args: Any, **kwargs: Any) -> tuple[Any, UiCollection]:
        if change == "revoke":
            service.revoke(target)
        elif change == "replace_credential":
            reference = store.write(target, "request_authentication", b"n" * 32)
            with factory.begin() as session:
                pairing = session.get(ExternalBridgePairingRow, str(target))
                assert pairing is not None
                pairing.bridge_secret_reference = str(reference)
            store.retire(snapshot.secret_reference)
        return identity, movie_collection()

    monkeypatch.setattr(service._client, "inspect_ui_state", collect)
    if change is None:
        assert normalize_collection(
            service.inspect_ui_state(target)
        ).selected_item.associated
        assert store.read(target, snapshot.secret_reference, "request_authentication")
    else:
        with pytest.raises(ExternalBridgeServiceError):
            service.inspect_ui_state(target)
        with factory() as session:
            pairing = session.get(ExternalBridgePairingRow, str(target))
            assert pairing is not None
            if change == "revoke":
                assert pairing.bridge_secret_reference is None
            else:
                assert (
                    UUID(pairing.bridge_secret_reference) != snapshot.secret_reference
                )
