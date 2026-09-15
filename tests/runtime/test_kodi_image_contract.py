from pathlib import Path

import pytest

RUNTIME_ROOT = Path(__file__).parents[2] / "runtime"

RUNTIMES = (
    (
        "kodi-19.5",
        "ubuntu:22.04@sha256:"
        "281c5745f657873d78e5531fc5ba8575f46ab7769b94550ac99543f122679986",
        "6:19.5+git20221224.1006-f8fdeb6b1b-0~jammy",
        "/usr/bin/kodi",
        "kodi.bin",
    ),
    (
        "kodi-21.3",
        "ubuntu:26.04@sha256:"
        "e5a4d6262ab5dbc25a85e60550dd7c87fd41a74fe43881534ed8288b2a7a3f8d",
        "2:21.3+dfsg-1ubuntu1",
        "/usr/bin/kodi",
        "kodi.bin",
    ),
    (
        "kodi-20.5",
        "ubuntu:24.04@sha256:"
        "a61567bd31828687156d735ea8eb01ba4e37636e225dd6a48ba94136a70d9d61",
        "2:20.5+dfsg-1ubuntu1",
        "/usr/bin/kodi",
        "kodi.bin",
    ),
    (
        "kodi-22.0-beta2",
        "ubuntu:26.04@sha256:"
        "e5a4d6262ab5dbc25a85e60550dd7c87fd41a74fe43881534ed8288b2a7a3f8d",
        "e513e0ff4331fc25fd2454659a9dd3e6b7670146",
        "/opt/kodi/bin/kodi",
        "kodi-x11",
    ),
)


@pytest.mark.parametrize(
    ("directory", "base", "provenance_marker", "_command", "_process"), RUNTIMES
)
def test_dockerfile_is_pinned_nonroot_and_has_fixed_healthcheck(
    directory: str,
    base: str,
    provenance_marker: str,
    _command: str,
    _process: str,
) -> None:
    dockerfile = (RUNTIME_ROOT / directory / "Dockerfile").read_text(encoding="utf-8")
    assert f"FROM {base}" in dockerfile
    assert provenance_marker in dockerfile
    assert "USER kodi" in dockerfile
    assert any(
        line.strip() == "/home/kodi/.kodi \\" for line in dockerfile.splitlines()
    )
    assert 'ENTRYPOINT ["/usr/local/bin/kodi-lab-entrypoint"]' in dockerfile
    assert 'CMD ["/usr/local/bin/kodi-lab-kodi-healthcheck"]' in dockerfile
    assert "VOLUME" not in dockerfile
    assert "EXPOSE" not in dockerfile
    assert "novnc" not in dockerfile.lower()
    assert "x11vnc" not in dockerfile.lower()


@pytest.mark.parametrize(
    ("directory", "_base", "_marker", "command", "_process"), RUNTIMES
)
def test_entrypoint_is_fixed_headless_kodi_sequence_without_exec_escape(
    directory: str,
    _base: str,
    _marker: str,
    command: str,
    _process: str,
) -> None:
    entrypoint = (RUNTIME_ROOT / directory / "entrypoint.sh").read_text(
        encoding="utf-8"
    )
    assert "Xvfb :99" in entrypoint
    assert f"dbus-run-session -- {command} --standalone" in entrypoint
    assert 'kill -TERM "$kodi_pid"' in entrypoint
    assert '"$@"' not in entrypoint
    assert "eval " not in entrypoint
    assert "x11vnc" not in entrypoint
    assert "websockify" not in entrypoint


@pytest.mark.parametrize(
    ("directory", "_base", "_marker", "_command", "process"), RUNTIMES
)
def test_healthcheck_observes_only_expected_kodi_process(
    directory: str,
    _base: str,
    _marker: str,
    _command: str,
    process: str,
) -> None:
    healthcheck = (RUNTIME_ROOT / directory / "healthcheck.sh").read_text(
        encoding="utf-8"
    )
    assert healthcheck.strip().endswith(f"pgrep -x {process} >/dev/null")
    assert "curl" not in healthcheck
    assert "http" not in healthcheck


@pytest.mark.parametrize("directory", (item[0] for item in RUNTIMES))
def test_seed_profile_enables_only_internal_jsonrpc_without_user_content(
    directory: str,
) -> None:
    settings = (RUNTIME_ROOT / directory / "profile" / "guisettings.xml").read_text(
        encoding="utf-8"
    )
    assert '<setting id="services.webserver">true</setting>' in settings
    assert '<setting id="services.webserverport">8080</setting>' in settings
    assert '<setting id="services.webserverauthentication">false</setting>' in settings
    assert '<setting id="services.esenabled">false</setting>' in settings
    assert '<setting id="services.webserverpassword"></setting>' in settings
    for forbidden in ("sources", "library", "addon", "history"):
        assert forbidden not in settings.lower()


def test_kodi_20_font_compatibility_link_is_version_owned() -> None:
    kodi19 = (RUNTIME_ROOT / "kodi-19.5" / "Dockerfile").read_text(encoding="utf-8")
    kodi20 = (RUNTIME_ROOT / "kodi-20.5" / "Dockerfile").read_text(encoding="utf-8")
    kodi21 = (RUNTIME_ROOT / "kodi-21.3" / "Dockerfile").read_text(encoding="utf-8")
    source = "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Thin.ttf"
    target = "/usr/share/kodi/media/Fonts/roboto-thin.ttf"
    assert source in kodi20
    assert target in kodi20
    assert target not in kodi19
    assert target not in kodi21


def test_kodi_19_uses_only_pinned_team_kodi_old_packages() -> None:
    dockerfile = (RUNTIME_ROOT / "kodi-19.5" / "Dockerfile").read_text(encoding="utf-8")
    assert "team-xbmc/kodi-old" in dockerfile
    assert "04AE50726660A0D55355E966C8B28F649961BE33" in dockerfile
    assert (
        "7aaaa271143cffdd4509dba7f41df0669add970e6ecadc582441a43df83e722d" in dockerfile
    )
    assert (
        "b48f7483f95fd4e336f00a5d765b1b9e86e6ae045bfd2e4bf1a4845afb370bf2" in dockerfile
    )
    assert "add-apt-repository" not in dockerfile


def test_kodi_22_beta2_source_build_is_exact_and_toolchain_is_builder_only() -> None:
    dockerfile = (RUNTIME_ROOT / "kodi-22.0-beta2" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "AS kodi22-builder" in dockerfile
    assert "22.0b2-Piers" in dockerfile
    assert "e513e0ff4331fc25fd2454659a9dd3e6b7670146" in dockerfile
    assert (
        "0a1f5c0910f39f21e7d7521835ae0eba6286cff1a6c665badf502443f8e4acd0" in dockerfile
    )
    assert '"BETA2"' in dockerfile
    assert "-DENABLE_INTERNAL_TAGLIB=ON" in dockerfile
    assert "COPY --from=kodi22-builder /opt/kodi /opt/kodi" in dockerfile
    assert "apt-get purge -y kodi kodi-bin kodi-data" in dockerfile
    final_stage = dockerfile.rsplit("\nFROM ubuntu:26.04@", maxsplit=1)[1]
    for build_tool in ("ccache", "cmake", "meson", "nasm", "ninja-build"):
        assert f"        {build_tool} \\" not in final_stage
