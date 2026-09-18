from __future__ import annotations

import stat
import time
import zipfile
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from .conftest import INSTANCE_ID, ORIGIN, login


def _valid_zip() -> bytes:
    output = BytesIO()
    addon_id = "plugin.video.webzip"
    descriptor = (
        f'<addon id="{addon_id}" name="Web ZIP" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata" /></addon>'
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{addon_id}/addon.xml", descriptor)
        archive.writestr(f"{addon_id}/default.py", "print('passive')\n")
    return output.getvalue()


def _unsafe_zip(case: str) -> bytes:
    output = BytesIO()
    addon_id = "plugin.video.unsafe"
    descriptor = (
        f'<addon id="{addon_id}" name="Unsafe" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension></addon>"
    )
    if case == "repository":
        descriptor = (
            f'<addon id="{addon_id}" name="Unsafe" version="1.0.0">'
            '<extension point="xbmc.addon.repository" /></addon>'
        )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{addon_id}/addon.xml", descriptor)
        archive.writestr(f"{addon_id}/default.py", "pass\n")
        if case == "traversal":
            archive.writestr("../escape", "x")
        elif case == "vcs":
            archive.writestr(f"{addon_id}/.GiT/config", "x")
        elif case == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr(f"{addon_id}/default.py", "other\n")
        elif case == "symlink":
            link = zipfile.ZipInfo(f"{addon_id}/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "default.py")
    return output.getvalue()


def _wait_for_import(client: TestClient, status_url: str) -> dict[str, object]:
    for _ in range(100):
        response = client.get(status_url)
        assert response.status_code == 200
        operation = response.json()
        if operation["state"] in {"succeeded", "failed"}:
            return cast(dict[str, object], operation)
        time.sleep(0.01)
    raise AssertionError("ZIP import did not terminalize")


def test_upload_requires_auth_origin_csrf_and_bounded_multipart(
    client: TestClient, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root.mkdir(mode=0o700)
    payload = _valid_zip()
    files = {"zip": ("ignored-browser-name.zip", payload, "application/zip")}
    data = {"logical_label": "Web upload"}

    assert client.post("/api/addon-zips", files=files, data=data).status_code == 401
    csrf = login(client)
    assert (
        client.post(
            "/api/addon-zips",
            files=files,
            data=data,
            headers={"Origin": "http://attacker.invalid", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/addon-zips", files=files, data=data, headers=ORIGIN
        ).status_code
        == 403
    )
    malformed = client.post(
        "/api/addon-zips",
        content=payload,
        headers={
            **ORIGIN,
            "X-Kodi-Lab-CSRF": csrf,
            "Content-Type": "application/zip",
        },
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "addon_zip_multipart_invalid"

    monkeypatch.setattr("kodi_lab.web.app.MAX_COMPRESSED_BYTES", 1)
    oversized = client.post(
        "/api/addon-zips",
        content=b"x" * (1024 * 1024 + 2),
        headers={
            **ORIGIN,
            "X-Kodi-Lab-CSRF": csrf,
            "Content-Type": "multipart/form-data; boundary=x",
        },
    )
    assert oversized.status_code == 422
    assert oversized.json()["error"]["code"] in {
        "addon_zip_compressed_size_exceeded",
        "addon_zip_multipart_invalid",
    }


def test_upload_is_async_filename_free_and_presents_only_safe_metadata(
    client: TestClient, data_root: Path
) -> None:
    data_root.mkdir(mode=0o700)
    csrf = login(client)
    response = client.post(
        "/api/addon-zips",
        files={
            "zip": (
                "../../publisher-repository-secret.zip",
                _valid_zip(),
                "application/zip",
            )
        },
        data={"logical_label": "  Web upload  "},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 202, response.text
    accepted = response.json()
    operation = _wait_for_import(client, accepted["status_url"])
    assert operation["state"] == "succeeded"
    assert operation["logical_label"] == "Web upload"

    artifacts = client.get("/api/addon-zips")
    assert artifacts.status_code == 200
    item = artifacts.json()[0]
    assert item["artifact_id"] == operation["artifact_id"]
    assert item["addon_id"] == "plugin.video.webzip"
    assert item["origin"] == "uploaded_zip"
    assert item["validation_state"] == "accepted"
    assert set(item) == {
        "artifact_id",
        "logical_label",
        "addon_id",
        "addon_name",
        "addon_version",
        "compressed_size",
        "total_uncompressed_bytes",
        "member_count",
        "origin",
        "validation_state",
        "imported_at",
        "latest_installation_state",
        "latest_installation_phase",
    }
    rendered = client.get("/addon-zips")
    assert rendered.status_code == 200
    assert "Web upload" in rendered.text
    assert "plugin.video.webzip" in rendered.text
    assert "publisher-repository-secret" not in rendered.text
    assert "original.zip" not in rendered.text
    assert "payload" not in rendered.text.lower()


def test_install_is_authenticated_origin_csrf_and_strict_empty_body(
    client: TestClient, data_root: Path
) -> None:
    data_root.mkdir(mode=0o700)
    csrf = login(client)
    uploaded = client.post(
        "/api/addon-zips",
        files={"zip": ("ignored.zip", _valid_zip(), "application/zip")},
        data={"logical_label": "Install candidate"},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    ).json()
    imported = _wait_for_import(client, uploaded["status_url"])
    artifact_id = imported["artifact_id"]
    path = f"/api/instances/{INSTANCE_ID}/addon-zips/{artifact_id}/install"

    client.post("/logout", data={"csrf_token": csrf}, headers=ORIGIN)
    assert client.post(path, json={}).status_code == 401
    csrf = login(client)
    assert (
        client.post(
            path,
            json={},
            headers={"Origin": "http://attacker.invalid", "X-Kodi-Lab-CSRF": csrf},
        ).status_code
        == 403
    )
    assert client.post(path, json={}, headers=ORIGIN).status_code == 403
    for forbidden in (
        {"path": "/tmp/archive.zip"},
        {"url": "https://example.invalid/archive.zip"},
        {"destination": "/home/kodi/.kodi/addons"},
    ):
        assert (
            client.post(
                path,
                json=forbidden,
                headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
            ).status_code
            == 422
        )

    response = client.post(
        path,
        json={},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["operation"]["artifact_id"] == artifact_id
    assert body["operation"]["original_managed_instance_id"] == str(INSTANCE_ID)
    assert "path" not in response.text
    assert "container_id" not in response.text


@pytest.mark.parametrize(
    "case", ["traversal", "symlink", "vcs", "duplicate", "repository"]
)
def test_unsafe_zip_uploads_fail_validation_without_catalog_publication(
    client: TestClient, data_root: Path, case: str
) -> None:
    data_root.mkdir(mode=0o700)
    csrf = login(client)
    response = client.post(
        "/api/addon-zips",
        files={"zip": ("untrusted-name.zip", _unsafe_zip(case), "application/zip")},
        data={"logical_label": f"Rejected {case}"},
        headers={**ORIGIN, "X-Kodi-Lab-CSRF": csrf},
    )
    assert response.status_code == 202
    operation = _wait_for_import(client, response.json()["status_url"])
    assert operation["state"] == "failed"
    assert client.get("/api/addon-zips").json() == []
    staging = data_root / "addon-zips" / "staging"
    assert list(staging.iterdir()) == []
