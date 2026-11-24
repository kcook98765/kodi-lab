from kodi_lab.broker.operations import build_reviewed_runtime_registry


def test_packaged_control_lane_keeps_only_reviewed_acquisition() -> None:
    assert set(build_reviewed_runtime_registry().names) == {
        "active_reviewed_runtime_installation",
        "broker_info",
        "capabilities",
        "launch_reviewed_runtime_installation",
        "observe_reviewed_runtime_installation",
        "ping",
        "reviewed_runtime_image_present",
    }
