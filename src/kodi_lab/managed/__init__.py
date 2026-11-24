"""Broker-owned managed Kodi application operations."""

RELEASES = {"matrix": (19, 4), "nexus": (20, 5), "omega": (21, 3), "piers": (22, 0)}


def reviewed_image_file(generation: str) -> str:
    return f"reviewed-{generation}-image"


REVIEWED_IMAGE_FILE = reviewed_image_file("omega")
