from __future__ import annotations

import gzip
import ipaddress
from collections.abc import Iterator

import pytest

import kodi_lab.development.addon_repository as repository_module
from kodi_lab.development.addon_repository import (
    AddonRepositoryError,
    PinnedHttpsTransport,
    canonicalize_repository_url,
    derive_repository_package_url,
    is_explicit_global_unicast,
    parse_repository_metadata,
    validate_dns_answers,
)


def _addon(addon_id: str = "plugin.video.demo", version: str = "1.2.3") -> str:
    return f"""<addon id="{addon_id}" name="Demo" version="{version}">
  <requires><import addon="xbmc.python" version="3.0.0" /></requires>
  <extension point="xbmc.python.pluginsource" library="default.py">
    <provides>video</provides>
  </extension>
  <extension point="xbmc.addon.metadata" />
</addon>"""


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("http://example.com/addons.xml", "repository_https_required"),
        ("file:///addons.xml", "repository_https_required"),
        ("ftp://example.com/addons.xml", "repository_https_required"),
        ("https://127.0.0.1/addons.xml", "repository_ip_literal_forbidden"),
        ("https://user@example.com/addons.xml", "repository_credentials_forbidden"),
        ("https://example.com/addons.xml?q=1", "repository_url_components_forbidden"),
        ("https://example.com/addons.xml#x", "repository_url_components_forbidden"),
        ("https://example.com:444/addons.xml", "repository_port_forbidden"),
        ("https://example.com/a/../addons.xml", "repository_path_invalid"),
        ("https://example.com/a/%2f/addons.xml", "repository_encoded_separator"),
        ("https://example.com/a/%2e%2e/addons.xml", "repository_path_invalid"),
        ("https://example.com/a\n/addons.xml", "repository_url_unsafe"),
        ("https://example.com/repository.xml", "repository_index_suffix_required"),
    ],
)
def test_repository_url_policy_rejects_unsafe_values(value: str, code: str) -> None:
    with pytest.raises(AddonRepositoryError) as captured:
        canonicalize_repository_url(value)
    assert captured.value.code == code


def test_repository_url_policy_normalizes_hostname_port_and_idna() -> None:
    assert (
        canonicalize_repository_url("https://BÜCHER.Example:443/repo/addons.xml.gz").url
        == "https://xn--bcher-kva.example/repo/addons.xml.gz"
    )


@pytest.mark.parametrize(
    "value",
    ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2001:4860:4860::8888"],
)
def test_explicit_global_unicast_accepts_public_examples(value: str) -> None:
    assert is_explicit_global_unicast(value)


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "100.64.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "240.0.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "169.254.169.254",
        "168.63.129.16",
        "::1",
        "fc00::1",
        "fec0::1",
        "fe80::1",
        "ff00::1",
        "::ffff:10.0.0.1",
        "2001:db8::1",
    ],
)
def test_explicit_global_unicast_rejects_special_ranges(value: str) -> None:
    assert not is_explicit_global_unicast(value)


def test_multicast_regression_does_not_follow_is_global_alone() -> None:
    address = ipaddress.ip_address("224.0.0.1")
    assert address.is_global
    assert not is_explicit_global_unicast(address)


def test_mixed_dns_answers_fail_closed() -> None:
    with pytest.raises(AddonRepositoryError, match="non-public"):
        validate_dns_answers(("8.8.8.8", "127.0.0.1"))


def test_dns_answer_count_is_bounded() -> None:
    with pytest.raises(AddonRepositoryError) as captured:
        validate_dns_answers(tuple(f"8.8.8.{value}" for value in range(1, 10)))
    assert captured.value.code == "repository_dns_answer_count"


class _Socket:
    def __init__(self, response: bytes) -> None:
        self._response = bytearray(response)
        self.sent = b""
        self.closed = False

    def settimeout(self, _value: float) -> None:  # pragma: no cover - contract stub
        return

    def sendall(self, value: bytes) -> None:
        self.sent += value

    def recv(self, count: int) -> bytes:
        result = bytes(self._response[:count])
        del self._response[:count]
        return result

    def close(self) -> None:
        self.closed = True


def _responses(*values: bytes) -> Iterator[_Socket]:
    yield from (_Socket(value) for value in values)


def test_pinned_transport_uses_validated_ip_and_original_host() -> None:
    sockets = _responses(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/xml\r\n"
        b"Content-Length: 9\r\n\r\n<addons/>",
    )
    connections: list[tuple[str, str]] = []
    opened: list[_Socket] = []

    def connector(address: str, hostname: str, _timeout: float) -> _Socket:
        connections.append((address, hostname))
        opened.append(next(sockets))
        return opened[-1]

    resolver_calls: list[str] = []

    def resolver(hostname: str) -> tuple[str, ...]:
        resolver_calls.append(hostname)
        return ("8.8.8.8",)

    result = PinnedHttpsTransport(resolver=resolver, connector=connector).get(
        "https://Repo.Example/addons.xml"
    )
    assert result.body == b"<addons/>"
    assert resolver_calls == ["repo.example"]
    assert connections == [("8.8.8.8", "repo.example")]
    assert b"Host: repo.example\r\n" in opened[0].sent
    assert opened[0].closed


def test_redirect_revalidates_same_host_and_rejects_cross_origin() -> None:
    same = _responses(
        b"HTTP/1.1 302 Found\r\nLocation: /next/addons.xml\r\n"
        b"Content-Length: 0\r\nContent-Type: text/xml\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\n"
        b"Content-Length: 9\r\n\r\n<addons/>",
    )
    calls: list[str] = []

    def connector(_address: str, _hostname: str, _timeout: float) -> _Socket:
        return next(same)

    def same_resolver(host: str) -> tuple[str, ...]:
        calls.append(host)
        return ("8.8.8.8",)

    transport = PinnedHttpsTransport(resolver=same_resolver, connector=connector)
    assert transport.get("https://repo.example/addons.xml").status == 200
    assert calls == ["repo.example", "repo.example"]

    cross = _responses(
        b"HTTP/1.1 302 Found\r\nLocation: https://other.example/addons.xml\r\n"
        b"Content-Length: 0\r\nContent-Type: text/xml\r\n\r\n"
    )
    rejected = PinnedHttpsTransport(
        resolver=lambda _host: ("8.8.8.8",),
        connector=lambda _address, _host, _timeout: next(cross),
    )
    with pytest.raises(AddonRepositoryError) as captured:
        rejected.get("https://repo.example/addons.xml")
    assert captured.value.code == "repository_redirect_cross_origin"


def test_metadata_parser_accepts_xml_and_one_gzip_layer() -> None:
    xml = f"<addons>{_addon()}</addons>".encode()
    direct = parse_repository_metadata(
        xml, request_path="/addons.xml", content_type="application/xml"
    )
    compressed = gzip.compress(xml)
    gzipped = parse_repository_metadata(
        compressed,
        request_path="/addons.xml.gz",
        content_type="application/gzip",
    )
    assert len(direct.candidates) == 1
    assert direct.candidates[0].support.value == "supported"
    assert gzipped.candidates == direct.candidates


@pytest.mark.parametrize(
    ("body", "path", "content_type", "code"),
    [
        (
            b"not gzip",
            "/addons.xml.gz",
            "application/gzip",
            "repository_metadata_gzip_invalid",
        ),
        (
            b"<html></html>",
            "/addons.xml",
            "application/xml",
            "repository_metadata_html",
        ),
        (
            b"<addon id='x'/>",
            "/addons.xml",
            "application/xml",
            "repository_metadata_shape",
        ),
        (
            b"<!DOCTYPE addons><addons/>",
            "/addons.xml",
            "application/xml",
            "repository_metadata_xml_unsafe",
        ),
        (b"<addons/>", "/addons.xml", "text/html", "repository_metadata_content_type"),
    ],
)
def test_metadata_parser_rejects_unsafe_shapes(
    body: bytes, path: str, content_type: str, code: str
) -> None:
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(body, request_path=path, content_type=content_type)
    assert captured.value.code == code


def test_metadata_parser_rejects_duplicates_and_classifies_repository_descriptor() -> (
    None
):
    duplicate = f"<addons>{_addon()}{_addon()}</addons>".encode()
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(
            duplicate, request_path="/addons.xml", content_type="text/xml"
        )
    assert captured.value.code == "repository_candidate_duplicate"

    descriptor = b"""<addons><addon id="repository.demo" name="Repo" version="1.0">
      <extension point="xbmc.addon.repository" /></addon></addons>"""
    result = parse_repository_metadata(
        descriptor, request_path="/addons.xml", content_type="text/xml"
    )
    assert result.candidates[0].support.value == "unsupported"
    assert result.candidates[0].unsupported_reason == "repository_descriptor"


def test_package_derivation_is_server_owned_and_version_safe() -> None:
    assert derive_repository_package_url(
        "https://repo.example/base/addons.xml",
        "plugin.video.demo",
        "1.2.3",
    ) == ("https://repo.example/base/plugin.video.demo/plugin.video.demo-1.2.3.zip")
    with pytest.raises(AddonRepositoryError) as captured:
        derive_repository_package_url(
            "https://repo.example/base/addons.xml", "plugin.video.demo", "../bad"
        )
    assert captured.value.code == "repository_package_identity_unsafe"


def test_metadata_bounds_cover_body_expansion_entries_depth_text_and_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository_module, "MAX_TRANSFER_BYTES", 16)
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(
            b"x" * 17, request_path="/addons.xml", content_type="text/xml"
        )
    assert captured.value.code == "repository_metadata_transfer_size"
    monkeypatch.setattr(repository_module, "MAX_TRANSFER_BYTES", 8 * 1024 * 1024)

    bomb = gzip.compress(b" " * 20_000)
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(
            bomb,
            request_path="/addons.xml.gz",
            content_type="application/gzip",
        )
    assert captured.value.code == "repository_metadata_expansion_ratio"

    monkeypatch.setattr(repository_module, "MAX_CANDIDATES", 1)
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(
            f"<addons>{_addon()}{_addon('plugin.video.second')}</addons>".encode(),
            request_path="/addons.xml",
            content_type="text/xml",
        )
    assert captured.value.code == "repository_candidate_count"
    monkeypatch.setattr(repository_module, "MAX_CANDIDATES", 2048)

    monkeypatch.setattr(repository_module, "MAX_XML_DEPTH", 2)
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(
            f"<addons>{_addon()}</addons>".encode(),
            request_path="/addons.xml",
            content_type="text/xml",
        )
    assert captured.value.code == "repository_metadata_depth"
    monkeypatch.setattr(repository_module, "MAX_XML_DEPTH", 32)

    monkeypatch.setattr(repository_module, "MAX_AGGREGATE_TEXT", 4)
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(
            f"<addons>{_addon()}</addons>".encode(),
            request_path="/addons.xml",
            content_type="text/xml",
        )
    assert captured.value.code == "repository_metadata_text"
    monkeypatch.setattr(repository_module, "MAX_AGGREGATE_TEXT", 8 * 1024 * 1024)

    imports = "".join(
        f'<import addon="script.module.dep{value}" />' for value in range(257)
    )
    excessive_dependencies = (
        '<addons><addon id="plugin.video.demo" name="Demo" version="1.0">'
        f"<requires>{imports}</requires>"
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension></addon></addons>"
    ).encode()
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(
            excessive_dependencies,
            request_path="/addons.xml",
            content_type="text/xml",
        )
    assert captured.value.code == "repository_candidate_invalid"


def test_metadata_rejects_nested_gzip_case_collision_and_package_authority() -> None:
    nested = gzip.compress(gzip.compress(f"<addons>{_addon()}</addons>".encode()))
    with pytest.raises(AddonRepositoryError):
        parse_repository_metadata(
            nested,
            request_path="/addons.xml.gz",
            content_type="application/gzip",
        )

    collision = f"<addons>{_addon(version='V1')}{_addon(version='v1')}</addons>"
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(
            collision.encode(),
            request_path="/addons.xml",
            content_type="text/xml",
        )
    assert captured.value.code == "repository_candidate_duplicate"

    authority = _addon().replace(
        "</addon>", "<package>https://other.example/package.zip</package></addon>"
    )
    with pytest.raises(AddonRepositoryError) as captured:
        parse_repository_metadata(
            f"<addons>{authority}</addons>".encode(),
            request_path="/addons.xml",
            content_type="text/xml",
        )
    assert captured.value.code == "repository_candidate_authority_forbidden"
