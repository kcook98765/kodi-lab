from pathlib import Path


def test_web_package_has_no_docker_sdk_or_generic_proxy_surface() -> None:
    root = Path(__file__).parents[2] / "src" / "kodi_lab" / "web"
    sources = "\n".join(
        path.read_text() for path in root.rglob("*.py") if path.is_file()
    )
    assert "import docker" not in sources
    assert "from docker" not in sources
    assert '"/docker' not in sources
    assert '"/broker/call' not in sources
    assert '"/jsonrpc' not in sources
    assert '"/execute' not in sources
