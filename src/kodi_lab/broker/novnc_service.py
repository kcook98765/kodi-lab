"""Administrator opt-in for the existing human display lane, never an MCP API."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from uuid import UUID, uuid4

import docker  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.novnc import CatalogViewAuthority
from kodi_lab.application.novnc_control import HumanViewerOwnership
from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.novnc import (
    DisplayLane,
    DockerViewTunnel,
    ViewBinding,
    ViewRequest,
)
from kodi_lab.broker.provisioning import SqliteProvisioningResolver
from kodi_lab.broker.server import ProductionBrokerConfiguration
from kodi_lab.config import Settings
from kodi_lab.runtime.recipes import NOVNC_PHASE1_DIGEST
from kodi_lab.runtime.reviewed_capabilities import supports_reviewed_recipe_capability
from kodi_lab.storage.models import NovncSessionRow
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeRevisionRepository,
)


class HumanDisplayService:
    """Resolve only explicitly enabled, sealed, validated Ubuntu Omega r4 pins."""

    def __init__(self, settings: Settings, sessions: sessionmaker[Session]) -> None:
        self.settings = settings
        self.sessions = sessions
        if os.geteuid() == 0:
            from kodi_lab.broker.novnc_state import StateOwnerOwnership

            self.ownership: HumanViewerOwnership = StateOwnerOwnership(
                settings, sessions
            )
        else:
            self.ownership = HumanViewerOwnership(sessions)
        self.broker_id = str(uuid4())
        self.provisioning = SqliteProvisioningResolver(sessions)

    def qualified(self, instance: UUID) -> None:
        if instance not in self.settings.experimental_novnc_instances:
            raise ValueError("experimental human viewer is not enabled")
        with self.sessions() as db:
            managed = ManagedInstanceRepository(db).get(instance)
            revision = (
                RuntimeRevisionRepository(db).get(managed.runtime_revision_id)
                if managed is not None
                else None
            )
            if (
                revision is None
                or revision.manifest_digest != NOVNC_PHASE1_DIGEST
                or not supports_reviewed_recipe_capability(
                    db, revision.id, "screenshot"
                )
                or not supports_reviewed_recipe_capability(
                    db, revision.id, "navigation"
                )
            ):
                raise ValueError("experimental runtime is not qualified")

    def resolve(self, instance: UUID) -> ViewBinding:
        self.qualified(instance)
        spec = self.provisioning.resolve(str(instance))
        client = docker.APIClient(version="auto", timeout=1)
        try:
            containers = client.containers(
                all=True,
                filters={"label": ["io.kodi-lab.instance-id=" + str(instance)]},
            )
            if len(containers) != 1:
                raise ValueError("experimental runtime identity unavailable")
            actual = client.inspect_container(containers[0]["Id"])
            image = client.inspect_image(actual["Image"])
            DockerManagedContainerAdapter._validate_provisioned_container(
                actual,
                expected_container_id=containers[0]["Id"],
                spec=spec,
                image_config=image["Config"],
            )
            if (
                not actual["State"]["Running"]
                or actual["State"].get("Paused")
                or actual["State"].get("Health", {}).get("Status") != "healthy"
            ):
                raise ValueError("experimental runtime is not healthy")
            binding = ViewBinding(
                actual["Id"], actual["Image"], actual["State"]["StartedAt"], ()
            )
            return CatalogViewAuthority(self.sessions, {instance: binding}).resolve(
                instance
            )
        finally:
            client.close()

    def proof(
        self, container: str, exec_id: str | None = None, interactive: bool = False
    ) -> bool:
        client = docker.APIClient(version="auto", timeout=1)
        try:
            actual = client.inspect_container(container)
            if actual["Id"] != container or actual["State"]["Paused"]:
                return False
            if not actual["State"]["Running"]:
                return True
            if exec_id is not None and client.exec_inspect(exec_id)["Running"]:
                return False
            check = client.exec_create(
                container,
                cmd=[
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "/usr/local/lib/kodi-lab-novnc-view.py",
                    "--probe-control" if interactive else "--probe",
                ],
                user="1000:1000",
                stdout=True,
                stderr=False,
                tty=False,
                privileged=False,
            )
            client.exec_start(check["Id"], stream=False, tty=False)
            state = client.exec_inspect(check["Id"])
            return bool(not state["Running"] and state["ExitCode"] == 0)
        except Exception:
            return False
        finally:
            client.close()

    def admit(self, request: ViewRequest) -> DockerViewTunnel:
        if request.attempt is None or request.rate != 2:
            raise ValueError("human Web viewer admission required")
        selected = self.resolve(request.instance)
        attempt = request.attempt
        client = docker.APIClient(version="auto", timeout=1)
        try:
            spec = self.provisioning.resolve(str(request.instance))
            image = client.inspect_image(selected.image)
            self.ownership.claim(
                request.instance,
                attempt,
                request.owner,
                selected,
                request.mode,
                self.broker_id,
            )
        except BaseException:
            client.close()
            self.ownership.finish(
                request.instance,
                attempt,
                self.broker_id,
                self.proof(selected.container),
            )
            raise

        def valid() -> bool:
            try:
                self.qualified(request.instance)
                if (
                    not self.ownership.valid(request.instance, attempt, self.broker_id)
                    or CatalogViewAuthority(
                        self.sessions, {request.instance: selected}
                    ).resolve(request.instance, mode=request.mode)
                    != selected
                ):
                    return False
                actual = client.inspect_container(selected.container)
                DockerManagedContainerAdapter._validate_provisioned_container(
                    actual,
                    expected_container_id=selected.container,
                    spec=spec,
                    image_config=image["Config"],
                )
                return bool(
                    actual["State"]["Running"]
                    and not actual["State"]["Paused"]
                    and actual["State"]["Health"]["Status"] == "healthy"
                    and actual["State"]["StartedAt"] == selected.started
                    and actual["Image"] == selected.image
                )
            except Exception:
                return False

        return DockerViewTunnel(
            selected,
            request,
            valid,
            client=client,
            started=lambda eid: self.ownership.started(
                request.instance, attempt, self.broker_id, eid
            ),
            proof=lambda eid: self.proof(
                selected.container, eid, request.mode == "interactive"
            ),
            finished=lambda proven: self.ownership.finish(
                request.instance, attempt, self.broker_id, proven
            ),
        )

    def reconcile(self, lane: DisplayLane) -> None:
        self.ownership.expire_pending()
        with self.sessions() as db:
            rows = list(
                db.scalars(
                    select(NovncSessionRow).where(
                        NovncSessionRow.broker_id == self.broker_id
                    )
                )
            )
        for row in rows:
            instance = UUID(row.instance_id)
            # Admission adds active before claiming. Keep that exclusion until
            # proof and durable completion, including an unconfirmed admission.
            with lane.lock:
                if instance in lane.active:
                    continue
                container = json.loads(row.identity_json)[0]
                if self.proof(container, row.exec_id, row.mode == "interactive"):
                    self.ownership.finish(
                        instance, UUID(row.attempt), self.broker_id, True
                    )

    def lane(self, configuration: ProductionBrokerConfiguration) -> DisplayLane:
        self.ownership.recover(self.broker_id, self.proof)
        lane = DisplayLane(
            replace(
                configuration.socket,
                socket_path=self.settings.runtime_root / "display.sock",
            ),
            configuration.authorization.expected_uid,
            self.admit,
            reconcile=lambda: self.reconcile(lane),
            resolve=self.resolve,
        )
        return lane
