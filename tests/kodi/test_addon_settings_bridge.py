"""Real resident settings handler uses declared types and never exposes values."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.development.deployment import capture_development_tree
from tests.kodi.test_managed_launch_bridge import setup_bridge
from tests.kodi.test_narrow_bridge_addon import _load

SCHEMA = b"""<settings version="1">
<section id="plugin.video.launch">
<category id="general">
<group id="1">
<setting id="debug" type="boolean">
<default>false</default>
<control type="toggle"/>
</setting>
<setting id="page_size" type="integer">
<default>10</default>
<constraints>
<minimum>1</minimum>
<maximum>100</maximum>
</constraints>
</setting>
<setting id="opacity" type="number">
<constraints>
<minimum>0</minimum>
<maximum>1</maximum>
</constraints>
</setting>
<setting id="title" type="string">
<control type="edit" format="string"/>
</setting>
<setting id="password" type="string">
<control type="edit" format="string">
<hidden>true</hidden>
</control>
</setting>
<setting id="api_key" type="string"/>
<setting id="paths" type="list"/>
<setting id="choice" type="integer">
<constraints>
<options>dynamic</options>
</constraints>
</setting>
</group>
</category>
</section>
</settings>"""


class Settings:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.calls: list[str] = []
        self.fail: str | None = None
        self.read_fail = False

    def __getattr__(self, method: str) -> Any:
        assert method in {
            "getBool",
            "getInt",
            "getNumber",
            "getString",
            "setBool",
            "setInt",
            "setNumber",
            "setString",
        }

        def call(key: str, *args: object) -> object:
            if method.startswith("set"):
                self.calls.append(method)
                self.values[key] = args[0]
                if self.fail == key:
                    raise RuntimeError("sensitive-value-must-never-escape")
                return None
            if self.read_fail:
                raise RuntimeError("sensitive-read-error")
            return self.values[key]

        return call


class Addon:
    def __init__(self, api: Settings) -> None:
        self.api = api
        self.persisted: dict[str, Any] = {}
        self.legacy_calls: list[str] = []

    def getSettings(self) -> Settings:
        return self.api

    def _save(self, method: str, key: str, value: object) -> bool:
        self.legacy_calls.append(method)
        getattr(self.api, method)(key, value)
        self.persisted = dict(self.api.values)
        return True

    def setSettingBool(self, key: str, value: bool) -> bool:
        return self._save("setBool", key, value)

    def setSettingInt(self, key: str, value: int) -> bool:
        return self._save("setInt", key, value)

    def setSettingNumber(self, key: str, value: float) -> bool:
        return self._save("setNumber", key, value)

    def setSettingString(self, key: str, value: str) -> bool:
        return self._save("setString", key, value)


def test_typed_addon_setters_persist_when_nexus_settings_api_only_changes_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = module(monkeypatch)
    api = Settings()
    addon = Addon(api)
    # Model Nexus: Settings.set* mutates the in-memory view but Save() does not
    # save the add-on store. Only Addon.setSetting* crosses that boundary.
    api.setBool("debug", False)
    assert addon.persisted == {}
    api.calls.clear()
    result = bridge["_settings_apply"](
        addon,
        bridge["_settings_schema"](SCHEMA),
        [
            {"id": "debug", "value": True},
            {"id": "page_size", "value": 23},
            {"id": "opacity", "value": 0.5},
            {"id": "title", "value": "ordinary"},
        ],
    )
    assert all(item["status"] == "verified" for item in result)
    assert addon.legacy_calls == ["setBool", "setInt", "setNumber", "setString"]
    api.values = dict(addon.persisted)  # A separate add-on instance/restart.
    assert api.values == {
        "debug": True,
        "page_size": 23,
        "opacity": 0.5,
        "title": "ordinary",
    }


@pytest.mark.parametrize(
    "returned,expected", [(False, "internal_failure"), (True, "applied_unverified")]
)
def test_typed_setter_failure_or_unchanged_readback_never_claims_verified(
    monkeypatch: pytest.MonkeyPatch, returned: bool, expected: str
) -> None:
    bridge = module(monkeypatch)
    api = Settings()
    api.values["debug"] = False
    addon = Addon(api)
    monkeypatch.setattr(addon, "setSettingBool", lambda _key, _value: returned)
    result = bridge["_settings_apply"](
        addon, bridge["_settings_schema"](SCHEMA), [{"id": "debug", "value": True}]
    )
    assert result == [{"id": "debug", "status": expected}]
    assert addon.persisted == {} and api.values == {"debug": False}


def module(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _load(monkeypatch)


@pytest.mark.parametrize(
    ("key", "value", "status"),
    [
        ("debug", True, "verified"),
        ("page_size", 30, "verified"),
        ("opacity", 0.5, "verified"),
        ("title", "ordinary label", "verified"),
        ("password", "sensitive-value", "denied"),
        ("api_key", "sensitive-value", "denied"),
        ("unknown", True, "invalid_or_unknown"),
        ("debug", 1, "invalid_type"),
        ("page_size", True, "invalid_type"),
        ("page_size", 1.2, "invalid_type"),
        ("page_size", 101, "invalid_range"),
        ("page_size", 0, "invalid_range"),
        ("opacity", float("nan"), "invalid_range"),
        ("opacity", float("inf"), "invalid_range"),
        ("title", "x" * 1025, "invalid_range"),
        ("title", "line\nbreak", "invalid_range"),
        ("paths", [], "unsupported_type"),
        ("choice", 1, "unsupported_type"),
    ],
)
def test_typed_validation_and_no_value_disclosure(
    monkeypatch: pytest.MonkeyPatch, key: str, value: object, status: str
) -> None:
    bridge = module(monkeypatch)
    api = Settings()
    addon = Addon(api)
    result = bridge["_settings_apply"](
        addon, bridge["_settings_schema"](SCHEMA), [{"id": key, "value": value}]
    )
    assert result == [{"id": key, "status": status}]
    assert not api.calls if status != "verified" else api.calls
    assert set(result[0]) == {"id", "status"}


def test_partial_writes_and_readback_failure_are_not_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = module(monkeypatch)
    api = Settings()
    api.read_fail = True
    addon = Addon(api)
    result = bridge["_settings_apply"](
        addon,
        bridge["_settings_schema"](SCHEMA),
        [
            {"id": "debug", "value": True},
            {"id": "unknown", "value": 2},
            {"id": "page_size", "value": 30},
        ],
    )
    assert [v["status"] for v in result] == [
        "applied_readback_unavailable",
        "invalid_or_unknown",
        "applied_readback_unavailable",
    ]
    assert api.values == {"debug": True, "page_size": 30}
    assert api.calls == ["setBool", "setInt"]


@pytest.mark.parametrize(
    "raw",
    [
        b'<!DOCTYPE settings [<!ENTITY x "value">]><settings/>',
        b'<settings><setting id="debug" type="bool"/>'
        b'<setting id="debug" type="bool"/></settings>',
        b'<settings version="2"/>',
        b'<settings><setting id="../escape" type="bool"/></settings>',
        b"x" * 65537,
    ],
)
def test_untrusted_or_ambiguous_definitions_fail_closed(
    monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    with pytest.raises(ValueError):
        module(monkeypatch)["_settings_schema"](raw)


def test_full_handler_verifies_deployed_tree_and_managed_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, state, launch, _calls = setup_bridge(
        tmp_path,
        monkeypatch,
        '<extension point="xbmc.python.script" library="main.py"/>',
    )
    root = tmp_path / "addons"
    resources = root / "plugin.video.launch/resources"
    resources.mkdir()
    (resources / "settings.xml").write_bytes(SCHEMA)
    api = Settings()
    monkeypatch.setattr(
        bridge["xbmcaddon"],
        "Addon",
        lambda _addon: Addon(api),
    )
    payload = {k: v for k, v in launch.items() if k not in {"launch_id", "ordinal"}}
    payload.update(
        operation_id=str(uuid4()),
        not_after=(datetime.now(UTC) + timedelta(seconds=10))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        manifest_digest=capture_development_tree(root, "plugin.video.launch").digest,
        settings=[
            {"id": "debug", "value": True},
            {"id": "page_size", "value": 30},
            {"id": "password", "value": "private-canary-value"},
        ],
    )
    result = bridge["_addon_settings"](payload, state)
    assert [v["status"] for v in result["results"]] == [
        "verified",
        "verified",
        "denied",
    ]
    assert "private-canary-value" not in json.dumps(result)
    assert api.values == {"debug": True, "page_size": 30}
    # Repeated transport requests for the same resident operation never write.
    assert bridge["_addon_settings"](payload, state) == result
    receipts = list((tmp_path / "private").glob("settings-*.json"))
    assert len(receipts) == 1
    assert "private-canary-value" not in receipts[0].read_text()
    assert '"value"' not in receipts[0].read_text()
    (resources / "settings.xml").write_bytes(SCHEMA + b" ")
    with pytest.raises(ValueError, match="deployment changed"):
        bridge["_addon_settings"](payload, state)
    assert api.calls == ["setBool", "setInt"]


def test_setter_exception_is_uncertain_and_never_discloses_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = module(monkeypatch)
    api = Settings()
    api.fail = "debug"
    addon = Addon(api)
    result = bridge["_settings_apply"](
        addon,
        bridge["_settings_schema"](SCHEMA),
        [{"id": "debug", "value": True}, {"id": "page_size", "value": 30}],
    )
    assert result == [
        {"id": "debug", "status": "write_unconfirmed"},
        {"id": "page_size", "status": "verified"},
    ]
    assert api.values == {"debug": True, "page_size": 30}
    assert "sensitive" not in json.dumps(result)


def test_utf16_entity_definition_is_refused_before_xml_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = '<!DOCTYPE settings [<!ENTITY x "value">]><settings>&x;</settings>'
    with pytest.raises((ValueError, UnicodeError)):
        module(monkeypatch)["_settings_schema"](raw.encode("utf-16"))


@pytest.mark.parametrize(
    "option", ["hidden", "HIDDEN", "hidden|verify", "password", "urlencoded"]
)
def test_legacy_hidden_and_unsupported_options_never_call_the_api(
    monkeypatch: pytest.MonkeyPatch, option: str
) -> None:
    bridge = module(monkeypatch)
    raw = (
        '<settings><setting id="access" type="text" label="30001" '
        'option="' + option + '"/></settings>'
    ).encode()
    api = Settings()
    addon = Addon(api)
    result = bridge["_settings_apply"](
        addon,
        bridge["_settings_schema"](raw),
        [{"id": "access", "value": "secret-canary"}],
    )
    assert result == [
        {
            "id": "access",
            "status": "unsupported_type" if option == "urlencoded" else "denied",
        }
    ]
    assert api.calls == [] and api.values == {}


def test_deadline_crossing_during_api_acquisition_never_starts_a_setter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = module(monkeypatch)
    now = datetime.now(UTC)
    clock = {"now": now}

    class Clock:
        @staticmethod
        def now(_zone: object) -> datetime:
            return clock["now"]

    api = Settings()

    class Addon:
        def getSettings(self) -> Settings:
            clock["now"] = now + timedelta(seconds=2)
            return api

    monkeypatch.setitem(bridge["_settings_apply"].__globals__, "datetime", Clock)
    result = bridge["_settings_apply"](
        Addon(),
        bridge["_settings_schema"](SCHEMA),
        [{"id": "debug", "value": True}],
        not_after=now + timedelta(seconds=1),
    )
    assert result == [{"id": "debug", "status": "not_attempted"}]
    assert api.calls == [] and api.values == {}
