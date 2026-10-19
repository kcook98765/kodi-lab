"""Idle stop distinguishes a proven idle player list from unavailable evidence."""

import pytest

from kodi_lab.kodi.remote import RemoteKodiRejectedError
from tests.kodi.test_remote import RecordingTransport, adapter


@pytest.mark.parametrize("active", [False, True])
def test_playback_probe_only_queries_active_players(active: bool) -> None:
    transport = RecordingTransport(
        active_players=[{"playerid": 1, "type": "video"}] if active else []
    )
    assert adapter(transport).playback_active() is active
    assert [item["method"] for item in transport.requests] == [
        "Player.GetActivePlayers"
    ]


def test_malformed_player_evidence_is_not_reported_as_idle() -> None:
    transport = RecordingTransport(active_players=[{"playerid": True, "type": "video"}])
    with pytest.raises(RemoteKodiRejectedError):
        adapter(transport).playback_active()
