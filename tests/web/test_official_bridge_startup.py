from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.repository import stable
from kodi_lab.storage.models import ReviewedBridgeBuildRow
from tests.repository.test_stable import BASE, candidate
from tests.web.conftest import login


@pytest.fixture(autouse=True)
def official_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> str:
    mode = getattr(request, "param", "valid")
    bundle = candidate(tmp_path)
    root = tmp_path / "feed"
    stable.generate(bundle, root, BASE)
    pin = stable.sha256((bundle / "BUNDLE.json").read_bytes())
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BASE_URL", BASE)
    monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(root))
    if mode == "missing":
        monkeypatch.setenv("KODI_LAB_REPOSITORY_DATA_ROOT", str(tmp_path / "absent"))
    if mode == "bad_pin":
        pin = "f" * 64
    if mode == "unconfigured":
        pin = ""
    monkeypatch.setenv("KODI_LAB_REPOSITORY_BUNDLE_SHA256", pin)
    return str(mode)


@pytest.mark.parametrize(
    "official_repository",
    ["valid", "missing", "bad_pin", "unconfigured"],
    indirect=True,
)
def test_web_startup_only_recognizes_verified_pinned_official_artifacts(
    client: TestClient,
    database: sessionmaker[Session],
    official_repository: str,
) -> None:
    expected = {
        "valid": "registered",
        "missing": "unavailable",
        "bad_pin": "unavailable",
        "unconfigured": "unconfigured",
    }[official_repository]
    assert cast(FastAPI, client.app).state.official_bridge_recognition == expected
    with database() as session:
        rows = session.scalars(select(ReviewedBridgeBuildRow)).all()
        assert len(rows) == (1 if expected == "registered" else 0)
    login(client)
    assert client.get("/add-kodi").status_code == 200
    assert client.get("/pair-kodi").status_code == 200


@pytest.fixture
def writes(database: sessionmaker[Session]) -> Iterator[list[str]]:
    observed: list[str] = []

    def capture(_connection: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        if re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE)\b", statement, re.I):
            observed.append(statement)

    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield observed
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_viewing_add_kodi_does_not_repeat_build_registration(
    client: TestClient, database: sessionmaker[Session], writes: list[str]
) -> None:
    login(client)
    writes.clear()
    with database() as session:
        before = session.execute(select(ReviewedBridgeBuildRow.__table__)).all()
    for route in ("/add-kodi", "/add-kodi?mode=development", "/pair-kodi", "/kodi"):
        assert client.get(route).status_code == 200
    with database() as session:
        assert session.execute(select(ReviewedBridgeBuildRow.__table__)).all() == before
    assert all("web_sessions" in statement for statement in writes)
