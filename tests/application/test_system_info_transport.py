"""Keep native diagnostics transport failures typed and free of server text."""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kodi_lab.application.ui_state import ManagedUiObservationService
from kodi_lab.kodi.readiness import (
    JsonRpcInvalidResponseError,
    JsonRpcUnavailableError,
)
from kodi_lab.kodi.ui_state_native import UiObservationError


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (JsonRpcUnavailableError("private endpoint"), "target_unreachable"),
        (JsonRpcInvalidResponseError("private response"), "invalid_kodi_response"),
        ([], "invalid_kodi_response"),
        ({}, "invalid_kodi_response"),
    ],
)
def test_managed_system_transport_failure_classification(
    failure: object,
    expected: str,
) -> None:
    class Transport:
        def post(self, **kwargs: Any) -> bytes:
            if isinstance(failure, Exception):
                raise failure
            request = json.loads(kwargs["body"])
            return json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "error": {"code": failure}}
            ).encode()

    service = ManagedUiObservationService(
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        transport=Transport(),
    )
    call = service._native_call(
        cast(Any, SimpleNamespace(ipv4_address="127.0.0.1")),
        cast(
            Any,
            SimpleNamespace(
                jsonrpc_port=8080, jsonrpc_path="/jsonrpc", http_timeout_seconds=2
            ),
        ),
        system_info=True,
    )
    with pytest.raises(UiObservationError, match=expected):
        call("XBMC.GetInfoLabels", {"labels": []})
