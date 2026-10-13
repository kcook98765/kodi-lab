"""Real descriptor/seal/race tests; fixture acquisition uses explicit flow doubles."""

import dataclasses
import errno
import os
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest

from kodi_lab.runtime import ostree
from kodi_lab.runtime.ostree import (
    AcquiredOstree,
    OstreeAcquirer,
    OstreeAcquisitionError,
)
from kodi_lab.runtime.ostree_context import ContextError, verified_context
from kodi_lab.runtime.recipes import load_inactive_ostree_recipe
from tests.runtime.ostree_support import signed_fixture
from tests.runtime.test_ostree_acquisition import FakeRunner


class PayloadRunner(FakeRunner):
    def run(self, argv, *, stage, timeout):  # type: ignore[no-untyped-def]
        result = super().run(argv, stage=stage, timeout=timeout)
        if "checkout" in argv:
            root = Path(argv[-1])
            component = next(
                c
                for c in load_inactive_ostree_recipe().recipe.provider.components
                if c.commit == argv[-2]
            )
            raw = ostree.signed_component_metadata(
                signed_fixture(component.role)[0], component
            )
            (root / "metadata").write_text(raw)
            (root / "files").mkdir()
            (root / "files" / "binary").write_bytes(b"reviewed-content\n" * 100)
            (root / "files" / "internal-link").symlink_to("binary")
        return result


@pytest.fixture
def acquirer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OstreeAcquirer:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(ostree, "MIN_FREE_BYTES", 1)
    monkeypatch.setattr(ostree, "_application_release", lambda _: None)
    monkeypatch.setattr(
        ostree, "_CRITICAL_FILES", {role: {} for role in ("app", "platform", "gl")}
    )
    return OstreeAcquirer(tmp_path, runner=PayloadRunner())


def test_exact_snapshot_is_sealed_and_deterministic(acquirer: OstreeAcquirer) -> None:
    operation = uuid4()
    with acquirer.acquire(operation) as acquired:
        digests = []
        for _ in range(2):
            with verified_context(
                acquired, operation, entries={"Dockerfile": b"FROM scratch\n"}
            ) as context:
                context.assert_sealed(operation)
                digests.append(context.digest)
                assert context.stream.read(2) == b"\x1f\x8b"
                context.stream.seek(0)
                with tarfile.open(fileobj=context.stream, mode="r:gz") as archive:
                    payload = archive.extractfile("payload/app/files/binary")
                    assert payload is not None and payload.read().startswith(
                        b"reviewed-content"
                    )
                with pytest.raises(OSError) as error:
                    os.pwrite(context.stream.fileno(), b"CORRUPT", 0)
                assert error.value.errno == errno.EPERM
        assert digests[0] == digests[1]
    assert not acquired.stage.exists()


@pytest.mark.parametrize(
    "kind",
    ["no-lease", "wrong-operation", "expired", "wrong-evidence", "wrong-revision"],
)
def test_invalid_handoff_is_rejected(kind: str, acquirer: OstreeAcquirer) -> None:
    operation = uuid4()
    with acquirer.acquire(operation) as original:
        acquired = original
        if kind == "no-lease":
            acquired = AcquiredOstree(original.stage, original.evidence)
        if kind == "wrong-operation":
            operation = uuid4()
        if kind == "expired":
            assert original._lease is not None
            original._lease.active = False
        if kind == "wrong-evidence":
            acquired = dataclasses.replace(
                original,
                evidence=original.evidence.model_copy(
                    update={"recipe_digest": "sha256:" + "0" * 64}
                ),
            )
        if kind == "wrong-revision":
            acquired = dataclasses.replace(
                original,
                evidence=original.evidence.model_copy(update={"recipe_revision": 2}),
            )
        with (
            pytest.raises(OstreeAcquisitionError, match="invalid_acquisition_handoff"),
            verified_context(acquired, operation, entries={}),
        ):
            pytest.fail("invalid handoff entered")


@pytest.mark.parametrize(
    "kind", ["changed", "missing", "extra", "symlink", "hardlink", "directory-link"]
)
def test_tampered_verified_tree_is_rejected(
    kind: str, acquirer: OstreeAcquirer
) -> None:
    operation = uuid4()
    with acquirer.acquire(operation) as acquired:
        root = acquired.component_path("app")
        file = root / "files/binary"
        if kind == "changed":
            file.write_bytes(b"substituted rc2")
        elif kind == "missing":
            file.unlink()
        elif kind == "extra":
            (root / "extra").write_text("unreviewed")
        elif kind == "symlink":
            file.unlink()
            file.symlink_to("/etc/passwd")
        elif kind == "hardlink":
            os.link(file, root / "hardlink")
        elif kind == "directory-link":
            (root / "files/internal-link").unlink()
            file.unlink()
            (root / "files").rmdir()
            (root / "files").symlink_to("/tmp")
        with (
            pytest.raises(ContextError),
            verified_context(acquired, operation, entries={}),
        ):
            pytest.fail("tampered checkout entered Docker")


def test_mutation_during_copy_is_detected(
    acquirer: OstreeAcquirer, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = uuid4()
    original = tarfile.TarFile.addfile
    with acquirer.acquire(operation) as acquired:

        def mutate(archive, member, fileobj=None):  # type: ignore[no-untyped-def]
            if member.name == "payload/app/files/binary":
                (acquired.component_path("app") / "files/binary").write_bytes(
                    b"X" * member.size
                )
            return original(archive, member, fileobj)

        monkeypatch.setattr(tarfile.TarFile, "addfile", mutate)
        with (
            pytest.raises(ContextError, match="verified_payload_tampered"),
            verified_context(acquired, operation, entries={}),
        ):
            pytest.fail("racing mutation accepted")


def test_later_mutation_cannot_change_docker_snapshot(acquirer: OstreeAcquirer) -> None:
    operation = uuid4()
    with (
        acquirer.acquire(operation) as acquired,
        verified_context(acquired, operation, entries={}) as context,
    ):
        before = context.stream.read()
        (acquired.component_path("app") / "files/binary").write_text(
            "later untrusted change"
        )
        context.stream.seek(0)
        assert context.stream.read() == before
        with pytest.raises(ContextError, match="invalid_sealed_handoff"):
            context.assert_sealed(uuid4())


def test_interruption_removes_stage_and_closes_context(
    acquirer: OstreeAcquirer,
) -> None:
    operation = uuid4()
    with (
        pytest.raises(KeyboardInterrupt),
        acquirer.acquire(operation) as acquired,
        verified_context(acquired, operation, entries={}) as context,
    ):
        raise KeyboardInterrupt
    assert context.stream.closed and not acquired.stage.exists()
    with pytest.raises(OstreeAcquisitionError):
        acquired.assert_handoff(operation)


@pytest.mark.parametrize(
    "name", ["../Dockerfile", "/Dockerfile", "a//Dockerfile", "a/./Dockerfile"]
)
def test_unsafe_context_asset_rejected(name: str, acquirer: OstreeAcquirer) -> None:
    operation = uuid4()
    with (
        acquirer.acquire(operation) as acquired,
        pytest.raises(ContextError, match="unsafe_context_asset"),
        verified_context(acquired, operation, entries={name: b"untrusted"}),
    ):
        pytest.fail("escaped context")


def test_expired_result_cannot_be_reused(acquirer: OstreeAcquirer) -> None:
    operation = uuid4()
    with acquirer.acquire(operation) as acquired:
        pass
    with (
        pytest.raises(OstreeAcquisitionError),
        verified_context(acquired, operation, entries={}),
    ):
        pytest.fail("expired operation reused")


def test_only_one_concurrent_assembly_consumer(acquirer: OstreeAcquirer) -> None:
    from concurrent.futures import ThreadPoolExecutor

    operation = uuid4()
    with acquirer.acquire(operation) as acquired:

        def claim() -> bool:
            try:
                acquired.claim_assembly(operation)
            except OstreeAcquisitionError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as threads:
            results = list(threads.map(lambda _: claim(), range(2)))
        assert sorted(results) == [False, True]


@pytest.mark.parametrize("bound", ["size", "time"])
def test_context_resource_bounds_cleanup(
    bound: str, acquirer: OstreeAcquirer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodi_lab.runtime import ostree_context

    operation = uuid4()
    if bound == "size":
        monkeypatch.setattr(ostree_context, "MAX_CONTEXT_BYTES", 1)
    else:
        monkeypatch.setattr(ostree_context, "MAX_CAPTURE_SECONDS", -1)
    with (
        pytest.raises(OstreeAcquisitionError),
        acquirer.acquire(operation) as acquired,
        verified_context(acquired, operation, entries={}),
    ):
        pytest.fail("unbounded context")
    assert not acquired.stage.exists()


@pytest.mark.parametrize("transient", [True, False])
def test_quota_monitor_handles_only_ostree_download_rename(
    transient: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo/tmp" if transient else tmp_path / "checkout/app"
    root.mkdir(parents=True)
    entry = root / "object.filez"
    entry.write_bytes(b"object")
    original = Path.lstat

    def renamed(path: Path):  # type: ignore[no-untyped-def]
        if path == entry:
            raise FileNotFoundError("atomic download promotion")
        return original(path)

    monkeypatch.setattr(Path, "lstat", renamed)
    if transient:
        ostree._stage_quota(tmp_path)
    else:
        with pytest.raises(FileNotFoundError):
            ostree._stage_quota(tmp_path)


def test_grown_sparse_payload_is_rejected_before_read(acquirer: OstreeAcquirer) -> None:
    operation = uuid4()
    with acquirer.acquire(operation) as acquired:
        path = acquired.component_path("app") / "files/binary"
        with path.open("wb") as stream:
            stream.truncate(ostree.MAX_STAGE_BYTES + 1)
        with (
            pytest.raises(ContextError, match="context_resource_bound"),
            verified_context(acquired, operation, entries={}),
        ):
            pytest.fail("oversized sparse payload was read")
