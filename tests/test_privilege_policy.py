from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_development_checks_reject_root_before_repository_setup() -> None:
    for name in ("check", "check-fast", "check-smoke"):
        source = (PROJECT_ROOT / "scripts" / name).read_text(encoding="utf-8")
        guard = "if (( EUID == 0 )); then"

        assert "Kodi Lab development checks must be run as a non-root user." in source
        assert source.index(guard) < source.index("repo_root=")
