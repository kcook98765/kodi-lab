"""Readiness requires the private X11 display and exact native Omega identity."""

import base64
import json
import subprocess
import urllib.request
from pathlib import Path


def main() -> None:
    subprocess.run(
        ["/usr/bin/xdpyinfo", "-display", ":99"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=1,
    )
    query = {
        "jsonrpc": "2.0",
        "id": "health",
        "method": "Application.GetProperties",
        "params": {"properties": ["name", "version"]},
    }
    request = urllib.request.Request(
        "http://127.0.0.1:8080/jsonrpc",
        data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json"},
    )
    rpc = Path("/managed-v2-credentials/rpc")
    if rpc.is_file():
        password = rpc.read_text().strip()
        auth = base64.b64encode(f"managed:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(request, timeout=1) as response:
        data = response.read(8193)
    if len(data) > 8192:
        raise RuntimeError("oversized health response")
    result = json.loads(data)["result"]
    version = result["version"]
    if (
        result["name"] != "Kodi"
        or type(version["major"]) is not int
        or version["major"] != 21
        or type(version["minor"]) is not int
        or version["minor"] != 3
        or version["tag"] != "stable"
        or version.get("revision") != "20251031-a3a448d-dirty"
    ):
        raise RuntimeError("Flathub Omega identity mismatch")


if __name__ == "__main__":
    main()
