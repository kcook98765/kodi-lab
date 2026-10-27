"""Shared repository guidance is read-only and uses existing delivery identity."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.repository.stable import bootstrap_zip
from tests.web.conftest import login

BASE = "http://192.168.50.10:18120"


@pytest.mark.parametrize("mode", ["home", "development"])
def test_both_modes_have_shared_accurate_repository_instructions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    login(client)
    page = client.get(f"/add-kodi?mode={mode}")
    assert page.status_code == 200
    text = page.text
    assert text.count("<summary>Install Kodi Lab Bridge</summary>") == 1
    assert text.index("data-installation") < text.index("data-mode-controls")
    assert f'value="{BASE}/install/" readonly' in text
    assert "data-copy-repository-url hidden>Copy URL" in text
    assert text.count("add-kodi-installation.js") == 1
    steps = text.split('<ol class="kodi-installation-steps">')[1].split("</ol>")[0]
    assert steps.count("<li>") == 12
    navigation = [
        "Open Kodi <strong>Add-ons</strong>",
        "Select the <strong>package / open-box icon</strong>",
        "Select <strong>Install from zip file</strong>",
        "Select the <strong>Kodi Lab</strong> source",
        "Install <strong>repository.kodi-lab-1.0.0.zip</strong>",
    ]
    positions = [steps.index(instruction) for instruction in navigation]
    assert positions == sorted(positions)
    for instruction in [
        "Settings → File manager → Add source",
        "Unknown sources",
        "repository.kodi-lab-1.0.0.zip",
        "Install from repository → Kodi Lab",
        "Kodi Lab Narrow Bridge",
        "Enable bridge",
        "restart Kodi locally",
        "Pair Kodi Lab",
        "Generate new code",
    ]:
        assert instruction in steps
    for instruction in [
        "Allow remote control via HTTP",
        "Require authentication",
        "JSON-RPC",
        "8765",
        "Advanced ports (Development)",
    ]:
        assert instruction in text
    assert 'name="kodi_use" value="' + mode + '" checked' in text
    assert "data-consent-form" in text and "data-development-recovery" in text
    assert (
        "/stable/zips/" not in text
    )  # Install the repository, not a direct Bridge ZIP.
    assert "service.kodi_lab_bridge-" not in text
    assert (
        "official Bridge releases from the Kodi Lab repository "
        "are recognized automatically" in text
    )
    assert "No separate build approval is needed" in text
    assert "Advanced compatibility details" in text
    assert "supports Bridge <strong>1.1.31</strong>" in text
    assert "reviewed by your Kodi Lab administrator" not in text
    assert "repository.kodi-lab-dev-" not in text
    with ZipFile(io.BytesIO(bootstrap_zip(BASE))) as archive:
        descriptor = ET.fromstring(archive.read("repository.kodi-lab/addon.xml"))
    assert descriptor.attrib["name"] == "Kodi Lab"
    assert descriptor.findtext("extension/dir/info") == BASE + "/stable/addons.xml"
    assert descriptor.findtext("extension/dir/datadir") == BASE + "/stable/zips/"


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "javascript:alert(1)",
        "http://username:secret@192.168.50.10:18120",
        "http://192.168.50.10:18120/install/",
        "http://192.168.50.10:18120?secret=value",
        "http://192.168.50.10:18120#fragment",
        "http://127.0.0.1:18120",
        "http://192.168.50.10:99999",
        "http://192.168.50.10:18120\n",
    ],
)
def test_unconfigured_or_invalid_origin_has_no_invented_install_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", origin)
    login(client)
    text = client.get("/add-kodi").text
    assert "repository source URL is not configured" in text
    assert "data-repository-source-url" not in text
    assert "data-copy-repository-url" not in text
    assert "username:secret" not in text
    assert "secret=value" not in text
    assert "data-add-kodi" in text and "data-pair-kodi" in text


def test_repository_origin_is_configuration_not_request_host(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", "https://kodi-lab.example:18120")
    login(client)
    text = client.get("/add-kodi").text
    assert 'value="https://kodi-lab.example:18120/install/"' in text
    assert 'value="http://127.0.0.1/install/"' not in text


@pytest.fixture
def no_writes(database: sessionmaker[Session]) -> Iterator[list[str]]:
    writes: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _many: object,
    ) -> None:
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.I):
            writes.append(statement)

    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield writes
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_installation_view_never_probes_or_mutates(
    client: TestClient,
    no_writes: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login(client)
    no_writes.clear()  # Authentication setup precedes the page-view boundary.
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)

    def reject(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Rendering installation instructions performed network IO")

    monkeypatch.setattr("kodi_lab.web.app.httpx.AsyncClient", reject)
    for mode in ["home", "development"]:
        assert client.get(f"/add-kodi?mode={mode}").status_code == 200
    assert not no_writes


def test_unconfigured_template_still_renders_without_new_controller_context() -> None:
    from jinja2 import Environment, FileSystemLoader

    templates = Environment(
        loader=FileSystemLoader(
            Path(__file__).parents[2] / "src" / "kodi_lab" / "web" / "templates"
        ),
        autoescape=True,
    )
    templates.globals["url_for"] = lambda *_args, **_kwargs: "/static/test"
    text = templates.get_template("add-kodi.html").render()
    assert "repository source URL is not configured" in text
    assert "<summary>Install Kodi Lab Bridge</summary>" in text
