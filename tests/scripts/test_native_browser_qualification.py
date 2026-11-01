"""Exercise held-button activation and bounded asynchronous close observations."""

import subprocess
from pathlib import Path


def test_native_browser_contracts() -> None:
    result = subprocess.run(
        ["node", str(Path(__file__).with_suffix(".cjs"))],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert "6 native browser qualification contracts passed" in result.stdout
