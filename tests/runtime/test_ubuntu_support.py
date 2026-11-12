"""Pinned Ubuntu closure and fail-closed downloads; crypto review is a live gate."""

import hashlib
import json
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.runtime import ubuntu_support as support
from kodi_lab.runtime.ubuntu_support import SupportError


def data() -> dict[str, Any]:
    value = json.loads(support.load_support_closure().raw)
    assert isinstance(value, dict)
    return value


def test_exact_support_closure() -> None:
    closure = support.load_support_closure()
    assert len(closure.packages) == 97
    assert sum(x.size for x in closure.packages) == 64991044
    assert len(closure.final_inventory) == 178
    assert closure.digest == "sha256:" + support.SUPPORT_SHA256
    assert {"xvfb", "python3", "dbus-bin", "dbus-daemon", "libxrandr2"} <= {
        x.name for x in closure.packages
    }
    assert not {"ostree", "curl", "gnupg", "mesa-utils"} & {
        x.name for x in closure.packages
    }


@pytest.mark.parametrize(
    "kind",
    [
        "arch",
        "version",
        "url",
        "redirect-url",
        "digest",
        "missing",
        "extra",
        "signer",
        "inventory",
        "base",
        "platform",
        "closure-index",
        "size",
        "toolchain",
    ],
)
def test_support_changes_fail_closed(kind: str) -> None:
    doc = data()
    p = doc["packages"][0]
    if kind == "arch":
        p["architecture"] = "arm64"
    if kind == "version":
        p["version"] = "1; $(touch /tmp/untrusted)"
    if kind == "url":
        p["url"] = "https://evil.example/a.deb"
    if kind == "redirect-url":
        p["url"] = "http://archive.ubuntu.com/ubuntu/pool/" + p["archive_name"]
    if kind == "digest":
        p["sha256"] = "not-a-digest"
    if kind == "missing":
        doc["packages"] = [p for p in doc["packages"] if p["name"] != "xvfb"]
    if kind == "extra":
        doc["arbitrary"] = "ignored"
    if kind == "signer":
        doc["signed_metadata"][0]["signers"] = ["0" * 40]
    if kind == "inventory":
        doc["final_inventory"][0][1] = "changed"
    if kind == "base":
        doc["base_image"] = "ubuntu:26.04"
    if kind == "platform":
        doc["platform"] = "linux/arm64"
    if kind == "closure-index":
        p["index"] = "unverified-index"
    if kind == "size":
        p["size"] = support.MAX_SUPPORT_BYTES + 1
    if kind == "toolchain":
        p["name"] = "gcc"
        p["archive_name"] = "gcc_1_amd64.deb"
    with pytest.raises(SupportError):
        support._parse(json.dumps(doc).encode())


@pytest.mark.parametrize("kind", ["missing", "additional", "corrupt"])
def test_exact_deb_set_required(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    p = support.SupportPackage(
        "fixture",
        "1",
        "amd64",
        "fixture_1_amd64.deb",
        "https://archive.ubuntu.com/ubuntu/pool/f/fixture_1_amd64.deb",
        hashlib.sha256(b"fixture").hexdigest(),
        7,
        "index",
    )
    closure = support.SupportClosure(b"{}", (p,), (), ())
    monkeypatch.setattr(support, "load_support_closure", lambda **kwargs: closure)
    entries = {"support/" + p.archive_name: b"fixture"}
    if kind == "missing":
        entries.clear()
    if kind == "additional":
        entries["support/extra.deb"] = b"extra"
    if kind == "corrupt":
        entries["support/" + p.archive_name] = b"changed"
    with pytest.raises(SupportError):
        support.validate_support_entries(entries)


def test_redirect_is_rejected() -> None:
    with pytest.raises(SupportError, match="support_redirect_rejected"):
        support._NoRedirect().redirect_request(
            None, None, 302, "moved", None, "https://evil.example/"
        )


class Lease:
    def assert_handoff(self, operation_id):  # type: ignore[no-untyped-def]
        assert operation_id is not None


@pytest.mark.parametrize(
    "kind", ["unavailable", "bad-digest", "too-large", "interrupted", "exact"]
)
def test_bounded_download_failures(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.request

    p = support.SupportPackage(
        "fixture",
        "1",
        "amd64",
        "fixture_1_amd64.deb",
        "https://archive.ubuntu.com/ubuntu/pool/f/fixture_1_amd64.deb",
        hashlib.sha256(b"fixture").hexdigest(),
        7,
        "index",
    )
    monkeypatch.setattr(
        support,
        "load_support_closure",
        lambda **kwargs: support.SupportClosure(b"{}", (p,), (), ()),
    )

    class Opener:
        def open(self, request, *, timeout):  # type: ignore[no-untyped-def]
            assert request.full_url == p.url and timeout <= 30
            if kind == "unavailable":
                raise OSError("missing historical package")
            if kind == "interrupted":
                raise KeyboardInterrupt
            return io.BytesIO(
                b"fixture"
                if kind == "exact"
                else b"changed!"
                if kind == "too-large"
                else b"changed"
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    if kind == "exact":
        entries = support.download_support(Lease(), uuid4())  # type: ignore[arg-type]
        support.validate_support_entries(entries)
    else:
        with pytest.raises(
            KeyboardInterrupt if kind == "interrupted" else SupportError
        ):
            support.download_support(Lease(), uuid4())  # type: ignore[arg-type]


def test_snapshot_allowlist_binds_only_the_four_original_r3_perl_artifacts() -> None:
    from urllib.parse import urlsplit

    closure = support.load_support_closure(revision=3)
    admitted = []
    for (url, digest), fallback in support._R3_ARCHIVE_FALLBACKS.items():
        package = next(
            p for p in closure.packages if (p.url, p.sha256) == (url, digest)
        )
        assert package.version == "5.40.1-7ubuntu0.3"
        parsed = urlsplit(fallback)
        assert parsed.scheme == "https" and parsed.netloc == "snapshot.ubuntu.com"
        assert parsed.path == (
            "/ubuntu/20261001T000000Z/pool/main/p/perl/" + package.archive_name
        )
        admitted.append(package.name)
    assert set(admitted) == {"libperl5.40", "perl", "perl-base", "perl-modules-5.40"}


@pytest.mark.parametrize(
    "kind",
    [
        "exact",
        "primary-success",
        "other-revision",
        "unmapped-url",
        "unmapped-hash",
        "server-error",
        "snapshot-unavailable",
        "bad-digest",
        "too-large",
        "redirect",
    ],
)
def test_snapshot_recovery_preserves_content_bounds_and_closed_admission(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import urllib.error
    import urllib.request
    from email.message import Message

    original = "https://archive.ubuntu.com/ubuntu/pool/main/f/fixture_1_all.deb"
    fallback = "https://snapshot.ubuntu.com/ubuntu/20261001T000000Z/pool/main/f/fixture_1_all.deb"
    digest = hashlib.sha256(b"fixture").hexdigest()
    # A tiny policy fixture exercises transport without adding multi-MiB DEBs to Git.
    # The separate contract above checks the actual immutable four-artifact policy.
    monkeypatch.setattr(
        support, "_R3_ARCHIVE_FALLBACKS", {(original, digest): fallback}
    )
    package = support.SupportPackage(
        "fixture",
        "1",
        "all",
        "fixture_1_all.deb",
        original if kind != "unmapped-url" else original + "?other",
        digest if kind != "unmapped-hash" else "0" * 64,
        7,
        "index",
    )
    monkeypatch.setattr(
        support,
        "load_support_closure",
        lambda **kwargs: support.SupportClosure(b"{}", (package,), (), ()),
    )
    calls = []

    class Opener:
        def open(self, request, *, timeout):  # type: ignore[no-untyped-def]
            assert 0 < timeout <= 30
            calls.append(request.full_url)
            if request.full_url == package.url:
                if kind == "primary-success":
                    return io.BytesIO(b"fixture")
                raise urllib.error.HTTPError(
                    package.url,
                    503 if kind == "server-error" else 404,
                    "unavailable",
                    Message(),
                    None,
                )
            assert request.full_url == fallback
            if kind == "snapshot-unavailable":
                raise urllib.error.HTTPError(
                    fallback, 404, "unavailable", Message(), None
                )
            if kind == "redirect":
                raise SupportError("support_redirect_rejected")
            return io.BytesIO(
                b"changed!"
                if kind == "too-large"
                else b"changed"
                if kind == "bad-digest"
                else b"fixture"
            )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    revision = 2 if kind == "other-revision" else 3
    if kind in {"exact", "primary-success"}:
        assert support.fetch_support_packages(revision=revision) == {
            "support/fixture_1_all.deb": b"fixture"
        }
    else:
        with pytest.raises(SupportError):
            support.fetch_support_packages(revision=revision)
    assert calls == (
        [package.url]
        if kind
        in {
            "primary-success",
            "other-revision",
            "unmapped-url",
            "unmapped-hash",
            "server-error",
        }
        else [package.url, fallback]
    )
