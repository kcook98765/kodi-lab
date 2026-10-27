"""Browser-controller state races and backpressure without network/native Kodi."""

import subprocess
from pathlib import Path


def test_preview_browser_deferred_start_decode_stop_and_still_coexistence() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("live_preview_browser.cjs")),
            str(root / "src/kodi_lab/web/static/live-display.js"),
            str(root / "src/kodi_lab/web/static/managed-screenshot.js"),
        ],
        check=True,
        timeout=10,
    )
