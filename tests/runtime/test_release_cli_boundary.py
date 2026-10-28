"""Release images cannot enter the private archive bootstrap path."""

from pathlib import Path

import pytest

from kodi_lab.config import Settings
from kodi_lab.runtime import lab
from kodi_lab.runtime.distribution import main
from kodi_lab.storage import metadata
from kodi_lab.storage.engine import create_database_engine


@pytest.mark.parametrize("command", ("list", "verify", "bootstrap"))
def test_release_profile_rejects_private_archive_commands(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setenv("KODI_LAB_RELEASE_BUNDLE", "1")
    with pytest.raises(SystemExit) as error:
        main([command])
    assert error.value.code == 2


def test_release_network_setup_does_not_load_private_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kodi_lab.runtime import distribution

    class Client:
        api = object()

        def close(self) -> None:
            pass

    monkeypatch.setenv("KODI_LAB_RELEASE_BUNDLE", "1")
    monkeypatch.setattr("docker.from_env", lambda: Client())
    monkeypatch.setattr(distribution, "ensure_managed_network", lambda api: True)
    monkeypatch.setattr(
        distribution,
        "load_curated_runtimes",
        lambda: pytest.fail("private catalog loaded"),
    )
    assert main(["network-ensure"]) == 0


def test_release_status_reports_unacquired_recipes_without_private_catalog(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'lab.db'}")
    metadata.create_all(engine)

    class Broker:
        def ping(self) -> None:
            pass

    monkeypatch.setenv("KODI_LAB_RELEASE_BUNDLE", "1")
    monkeypatch.setattr(lab, "remote_validate", lambda *args, **kwargs: None)
    monkeypatch.setattr(lab, "create_database_engine", lambda url: engine)
    monkeypatch.setattr(lab, "BrokerClient", Broker)
    monkeypatch.setattr(
        lab,
        "load_curated_runtimes",
        lambda: pytest.fail("private catalog loaded"),
    )
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert lab._run_control(settings, "status", ()) == 0
    assert capsys.readouterr().out.splitlines() == [
        "matrix: supported unacquired",
        "nexus: supported unacquired",
        "omega: supported unacquired",
    ]
    with pytest.raises(lab.LabWorkflowError, match="private archive selectors"):
        lab._run_control(settings, "start", ("19",))


@pytest.mark.parametrize("experimental", (False, True))
def test_explicit_omega_cli_uses_sealed_installer_without_default_promotion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    experimental: bool,
) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    calls = []

    def install(generation: str, **kwargs: object) -> object:
        calls.append((generation, kwargs.get("novnc_phase1", False)))
        return SimpleNamespace(
            runtime_revision_id=uuid4(), image_id="sha256:" + "a" * 64, reused=False
        )

    monkeypatch.setenv("KODI_LAB_RELEASE_BUNDLE", "1")
    monkeypatch.setattr("kodi_lab.runtime.install.install_reviewed_runtime", install)
    args = ["install", "omega"]
    if experimental:
        args.append("--experimental-novnc")
    assert main(args) == 0
    assert calls == [("omega", experimental)]
    assert ("experimental viewer target=" in capsys.readouterr().out) == experimental


def test_experimental_cli_rejects_other_generations_before_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kodi_lab.runtime.install.install_reviewed_runtime",
        lambda *a, **k: pytest.fail("must not acquire another generation"),
    )
    with pytest.raises(SystemExit, match="2"):
        main(["install", "nexus", "--experimental-novnc"])
