import pytest

from kodi_lab.domain import (
    KodiReleaseChannel,
    KodiReleaseIdentity,
    RuntimeSupportTier,
    validate_support_tier_for_release,
    validate_support_transition,
)


@pytest.mark.parametrize(
    ("text", "expected", "display"),
    [
        ("19.5", (19, 5, KodiReleaseChannel.STABLE, None), "19.5 stable"),
        ("20.5", (20, 5, KodiReleaseChannel.STABLE, None), "20.5 stable"),
        ("21.3", (21, 3, KodiReleaseChannel.STABLE, None), "21.3 stable"),
        ("22.0-alpha.1", (22, 0, KodiReleaseChannel.ALPHA, 1), "22.0 Alpha 1"),
        ("22.0-beta.1", (22, 0, KodiReleaseChannel.BETA, 1), "22.0 Beta 1"),
        ("22.0-beta.2", (22, 0, KodiReleaseChannel.BETA, 2), "22.0 Beta 2"),
        ("22.0-rc.1", (22, 0, KodiReleaseChannel.RC, 1), "22.0 RC 1"),
    ],
)
def test_exact_release_parse_format_and_display(
    text: str,
    expected: tuple[int, int, KodiReleaseChannel, int | None],
    display: str,
) -> None:
    release = KodiReleaseIdentity.parse(text)
    assert (
        release.major,
        release.minor,
        release.channel,
        release.prerelease_ordinal,
    ) == expected
    assert release.canonical == text
    assert str(release) == text
    assert release.display == display


def test_prerelease_ordinals_and_channels_are_distinct() -> None:
    identities = {
        KodiReleaseIdentity.parse("22.0-beta.1"),
        KodiReleaseIdentity.parse("22.0-beta.2"),
        KodiReleaseIdentity.parse("22.0-rc.1"),
        KodiReleaseIdentity.parse("22.0"),
    }
    assert len(identities) == 4


@pytest.mark.parametrize(
    "value",
    [
        "",
        "21",
        "v21.3",
        "21.03",
        "22.0-beta",
        "22.0-beta.0",
        "22.0-beta.01",
        "22.0-Beta.2",
        "22.0-nightly.1",
        "0.1",
    ],
)
def test_malformed_release_identity_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        KodiReleaseIdentity.parse(value)


def test_channel_and_support_tier_are_independent_but_consistent() -> None:
    stable = KodiReleaseIdentity.parse("21.3")
    prerelease = KodiReleaseIdentity.parse("22.0-beta.2")
    for tier in (
        RuntimeSupportTier.CURRENT,
        RuntimeSupportTier.LEGACY,
        RuntimeSupportTier.RETIRED,
    ):
        validate_support_tier_for_release(stable, tier)
    validate_support_tier_for_release(prerelease, RuntimeSupportTier.PREVIEW)
    with pytest.raises(ValueError):
        validate_support_tier_for_release(stable, RuntimeSupportTier.PREVIEW)
    with pytest.raises(ValueError):
        validate_support_tier_for_release(prerelease, RuntimeSupportTier.CURRENT)


def test_support_transitions_are_monotonic_and_retired_is_terminal() -> None:
    stable = KodiReleaseIdentity.parse("21.3")
    validate_support_transition(
        stable, RuntimeSupportTier.CURRENT, RuntimeSupportTier.LEGACY
    )
    validate_support_transition(
        stable, RuntimeSupportTier.CURRENT, RuntimeSupportTier.RETIRED
    )
    validate_support_transition(
        stable, RuntimeSupportTier.LEGACY, RuntimeSupportTier.RETIRED
    )
    with pytest.raises(ValueError):
        validate_support_transition(
            stable, RuntimeSupportTier.LEGACY, RuntimeSupportTier.CURRENT
        )
    with pytest.raises(ValueError):
        validate_support_transition(
            stable, RuntimeSupportTier.RETIRED, RuntimeSupportTier.CURRENT
        )
