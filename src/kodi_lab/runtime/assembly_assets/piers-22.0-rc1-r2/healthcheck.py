"""Readiness requires the private X11 display and exact native RC1 identity."""

import json
import subprocess
import urllib.request


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
    with urllib.request.urlopen(request, timeout=1) as response:
        data = response.read(8193)
    if len(data) > 8192:
        raise RuntimeError("oversized health response")
    result = json.loads(data)["result"]
    version = result["version"]
    if (
        result["name"] != "Kodi"
        or type(version["major"]) is not int
        or version["major"] != 22
        or type(version["minor"]) is not int
        or version["minor"] != 0
        or version["tag"] != "releasecandidate"
        or type(version["tagversion"]) not in (int, str)
        or str(version["tagversion"]) != "1"
    ):
        raise RuntimeError("RC1 identity mismatch")


if __name__ == "__main__":
    main()
