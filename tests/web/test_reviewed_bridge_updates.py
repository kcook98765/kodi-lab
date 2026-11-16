"""Owner-only default-OFF policy, CSRF/concurrency and manual recovery controls."""

from typing import Any

from tests.application.test_production_otp_pairing import _code

from .test_production_otp_pairing import _headers, _pair
from .test_production_otp_pairing import ready_web as ready_web


def test_reviewed_updates_are_explicit_owner_policy_and_can_be_disabled_when_unpaired(
    ready_web: Any,
) -> None:
    client, bridge, port, target, csrf = ready_web
    url = f"/api/production-targets/{target}/bridge/reviewed-updates"
    assert client.get(url).json() == {
        "target_id": target,
        "enabled": False,
        "revision": 0,
        "last_adoption_id": None,
        "last_adopted_version": None,
        "last_adopted_at": None,
    }
    data = {"enabled": True, "expected_revision": 0, "confirmed": True}
    assert client.post(url, json=data).status_code == 403
    assert (
        client.post(
            url, json={**data, "enabled": "true"}, headers=_headers(csrf)
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, json={"enabled": True, "expected_revision": 0}, headers=_headers(csrf)
        ).status_code
        == 422
    )
    assert client.post(url, json=data, headers=_headers(csrf)).status_code == 409
    assert _pair(client, target, csrf, port, _code(bridge)).status_code == 200
    confirm = client.post(
        f"/api/production-targets/{target}/bridge/confirm",
        json={
            "confirmed": True,
            "acknowledgement": (
                "I confirm this bridge is installed on the production Kodi target "
                "and the displayed bridge identity and package provenance match "
                "the reviewed package."
            ),
        },
        headers=_headers(csrf),
    )
    assert confirm.status_code == 200
    response = client.post(url, json=data, headers=_headers(csrf))
    assert response.status_code == 200 and response.json()["enabled"]
    assert client.post(url, json=data, headers=_headers(csrf)).status_code == 409
    assert (
        "Trust updates from Kodi Lab's reviewed Bridge releases"
        in client.get("/production-targets").text
    )
    client.post(
        f"/api/production-targets/{target}/bridge/revoke",
        json={"confirmed": True},
        headers=_headers(csrf),
    )
    off = client.post(
        url,
        json={"enabled": False, "expected_revision": 1, "confirmed": True},
        headers=_headers(csrf),
    )
    assert (
        off.status_code == 200
        and off.json()["revision"] == 2
        and not off.json()["enabled"]
    )
