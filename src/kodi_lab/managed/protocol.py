"""Closed broker requests shared with the unprivileged HTTP frontend."""

from collections.abc import Callable
from typing import Any, TypedDict
from uuid import UUID

# Docker, Kodi JSON-RPC and the durable operation document are decoded at
# their respective boundaries. The closed request and outer reply are typed.
Document = dict[str, Any]
Checkpoint = Callable[..., None]


class OperationReply(TypedDict):
    result: object
    elapsed_seconds: float


READ = {
    "managed_status",
    "snapshot_list",
    "addon_inventory",
    "managed_logs",
    "managed_screen",
    "managed_gui",
    "workspace_tree",
    "workspace_read",
}
PRIVATE = {
    "snapshot_list",
    "snapshot_delete",
    "workspace_open",
    "workspace_tree",
    "workspace_read",
    "workspace_edit",
    "workspace_package",
    "package_import",
}
ADMIN = {"managed_take_control", "managed_recover"}
FIELDS = {
    "managed_prepare": (),
    "managed_start": (),
    "managed_stop": (),
    "managed_reset": (),
    "managed_status": (),
    "managed_acquire": (),
    "managed_release": (),
    "managed_take_control": (),
    "managed_logs": (),
    "managed_gui": (),
    "managed_screen": (),
    "managed_navigate": ("action",),
    "managed_recover": (),
    "snapshot_list": (),
    "snapshot_save": ("name",),
    "snapshot_load": ("name",),
    "snapshot_delete": ("name",),
    "workspace_open": ("digest",),
    "workspace_tree": ("addon",),
    "workspace_read": ("addon", "path"),
    "workspace_edit": ("addon", "path", "content", "expected_digest"),
    "workspace_package": ("addon",),
    "workspace_deploy": ("addon",),
    "addon_install": ("addon", "version", "digest"),
    "package_import": ("encoded", "digest"),
    "addon_launch": ("addon",),
    "addon_setting": ("addon", "key", "value"),
    "addon_inventory": (),
    "managed_skin": ("addon",),
}


class Refusal(Exception):
    pass


def actor_key(actor: object) -> str:
    if actor == "admin":
        return "admin"
    if isinstance(actor, str) and len(actor) == 36:
        try:
            if str(UUID(actor)) == actor:
                return actor
        except ValueError:
            pass
    raise Refusal("invalid_managed_actor")


def validate(name: object, args: object) -> tuple[str, dict[str, str]]:
    if (
        not isinstance(name, str)
        or name not in FIELDS
        or not isinstance(args, dict)
        or not (
            set(args) == set(FIELDS[name])
            or (
                name == "addon_install"
                and set(args) == {*FIELDS[name], "allow_services"}
            )
        )
        or any(
            not isinstance(value, str)
            or len(value) > ({"encoded": 740_000, "content": 24_000}.get(key, 1024))
            for key, value in args.items()
        )
    ):
        raise Refusal("invalid_arguments")
    return name, args
