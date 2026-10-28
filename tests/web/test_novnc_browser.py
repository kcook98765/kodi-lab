import subprocess
from pathlib import Path


def test_async_old_callbacks_cannot_stop_newer_attempts() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("novnc_browser.cjs")),
            str(root / "src/kodi_lab/web/static/novnc-phase1.js"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
