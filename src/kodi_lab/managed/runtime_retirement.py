"""Explicit retirement of a stopped generation's owned runtime services."""

from __future__ import annotations

import json
import subprocess
import time
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from kodi_lab.storage.inventory_snapshot import read_file

from .feed_retirement import docker_read, idle_feed, observed_engine
from .packages import HEX
from .protocol import Document, Refusal
from .runtime import LABEL, Runtime, atomic, sync_dir

if TYPE_CHECKING:
    from .controller import Controller

OPERATIONS = frozenset({"runtime_retirement_review", "runtime_retirement_execute"})
OBSERVATION_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    IndexError,
    KeyError,
    subprocess.SubprocessError,
)
RECORD_LIMIT = 12 * 1024
PREVIEW_SECONDS = 300
DEPENDENCY_LIMIT = 128


class RetirementModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Selection(RetirementModel):
    generation: str
    target: UUID
    image: str
    slot: str | None
    profile_identity: tuple[int, int] | None


class Assets(RetirementModel):
    engine: str
    feed: str | None
    network: str | None


class Preview(RetirementModel):
    session: str
    expires: int
    action: Literal["retire", "reactivate"]
    identity: str
    previous: str | None = None
    selection: Selection
    assets: Assets


class Retirement(RetirementModel):
    revision: Literal[1] = 1
    identity: str
    selection: Selection
    assets: Assets
    state: Literal["enabled", "pending", "retired"] = "pending"


class RuntimeRetirement:
    def __init__(self, controller: Controller) -> None:
        self.controller = controller

    def runtime(self, generation: str) -> Runtime:
        if generation not in self.controller.runtimes:
            raise Refusal("managed_generation_unavailable")
        return self.controller.runtimes[generation]

    @staticmethod
    def read(native: Runtime) -> Retirement | None:
        try:
            record = Retirement.model_validate_json(
                read_file(native.retirement_file, RECORD_LIMIT)
            )
        except FileNotFoundError:
            return None
        if (
            record.selection.target != native.target
            or record.selection.generation != native.generation
        ):
            raise Refusal("managed_retirement_identity")
        return record

    @staticmethod
    def selection(native: Runtime) -> Selection:
        image = native.image
        slot = None
        ownership = None
        if native.pointer.is_symlink():
            raise Refusal("profile_identity")
        if native.pointer.exists():
            slot = native.read_pointer()
            observed = native.profile(slot).lstat()
            ownership = observed.st_dev, observed.st_ino
        return Selection(
            generation=native.generation,
            target=native.target,
            image=image,
            slot=slot,
            profile_identity=ownership,
        )

    def active_operation(self, native: Runtime) -> bool:
        previous = self.controller.last_operation()
        return (
            native.public.session.exists()
            or native.public.session.is_symlink()
            or bool(previous and previous["state"] == "pending")
            or self.controller.owner() not in {None, "admin"}
        )

    def review(self, generation: str, session: str) -> Document:
        native = self.runtime(generation)
        result: Document = {
            "generation": generation,
            "state": "available",
            "token": None,
            "image": None,
            "working_profile": None,
            "snapshots": None,
            "shared_versions": [],
            "eligible": [],
            "reason": None,
        }
        try:
            record = self.read(native)
            if record and record.state != "enabled":
                result["state"] = record.state
            selected = self.selection(native)
            result["image"] = selected.image
            result["working_profile"] = selected.slot is not None
            result["snapshots"] = self.admin_snapshots(selected.image)
            result["shared_versions"] = self.shared_images(native, selected.image)
            if self.active_operation(native):
                raise Refusal("managed_retirement_pending_operation")
            pending = record if record and record.state == "pending" else None
            if (
                record
                and record.state != "enabled"
                and not same_portable_selection(record.selection, selected)
            ):
                raise Refusal("managed_retirement_identity")
            current = observe_assets(native, None)
            if (
                record
                and record.state != "enabled"
                and (
                    record.state == "retired"
                    or record.selection != selected
                    or record.assets.engine != current.engine
                )
            ):
                if current.feed or current.network:
                    raise Refusal("managed_retirement_assets_changed")
                plan = Preview(
                    session=session,
                    expires=int(time.time()) + PREVIEW_SECONDS,
                    action="reactivate",
                    identity=record.identity,
                    selection=selected,
                    assets=current,
                )
            else:
                if pending and not remaining(pending.assets, current):
                    raise Refusal("managed_retirement_assets_changed")
                assets = pending.assets if pending else current
                if current.feed:
                    result["eligible"].append("Private feed helper")
                if current.network:
                    result["eligible"].append("Private runtime network")
                plan = Preview(
                    session=session,
                    expires=int(time.time()) + PREVIEW_SECONDS,
                    action="retire",
                    identity=pending.identity if pending else uuid4().hex,
                    previous=record.identity if record and pending is None else None,
                    selection=selected,
                    assets=assets,
                )
            result.update(
                token=self.controller.storage_maintenance.token(plan),
                action=plan.action,
            )
        except (Refusal, *OBSERVATION_ERRORS) as error:
            result["reason"] = {
                "managed_retirement_pending_operation": (
                    "Finish the pending native operation or release agent control."
                ),
                "managed_retirement_runtime_active": (
                    "Stop this Kodi runtime in Managed Kodi before retiring it."
                ),
                "managed_retirement_assets_changed": (
                    "The reviewed services changed or have other dependents."
                ),
                "managed_retirement_dependencies_unknown": (
                    "The Engine could not confirm service dependencies."
                ),
            }.get(
                str(error),
                "The runtime, Engine or recovery state could not be verified.",
            )
        return result

    def admin_snapshots(self, image: str) -> list[Document]:
        directory = self.controller.omega.root / "snapshots"
        result: list[Document] = []
        for index, path in enumerate(directory.iterdir()):
            if index >= 256:
                break
            if (
                not path.name.endswith(".tar.gz")
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            try:
                metadata = json.loads(read_file(path.with_suffix(".json"), 256 * 1024))
                if isinstance(metadata, dict) and metadata.get("image") == image:
                    result.append(
                        {
                            "name": path.name.removesuffix(".tar.gz"),
                            "bytes": path.stat().st_size,
                        }
                    )
            except (ValueError, OSError):
                continue
        return result

    def shared_images(self, native: Runtime, image: str) -> list[str]:
        result = []
        for peer in self.controller.runtimes.values():
            if peer is native:
                continue
            try:
                if peer.image == image:
                    result.append(peer.generation)
            except (Refusal, OSError):
                continue
        return result

    def execute(self, raw: str, session: str, generation: str, action: str) -> Document:
        try:
            plan = Preview.model_validate_json(
                self.controller.storage_maintenance.verified_payload(raw)
            )
            if (
                plan.session != session
                or plan.expires <= time.time()
                or plan.selection.generation != generation
                or plan.action != action
            ):
                raise ValueError
        except (ValueError, Refusal):
            raise Refusal("storage_maintenance_preview_expired") from None
        native = self.runtime(generation)
        with self.controller.omega.exclusive():
            state = self.read(native)
            if (
                self.active_operation(native)
                or self.selection(native) != plan.selection
            ):
                raise Refusal("managed_retirement_identity")
            if plan.action == "reactivate":
                if (
                    not state
                    or state.identity != plan.identity
                    or not same_portable_selection(state.selection, plan.selection)
                ):
                    raise Refusal("managed_retirement_identity")
                if state.state != "enabled":
                    current = observe_assets(native, plan.assets.engine)
                    if current != plan.assets or current.feed or current.network:
                        raise Refusal("managed_retirement_assets_changed")
                    self.save(
                        native,
                        state.model_copy(
                            update={
                                "state": "enabled",
                                "assets": plan.assets,
                                "selection": plan.selection,
                            }
                        ),
                    )
                else:
                    sync_dir(native.root)
                return {"state": "available", "generation": generation}
            if state and state.state == "enabled":
                if plan.previous != state.identity:
                    raise Refusal("managed_retirement_identity")
                state = None
            elif (
                state
                and (
                    state.identity != plan.identity
                    or state.selection != plan.selection
                    or state.assets != plan.assets
                )
            ) or (state is None and plan.previous is not None):
                raise Refusal("managed_retirement_identity")
            if state and state.state == "retired":
                return {"state": "retired", "generation": generation}
            current = observe_assets(native, plan.assets.engine)
            if (state and not remaining(state.assets, current)) or (
                not state and current != plan.assets
            ):
                raise Refusal("managed_retirement_assets_changed")
            if state is None:
                state = Retirement(
                    identity=plan.identity, selection=plan.selection, assets=plan.assets
                )
                self.save(native, state)
            else:
                sync_dir(native.root)
            try:
                retire_services(native, state.assets)
            except (Refusal, *OBSERVATION_ERRORS):
                return {"state": "pending", "generation": generation}
            self.save(native, state.model_copy(update={"state": "retired"}))
            return {"state": "retired", "generation": generation}

    @staticmethod
    def save(native: Runtime, state: Retirement) -> None:
        atomic(native.retirement_file, state.model_dump_json().encode())

    def request(self, name: str, args: object) -> Document:
        if not isinstance(args, dict) or not all(
            isinstance(value, str) for value in args.values()
        ):
            raise Refusal("invalid_storage_maintenance_request")
        session = args.get("session", "")
        if not HEX.fullmatch(session):
            raise Refusal("invalid_storage_maintenance_request")
        if name == "runtime_retirement_review" and set(args) == {
            "session",
            "generation",
        }:
            return self.review(args["generation"], session)
        if (
            name == "runtime_retirement_execute"
            and set(args) == {"session", "generation", "action", "token"}
            and 1 <= len(args["token"]) <= 2048
        ):
            return self.execute(
                args["token"], session, args["generation"], args["action"]
            )
        raise Refusal("invalid_storage_maintenance_request")


def same_portable_selection(previous: Selection, current: Selection) -> bool:
    # A restore keeps the slot and pinned image. Without a slot, an installer
    # may reacquire a newer reviewed image while the runtime is disabled.
    return (
        previous.generation == current.generation
        and previous.target == current.target
        and previous.slot == current.slot
        and (previous.slot is None or previous.image == current.image)
    )


def remaining(original: Assets, current: Assets) -> bool:
    return (
        original.engine == current.engine
        and current.feed in (None, original.feed)
        and current.network in (None, original.network)
    )


def observe_assets(
    native: Runtime, expected: str | None, *, for_network_change: bool = False
) -> Assets:
    deadline = time.monotonic() + (15 if for_network_change else 1.5)
    engine = expected or observed_engine(deadline)
    helper = idle_feed(native, engine, deadline=deadline)
    if helper is None:
        raise Refusal("managed_retirement_runtime_active")
    names = docker_read(
        "ps",
        "-aq",
        "--no-trunc",
        "--filter",
        f"name=^/{native.feed_name}$",
        deadline=deadline,
    ).splitlines()
    if names != ([helper.identity] if helper.identity else []):
        raise Refusal("managed_retirement_assets_changed")
    networks = docker_read(
        "network",
        "ls",
        "--no-trunc",
        "-q",
        "--filter",
        f"name=^{native.network_name}$",
        deadline=deadline,
    ).splitlines()
    if len(networks) > 1:
        raise Refusal("managed_retirement_assets_changed")
    network = networks[0] if networks else None
    if network:
        if not HEX.fullmatch(network):
            raise Refusal("managed_retirement_assets_changed")
        observed = json.loads(
            docker_read("network", "inspect", network, deadline=deadline)
        )[0]
        containers = observed.get("Containers") or {}
        if (
            observed["Id"] != network
            or observed["Name"] != native.network_name
            or observed["Driver"] != "bridge"
            or type(observed["Internal"]) is not bool
            or observed["Labels"].get(LABEL) != str(native.target)
            or not isinstance(containers, dict)
            or set(containers) - {helper.identity}
        ):
            raise Refusal("managed_retirement_assets_changed")
        if helper.identity:
            require_feed_network(native, helper.identity, network, deadline)
        require_no_service_dependents(
            native,
            helper.identity,
            network,
            deadline,
            bounded_inventory=not for_network_change,
        )
    elif helper.identity is not None:
        raise Refusal("managed_retirement_assets_changed")
    return Assets(engine=engine, feed=helper.identity, network=network)


def require_feed_network(
    native: Runtime, feed: str, network: str, deadline: float
) -> None:
    observed = json.loads(
        docker_read("inspect", "--type", "container", feed, deadline=deadline)
    )[0]
    attached = observed["NetworkSettings"]["Networks"]
    if (
        observed["Id"] != feed
        or observed["HostConfig"]["NetworkMode"] != network
        or set(attached) - {native.network_name}
    ):
        raise Refusal("managed_retirement_assets_changed")
    own = attached.get(native.network_name)
    if own and own["NetworkID"] == network:
        return
    # Docker releases a stopped container's live endpoint but retains its mode.
    if observed["State"]["Running"] is False and (not own or not own["NetworkID"]):
        return
    raise Refusal("managed_retirement_assets_changed")


def require_no_service_dependents(
    native: Runtime,
    feed: str | None,
    network: str,
    deadline: float,
    *,
    bounded_inventory: bool = True,
) -> None:
    # Network inspect reports active endpoints. Stopped containers can keep
    # dependencies on the network or on the helper's network namespace.
    identifiers = docker_read(
        "container",
        "ls",
        "--all",
        "--quiet",
        "--no-trunc",
        *(["--last", str(DEPENDENCY_LIMIT + 1)] if bounded_inventory else []),
        deadline=deadline,
    ).splitlines()
    if (bounded_inventory and len(identifiers) > DEPENDENCY_LIMIT) or not all(
        HEX.fullmatch(item) for item in identifiers
    ):
        raise Refusal("managed_retirement_dependencies_unknown")
    others = [item for item in identifiers if item != feed]
    if not others:
        return
    state_format = "\t".join(
        (
            "{{.Id}}",
            "{{json .HostConfig.NetworkMode}}",
            "{{json .HostConfig.IpcMode}}",
            "{{json .HostConfig.PidMode}}",
            "{{json .HostConfig.VolumesFrom}}",
            "{{json .HostConfig.Links}}",
            "{{json .NetworkSettings.Networks}}",
        )
    )
    states: list[str] = []
    for offset in range(0, len(others), DEPENDENCY_LIMIT):
        states.extend(
            docker_read(
                "container",
                "inspect",
                "--format",
                state_format,
                *others[offset : offset + DEPENDENCY_LIMIT],
                deadline=deadline,
            ).splitlines()
        )
    if len(states) != len(others):
        raise Refusal("managed_retirement_dependencies_unknown")
    for identifier, line in zip(others, states, strict=True):
        observed, network_mode, ipc, pid, volumes, links, attached = line.split("\t")
        if observed != identifier:
            raise Refusal("managed_retirement_dependencies_unknown")
        mode = json.loads(network_mode).removeprefix("/")
        attachments = json.loads(attached) or {}
        if (
            mode == native.network_name
            or matches_id(network, mode)
            or native.network_name in attachments
            or any(item["NetworkID"] == network for item in attachments.values())
        ):
            raise Refusal("managed_retirement_assets_changed")
        if feed and refers_to_helper(native, feed, mode, ipc, pid, volumes, links):
            raise Refusal("managed_retirement_assets_changed")


def refers_to_helper(
    native: Runtime, feed: str, mode: str, ipc: str, pid: str, volumes: str, links: str
) -> bool:
    namespaces = [
        namespace.removeprefix("container:")
        for namespace in (mode, json.loads(ipc), json.loads(pid))
        if namespace.startswith("container:")
    ]
    references = namespaces + (json.loads(volumes) or []) + (json.loads(links) or [])
    for reference in references:
        name = reference.removeprefix("/").partition(":")[0]
        if name == native.feed_name or matches_id(feed, name):
            return True
    return False


def matches_id(identifier: str, short: str) -> bool:
    return bool(short) and identifier.startswith(short)


def retire_services(native: Runtime, assets: Assets) -> None:
    from .runtime import DOCKER

    current = observe_assets(native, assets.engine)
    if not remaining(assets, current):
        raise Refusal("managed_retirement_assets_changed")
    if current.feed:
        feed = current.feed
        subprocess.run(
            [*DOCKER, "container", "stop", "--time", "2", feed],
            capture_output=True,
            timeout=4,
            check=True,
        )
        current = observe_assets(native, assets.engine)
        if not remaining(assets, current):
            raise Refusal("managed_retirement_assets_changed")
        if current.feed:
            subprocess.run(
                [*DOCKER, "container", "rm", feed],
                capture_output=True,
                timeout=4,
                check=True,
            )
    current = observe_assets(native, assets.engine)
    if not remaining(assets, current) or current.feed:
        raise Refusal("managed_retirement_assets_changed")
    if current.network:
        subprocess.run(
            [*DOCKER, "network", "rm", current.network],
            capture_output=True,
            timeout=4,
            check=True,
        )
    current = observe_assets(native, assets.engine)
    if current.feed or current.network:
        raise Refusal("managed_retirement_assets_changed")
