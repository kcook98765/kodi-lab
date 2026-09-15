from pathlib import Path

RUNTIME = Path(__file__).parents[2] / "runtime" / "kodi-21.3"


def test_dockerfile_is_pinned_nonroot_and_has_fixed_healthcheck() -> None:
    dockerfile = (RUNTIME / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "FROM ubuntu:26.04@sha256:"
        "e5a4d6262ab5dbc25a85e60550dd7c87fd41a74fe43881534ed8288b2a7a3f8d"
    ) in dockerfile
    assert "2:21.3+dfsg-1ubuntu1" in dockerfile
    assert "USER kodi" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/kodi-lab-entrypoint"]' in dockerfile
    assert 'CMD ["/usr/local/bin/kodi-lab-kodi-healthcheck"]' in dockerfile
    assert "VOLUME" not in dockerfile
    assert "EXPOSE" not in dockerfile
    assert "novnc" not in dockerfile.lower()
    assert "x11vnc" not in dockerfile.lower()


def test_entrypoint_is_fixed_headless_kodi_sequence_without_exec_escape() -> None:
    entrypoint = (RUNTIME / "entrypoint.sh").read_text(encoding="utf-8")
    assert "Xvfb :99" in entrypoint
    assert "dbus-run-session -- /usr/bin/kodi --standalone" in entrypoint
    assert 'kill -TERM "$kodi_pid"' in entrypoint
    assert '"$@"' not in entrypoint
    assert "eval " not in entrypoint
    assert "x11vnc" not in entrypoint
    assert "websockify" not in entrypoint


def test_healthcheck_observes_only_expected_kodi_process() -> None:
    healthcheck = (RUNTIME / "healthcheck.sh").read_text(encoding="utf-8")
    assert healthcheck.strip().endswith("pgrep -x kodi.bin >/dev/null")
    assert "curl" not in healthcheck
    assert "http" not in healthcheck


def test_seed_profile_enables_only_internal_jsonrpc_without_user_content() -> None:
    settings = (RUNTIME / "profile" / "guisettings.xml").read_text(encoding="utf-8")
    assert '<setting id="services.webserver">true</setting>' in settings
    assert '<setting id="services.webserverport">8080</setting>' in settings
    assert '<setting id="services.webserverauthentication">false</setting>' in settings
    assert '<setting id="services.esenabled">false</setting>' in settings
    assert '<setting id="services.webserverpassword"></setting>' in settings
    for forbidden in ("sources", "library", "addon", "history"):
        assert forbidden not in settings.lower()
