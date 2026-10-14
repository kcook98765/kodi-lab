from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import uuid4

import pytest

ADDON_ROOT = Path(__file__).parents[2] / "bridge" / "service.kodi_lab_bridge"
OTP = runpy.run_path(str(ADDON_ROOT / "onboarding.py"))
Store = cast(Any, OTP["OnboardingStore"])
Error = cast(Any, OTP["OnboardingError"])


def _store(
    tmp_path: Path,
    now: list[float],
    codes: str = "7KQ4A7H2Z9P3",
    events: list[str] | None = None,
) -> Any:
    characters = iter(codes)
    return Store(
        tmp_path / "onboarding.json",
        clock=lambda: now[0],
        choose=lambda alphabet: next(characters),
        event=None if events is None else events.append,
    )


def _wrong(store: Any, count: int, source: str = "192.168.2.10") -> None:
    for _ in range(count):
        assert not store.verify_and_reserve(
            "ZZZZ", source, uuid4(), uuid4(), paired=False
        )


def test_alphabet_random_interface_and_ambiguous_characters(tmp_path: Path) -> None:
    alphabet = OTP["ALPHABET"]
    assert alphabet == "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    assert len(alphabet) == 32
    assert not set("01IO") & set(alphabet)
    now = [1000.0]
    chosen: list[str] = []
    characters = iter("7KQ4")

    def choose(value: str) -> str:
        chosen.append(value)
        return next(characters)

    store = Store(tmp_path / "onboarding.json", clock=lambda: now[0], choose=choose)
    assert store.initialize(paired=False) == "Pairing code active"
    assert store.status(paired=False)["code"] == "7KQ4"
    assert chosen == [alphabet] * 4
    assert store.status(paired=False)["expires_at"] == 1300.0


def test_default_randomness_uses_secrets_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = sys.modules["secrets"]
    selected: list[str] = []

    def choice(alphabet: str) -> str:
        selected.append(alphabet)
        return "7"

    monkeypatch.setattr(module, "choice", choice)
    store = Store(tmp_path / "onboarding.json", clock=lambda: 1000.0)
    store.initialize(paired=False)
    assert store.status(paired=False)["code"] == "7777"
    assert selected == [OTP["ALPHABET"]] * 4


def test_five_minute_expiry_boundary_does_not_autorenew(tmp_path: Path) -> None:
    now = [1000.0]
    store = _store(tmp_path, now)
    store.initialize(paired=False)
    now[0] = 1299.999
    assert store.status(paired=False)["status"] == "Pairing code active"
    now[0] = 1300.0
    assert store.status(paired=False) == {
        "status": "Pairing code expired",
        "code": None,
        "expires_at": None,
    }
    with pytest.raises(Error, match="pairing_code_expired"):
        store.verify_and_reserve("7KQ4", "192.168.2.10", uuid4(), uuid4(), paired=False)
    assert store.initialize(paired=False) == "Pairing code expired"
    state = json.loads(Path(store.path).read_text())
    assert state["code"] == "7KQ4"
    assert state["code_expired"] is True
    now[0] = 1200.0
    assert store.status(paired=False)["status"] == "Pairing code expired"


def test_replacement_cooldown_resets_code_failures_but_not_abuse(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    store = _store(tmp_path, now)
    store.initialize(paired=False)
    _wrong(store, 2)
    with pytest.raises(Error, match="generation_cooldown"):
        store.generate_new_code(paired=False)
    now[0] = 1030.0
    assert store.generate_new_code(paired=False) == "A7H2"
    state = json.loads(Path(store.path).read_text())
    assert state["failed_attempts"] == 0
    assert len(state["bridge_failures"]) == 2
    assert len(state["source_failures"]["192.168.2.10"]) == 2
    assert state["code"] != "7KQ4"
    assert not store.verify_and_reserve(
        "7KQ4", "192.168.2.11", uuid4(), uuid4(), paired=False
    )


def test_active_code_survives_restart_and_source_window_expires(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    store = _store(tmp_path, now)
    store.initialize(paired=False)
    _wrong(store, 1)
    restarted = Store(store.path, clock=lambda: now[0], choose=lambda _a: "A")
    assert restarted.initialize(paired=False) == "Pairing code active"
    assert restarted.status(paired=False)["code"] == "7KQ4"
    now[0] = 1300.0
    assert restarted.generate_new_code(paired=False) == "AAAA"
    state = json.loads(Path(store.path).read_text())
    assert state["source_failures"] == {}
    assert len(state["bridge_failures"]) == 1


def test_replacement_redraws_an_identical_code(tmp_path: Path) -> None:
    now = [1000.0]
    store = _store(tmp_path, now, codes="7KQ47KQ4A7H2")
    store.initialize(paired=False)
    now[0] = 1030.0
    assert store.generate_new_code(paired=False) == "A7H2"


def test_five_failures_lock_code_and_bridge_across_restart(tmp_path: Path) -> None:
    now = [1000.0]
    store = _store(tmp_path, now)
    store.initialize(paired=False)
    _wrong(store, 5)
    assert store.status(paired=False)["status"] == "Pairing temporarily locked"
    restarted = Store(store.path, clock=lambda: now[0])
    with pytest.raises(Error, match="pairing_temporarily_locked"):
        restarted.verify_and_reserve(
            "7KQ4", "192.168.2.10", uuid4(), uuid4(), paired=False
        )
    now[0] = 1030.0
    with pytest.raises(Error, match="pairing_temporarily_locked"):
        restarted.generate_new_code(paired=False)
    state = json.loads(Path(store.path).read_text())
    assert state["failed_attempts"] == 5
    assert len(state["source_failures"]["192.168.2.10"]) == 5


def test_bridge_wide_and_source_windows_survive_replacement(tmp_path: Path) -> None:
    now = [1000.0]
    store = _store(tmp_path, now)
    store.initialize(paired=False)
    _wrong(store, 3, "192.168.2.10")
    now[0] = 1030.0
    store.generate_new_code(paired=False)
    _wrong(store, 2, "192.168.2.11")
    assert store.status(paired=False)["status"] == "Pairing temporarily locked"
    state = json.loads(Path(store.path).read_text())
    assert state["failed_attempts"] == 2
    assert len(state["bridge_failures"]) == 5
    assert len(state["source_failures"]["192.168.2.10"]) == 3
    assert len(state["source_failures"]["192.168.2.11"]) == 2
    now[0] = 1630.0
    assert store.generate_new_code(paired=False) == "Z9P3"
    state = json.loads(Path(store.path).read_text())
    assert state["bridge_failures"] == []
    assert state["source_failures"] == {}


def test_single_transaction_slot_consumes_code(tmp_path: Path) -> None:
    now = [1000.0]
    store = _store(tmp_path, now)
    store.initialize(paired=False)
    target = uuid4()
    request = uuid4()
    assert store.verify_and_reserve(
        "7KQ4", "192.168.2.10", target, request, paired=False
    )
    state = json.loads(Path(store.path).read_text())
    assert state["code"] is None
    assert state["transaction"] == {
        "target_id": str(target),
        "request_id": str(request),
        "expires_at": 1300.0,
    }
    with pytest.raises(Error, match="pairing_in_progress"):
        store.verify_and_reserve("7KQ4", "192.168.2.10", target, uuid4(), paired=False)
    now[0] = 1030.0
    with pytest.raises(Error, match="pairing_in_progress"):
        store.generate_new_code(paired=False)
    now[0] = 1300.0
    with pytest.raises(Error, match="pairing_code_expired"):
        store.verify_and_reserve("7KQ4", "192.168.2.10", target, uuid4(), paired=False)
    assert store.generate_new_code(paired=False) == "A7H2"


def test_existing_paired_state_overrides_corrupt_onboarding(tmp_path: Path) -> None:
    path = tmp_path / "onboarding.json"
    path.write_text("{truncated")
    store = Store(path, clock=lambda: 1000.0)
    assert store.initialize(paired=True) == "Paired"
    assert store.status(paired=True)["status"] == "Paired"
    with pytest.raises(Error, match="already_paired"):
        store.generate_new_code(paired=True)
    assert path.read_text() == "{truncated"


def test_existing_slice1_onboarding_state_migrates_without_reset(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    store = _store(tmp_path, now)
    store.initialize(paired=False)
    _wrong(store, 2)
    old = json.loads(Path(store.path).read_text())
    old["format"] = 1
    del old["bootstrap_replay"]
    del old["committed_request_id"]
    Path(store.path).write_text(json.dumps(old))
    restarted = Store(store.path, clock=lambda: now[0])
    assert restarted.status(paired=False)["code"] == "7KQ4"
    migrated = json.loads(Path(store.path).read_text())
    assert migrated["format"] == 2
    assert migrated["failed_attempts"] == 2
    assert len(migrated["bridge_failures"]) == 2
    assert migrated["bootstrap_replay"] == []
    assert migrated["committed_request_id"] is None


@pytest.mark.parametrize("payload", ["{truncated", "[]", "{}"])
def test_corrupt_onboarding_state_fails_closed(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "onboarding.json"
    path.write_text(payload)
    store = Store(path, clock=lambda: 1000.0)
    with pytest.raises(Error, match="onboarding_state_invalid"):
        store.initialize(paired=False)
    with pytest.raises(Error, match="onboarding_state_invalid"):
        store.generate_new_code(paired=False)
    assert path.read_text() == payload


def test_onboarding_state_link_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "other.json"
    source.write_text("{}")
    path = tmp_path / "onboarding.json"
    path.symlink_to(source)
    store = Store(path, clock=lambda: 1000.0)
    with pytest.raises(Error, match="onboarding_state_invalid"):
        store.initialize(paired=False)
    assert source.read_text() == "{}"


def test_code_does_not_appear_in_transition_logs(tmp_path: Path) -> None:
    now = [1000.0]
    events: list[str] = []
    store = _store(tmp_path, now, events=events)
    store.initialize(paired=False)
    _wrong(store, 1)
    now[0] = 1030.0
    store.generate_new_code(paired=False)
    assert events == [
        "code_generated",
        "incorrect_code",
        "code_generated",
        "code_replaced",
    ]
    assert all("7KQ4" not in event and "A7H2" not in event for event in events)


def _module(name: str, **values: object) -> ModuleType:
    result = ModuleType(name)
    for key, value in values.items():
        setattr(result, key, value)
    return result


def test_kodi_dialog_shows_active_code_and_local_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    store = _store(tmp_path, now)
    store.initialize(paired=False)
    now[0] = 1030.0
    messages: list[str] = []

    class Dialog:
        def yesno(self, heading: str, message: str, **labels: str) -> bool:
            assert heading == "Pair Kodi Lab"
            assert labels == {"nolabel": "Close", "yeslabel": "Generate new code"}
            messages.append(message)
            return True

        def ok(self, heading: str, message: str) -> None:
            assert heading == "Pair Kodi Lab"
            messages.append(message)

    monkeypatch.setitem(sys.modules, "xbmcgui", _module("xbmcgui", Dialog=Dialog))
    monkeypatch.setitem(
        sys.modules,
        "xbmcaddon",
        _module(
            "xbmcaddon",
            Addon=lambda _id: _module("addon", getSettingBool=lambda _s: True),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "narrow_bridge",
        _module(
            "narrow_bridge",
            ADDON_ID="service.kodi_lab_bridge",
            _local_pairing_status=lambda: False,
            _reset_pairing=lambda: pytest.fail("unpaired reset must not be offered"),
            _onboarding_store=lambda: store,
        ),
    )
    monkeypatch.setitem(
        sys.modules, "onboarding", _module("onboarding", OnboardingError=Error)
    )
    ui = runpy.run_path(str(ADDON_ROOT / "pairing_ui.py"))
    monkeypatch.setitem(
        cast(Any, ui["main"]).__globals__["time"].__dict__, "time", lambda: now[0]
    )
    cast(Any, ui["main"])()
    assert "7KQ4" in messages[0]
    assert "Expires at" in messages[0]
    assert "4:30" in messages[0]
    assert "A7H2" in messages[1]
    assert store.status(paired=False)["code"] == "A7H2"


def test_kodi_dialog_paired_reset_cancel_never_reads_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[str] = []

    class Dialog:
        def yesno(self, heading: str, message: str, **labels: str) -> bool:
            assert heading == "Pair Kodi Lab"
            assert "current Kodi Lab connection" in message
            assert labels == {"nolabel": "Cancel", "yeslabel": "Reset pairing"}
            return False

        def ok(self, _heading: str, message: str) -> None:
            messages.append(message)

    monkeypatch.setitem(sys.modules, "xbmcgui", _module("xbmcgui", Dialog=Dialog))
    monkeypatch.setitem(
        sys.modules,
        "xbmcaddon",
        _module(
            "xbmcaddon",
            Addon=lambda _id: _module("addon", getSettingBool=lambda _s: False),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "narrow_bridge",
        _module(
            "narrow_bridge",
            ADDON_ID="service.kodi_lab_bridge",
            _local_pairing_status=lambda: True,
            _reset_pairing=lambda: pytest.fail("cancel must not mutate pairing"),
            _onboarding_store=lambda: pytest.fail("paired code state must not be read"),
        ),
    )
    monkeypatch.setitem(
        sys.modules, "onboarding", _module("onboarding", OnboardingError=Error)
    )
    cast(Any, runpy.run_path(str(ADDON_ROOT / "pairing_ui.py"))["main"])()
    assert messages == []


def test_kodi_dialog_paired_reset_confirm_calls_narrow_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Dialog:
        def yesno(self, _heading: str, _message: str, **_labels: str) -> bool:
            return True

        def ok(self, _heading: str, message: str) -> None:
            calls.append(message)

    monkeypatch.setitem(sys.modules, "xbmcgui", _module("xbmcgui", Dialog=Dialog))
    monkeypatch.setitem(
        sys.modules,
        "xbmcaddon",
        _module("xbmcaddon", Addon=lambda _id: _module("addon")),
    )

    def reset_pairing() -> bool:
        calls.append("reset")
        return True

    monkeypatch.setitem(
        sys.modules,
        "narrow_bridge",
        _module(
            "narrow_bridge",
            ADDON_ID="service.kodi_lab_bridge",
            _local_pairing_status=lambda: True,
            _reset_pairing=reset_pairing,
            _onboarding_store=lambda: pytest.fail("paired code state must not be read"),
        ),
    )
    monkeypatch.setitem(
        sys.modules, "onboarding", _module("onboarding", OnboardingError=Error)
    )
    cast(Any, runpy.run_path(str(ADDON_ROOT / "pairing_ui.py"))["main"])()
    assert calls == ["reset", "Pairing reset. Generate a new code to pair Kodi Lab."]


def test_kodi_dialog_has_expired_and_locked_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "xbmcaddon", _module("xbmcaddon"))
    monkeypatch.setitem(sys.modules, "xbmcgui", _module("xbmcgui"))
    monkeypatch.setitem(
        sys.modules,
        "narrow_bridge",
        _module(
            "narrow_bridge",
            ADDON_ID="service.kodi_lab_bridge",
            _local_pairing_status=lambda: False,
            _onboarding_store=lambda: None,
            _reset_pairing=lambda: None,
        ),
    )
    monkeypatch.setitem(
        sys.modules, "onboarding", _module("onboarding", OnboardingError=Error)
    )
    message: Any = runpy.run_path(str(ADDON_ROOT / "pairing_ui.py"))["_message"]
    assert "expired" in message({"status": "Pairing code expired"}, 1000.0)
    assert "locked" in message({"status": "Pairing temporarily locked"}, 1000.0)
    assert "Not paired" in message({"status": "Not paired"}, 1000.0)
