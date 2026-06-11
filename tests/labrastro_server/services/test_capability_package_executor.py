from labrastro_server.services.capability_package_executor import (
    CapabilityPackageServerExecutor,
)
from labrastro_server.services.capability_package_install_plan import InstallAction


def test_executor_records_check_executable_result_without_marking_peer_verified(
    tmp_path,
) -> None:
    executor = CapabilityPackageServerExecutor(runtime_root=tmp_path)

    result = executor.execute_action(
        InstallAction.from_dict(
            {
                "id": "check-gh",
                "type": "check_executable",
                "target": "server",
                "params": {"executable": "gh"},
            }
        )
    )

    assert result.action_id == "check-gh"
    assert result.target == "server"
    assert result.status in {"passed", "missing", "failed"}
    assert "server" in result.target_facts
    assert "local_peer" not in result.target_facts


def test_executor_runs_server_half_for_both_target_action(tmp_path) -> None:
    executor = CapabilityPackageServerExecutor(runtime_root=tmp_path)

    result = executor.execute_action(
        InstallAction.from_dict(
            {
                "id": "check-gh",
                "type": "check_executable",
                "target": "both",
                "params": {"executable": "gh"},
            }
        )
    )

    assert result.action_id == "check-gh"
    assert result.target == "server"
    assert result.status in {"passed", "missing", "failed"}
    assert "server" in result.target_facts


def test_python_packages_install_to_package_local_venv_path(tmp_path) -> None:
    executor = CapabilityPackageServerExecutor(runtime_root=tmp_path)

    result = executor.plan_runtime_path(
        package_id="waza",
        action_type="install_python_packages",
    )

    assert str(result).startswith(str(tmp_path))
    assert "waza" in str(result)
