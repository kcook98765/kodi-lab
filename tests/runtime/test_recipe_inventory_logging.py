# Inventory reads must not depend on the Engine's default logging driver.

from types import SimpleNamespace
from typing import Any

import pytest

from kodi_lab.runtime.assembly import RecipeAssemblyError, _inventory


@pytest.mark.parametrize("daemon_driver", ("local", "none"))
def test_inventory_reads_with_unsupported_sdk_default(daemon_driver: str) -> None:
    payload = b"kodi\t2:20.5+dfsg-1ubuntu1\tamd64\n"

    def run(image: str, path: str, **kwargs: Any) -> bytes | None:
        assert image == "sha256:" + "a" * 64
        assert path == "/usr/share/kodi-lab/installed-packages.tsv"
        assert kwargs["entrypoint"] == "/bin/cat"
        assert kwargs["network_mode"] == "none"
        assert kwargs["remove"] is True
        log_config = kwargs.get("log_config", {})
        if log_config.get("Type", daemon_driver) not in ("json-file", "journald"):
            return None  # Real Docker SDK synchronous run behavior.
        assert log_config["Config"] == {"max-size": "1m", "max-file": "1"}
        return payload

    client = SimpleNamespace(containers=SimpleNamespace(run=run))
    assert (
        _inventory(
            client, "sha256:" + "a" * 64, "/usr/share/kodi-lab/installed-packages.tsv"
        )
        == payload
    )


@pytest.mark.parametrize("output", (None, b"", b"x" * (64 * 1024 + 1), "not bytes"))
def test_inventory_invalid_output_still_fails_closed(output: object) -> None:
    client = SimpleNamespace(
        containers=SimpleNamespace(run=lambda *_args, **_kwargs: output)
    )
    with pytest.raises(RecipeAssemblyError, match="inventory is invalid"):
        _inventory(
            client, "sha256:" + "a" * 64, "/usr/share/kodi-lab/installed-packages.tsv"
        )
