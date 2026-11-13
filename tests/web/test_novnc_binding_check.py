"""Recurring checks retain reconciliation after removing one earlier duplicate."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.novnc import CatalogViewAuthority
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.broker.novnc import ViewBinding
from kodi_lab.config import Settings
from kodi_lab.runtime.display_capabilities import EXPERIMENTAL_OMEGA_DISPLAY
from kodi_lab.storage.models import RuntimeContainerArtifactRow
from kodi_lab.web import novnc_optin

from .conftest import IMAGE_ID, INSTANCE_ID, REVISION_ID
from .test_novnc_control import mutation


def test_recurring_interactive_check_removes_only_earlier_reconciliation(
    database: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    with database.begin() as db:
        db.add(
            RuntimeContainerArtifactRow(
                runtime_revision_id=str(REVISION_ID), image_id=IMAGE_ID
            )
        )
    pin = ViewBinding("a" * 64, IMAGE_ID, "one-incarnation", ())
    counts: Counter[str] = Counter()
    status = ManagedInstanceReconciliationService.status

    def reconciliation(self: Any, *args: Any, **kwargs: Any) -> Any:
        counts["reconciliation"] += 1
        return status(self, *args, **kwargs)

    def capability(*args: Any, **kwargs: Any) -> Any:
        counts["capability"] += 1
        return EXPERIMENTAL_OMEGA_DISPLAY

    def lookup(path: Path, instance: UUID) -> ViewBinding:
        # Real catalog on both sides; Docker and sealed capability acquisition
        # are doubles. This measures call/query reduction, never native latency.
        counts["broker_lookup"] += 1
        return CatalogViewAuthority(database, {instance: pin}).resolve(instance)

    def statement(*args: Any) -> None:
        counts[args[2].split()[0].upper()] += 1

    monkeypatch.setattr(ManagedInstanceReconciliationService, "status", reconciliation)
    monkeypatch.setattr(novnc_optin, "admitted_display_capability", capability)
    monkeypatch.setattr(novnc_optin, "resolve_binding", lookup)
    viewer = novnc_optin.build_viewer(Settings(runtime_root=tmp_path), database)
    ownership = viewer.ownership
    assert ownership is not None
    engine = database.kw["bind"]
    event.listen(engine, "before_cursor_execute", statement)
    try:
        # Exact old preflight, with the remaining path and DB unchanged.
        with monkeypatch.context() as patch:
            patch.setattr(
                ownership,
                "require_input_available",
                ownership.require_control_available,
            )
            before_binding = viewer.binding(INSTANCE_ID, "interactive")
        before = counts.copy()
        counts.clear()
        assert viewer.binding(INSTANCE_ID, "interactive") == before_binding
        after = counts.copy()
    finally:
        event.remove(engine, "before_cursor_execute", statement)
    assert before["reconciliation"] == 3 and after["reconciliation"] == 2
    assert before["capability"] == after["capability"] == 2
    assert before["broker_lookup"] == after["broker_lookup"] == 1
    assert before["SELECT"] > after["SELECT"] > 0
    assert before["PRAGMA"] > after["PRAGMA"] > 0
    assert not ({"INSERT", "UPDATE", "DELETE", "COMMIT", "BEGIN"} & after.keys())
    record_property("binding_calls_before", dict(before))
    record_property("binding_calls_after", dict(after))

    # Full preflight remains available for Take Control before old-worker retirement.
    counts.clear()
    ownership.require_control_available(INSTANCE_ID)
    assert counts["reconciliation"] == 1
    # Current exclusion is still consulted before doing a private lookup.
    mutation(database)
    counts.clear()
    with pytest.raises(ValueError, match="agent operation active"):
        viewer.binding(INSTANCE_ID, "interactive")
    assert counts["broker_lookup"] == 0
