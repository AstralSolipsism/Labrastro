from labrastro_server.services.capability_package_install_plan import (
    INSTALL_ACTION_TYPES,
    InstallAction,
    InstallPlan,
)


def test_install_action_catalog_is_typed() -> None:
    assert "install_python_packages" in INSTALL_ACTION_TYPES

    action = InstallAction.from_dict(
        {
            "id": "act-1",
            "type": "install_python_packages",
            "target": "server",
            "params": {"packages": ["readability-lxml"], "venv": "venvs/waza"},
        }
    )

    assert action.type == "install_python_packages"
    assert action.params["packages"] == ["readability-lxml"]


def test_unknown_install_action_is_rejected() -> None:
    try:
        InstallAction.from_dict({"id": "act-1", "type": "shell", "target": "server"})
    except ValueError as exc:
        assert "unknown install action type" in str(exc)
    else:
        raise AssertionError("unknown shell action should be rejected")


def test_install_plan_roundtrip_preserves_typed_actions() -> None:
    plan = InstallPlan.from_dict(
        {
            "package_id": "waza",
            "actions": [
                {
                    "id": "check-gh",
                    "type": "check_executable",
                    "target": "server",
                    "params": {"executable": "gh"},
                }
            ],
        }
    )

    assert [action.id for action in plan.actions] == ["check-gh"]
    assert plan.to_dict()["actions"][0]["type"] == "check_executable"


def test_install_action_accepts_action_id_alias() -> None:
    action = InstallAction.from_dict(
        {
            "action_id": "check-gh",
            "type": "check_executable",
            "target": "local_peer",
            "component_id": "envreq:executable:gh",
        }
    )

    assert action.id == "check-gh"
    assert action.to_dict()["id"] == "check-gh"
