"""Disposable stdlib MCP client: JSON secrets enter via stdin, never argv/logs.

Launch on an explicitly authorized second LAN host via SSH. SSH launches this
process only; HTTP(S) requests go directly to the supplied Kodi Lab endpoint.
The caller supplies isolated tokens and an optional test CA in memory. No files,
packages, SSH configuration or persistent trust stores change on the client host.
"""

from __future__ import annotations

import ipaddress
import json
import os
import pwd
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def exercise(configuration: dict[str, Any]) -> dict[str, Any]:
    endpoint = configuration["endpoint"]
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"https", "http"} or parsed.path != "/mcp" or parsed.query:
        raise RuntimeError("invalid_test_endpoint")
    if parsed.scheme == "http":
        private = tuple(
            ipaddress.ip_network(n)
            for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        address = ipaddress.ip_address(parsed.hostname or "")
        if not configuration.get("trusted_lan_http") or not any(
            address in n for n in private
        ):
            raise RuntimeError("http_test_requires_explicit_private_opt_in")
    context = ssl.create_default_context()
    if parsed.scheme == "https":
        context.load_verify_locations(cadata=configuration["ca_pem"])
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        NoRedirect(),
    )
    checks: list[dict[str, Any]] = []
    sequence = 0

    def expect(condition: bool, label: str, **details: Any) -> None:
        if not condition:
            raise RuntimeError(label)
        checks.append({"check": label, "passed": True, **details})

    def post(
        body: dict[str, Any] | bytes,
        token: str | None = None,
        version: str | None = None,
        headers: dict[str, str] | None = None,
        url: str = endpoint,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if version:
            request_headers["MCP-Protocol-Version"] = version
        request_headers.update(headers or {})
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(
            url, data=payload, headers=request_headers, method="POST"
        )
        try:
            response = opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise RuntimeError("unbounded_test_response")
            response_headers = dict(response.headers)
            if any(key.lower() == "mcp-session-id" for key in response_headers):
                raise RuntimeError("unexpected_session_header")
            try:
                document = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                document = {}
            return response.status, document, response_headers

    def rpc(
        method: str,
        params: dict[str, Any],
        token: str,
        version: str,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        nonlocal sequence
        sequence += 1
        if version == "2026-07-28":
            params = {
                **params,
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": version,
                    "io.modelcontextprotocol/clientCapabilities": {},
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "ryzen-lan-test",
                        "version": "1",
                    },
                },
            }
        routing_headers = {"Mcp-Method": method}
        if method == "tools/call":
            routing_headers["Mcp-Name"] = params["name"]
        return post(
            {"jsonrpc": "2.0", "id": sequence, "method": method, "params": params},
            token,
            version,
            {**routing_headers, **(headers or {})},
        )

    if configuration.get("scope_revoked_only"):
        status, document, _ = rpc(
            "tools/call",
            {
                "name": "target_status",
                "arguments": {"target_id": configuration["home_target"]},
            },
            configuration["home_token"],
            "2026-07-28",
        )
        expect(
            status == 200
            and document["result"]["isError"]
            and document["result"]["content"][0]["text"]
            == "target_not_found_or_not_authorized",
            "revoked_home_scope_denied",
        )
    else:
        for version in ("2025-11-25", "2026-07-28"):
            token = configuration["token"]
            if version == "2025-11-25":
                status, document, _ = post(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": version,
                            "capabilities": {},
                            "clientInfo": {"name": "ryzen-lan-test", "version": "1"},
                        },
                    },
                    token,
                )
                expect(
                    status == 200 and document["result"]["protocolVersion"] == version,
                    "initialize",
                    protocol=version,
                )
                status, _, _ = post(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    token,
                    version,
                )
                expect(
                    status == 202,
                    "authenticated_initialized_notification",
                    protocol=version,
                )
                status, _, _ = post(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    None,
                    version,
                )
                expect(
                    status == 401,
                    "unauthenticated_notification_denied",
                    protocol=version,
                )
            else:
                status, document, _ = rpc("server/discover", {}, token, version)
                expect(
                    status == 200 and "result" in document,
                    f"server_discover_http_{status}",
                    protocol=version,
                )
            status, document, _ = rpc("tools/list", {}, token, version)
            names = [
                tool["name"] for tool in document.get("result", {}).get("tools", [])
            ]
            expect(
                status == 200
                and "target_status" in names
                and "workspace_edit" not in names,
                "read_only_tool_discovery",
                protocol=version,
                tools=names,
            )
            for reader_token, target, kind in (
                (token, configuration["target"], "disposable_managed"),
                (
                    configuration["home_token"],
                    configuration["home_target"],
                    "synthetic_home",
                ),
            ):
                status, document, _ = rpc(
                    "tools/call",
                    {"name": "target_list", "arguments": {}},
                    reader_token,
                    version,
                )
                result = document.get("result", {})
                listing = result.get("structuredContent", {})
                expect(
                    status == 200
                    and not result.get("isError")
                    and any(
                        item["target_id"] == target
                        for item in listing.get("targets", [])
                    ),
                    "authorized_target_listing",
                    protocol=version,
                    kind=kind,
                )
                # Home health inspections retain their production 1/5s limit.
                if kind == "synthetic_home":
                    time.sleep(5.1)
                status, document, _ = rpc(
                    "tools/call",
                    {"name": "target_status", "arguments": {"target_id": target}},
                    reader_token,
                    version,
                )
                result = document.get("result", {})
                expect(
                    status == 200
                    and not result.get("isError")
                    and "structuredContent" in result,
                    "authorized_health_inspection",
                    protocol=version,
                    kind=kind,
                )
                status, document, _ = rpc(
                    "tools/call",
                    {
                        "name": "target_status",
                        "arguments": {"target_id": configuration["ungranted_target"]},
                    },
                    reader_token,
                    version,
                )
                expect(
                    status == 200
                    and document["result"]["isError"]
                    and document["result"]["content"][0]["text"]
                    == "target_not_found_or_not_authorized",
                    "ungranted_target_denied",
                    protocol=version,
                    kind=kind,
                )
                for tool, arguments in (
                    (
                        "remote_action",
                        {
                            "target_id": target,
                            "request_id": "970d52ce-1eb8-4a55-b51d-ecf3e38c6b04",
                            "action": "left",
                        },
                    ),
                    ("development_source_list", {}),
                ):
                    status, document, _ = rpc(
                        "tools/call",
                        {"name": tool, "arguments": arguments},
                        reader_token,
                        version,
                    )
                    expect(
                        status == 200
                        and document["result"]["isError"]
                        and document["result"]["content"][0]["text"]
                        == "permission_denied",
                        "write_or_development_denied",
                        protocol=version,
                        kind=kind,
                        tool=tool,
                    )
            for kind, failed_token in (
                ("missing", None),
                ("malformed", "malformed"),
                *configuration["bad_tokens"].items(),
            ):
                status, document, _ = post(
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
                    failed_token,
                    version,
                )
                expect(
                    status == 401 and "tools" not in document,
                    "authentication_denied",
                    protocol=version,
                    kind=kind,
                )
            status, document, _ = rpc(
                "tools/call",
                {"name": "target_list", "arguments": {}},
                configuration["insufficient_token"],
                version,
            )
            expect(
                status == 200
                and document["result"]["isError"]
                and document["result"]["content"][0]["text"] == "permission_denied",
                "insufficient_permissions_denied",
                protocol=version,
            )
            for origin, expected in (
                (f"{parsed.scheme}://{parsed.netloc}", 200),
                ("https://attacker.invalid", 403),
                ("null", 403),
            ):
                status, _, _ = rpc("tools/list", {}, token, version, {"Origin": origin})
                expect(
                    status == expected,
                    "origin_validation",
                    protocol=version,
                    status=status,
                )
            status, _, _ = rpc(
                "tools/list", {}, token, version, {"Host": "attacker.invalid"}
            )
            expect(status == 421, "host_validation", protocol=version)
        status, _, _ = post(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "excluded-revision-test", "version": "1"},
                },
            },
            configuration["token"],
        )
        expect(status == 400, "unsupported_revision_rejected")
        status, _, _ = post(b"{", configuration["token"], "2025-11-25")
        expect(status == 400, "malformed_request_rejected")
        status, _, _ = post({}, None, "2025-11-25", url=endpoint + "?token=fake-secret")
        expect(status == 400, "query_credentials_rejected")
    with socket.create_connection(
        (parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)),
        timeout=5,
    ) as connection:
        source_address = connection.getsockname()[0]
    return {
        "passed": True,
        "hostname": socket.gethostname(),
        "user": pwd.getpwuid(os.geteuid()).pw_name,
        "source_address": source_address,
        "destination": endpoint,
        "tls_certificate_verified": parsed.scheme == "https",
        "trusted_lan_http": parsed.scheme == "http",
        "ssh_forwarding": False,
        "adapter_installed": False,
        "checks": checks,
    }


def main() -> int:
    try:
        configuration = json.load(sys.stdin)
        report = exercise(configuration)
    except Exception as error:
        # Do not print input, headers, tokens, tracebacks or caller-controlled errors.
        print(
            json.dumps(
                {
                    "passed": False,
                    "error_type": type(error).__name__,
                    "check": str(error)
                    if type(error) is RuntimeError
                    else "transport_failure",
                }
            )
        )
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
