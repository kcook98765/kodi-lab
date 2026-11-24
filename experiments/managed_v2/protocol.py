"""Closed broker requests shared with the unprivileged HTTP frontend."""

READ = {"managed_status", "snapshot_list", "addon_inventory"}
FIELDS = {
    "managed_prepare": (),
    "managed_start": (),
    "managed_stop": (),
    "managed_reset": (),
    "managed_status": (),
    "managed_recover": (),
    "snapshot_list": (),
    "snapshot_save": ("name",),
    "snapshot_load": ("name",),
    "snapshot_delete": ("name",),
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


def validate(name: object, args: object) -> None:
    if (
        not isinstance(name, str)
        or name not in FIELDS
        or not isinstance(args, dict)
        or set(args) != set(FIELDS[name])
        or any(
            not isinstance(value, str)
            or len(value) > (740_000 if key == "encoded" else 1024)
            for key, value in args.items()
        )
    ):
        raise Refusal("invalid_arguments")
