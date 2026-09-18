from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def test_development_checks_reject_root_before_repository_setup() -> None:
    for name in ("check", "check-fast", "check-smoke"):
        source = (PROJECT_ROOT / "scripts" / name).read_text(encoding="utf-8")
        guard = "if (( EUID == 0 )); then"

        assert "Kodi Lab development checks must be run as a non-root user." in source
        assert source.index(guard) < source.index("repo_root=")


def test_root_only_phase8_validators_disable_bytecode_before_project_imports() -> None:
    scripts_dir = PROJECT_ROOT / "scripts"
    expected = {
        "validate-phase8-addon-repository.py",
        "validate-phase8-copied-enablement.py",
        "validate-phase8-copied-smoke.py",
        "validate-phase8-step4.py",
        "validate-phase8-step5.py",
        "validate-phase8-uploaded-zip-enablement.py",
        "validate-phase8-uploaded-zip-smoke.py",
        "validate-phase8-uploaded-zip.py",
    }
    root_only = {
        path.name
        for path in scripts_dir.glob("validate-phase8-*.py")
        if "os.geteuid() == 0" in path.read_text(encoding="utf-8")
    }

    assert root_only == expected
    for name in sorted(expected):
        source = (scripts_dir / name).read_text(encoding="utf-8")
        assert "import sys" in source
        assert source.index("sys.dont_write_bytecode = True") < source.index(
            "from kodi_lab"
        )
