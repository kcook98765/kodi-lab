"""Bounded destructive tests for the one disposable Omega trial."""

import asyncio
import json

from .controller import Controller
from .packages import sha
from .runtime import ESTUARY, atomic, wait_until


async def interruptions(
    root,
    trial,
    call,
    install,
    launch,
    initial,
    update,
    skin_fixture,
    competing_call,
    third_party=None,
):
    controller = Controller(root)
    runtime = controller.runtime
    evidence = root / "evidence"
    rows = []
    fault = root / "qualification-fault"

    def note(case, **observed):
        rows.append({"case": case, "observed": observed})
        atomic(evidence / "interruptions.json", json.dumps(rows, indent=2).encode())

    async def lose(event, request):
        atomic(fault, event.encode())
        result = await request
        assert result["error"] == "operation_outcome_unconfirmed", result
        assert trial.broker.wait(timeout=5) == 86
        assert not fault.exists()
        trial.start_broker()
        status = await call("managed_status")
        assert status["operation"]["event"] == event, status
        return status

    async def recover(status):
        assert status["operation"]["state"] == "pending", status
        blocked = await call("managed_reset", required=False)
        assert blocked["error"] == "managed_recovery_required", blocked
        result = await call("managed_recover")
        assert result["completion"] == "unconfirmed", result
        return result

    await call("managed_prepare")
    slot = runtime.read_pointer()
    source = controller.packages.workspaces / initial.id / "default.py"
    source_digest = sha(source.read_bytes())

    status = await lose("runtime_created", call("managed_start", required=False))
    created = runtime.inspect()["Id"]
    await asyncio.to_thread(wait_until, runtime.ready, 30, "interrupted_start_readback")
    await recover(status)
    assert (await call("managed_status"))["state"] == "ready"
    note("Start after Docker create", observed_ready=True, same_container=created)

    cold = await install(initial)
    assert cold["state"] == "verified"
    await call(
        "addon_setting",
        {
            "addon": initial.id,
            "key": "marker",
            "value": "snapshot-sentinel",
        },
    )
    status = await lose("runtime_removed", call("managed_stop", required=False))
    assert not runtime.inspect()
    await recover(status)
    assert runtime.read_pointer() == slot
    assert not (runtime.credentials / "rpc").exists()
    note("Stop after container removal", no_container=True, same_profile=True)

    stable = await call("snapshot_save", {"name": "Known good"})
    status = await lose(
        "snapshot_before_publish",
        call("snapshot_save", {"name": "Interrupted"}, required=False),
    )
    await recover(status)
    assert not list(controller.snapshots.root.glob(".stage-*"))
    assert [row["name"] for row in await call("snapshot_list")] == ["Known good"]
    note("Snapshot before publication", interrupted_snapshot_absent=True)

    for tool, args in (
        ("snapshot_load", {"name": "Known good"}),
        ("managed_reset", {}),
    ):
        status = await lose(
            "profile_before_commit",
            call(tool, args, required=False),
        )
        assert runtime.read_pointer() == slot
        assert len(list((root / "volumes").iterdir())) == 2
        await recover(status)
        assert runtime.read_pointer() == slot
        assert len(list((root / "volumes").iterdir())) == 1
        note(
            tool + " before pointer commit",
            staged_volume_retired=True,
            same_profile=True,
        )

    await call("managed_start")
    assert runtime.inspect()["Id"] != created
    await launch("target-1.0.0 1.0.0 before=snapshot-sentinel deps=a+leaf,b")
    note("Recreate following interruption", new_container=True, retained_execution=True)

    native = asyncio.create_task(install(update, required=False))
    archive = (
        runtime.profile() / "addons/packages" / f"{update.id}-{update.version}.zip"
    )
    await asyncio.to_thread(
        wait_until,
        lambda: archive.is_file() and sha(archive.read_bytes()) == update.digest,
        12,
        "native_dispatch_readback",
    )
    # The zip proves Kodi received the intent. The controller has not committed a reply.
    before = controller.last_operation()
    assert before["state"] == "pending" and before["native_pending"], before
    trial.broker.kill()
    assert trial.broker.wait(timeout=5) == -9
    result = await native
    assert result["error"] == "operation_outcome_unconfirmed", result
    trial.start_broker()
    status = await call("managed_status")
    assert (await recover(status))["native_verification_required"]
    assert (await call("managed_status"))["state"] == "stopped"
    note(
        "Native installer interruption",
        native_zip_observed=True,
        phase=before["event"],
        completion="unconfirmed",
    )

    await call("managed_start")
    await call("snapshot_list")
    status = await lose("reply", install(update, required=False))
    operation = status["operation"]
    assert operation["state"] == "complete", operation
    assert operation["result"]["package"]["digest"] == update.digest
    assert operation["result"]["state"] == "verified"
    await launch("target-1.1.0 1.1.0 before=snapshot-sentinel deps=a+leaf,b")
    note(
        "Lost response after installation",
        reconciled_with="managed_status",
        verified_digest=update.digest,
        executed=True,
    )

    if third_party:
        observed = await install(third_party)
        assert observed["package"]["digest"] == third_party.digest
        note(
            "Third-party pinned ZIP via managed repository",
            package=observed["package"],
            dependencies=observed["dependencies"],
        )

    skin = skin_fixture()
    await install(skin)
    await call("managed_skin", {"addon": skin.id})
    status = await lose("estuary_active", install(update, required=False))
    assert runtime.active_skin() == ESTUARY
    await recover(status)
    assert runtime.active_skin() == skin.id
    await launch("target-1.1.0 1.1.0 before=snapshot-sentinel deps=a+leaf,b")
    note("Interruption in temporary Estuary", fallback=ESTUARY, restored=skin.id)

    earlier = controller.last_operation()["id"]
    first = asyncio.create_task(install(update))
    await asyncio.to_thread(
        wait_until,
        lambda: (
            (record := controller.last_operation())["id"] != earlier
            and record["state"] == "pending"
        ),
        4,
        "competing_request_window",
    )
    busy = await competing_call()
    assert busy["error"] == "managed_busy", busy
    assert (await first)["state"] == "verified"
    note("Competing authenticated mutations", second="managed_busy", first="verified")

    await call("managed_stop")
    assert [row["name"] for row in await call("snapshot_list")] == ["Known good"]
    assert sha(controller.snapshots.path("Known good").read_bytes()) == stable["sha256"]
    assert sha(source.read_bytes()) == source_digest
    note(
        "Independently retained assets",
        snapshot_sha256=stable["sha256"],
        source_sha256=source_digest,
    )
