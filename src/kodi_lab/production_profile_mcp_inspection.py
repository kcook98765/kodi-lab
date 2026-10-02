"""Bounded Slice E authority and MCP client for the production profile.

The helper is test-only.  Durable authority is created exclusively through the
ordinary authenticated Web client, while MCP requests use the mounted HTTP
transport.  The bearer exists only in this object's in-memory state.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Final, cast
from uuid import UUID, uuid4

import httpx

from kodi_lab.application.mcp_administration import READ_ONLY_INSPECTION_PRESET_ID
from kodi_lab.application.production_inspection import (
    PRODUCTION_INSPECTION_PRESET_ID,
    PRODUCTION_INSPECTION_PRESET_NAME,
    PRODUCTION_VISUAL_PRESET_ID,
    PRODUCTION_VISUAL_PRESET_NAME,
)
from kodi_lab.kodi.screenshots import (
    PNG_MIME_TYPE,
    ScreenshotArtifactError,
    validate_external_png,
)
from kodi_lab.mcp.transport import MCP_PROTOCOL_VERSION
from kodi_lab.production_profile_web_client import (
    PRODUCTION_PROFILE_BASE_URL,
    ProductionProfileWebClient,
)

_CLIENT_NAME: Final = "Disposable production inspection client"
_EXPECTED_TOOLS: Final = ("addon_list", "target_list", "target_status")
_EXPECTED_VISUAL_TOOLS: Final = (
    "addon_list",
    "screenshot_capture",
    "target_list",
    "target_status",
)
_META: Final = {
    "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "kodi-lab-production-profile-slice-e",
        "version": "1",
    },
}


class ProductionProfileMcpInspectionError(RuntimeError):
    """A bounded failure that never embeds a bearer or response body."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ProductionMcpAuthority:
    client_id: UUID
    credential_id: UUID
    preset_id: UUID
    preset_name: str
    target_id: UUID
    target_generation: int
    inspect_scope_id: UUID


@dataclass(frozen=True, slots=True)
class ProductionMcpProtocolEvidence:
    protocol_version: str
    discovery_request_id: UUID
    catalog_request_id: UUID
    tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProductionMcpInvocation:
    request_id: UUID
    tool_name: str
    result: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProductionScreenshotInvocation:
    protocol_request_id: UUID
    semantic_request_id: UUID
    success: bool
    result_code: str
    metadata: dict[str, object]
    image_metadata: dict[str, object] | None


def inspect_mcp_screenshot_image(result: dict[str, object]) -> dict[str, object]:
    """Strictly decode one MCP image block and return only bounded metadata."""

    content = result.get("content")
    if (
        not isinstance(content, list)
        or len(content) != 1
        or not isinstance(content[0], dict)
    ):
        raise ProductionProfileMcpInspectionError("screenshot_payload_missing")
    image = cast(dict[str, object], content[0])
    if image.get("type") != "image" or image.get("mimeType") != PNG_MIME_TYPE:
        raise ProductionProfileMcpInspectionError("screenshot_payload_type_invalid")
    encoded = image.get("data")
    if not isinstance(encoded, str):
        raise ProductionProfileMcpInspectionError("screenshot_payload_type_invalid")
    try:
        payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise ProductionProfileMcpInspectionError(
            "screenshot_base64_invalid"
        ) from error
    if not payload:
        raise ProductionProfileMcpInspectionError("screenshot_decoded_empty")
    if base64.b64encode(payload).decode("ascii") != encoded:
        raise ProductionProfileMcpInspectionError("screenshot_base64_invalid")
    try:
        validated = validate_external_png(payload)
    except ScreenshotArtifactError as error:
        raise ProductionProfileMcpInspectionError(
            "screenshot_decoder_rejected"
        ) from error
    return {
        "encoded_character_length": len(encoded),
        "decoded_byte_length": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "format": "png",
        "width": validated.width,
        "height": validated.height,
        "color_mode": "rgb",
        "validated_byte_length": len(payload),
        "validated_sha256": hashlib.sha256(payload).hexdigest(),
    }


class ProductionProfileMcpInspection:
    """Create exact inspection authority and make only the three Slice E reads."""

    def __init__(
        self,
        web: ProductionProfileWebClient,
        *,
        mcp_client: httpx.Client | None = None,
    ) -> None:
        self._web = web
        self._mcp = mcp_client or httpx.Client(
            base_url=PRODUCTION_PROFILE_BASE_URL,
            timeout=httpx.Timeout(35.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_mcp = mcp_client is None
        self._token: str | None = None
        self._authority: ProductionMcpAuthority | None = None
        self._protocol: ProductionMcpProtocolEvidence | None = None
        self._invocations: list[ProductionMcpInvocation] = []
        self._closed = False

    def __enter__(self) -> ProductionProfileMcpInspection:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def authority(self) -> ProductionMcpAuthority:
        if self._authority is None:
            raise ProductionProfileMcpInspectionError("mcp_authority_absent")
        return self._authority

    @property
    def evidence(self) -> MappingProxyType[str, object]:
        return MappingProxyType(
            {
                "authority": (
                    None if self._authority is None else asdict(self._authority)
                ),
                "protocol": (
                    None if self._protocol is None else asdict(self._protocol)
                ),
                "invocations": tuple(
                    {
                        "request_id": item.request_id,
                        "tool_name": item.tool_name,
                        "success": True,
                    }
                    for item in self._invocations
                ),
                "credential_plaintext_recorded": False,
            }
        )

    def establish_authority(
        self, *, target_id: UUID, identity_generation: int
    ) -> ProductionMcpAuthority:
        """Use only canonical Web routes for the exact inspection intersection."""

        self._require_open()
        if self._authority is not None or self._token is not None:
            raise ProductionProfileMcpInspectionError("mcp_authority_already_created")
        if identity_generation < 1:
            raise ProductionProfileMcpInspectionError("target_generation_invalid")
        production = self._require_production_preset()
        initial = self._require_preset(READ_ONLY_INSPECTION_PRESET_ID)
        if initial.get("display_name") != "Read-only Inspection":
            raise ProductionProfileMcpInspectionError("initial_preset_contract_drift")

        created = self._web_document(
            self._web.post_authenticated(
                "/api/mcp/clients",
                json={
                    "display_name": _CLIENT_NAME,
                    "preset_id": str(READ_ONLY_INSPECTION_PRESET_ID),
                },
            ),
            expected_status=201,
        )
        client_id = self._uuid(created, "id")
        if (
            created.get("enabled") is not True
            or created.get("credentials") != []
            or created.get("target_grants") != []
            or created.get("development_source_grants") != []
            or created.get("workspace_bindings") != []
        ):
            raise ProductionProfileMcpInspectionError("new_client_not_authority_empty")

        issued = self._web_document(
            self._web.post_authenticated(
                f"/api/mcp/clients/{client_id}/credentials",
                json={"expiration_seconds": 3600},
            ),
            expected_status=201,
        )
        token = issued.pop("token", None)
        credential = issued.get("credential")
        if (
            not isinstance(token, str)
            or len(token) != 43
            or not isinstance(credential, dict)
        ):
            raise ProductionProfileMcpInspectionError("credential_response_invalid")
        credential_id = self._uuid(cast(dict[str, object], credential), "id")
        self._token = token

        assigned = self._web_document(
            self._web.post_authenticated(
                f"/api/mcp/clients/{client_id}/preset",
                json={"preset_id": str(PRODUCTION_INSPECTION_PRESET_ID)},
            ),
            expected_status=200,
        )
        if self._nested_id(assigned, "preset") != PRODUCTION_INSPECTION_PRESET_ID:
            raise ProductionProfileMcpInspectionError("client_preset_assignment_failed")

        target_policy = self._web_document(
            self._web.post_authenticated(
                f"/api/mcp/target-policies/{target_id}",
                json={"preset_id": str(PRODUCTION_INSPECTION_PRESET_ID)},
            ),
            expected_status=200,
        )
        if (
            self._uuid(target_policy, "target_id") != target_id
            or target_policy.get("target_class") != "external_production"
            or target_policy.get("identity_generation") != identity_generation
            or self._nested_id(target_policy, "preset")
            != PRODUCTION_INSPECTION_PRESET_ID
        ):
            raise ProductionProfileMcpInspectionError("target_preset_assignment_failed")

        granted = self._web_document(
            self._web.post_authenticated(
                f"/api/mcp/clients/{client_id}/targets/{target_id}"
            ),
            expected_status=200,
        )
        if granted.get("target_grants") != [str(target_id)]:
            raise ProductionProfileMcpInspectionError("target_grant_failed")

        scope = self._web_document(
            self._web.post_authenticated(
                f"/api/mcp/clients/{client_id}/targets/{target_id}"
                "/production-inspection-scopes",
                json={"identity_generation": identity_generation},
            ),
            expected_status=201,
        )
        scope_id = self._uuid(scope, "scope_id")
        if (
            self._uuid(scope, "client_id") != client_id
            or self._uuid(scope, "target_id") != target_id
            or scope.get("identity_generation") != identity_generation
            or scope.get("current_identity_generation") != identity_generation
            or scope.get("scope") != "inspect"
            or scope.get("state") != "active"
            or scope.get("effective_state") != "active"
            or scope.get("bridge_trust_revision") is not None
        ):
            raise ProductionProfileMcpInspectionError("inspect_scope_not_current")

        self._authority = ProductionMcpAuthority(
            client_id=client_id,
            credential_id=credential_id,
            preset_id=self._uuid(production, "id"),
            preset_name=cast(str, production["display_name"]),
            target_id=target_id,
            target_generation=identity_generation,
            inspect_scope_id=scope_id,
        )
        return self._authority

    def inspect_catalog(self) -> ProductionMcpProtocolEvidence:
        """Negotiate modern stateless HTTP and require the exact visible catalog."""

        if self._authority is None:
            raise ProductionProfileMcpInspectionError("mcp_authority_absent")
        discovery_id = uuid4()
        discovery = self._mcp_request("server/discover", request_id=discovery_id)
        result = discovery.get("result")
        if (
            not isinstance(result, dict)
            or result.get("supportedVersions") != [MCP_PROTOCOL_VERSION]
            or set(cast(list[object], result.get("capabilities", []))) != {"tools"}
        ):
            raise ProductionProfileMcpInspectionError("mcp_discovery_contract_drift")
        catalog_id = uuid4()
        catalog = self._mcp_request("tools/list", request_id=catalog_id)
        catalog_result = catalog.get("result")
        tools = (
            catalog_result.get("tools") if isinstance(catalog_result, dict) else None
        )
        if not isinstance(tools, list) or any(
            not isinstance(item, dict) for item in tools
        ):
            raise ProductionProfileMcpInspectionError("mcp_catalog_invalid")
        names = tuple(cast(str, item.get("name")) for item in tools)
        expected = (
            _EXPECTED_VISUAL_TOOLS
            if self.authority.preset_id == PRODUCTION_VISUAL_PRESET_ID
            else _EXPECTED_TOOLS
        )
        if names != expected:
            raise ProductionProfileMcpInspectionError("mcp_catalog_unsafe")
        self._protocol = ProductionMcpProtocolEvidence(
            protocol_version=MCP_PROTOCOL_VERSION,
            discovery_request_id=discovery_id,
            catalog_request_id=catalog_id,
            tool_names=names,
        )
        return self._protocol

    def resume_visual_authority(
        self,
        *,
        client_id: UUID,
        credential_id: UUID,
        target_id: UUID,
        target_generation: int,
        inspect_scope_id: UUID,
    ) -> ProductionMcpAuthority:
        """Rotate the show-once credential after the Slice F process boundary."""

        self._require_open()
        if self._authority is not None or self._token is not None:
            raise ProductionProfileMcpInspectionError("mcp_authority_already_created")
        rotated = self._web_document(
            self._web.post_authenticated(
                f"/api/mcp/clients/{client_id}/credentials/{credential_id}/rotate",
                json={"expiration_seconds": 3600},
            ),
            expected_status=200,
        )
        token = rotated.pop("token", None)
        credential = rotated.get("credential")
        if (
            not isinstance(token, str)
            or len(token) != 43
            or not isinstance(credential, dict)
        ):
            raise ProductionProfileMcpInspectionError("credential_response_invalid")
        rotated_id = self._uuid(cast(dict[str, object], credential), "id")
        if rotated_id == credential_id:
            raise ProductionProfileMcpInspectionError("credential_rotation_failed")
        self._token = token
        self._authority = ProductionMcpAuthority(
            client_id=client_id,
            credential_id=rotated_id,
            preset_id=PRODUCTION_VISUAL_PRESET_ID,
            preset_name=PRODUCTION_VISUAL_PRESET_NAME,
            target_id=target_id,
            target_generation=target_generation,
            inspect_scope_id=inspect_scope_id,
        )
        return self._authority

    def invoke_screenshot_once(
        self, *, request_id: UUID
    ) -> ProductionScreenshotInvocation:
        """Issue one and only one screenshot tools/call and inspect its result."""

        authority = self.authority
        if self._protocol is None:
            raise ProductionProfileMcpInspectionError("mcp_catalog_not_checked")
        if self._invocations:
            raise ProductionProfileMcpInspectionError("mcp_tools_already_invoked")
        protocol_request_id = uuid4()
        response = self._mcp_request(
            "tools/call",
            request_id=protocol_request_id,
            params={
                "name": "screenshot_capture",
                "arguments": {
                    "target_id": str(authority.target_id),
                    "request_id": str(request_id),
                },
            },
            tool_name="screenshot_capture",
        )
        raw_result = response.get("result")
        if not isinstance(raw_result, dict):
            raise ProductionProfileMcpInspectionError("mcp_screenshot_result_invalid")
        if raw_result.get("isError") is True:
            content = raw_result.get("content")
            if (
                not isinstance(content, list)
                or len(content) != 1
                or not isinstance(content[0], dict)
                or content[0].get("type") != "text"
            ):
                raise ProductionProfileMcpInspectionError(
                    "mcp_screenshot_result_invalid"
                )
            code = content[0].get("text")
            if not isinstance(code, str) or not code.isidentifier():
                raise ProductionProfileMcpInspectionError(
                    "mcp_screenshot_result_invalid"
                )
            self._invocations.append(
                ProductionMcpInvocation(
                    request_id=protocol_request_id,
                    tool_name="screenshot_capture",
                    result={"error": code},
                )
            )
            return ProductionScreenshotInvocation(
                protocol_request_id=protocol_request_id,
                semantic_request_id=request_id,
                success=False,
                result_code=code,
                metadata={},
                image_metadata=None,
            )
        if raw_result.get("isError") is not False:
            raise ProductionProfileMcpInspectionError("mcp_screenshot_result_invalid")
        structured = raw_result.get("structuredContent")
        if not isinstance(structured, dict):
            raise ProductionProfileMcpInspectionError("mcp_screenshot_result_invalid")
        self._invocations.append(
            ProductionMcpInvocation(
                request_id=protocol_request_id,
                tool_name="screenshot_capture",
                result=cast(dict[str, object], structured),
            )
        )
        try:
            image_metadata = inspect_mcp_screenshot_image(raw_result)
        except ProductionProfileMcpInspectionError as error:
            return ProductionScreenshotInvocation(
                protocol_request_id=protocol_request_id,
                semantic_request_id=request_id,
                success=False,
                result_code=error.code,
                metadata=cast(dict[str, object], structured),
                image_metadata=None,
            )
        return ProductionScreenshotInvocation(
            protocol_request_id=protocol_request_id,
            semantic_request_id=request_id,
            success=True,
            result_code="success",
            metadata=cast(dict[str, object], structured),
            image_metadata=image_metadata,
        )

    def invoke_exact_three(self) -> tuple[ProductionMcpInvocation, ...]:
        """Invoke target_list, target_status, addon_list once each with no retry."""

        authority = self.authority
        if self._protocol is None:
            raise ProductionProfileMcpInspectionError("mcp_catalog_not_checked")
        if self._invocations:
            raise ProductionProfileMcpInspectionError("mcp_tools_already_invoked")
        calls: tuple[tuple[str, dict[str, object]], ...] = (
            ("target_list", {}),
            ("target_status", {"target_id": str(authority.target_id)}),
            (
                "addon_list",
                {"target_id": str(authority.target_id), "limit": 100},
            ),
        )
        for tool_name, arguments in calls:
            request_id = uuid4()
            response = self._mcp_request(
                "tools/call",
                request_id=request_id,
                params={"name": tool_name, "arguments": arguments},
                tool_name=tool_name,
            )
            raw_result = response.get("result")
            structured = (
                raw_result.get("structuredContent")
                if isinstance(raw_result, dict)
                else None
            )
            if (
                not isinstance(raw_result, dict)
                or raw_result.get("isError") is not False
                or not isinstance(structured, dict)
            ):
                raise ProductionProfileMcpInspectionError(f"mcp_{tool_name}_failed")
            self._invocations.append(
                ProductionMcpInvocation(
                    request_id=request_id,
                    tool_name=tool_name,
                    result=cast(dict[str, object], structured),
                )
            )
        return tuple(self._invocations)

    def close(self) -> None:
        if self._closed:
            return
        self._token = None
        if self._owns_mcp:
            self._mcp.close()
        self._closed = True

    def _require_production_preset(self) -> dict[str, object]:
        preset = self._require_preset(PRODUCTION_INSPECTION_PRESET_ID)
        if (
            preset.get("display_name") != PRODUCTION_INSPECTION_PRESET_NAME
            or preset.get("builtin") is not True
            or preset.get("assignable") is not True
            or preset.get("permissions") != ["target.inspect"]
        ):
            raise ProductionProfileMcpInspectionError("production_preset_unsafe")
        return preset

    def _require_preset(self, preset_id: UUID) -> dict[str, object]:
        response = self._web.get_authenticated("/api/mcp/presets")
        try:
            document = response.json()
        except ValueError as error:
            raise ProductionProfileMcpInspectionError(
                "preset_catalog_invalid"
            ) from error
        if not isinstance(document, list):
            raise ProductionProfileMcpInspectionError("preset_catalog_invalid")
        matches = [
            item
            for item in document
            if isinstance(item, dict) and item.get("id") == str(preset_id)
        ]
        if len(matches) != 1:
            raise ProductionProfileMcpInspectionError("preset_catalog_invalid")
        return cast(dict[str, object], matches[0])

    def _mcp_request(
        self,
        method: str,
        *,
        request_id: UUID,
        params: dict[str, object] | None = None,
        tool_name: str | None = None,
    ) -> dict[str, object]:
        self._require_open()
        if self._token is None:
            raise ProductionProfileMcpInspectionError("mcp_credential_absent")
        materialized = {} if params is None else dict(params)
        materialized["_meta"] = _META
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if tool_name is not None:
            headers["Mcp-Name"] = tool_name
        response = self._mcp.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": str(request_id),
                "method": method,
                "params": materialized,
            },
        )
        if response.status_code != 200:
            raise ProductionProfileMcpInspectionError(
                "mcp_transport_failed", status_code=response.status_code
            )
        try:
            document = response.json()
        except ValueError as error:
            raise ProductionProfileMcpInspectionError("mcp_response_invalid") from error
        if (
            not isinstance(document, dict)
            or document.get("jsonrpc") != "2.0"
            or document.get("id") != str(request_id)
            or "error" in document
        ):
            raise ProductionProfileMcpInspectionError("mcp_response_invalid")
        return cast(dict[str, object], document)

    @staticmethod
    def _web_document(
        response: httpx.Response, *, expected_status: int
    ) -> dict[str, object]:
        if response.status_code != expected_status:
            raise ProductionProfileMcpInspectionError(
                "web_authority_operation_failed", status_code=response.status_code
            )
        try:
            document = response.json()
        except ValueError as error:
            raise ProductionProfileMcpInspectionError(
                "web_authority_response_invalid"
            ) from error
        if not isinstance(document, dict):
            raise ProductionProfileMcpInspectionError("web_authority_response_invalid")
        return cast(dict[str, object], document)

    @staticmethod
    def _uuid(document: dict[str, object], key: str) -> UUID:
        try:
            return UUID(cast(str, document[key]))
        except (KeyError, TypeError, ValueError) as error:
            raise ProductionProfileMcpInspectionError(
                "web_authority_response_invalid"
            ) from error

    @classmethod
    def _nested_id(cls, document: dict[str, object], key: str) -> UUID:
        nested = document.get(key)
        if not isinstance(nested, dict):
            raise ProductionProfileMcpInspectionError("web_authority_response_invalid")
        return cls._uuid(cast(dict[str, object], nested), "id")

    def _require_open(self) -> None:
        if self._closed:
            raise ProductionProfileMcpInspectionError("mcp_inspection_closed")
