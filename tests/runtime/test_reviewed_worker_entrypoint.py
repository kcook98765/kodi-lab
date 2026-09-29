"""The fixed one-shot worker command passes only a reviewed operation identity."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from kodi_lab.runtime.distribution import main


def test_installed_worker_entrypoint_passes_exact_operation_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_id = uuid4()
    calls: list[tuple[str, object]] = []

    def install(generation: str, **kwargs: object) -> SimpleNamespace:
        calls.append((generation, kwargs["operation_id"]))
        return SimpleNamespace(
            runtime_revision_id=uuid4(),
            image_id="sha256:" + "a" * 64,
            reused=False,
        )

    monkeypatch.setattr("kodi_lab.runtime.install.install_reviewed_runtime", install)
    assert main(["install", "omega", "--operation-id", str(operation_id)]) == 0
    assert calls == [("omega", operation_id)]
    assert main(["install", "matrix"]) == 0
    assert calls[-1] == ("matrix", None)
    with pytest.raises(SystemExit):
        main(["install", "piers", "--operation-id", str(operation_id)])
    assert len(calls) == 2
