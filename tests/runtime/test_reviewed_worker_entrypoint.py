"""The fixed one-shot worker command passes only a reviewed operation identity."""

from __future__ import annotations

import os
import signal
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
        main(["install", "piers-rc2", "--operation-id", str(operation_id)])
    assert len(calls) == 2
    assert main(["install", "piers", "--operation-id", str(operation_id)]) == 0
    assert calls == [("omega", operation_id), ("matrix", None), ("piers", operation_id)]


def test_warm_worker_identifies_exact_reused_operation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requested, previous, revision = uuid4(), uuid4(), uuid4()
    image = "sha256:" + "a" * 64
    monkeypatch.setattr(
        "kodi_lab.runtime.install.install_reviewed_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(
            operation_id=previous,
            runtime_revision_id=revision,
            image_id=image,
            reused=True,
        ),
    )
    assert main(["install", "omega", "--operation-id", str(requested)]) == 0
    assert (
        f"kodi-lab-reviewed-reuse {requested} {previous} {image}"
        in capsys.readouterr().out
    )


def test_preview_cancellation_still_uses_the_existing_worker_failure_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    previous = signal.getsignal(signal.SIGTERM)

    def cancel(*_args: object, **_kwargs: object) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr("kodi_lab.runtime.install.install_reviewed_runtime", cancel)
    with pytest.raises(SystemExit) as raised:
        main(["install", "piers", "--operation-id", str(uuid4())])
    assert raised.value.code == 2
    assert "installation cancelled" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGTERM) == previous
