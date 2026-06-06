from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import labrastro_server.services.capability_packages as capability_packages_module
from labrastro_server.interfaces.http.remote.service import (
    RemoteRelayHTTPService,
    _RemoteSessionRun,
)
from labrastro_server.services.agent_runtime.control_plane import AgentRunControlPlane
from labrastro_server.services.agent_runtime.executor_backend import (
    ExecutorEvent,
    ExecutorRunResult,
)
from labrastro_server.services.agent_runtime.session_projection import (
    agent_run_events_to_session_events,
    agent_run_event_to_session_events,
)
from labrastro_server.services.capability_packages import (
    CapabilityDraftValidator,
    CapabilityPackagerRunner,
    CapabilityPackageIngestError,
    CapabilityPackageIngestService,
    CapabilityPackageInstaller,
    CapabilityPackageSessionRunService,
    CapabilitySourceCollector,
    EvidenceBundle,
)
from reuleauxcoder.domain.agent_runtime.models import CapabilityComponentConfig
from reuleauxcoder.domain.agent_runtime.models import AgentRunRecord
from reuleauxcoder.domain.session.document import apply_session_event


def _control_plane() -> AgentRunControlPlane:
    return AgentRunControlPlane(
        runtime_snapshot={
            "agents": {
                "capability_packager": {
                    "runtime_profile": "capability_packager_remote",
                }
            },
            "runtime_profiles": {
                "capability_packager_remote": {
                    "executor": "fake",
                    "execution_location": "remote_server",
                    "worker_kind": "sandbox_worker",
                    "sandbox": {},
                }
            },
        }
    )


def _wait_for(predicate, *, timeout_sec: float = 3.0):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for condition")


def _append_raw_agent_run_event(
    control: AgentRunControlPlane,
    task_id: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    with control._lock:  # type: ignore[attr-defined]
        control._append_event_locked(task_id, event_type, payload)  # type: ignore[attr-defined]


def _review_draft(*, command: str = "gh") -> dict[str, object]:
    return {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "contributions": {
            "environment_requirements": [
                {
                    "id": f"envreq:executable:{command}",
                    "kind": "executable",
                    "name": command,
                    "command": command,
                    "check": f"{command} --version",
                }
            ]
        },
        "install_plan": [f"Install {command}."],
        "usage": [f"Use {command} pr view."],
        "evidence": [{"title": "Project notes", "excerpt": f"Install {command} and run {command} --version"}],
        "credentials": ["GITHUB_TOKEN"],
        "risk_level": "low",
    }


def _capability_patch_json(
    field_path: str,
    value: object,
    *,
    source_path: str = "",
) -> str:
    patch: dict[str, object] = {
        "field_path": field_path,
        "value": value,
    }
    if source_path:
        patch["source_refs"] = [{"source_path": source_path}]
    return json.dumps({"capability_draft_patch": patch})


def _capability_patch_stream(
    patches: list[tuple[str, object]],
    *,
    source_path: str = "",
) -> str:
    return "\n".join(
        _capability_patch_json(field_path, value, source_path=source_path)
        for field_path, value in patches
    )


def test_agent_run_log_event_projects_as_process_context() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 1,
            "type": "log",
            "payload": {
                "type": "log",
                "text": "loading source bundle",
                "data": {"level": "info"},
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["context_event"]
    payload = session_events[0][1]
    assert payload["phase"] == "agent_run_log"
    assert payload["log"] == "loading source bundle"
    assert payload["level"] == "info"


def test_lifecycle_hook_session_projection_hides_raw_technical_details() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 2,
            "type": "lifecycle_hook",
            "payload": {
                "type": "lifecycle_hook",
                "data": {
                    "phase": "result",
                    "event_name": "PostToolUse",
                    "hook_id": "hook:command",
                    "display_name": "Command observer",
                    "source": "skill",
                    "handler_type": "command",
                    "decision": "none",
                    "continue_flow": True,
                    "level": "warning",
                    "title": "Command observer",
                    "payload": {
                        "tool_names": ["shell"],
                        "technical": {
                            "command": "RAW_COMMAND_SECRET",
                            "stdout": "RAW_STDOUT_SECRET",
                        },
                    },
                    "output": {
                        "diagnostics": [
                            {
                                "code": "command_nonzero_exit",
                                "message": "Command exited with code 1",
                                "stdout": "RAW_STDOUT_SECRET",
                                "stderr": "RAW_STDERR_SECRET",
                            }
                        ],
                        "artifacts": [{"kind": "raw", "content": "RAW_ARTIFACT_SECRET"}],
                    },
                    "diagnostics": [
                        {
                            "code": "command_nonzero_exit",
                            "message": "Command exited with code 1",
                            "stdout": "RAW_STDOUT_SECRET",
                        }
                    ],
                },
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["lifecycle_hook"]
    payload = session_events[0][1]
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert payload["phase"] == "result"
    assert payload["event_name"] == "PostToolUse"
    assert payload["hook_id"] == "hook:command"
    assert payload["display_name"] == "Command observer"
    assert payload["source"] == "skill"
    assert payload["handler_type"] == "command"
    assert payload["diagnostics"] == [
        {"code": "command_nonzero_exit", "message": "Command exited with code 1"}
    ]
    assert "payload" not in payload
    assert "output" not in payload
    assert "technical" not in rendered
    assert "RAW_COMMAND_SECRET" not in rendered
    assert "RAW_STDOUT_SECRET" not in rendered
    assert "RAW_STDERR_SECRET" not in rendered
    assert "RAW_ARTIFACT_SECRET" not in rendered
    assert payload["raw_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 2, "type": "lifecycle_hook"}
    ]


def test_lifecycle_hook_session_projection_exposes_overflow_artifact_refs_without_raw_values() -> None:
    huge = "OVERSIZED_LIFECYCLE_SECRET" * 1000
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 22,
            "type": "lifecycle_hook",
            "payload": {
                "type": "lifecycle_hook",
                "data": {
                    "phase": "result",
                    "event_name": "UserPromptSubmit",
                    "hook_id": "hook:oversized",
                    "display_name": "Oversized output guard",
                    "source": "skill",
                    "handler_type": "prompt",
                    "decision": "deny",
                    "continue_flow": False,
                    "diagnostics": [
                        {
                            "code": "lifecycle_output_overflow",
                            "message": "Lifecycle hook output exceeded size limits.",
                            "artifact_refs": [
                                "lifecycle-output-overflow:hook_oversized:1"
                            ],
                            "raw": huge,
                        }
                    ],
                    "output": {
                        "reason": "truncated reason",
                        "artifacts": [
                            {
                                "kind": "lifecycle_output_overflow",
                                "id": "lifecycle-output-overflow:hook_oversized:1",
                                "field": "reason",
                                "original_chars": len(huge),
                                "content": huge,
                            }
                        ],
                    },
                },
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["lifecycle_hook"]
    payload = session_events[0][1]
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert "OVERSIZED_LIFECYCLE_SECRET" not in rendered
    assert payload["diagnostics"] == [
        {
            "code": "lifecycle_output_overflow",
            "message": "Lifecycle hook output exceeded size limits.",
        }
    ]
    assert payload["artifacts"] == [
        {
            "kind": "lifecycle_output_overflow",
            "id": "lifecycle-output-overflow:hook_oversized:1",
            "field": "reason",
            "original_chars": len(huge),
        }
    ]
    assert payload["raw_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 22, "type": "lifecycle_hook"}
    ]


def test_lifecycle_hook_session_projection_preserves_output_audit_fields_without_model_only_values() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 23,
            "type": "lifecycle_hook",
            "payload": {
                "type": "lifecycle_hook",
                "data": {
                    "phase": "result",
                    "event_name": "PreToolUse",
                    "hook_id": "hook:pretool",
                    "display_name": "PreToolUse guard",
                    "source": "skill",
                    "handler_type": "command",
                    "decision": "deny",
                    "continue_flow": False,
                    "reason": "shell command blocked by lifecycle",
                    "user_message": "This command needs review.",
                    "diagnostics": [
                        {
                            "code": "lifecycle_output_field_ignored",
                            "message": "additional_context is model-only.",
                            "field": "additional_context",
                            "raw": "RAW_DIAGNOSTIC_SECRET",
                        }
                    ],
                    "artifacts": [
                        {
                            "kind": "review",
                            "id": "artifact-1",
                            "content": "RAW_ARTIFACT_SECRET",
                        }
                    ],
                    "output": {
                        "additional_context": [
                            {
                                "role": "system",
                                "content": "RAW_MODEL_CONTEXT_SECRET",
                            }
                        ],
                        "updated_input": {
                            "tool_call": {
                                "name": "shell",
                                "arguments": {"command": "RAW_REWRITTEN_COMMAND"},
                            }
                        },
                        "diagnostics": [
                            {
                                "code": "raw_output_diagnostic",
                                "message": "RAW_OUTPUT_DIAGNOSTIC_SECRET",
                            }
                        ],
                    },
                },
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["lifecycle_hook"]
    payload = session_events[0][1]
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert payload["decision"] == "deny"
    assert payload["continue_flow"] is False
    assert payload["reason"] == "shell command blocked by lifecycle"
    assert payload["user_message"] == "This command needs review."
    assert payload["diagnostics"] == [
        {
            "code": "lifecycle_output_field_ignored",
            "message": "additional_context is model-only.",
        }
    ]
    assert payload["artifacts"] == [{"kind": "review", "id": "artifact-1"}]
    assert "output" not in payload
    assert "updated_input" not in rendered
    assert "RAW_MODEL_CONTEXT_SECRET" not in rendered
    assert "RAW_REWRITTEN_COMMAND" not in rendered
    assert "RAW_DIAGNOSTIC_SECRET" not in rendered
    assert "RAW_ARTIFACT_SECRET" not in rendered
    assert "RAW_OUTPUT_DIAGNOSTIC_SECRET" not in rendered


@pytest.mark.parametrize(
    ("handler_type", "raw_fields"),
    [
        (
            "prompt",
            {
                "prompt": "RAW_PROMPT_SECRET",
                "completion": "RAW_COMPLETION_SECRET",
            },
        ),
        (
            "command",
            {
                "command": "RAW_COMMAND_SECRET",
                "stdout": "RAW_STDOUT_SECRET",
                "stderr": "RAW_STDERR_SECRET",
            },
        ),
        (
            "http",
            {
                "request_body": "RAW_HTTP_REQUEST_SECRET",
                "response_body": "RAW_HTTP_RESPONSE_SECRET",
            },
        ),
        (
            "mcp_tool",
            {
                "arguments": {"secret": "RAW_MCP_ARGUMENT_SECRET"},
                "result": "RAW_MCP_RESULT_SECRET",
            },
        ),
        (
            "agent",
            {
                "prompt": "RAW_AGENT_PROMPT_SECRET",
                "result": "RAW_AGENT_RESULT_SECRET",
            },
        ),
        (
            "internal",
            {
                "legacy_context": "RAW_INTERNAL_CONTEXT_SECRET",
                "result": "RAW_INTERNAL_RESULT_SECRET",
            },
        ),
    ],
)
def test_lifecycle_hook_session_projection_hides_handler_raw_outputs(
    handler_type: str,
    raw_fields: dict,
) -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 3,
            "type": "lifecycle_hook",
            "payload": {
                "type": "lifecycle_hook",
                "data": {
                    "phase": "result",
                    "event_name": "PostToolUse",
                    "hook_id": f"hook:{handler_type}",
                    "display_name": f"{handler_type} observer",
                    "source": "skill",
                    "handler_type": handler_type,
                    "payload": {
                        "tool_names": ["shell"],
                        "technical": dict(raw_fields),
                    },
                    "technical": {
                        "handler_ref": "RAW_HANDLER_REF_SECRET",
                        **raw_fields,
                    },
                    "output": {
                        "raw": dict(raw_fields),
                        "diagnostics": [
                            {
                                "code": f"{handler_type}_failed_open",
                                "message": f"{handler_type} failed open",
                                **raw_fields,
                            }
                        ],
                        "artifacts": [
                            {
                                "kind": "raw",
                                "content": (
                                    f"RAW_{handler_type.upper()}_ARTIFACT_SECRET"
                                ),
                            }
                        ],
                    },
                    "diagnostics": [
                        {
                            "code": f"{handler_type}_failed_open",
                            "message": f"{handler_type} failed open",
                            **raw_fields,
                        }
                    ],
                },
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["lifecycle_hook"]
    payload = session_events[0][1]
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert payload["handler_type"] == handler_type
    assert payload["diagnostics"] == [
        {
            "code": f"{handler_type}_failed_open",
            "message": f"{handler_type} failed open",
        }
    ]
    assert "payload" not in payload
    assert "technical" not in rendered
    assert "RAW_HANDLER_REF_SECRET" not in rendered
    for raw_value in raw_fields.values():
        if isinstance(raw_value, dict):
            for nested_value in raw_value.values():
                assert str(nested_value) not in rendered
            continue
        assert str(raw_value) not in rendered
    assert f"RAW_{handler_type.upper()}_ARTIFACT_SECRET" not in rendered


@pytest.mark.parametrize(
    "handler_type",
    ["prompt", "command", "http", "mcp_tool", "agent", "internal"],
)
def test_lifecycle_hook_session_projection_preserves_success_result_for_each_handler_type(
    handler_type: str,
) -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 4,
            "type": "lifecycle_hook",
            "payload": {
                "type": "lifecycle_hook",
                "data": {
                    "phase": "result",
                    "event_name": "PostToolUse",
                    "hook_id": f"hook:{handler_type}",
                    "display_name": f"{handler_type} observer",
                    "source": "skill",
                    "handler_type": handler_type,
                    "decision": "none",
                    "continue_flow": True,
                    "level": "info",
                    "message": f"{handler_type} lifecycle hook completed",
                    "payload": {
                        "tool_names": ["shell"],
                        "tool_call_ids": ["call-1"],
                        "tool_sources": ["builtin"],
                    },
                    "diagnostics": [
                        {
                            "code": f"{handler_type}_ok",
                            "message": f"{handler_type} completed",
                            "handler_type": handler_type,
                        }
                    ],
                },
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["lifecycle_hook"]
    payload = session_events[0][1]
    assert payload["phase"] == "result"
    assert payload["event_name"] == "PostToolUse"
    assert payload["hook_id"] == f"hook:{handler_type}"
    assert payload["handler_type"] == handler_type
    assert payload["decision"] == "none"
    assert payload["continue_flow"] is True
    assert payload["message"] == f"{handler_type} lifecycle hook completed"
    assert payload["tool_names"] == ["shell"]
    assert payload["tool_call_ids"] == ["call-1"]
    assert payload["tool_sources"] == ["builtin"]
    assert payload["diagnostics"] == [
        {
            "code": f"{handler_type}_ok",
            "message": f"{handler_type} completed",
            "handler_type": handler_type,
        }
    ]
    assert payload["raw_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 4, "type": "lifecycle_hook"}
    ]


def test_delegated_agent_run_completion_projects_as_process_context() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "parent-run",
            "seq": 2,
            "type": "delegated_run_completed",
            "payload": {
                "agent_run_id": "child-run",
                "agent_id": "reviewer",
                "status": "completed",
                "result": "review passed",
                "source": "delegation",
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["context_event"]
    payload = session_events[0][1]
    assert payload["phase"] == "delegated_run_completed"
    assert payload["title"] == "reviewer completed"
    assert payload["agent_run_id"] == "parent-run"
    assert payload["child_agent_run_id"] == "child-run"
    assert payload["agent_run_status"] == "completed"
    assert payload["result"] == "review passed"
    assert payload["meta"]["source"] == "delegation"


def test_agent_run_lifecycle_hook_event_projects_as_lifecycle_hook_session_event() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 3,
            "type": "lifecycle_hook",
            "payload": {
                "type": "lifecycle_hook",
                "data": {
                    "phase": "result",
                    "event_name": "Stop",
                    "hook_id": "hook:stop",
                    "display_name": "Stop review",
                    "level": "info",
                    "message": "review passed",
                },
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["lifecycle_hook"]
    payload = session_events[0][1]
    assert payload["phase"] == "result"
    assert payload["event_name"] == "Stop"
    assert payload["hook_id"] == "hook:stop"
    assert payload["agent_run_id"] == "run-1"
    assert payload["workflow"] == "agent_run"
    assert payload["raw_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 3, "type": "lifecycle_hook"}
    ]


def test_agent_run_usage_event_projects_lifecycle_prompt_token_accounting() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 4,
            "type": "usage",
            "payload": {
                "type": "usage",
                "data": {
                    "prompt_tokens": 7,
                    "completion_tokens": 10,
                    "run_status": "running",
                    "usage_extra": {
                        "lifecycle_prompt": {
                            "hook_id": "hook:prompt-review",
                            "provider": "test",
                        }
                    },
                },
            },
        }
    )

    assert session_events[0][0] == "context_event"
    payload = session_events[0][1]
    assert payload["phase"] == "agent_run_usage"
    assert payload["usage"]["prompt_tokens"] == 7
    assert payload["usage"]["completion_tokens"] == 10
    assert payload["usage"]["usage_extra"]["lifecycle_prompt"]["hook_id"] == (
        "hook:prompt-review"
    )
    assert payload["raw_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 4, "type": "usage"}
    ]


def test_agent_run_session_start_projects_final_prompt_to_session_document() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 5,
            "type": "session_run_start",
            "payload": {
                "type": "session_run_start",
                "data": {"prompt": "rewritten prompt"},
            },
        }
    )

    document = None
    for seq, (event_type, payload) in enumerate(session_events, start=1):
        document = apply_session_event(
            document,
            session_id="session-1",
            event_type=event_type,
            payload=payload,
            session_event_seq=seq,
            session_run_id="run-1",
            session_run_seq=seq,
        )

    assert [event_type for event_type, _ in session_events] == ["session_run_start"]
    assert document["turns"][0]["userMessage"]["text"] == "rewritten prompt"
    assert document["session"]["title"] == "rewritten prompt"
    assert document["stats"]["taskText"] == "rewritten prompt"
    assert document["turns"][0]["userMessage"]["rawEventRefs"] == [
        {"agent_run_id": "run-1", "seq": 5, "type": "session_run_start"}
    ]


@pytest.mark.parametrize(
    "event_name",
    ["PostToolUse", "PostToolUseFailure", "PostToolBatch"],
)
def test_post_tool_lifecycle_diagnostics_project_user_safely(
    event_name: str,
) -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 6,
            "type": "lifecycle_hook",
            "payload": {
                "type": "lifecycle_hook",
                "data": {
                    "phase": "result",
                    "event_name": event_name,
                    "hook_id": "hook:post-tool-audit",
                    "display_name": "Post tool audit",
                    "source": "skill",
                    "handler_type": "prompt",
                    "payload": {
                        "tool_names": ["write_file"],
                        "tool_call_ids": ["call-1"],
                        "tool_sources": ["builtin"],
                        "mcp_servers": ["github"],
                        "technical": {
                            "tool_names": ["RAW_TECHNICAL_TOOL"],
                            "trace": "RAW_TRACE_SECRET",
                        },
                    },
                    "diagnostics": [
                        {
                            "code": "post_tool_review",
                            "message": "Post-tool review recorded.",
                            "stdout": "RAW_STDOUT_SECRET",
                        }
                    ],
                    "output": {
                        "diagnostics": [
                            {
                                "code": "raw_output_diag",
                                "message": "RAW_OUTPUT_SECRET",
                            }
                        ]
                    },
                },
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["lifecycle_hook"]
    payload = session_events[0][1]
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert payload["event_name"] == event_name
    assert payload["tool_names"] == ["write_file"]
    assert payload["tool_call_ids"] == ["call-1"]
    assert payload["tool_sources"] == ["builtin"]
    assert payload["mcp_servers"] == ["github"]
    assert payload["diagnostics"] == [
        {"code": "post_tool_review", "message": "Post-tool review recorded."}
    ]
    assert "technical" not in rendered
    assert "RAW_TECHNICAL_TOOL" not in rendered
    assert "RAW_TRACE_SECRET" not in rendered
    assert "RAW_STDOUT_SECRET" not in rendered
    assert "RAW_OUTPUT_SECRET" not in rendered


@pytest.mark.parametrize(
    ("event_name", "message", "artifact_kind"),
    [
        ("Stop", "Final review recorded.", "review"),
        ("StopFailure", "Lifecycle recovery: retry after reconnecting.", "failure_report"),
    ],
)
def test_terminal_lifecycle_session_projection_keeps_message_and_safe_artifact_refs(
    event_name: str,
    message: str,
    artifact_kind: str,
) -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 4,
            "type": "lifecycle_hook",
            "payload": {
                "type": "lifecycle_hook",
                "data": {
                    "phase": "result",
                    "event_name": event_name,
                    "hook_id": f"hook:{event_name.lower()}",
                    "display_name": f"{event_name} review",
                    "level": "warning",
                    "message": message,
                    "artifacts": [
                        {
                            "kind": artifact_kind,
                            "id": "artifact-1",
                            "content": "RAW_ARTIFACT_SECRET",
                        }
                    ],
                    "diagnostics": [
                        {
                            "code": "lifecycle_output_field_ignored",
                            "message": "decision ignored",
                            "field": "decision",
                        }
                    ],
                },
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["lifecycle_hook"]
    payload = session_events[0][1]
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert payload["event_name"] == event_name
    assert payload["message"] == message
    assert payload["artifacts"] == [{"kind": artifact_kind, "id": "artifact-1"}]
    assert payload["diagnostics"] == [
        {"code": "lifecycle_output_field_ignored", "message": "decision ignored"}
    ]
    assert "RAW_ARTIFACT_SECRET" not in rendered


def test_mcp_elicitation_lifecycle_projects_safe_runtime_context() -> None:
    session_events = []
    for seq, (event_name, phase, extra_payload) in enumerate(
        [
            (
                "Elicitation",
                "request",
                {
                    "message": "Choose repository",
                    "input_schema": {
                        "type": "object",
                        "properties": {"token": {"description": "RAW_SCHEMA_SECRET"}},
                    },
                    "tool_arguments": {"query": "RAW_TOOL_ARGUMENT_SECRET"},
                },
            ),
            (
                "ElicitationResult",
                "result",
                {
                    "message": "MCP elicitation accepted.",
                    "result_action": "accept",
                    "result_content": {"token": "RAW_RESULT_SECRET"},
                },
            ),
        ],
        start=10,
    ):
        session_events.extend(
            agent_run_event_to_session_events(
                {
                    "agent_run_id": "run-1",
                    "seq": seq,
                    "type": "lifecycle_hook",
                    "payload": {
                        "type": "lifecycle_hook",
                        "data": {
                            "event_type": "lifecycle_hook",
                            "phase": phase,
                            "event_name": event_name,
                            "placement": "server",
                            "session_run_id": "session-1",
                            "agent_run_id": "run-1",
                            "turn_id": "turn-1",
                            "tool_call_id": "call-mcp-1",
                            "tool_name": "search",
                            "mcp_server": "docs",
                            "trigger_source": "mcp",
                            "source": "mcp_server",
                            "decision": "none",
                            "continue_flow": True,
                            "level": "info",
                            "title": event_name,
                            "message": extra_payload["message"],
                            "payload": {
                                "tool_call_id": "call-mcp-1",
                                "tool_name": "search",
                                "mcp_server": "docs",
                                **extra_payload,
                            },
                        },
                    },
                }
            )
        )

    assert [event_type for event_type, _payload in session_events] == [
        "lifecycle_hook",
        "lifecycle_hook",
    ]
    request_payload = session_events[0][1]
    result_payload = session_events[1][1]
    rendered = json.dumps([request_payload, result_payload], ensure_ascii=False)
    assert request_payload["event_name"] == "Elicitation"
    assert request_payload.get("tool_name") == "search"
    assert request_payload.get("tool_call_id") == "call-mcp-1"
    assert request_payload["mcp_server"] == "docs"
    assert request_payload["message"] == "Choose repository"
    assert result_payload["event_name"] == "ElicitationResult"
    assert result_payload["result_action"] == "accept"
    assert result_payload["message"] == "MCP elicitation accepted."
    assert "RAW_SCHEMA_SECRET" not in rendered
    assert "RAW_TOOL_ARGUMENT_SECRET" not in rendered
    assert "RAW_RESULT_SECRET" not in rendered


def test_agent_run_tool_events_project_with_stable_tool_identity() -> None:
    start_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 2,
            "type": "tool_use",
            "payload": {
                "type": "tool_use",
                "data": {
                    "tool_name": "fetch_capabilities",
                    "tool_call_id": "call-1",
                    "input": {"url": "https://example.test/repo"},
                },
            },
        }
    )
    end_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 3,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "text": "ok",
                "data": {
                    "tool_name": "fetch_capabilities",
                    "tool_call_id": "call-1",
                    "output": "ok",
                },
            },
        }
    )

    assert start_events[0][0] == "tool_call_start"
    assert end_events[0][0] == "tool_call_end"
    assert start_events[0][1]["tool_name"] == "fetch_capabilities"
    assert end_events[0][1]["tool_name"] == "fetch_capabilities"
    assert start_events[0][1]["tool_call_id"] == "call-1"
    assert end_events[0][1]["tool_call_id"] == "call-1"


def test_agent_run_tool_use_projection_preserves_lifecycle_transformed_args() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 7,
            "type": "tool_use",
            "payload": {
                "type": "tool_use",
                "data": {
                    "tool_name": "write_file",
                    "tool_call_id": "call-1",
                    "input": {
                        "path": "safe.txt",
                        "content": "rewritten content",
                    },
                    "meta": {
                        "lifecycle_updated_input": {
                            "original_path": "unsafe.txt",
                            "hook_id": "hook:rewrite-tool-call",
                        }
                    },
                },
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["tool_call_start"]
    payload = session_events[0][1]
    assert payload.get("tool_name") == "write_file"
    assert payload.get("tool_call_id") == "call-1"
    assert payload["tool_args"] == {
        "path": "safe.txt",
        "content": "rewritten content",
    }
    assert "unsafe.txt" not in json.dumps(payload["tool_args"], ensure_ascii=False)


def test_permission_denied_feedback_projects_through_tool_result_session_event() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 3,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "data": {
                    "tool_name": "write_file",
                    "tool_call_id": "call-denied",
                    "output": (
                        "Error: tool 'write_file' denied by permission gateway: "
                        "write_file denied by policy\n"
                        "Permission feedback: Use read_file or ask for write_file capability."
                    ),
                    "meta": {
                        "tool_diagnostics": [
                            {
                                "code": "permission_deny",
                                "metadata": {
                                    "permission": {
                                        "action": "deny",
                                        "authorized": False,
                                        "audit": {
                                            "permission_denied_lifecycle": [
                                                {
                                                    "hook_id": (
                                                        "hook:permission-denied-feedback"
                                                    ),
                                                    "user_message": (
                                                        "Use read_file or ask for "
                                                        "write_file capability."
                                                    ),
                                                    "diagnostics": [
                                                        {
                                                            "code": (
                                                                "recoverable_permission_denied"
                                                            )
                                                        }
                                                    ],
                                                }
                                            ]
                                        },
                                    }
                                },
                            }
                        ]
                    },
                },
            },
        }
    )

    assert session_events[0][0] == "tool_call_end"
    payload = session_events[0][1]
    assert payload.get("tool_name") == "write_file"
    assert payload.get("tool_call_id") == "call-denied"
    assert "Permission feedback: Use read_file" in payload["tool_result"]
    lifecycle_audit = payload["meta"]["meta"]["tool_diagnostics"][0]["metadata"][
        "permission"
    ]["audit"]["permission_denied_lifecycle"]
    assert lifecycle_audit[0]["hook_id"] == "hook:permission-denied-feedback"
    assert lifecycle_audit[0]["diagnostics"][0]["code"] == "recoverable_permission_denied"
    assert payload["raw_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 3, "type": "tool_result"}
    ]


def test_pre_tool_terminal_gate_projects_lifecycle_audit_and_blocked_tool_result() -> None:
    session_events = agent_run_events_to_session_events(
        [
            {
                "agent_run_id": "run-1",
                "seq": 2,
                "type": "lifecycle_hook",
                "payload": {
                    "type": "lifecycle_hook",
                    "data": {
                        "phase": "result",
                        "event_name": "PreToolUse",
                        "hook_id": "hook:pretool-risk",
                        "display_name": "Pre-tool risk guard",
                        "source": "skill",
                        "handler_type": "prompt",
                        "decision": "defer",
                        "continue_flow": True,
                        "level": "info",
                        "payload": {
                            "tool_names": ["read_file"],
                            "tool_call_ids": ["call-pretool"],
                            "tool_sources": ["builtin"],
                            "mcp_servers": [],
                            "technical": {"tool_call": {"arguments": {"path": "secret.txt"}}},
                        },
                        "output": {
                            "decision": "defer",
                            "reason": "read_file deferred by lifecycle",
                        },
                    },
                },
            },
            {
                "agent_run_id": "run-1",
                "seq": 3,
                "type": "tool_result",
                "payload": {
                    "type": "tool_result",
                    "data": {
                        "tool_name": "read_file",
                        "tool_call_id": "call-pretool",
                        "output": "read_file deferred by lifecycle",
                        "meta": {
                            "tool_diagnostics": [
                                {
                                    "stage": "preflight",
                                    "kind": "tool_result_error",
                                    "severity": "error",
                                    "code": "lifecycle_pre_tool_denied",
                                    "message": "read_file deferred by lifecycle",
                                }
                            ]
                        },
                    },
                },
            },
        ]
    )

    assert [event_type for event_type, _ in session_events] == [
        "lifecycle_hook",
        "tool_call_end",
    ]
    lifecycle_payload = session_events[0][1]
    assert lifecycle_payload["event_name"] == "PreToolUse"
    assert lifecycle_payload["decision"] == "defer"
    assert lifecycle_payload["hook_id"] == "hook:pretool-risk"
    assert lifecycle_payload["tool_names"] == ["read_file"]
    rendered_lifecycle = json.dumps(lifecycle_payload, ensure_ascii=False, sort_keys=True)
    assert "secret.txt" not in rendered_lifecycle
    assert lifecycle_payload["raw_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 2, "type": "lifecycle_hook"}
    ]

    tool_payload = session_events[1][1]
    assert tool_payload.get("tool_name") == "read_file"
    assert tool_payload.get("tool_call_id") == "call-pretool"
    assert tool_payload["tool_result"] == "read_file deferred by lifecycle"
    diagnostic = tool_payload["meta"]["meta"]["tool_diagnostics"][0]
    assert diagnostic["code"] == "lifecycle_pre_tool_denied"
    assert diagnostic["message"] == "read_file deferred by lifecycle"
    assert tool_payload["raw_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 3, "type": "tool_result"}
    ]


def test_permission_request_terminal_gate_projects_final_permission_audit() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 4,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "data": {
                    "tool_name": "shell",
                    "tool_call_id": "call-shell",
                    "output": (
                        "Error: tool 'shell' denied by permission gateway: "
                        "shell blocked by lifecycle"
                    ),
                    "meta": {
                        "tool_diagnostics": [
                            {
                                "stage": "preflight",
                                "kind": "tool_result_error",
                                "severity": "error",
                                "code": "permission_deny",
                                "message": "shell blocked by lifecycle",
                                "metadata": {
                                    "permission": {
                                        "action": "deny",
                                        "authorized": False,
                                        "policy_matched": "lifecycle_hook:deny",
                                        "audit": {
                                            "lifecycle_hooks": [
                                                {
                                                    "hook_id": "hook:shell-permission",
                                                    "display_name": "Shell guard",
                                                    "handler_type": "command",
                                                    "decision": "deny",
                                                    "reason": "shell blocked by lifecycle",
                                                }
                                            ]
                                        },
                                    }
                                },
                            }
                        ]
                    },
                },
            },
        }
    )

    assert session_events[0][0] == "tool_call_end"
    payload = session_events[0][1]
    assert payload.get("tool_name") == "shell"
    assert payload.get("tool_call_id") == "call-shell"
    assert "shell blocked by lifecycle" in payload["tool_result"]
    diagnostic = payload["meta"]["meta"]["tool_diagnostics"][0]
    permission = diagnostic["metadata"]["permission"]
    assert permission["action"] == "deny"
    assert permission["policy_matched"] == "lifecycle_hook:deny"
    assert permission["audit"]["lifecycle_hooks"] == [
        {
            "hook_id": "hook:shell-permission",
            "display_name": "Shell guard",
            "handler_type": "command",
            "decision": "deny",
            "reason": "shell blocked by lifecycle",
        }
    ]
    assert payload["raw_event_refs"] == [
        {"agent_run_id": "run-1", "seq": 4, "type": "tool_result"}
    ]


def test_agent_run_tool_result_projects_large_output_as_raw_audit_summary() -> None:
    large_output = "A" * 5000 + "TAIL"
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 3,
            "type": "tool_result",
            "payload": {
                "type": "tool_result",
                "data": {
                    "tool_name": "read_file",
                    "tool_call_id": "call-1",
                    "output": large_output,
                    "path": "SKILL.md",
                },
            },
        }
    )

    assert session_events[0][0] == "tool_call_end"
    payload = session_events[0][1]
    assert len(payload["tool_result"]) < len(large_output)
    assert "open raw events for the complete content" in payload["tool_result"]
    assert payload["tool_result"].endswith("TAIL")
    assert payload["meta"]["path"] == "SKILL.md"
    assert payload["meta"]["output_truncated"] is True
    assert payload["meta"]["output_chars"] == len(large_output)
    assert payload["meta"]["output_source"] == "raw_event"
    assert "output" not in payload["meta"]
    assert payload["raw_event_refs"] == [{"agent_run_id": "run-1", "seq": 3, "type": "tool_result"}]


def test_agent_run_tool_use_projects_large_arguments_as_raw_audit_summary() -> None:
    large_content = "C" * 5000 + "END"
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 2,
            "type": "tool_use",
            "payload": {
                "type": "tool_use",
                "data": {
                    "tool_name": "write_file",
                    "tool_call_id": "call-1",
                    "input": {"path": "SKILL.md", "content": large_content},
                },
            },
        }
    )

    payload = session_events[0][1]
    assert payload["tool_args"]["path"] == "SKILL.md"
    assert len(payload["tool_args"]["content"]) < len(large_content)
    assert payload["tool_args"]["content"].endswith("END")
    assert payload["tool_args"]["truncated_fields"] == ["content"]
    assert payload["tool_args"]["full_payload_source"] == "raw_event"
    assert payload["raw_event_refs"] == [{"agent_run_id": "run-1", "seq": 2, "type": "tool_use"}]


def test_agent_run_result_event_projects_as_structured_process_context() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 4,
            "type": "result",
            "payload": {
                "type": "result",
                "text": "draft complete",
                "data": {"status": "completed", "output": "draft complete"},
            },
        }
    )

    assert [event_type for event_type, _ in session_events] == ["context_event"]
    payload = session_events[0][1]
    assert payload["phase"] == "agent_run_result"
    assert payload["agent_run_status"] == "completed"
    assert payload["output"] == "draft complete"


def test_agent_run_result_event_omits_large_output_from_structured_context() -> None:
    large_output = "B" * 5000 + "DONE"
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 4,
            "type": "result",
            "payload": {
                "type": "result",
                "data": {"status": "completed", "output": large_output},
            },
        }
    )

    payload = session_events[0][1]
    assert len(payload["output"]) < len(large_output)
    assert payload["output"].endswith("DONE")
    assert payload["result"]["output_truncated"] is True
    assert payload["result"]["output_chars"] == len(large_output)
    assert "output" not in payload["result"]


def test_agent_run_terminal_event_omits_large_output_from_process_context() -> None:
    large_output = "D" * 5000 + "DONE"
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 5,
            "type": "completed",
            "payload": {
                "result": {"status": "completed", "output": large_output},
                "agent_run": {
                    "id": "run-1",
                    "status": "completed",
                    "output": large_output,
                },
            },
        }
    )

    payload = session_events[0][1]
    assert payload["phase"] == "agent_run_completed"
    assert len(payload["output"]) < len(large_output)
    assert payload["output"].endswith("DONE")
    assert len(payload["message"]) < len(large_output)
    assert payload["message"].endswith("DONE")
    assert payload["terminal"]["output_truncated"] is True
    assert payload["terminal"]["message_truncated"] is True
    assert payload["raw_event_refs"] == [{"agent_run_id": "run-1", "seq": 5, "type": "completed"}]


def test_agent_run_projection_batches_consecutive_text_and_thinking() -> None:
    session_events = agent_run_events_to_session_events(
        [
            {
                "agent_run_id": "run-1",
                "seq": 1,
                "type": "thinking",
                "payload": {"type": "thinking", "text": "a"},
            },
            {
                "agent_run_id": "run-1",
                "seq": 2,
                "type": "thinking",
                "payload": {"type": "thinking", "text": "b"},
            },
            {
                "agent_run_id": "run-1",
                "seq": 3,
                "type": "text",
                "payload": {"type": "text", "text": "c"},
            },
            {
                "agent_run_id": "run-1",
                "seq": 4,
                "type": "text",
                "payload": {"type": "text", "text": "d"},
            },
        ]
    )

    assert [event_type for event_type, _ in session_events] == [
        "reasoning_delta",
        "assistant_delta",
    ]
    assert session_events[0][1]["content"] == "ab"
    assert session_events[1][1]["content"] == "cd"
    assert [item["seq"] for item in session_events[0][1]["raw_event_refs"]] == [1, 2]


def test_agent_run_projection_summarizes_large_batched_text() -> None:
    events = [
        {
            "agent_run_id": "run-1",
            "seq": seq,
            "type": "text",
            "payload": {"type": "text", "text": "E" * 1000},
        }
        for seq in range(1, 12)
    ]
    session_events = agent_run_events_to_session_events(events)

    assert [event_type for event_type, _ in session_events] == ["assistant_delta"]
    payload = session_events[0][1]
    assert len(payload["content"]) < 11_000
    assert "open raw events for the complete content" in payload["content"]
    assert payload["content_projection"]["content_truncated"] is True
    assert [item["seq"] for item in payload["raw_event_refs"]] == list(range(1, 12))


def test_agent_run_session_run_end_projects_budget_exceeded_terminal_state() -> None:
    session_events = agent_run_event_to_session_events(
        {
            "agent_run_id": "run-1",
            "seq": 3,
            "type": "session_run_end",
            "payload": {
                "type": "session_run_end",
                "data": {
                    "response": "(AgentRun budget exceeded: max_turns=1)",
                    "response_rendered": True,
                    "status": "budget_exceeded",
                    "error": "AgentRun budget exceeded: max_turns=1",
                    "session_state": "budget_exceeded",
                },
            },
        }
    )

    assert session_events == [
        (
            "session_run_end",
            {
                "agent_run_id": "run-1",
                "agent_id": "agent",
                "workflow": "agent_run",
                "raw_event_refs": [
                    {"agent_run_id": "run-1", "seq": 3, "type": "session_run_end"}
                ],
                "response": "(AgentRun budget exceeded: max_turns=1)",
                "response_rendered": True,
                "status": "budget_exceeded",
                "error": "AgentRun budget exceeded: max_turns=1",
                "session_state": "budget_exceeded",
            },
        )
    ]

    document = apply_session_event(
        None,
        session_id="session-1",
        event_type="session_run_start",
        payload={"prompt": "inspect repo"},
        session_event_seq=1,
        session_run_id="session-run-1",
        session_run_seq=1,
    )
    document = apply_session_event(
        document,
        session_id="session-1",
        event_type=session_events[0][0],
        payload=session_events[0][1],
        session_event_seq=2,
        session_run_id="session-run-1",
        session_run_seq=2,
    )

    assert document["run_state"]["status"] == "budget_exceeded"
    assert document["stats"]["runStatus"] == "budget_exceeded"
    assert document["run_state"]["error"] == "AgentRun budget exceeded: max_turns=1"
    assert document["session"]["state"] == "budget_exceeded"


def test_project_notes_input_creates_read_only_ingest_run() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)

    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
                "package_id_hint": "review",
            }
        }
    )

    assert result.agent_run.agent_id == "capability_packager"
    assert result.agent_run.source.value == "capability_ingest"
    assert result.agent_run.metadata["worker_kind"] == "sandbox_worker"
    assert result.agent_run.metadata["workflow"] == "capability_package_ingest"
    assert result.source["type"] == "project_notes"
    assert result.source["package_id_hint"] == "review"
    assert result.source_bundle["documents"][0]["title"] == "Project notes"
    assert result.source_bundle["documents"][0]["source_document_id"].startswith(
        "cap-src-doc-"
    )
    seed_artifact_id = f"capability-source-seed-bundle:{result.agent_run.id}"
    seed_artifacts = [
        item
        for item in control.artifacts_to_dict(result.agent_run.id)
        if item["id"] == seed_artifact_id
    ]
    assert len(seed_artifacts) == 1
    assert seed_artifacts[0]["metadata"]["kind"] == "capability_source_seed_bundle"
    assert json.loads(seed_artifacts[0]["content"])["documents"][0][
        "source_document_id"
    ].startswith("cap-src-doc-")
    assert "capability_packages" not in control.runtime_snapshot
    assert "capability_components" not in control.runtime_snapshot
    assert '"skill_content"' not in result.agent_run.prompt
    assert "source_path" in result.agent_run.prompt
    assert "source_document_id" in result.agent_run.prompt
    assert "content_ref only for observed tool-call" in result.agent_run.prompt
    assert "Do not copy large Skill files into the model output." in result.agent_run.prompt


def test_packager_prompt_requires_lifecycle_hooks_runtime_and_trust_contract() -> None:
    prompt = capability_packages_module._render_packager_prompt(
        bundle={"source": {"type": "project_notes"}, "items": []},
        locale="en-US",
    )

    assert '"capability_draft_patch"' in prompt
    assert '"capability_draft_patches"' in prompt
    assert '"field_path": "repo_summary"' in prompt
    assert '"field_path": "contributions.skills"' in prompt
    assert '"field_path": "install_plan"' in prompt
    assert '"field_path": "usage"' in prompt
    assert '"field_path": "evidence"' in prompt
    assert '"field_path": "risk_level"' in prompt
    assert "Do not produce a complete final draft JSON as the primary output" in prompt
    assert "complete draft JSON is accepted only as a legacy fallback" in prompt
    assert '"hooks"' in prompt
    assert '"runtime_footprint"' in prompt
    assert '"placement": "server|peer|both"' in prompt
    assert '"handler_type": "command|http|mcp_tool|prompt|agent"' in prompt
    assert "must not declare internal handlers" in prompt
    assert "do not output SessionStart/SessionEnd" in prompt
    assert '"matcher": {"tool_names": ["read_file"]}' in prompt
    assert '"permissions": []' in prompt
    assert "trust defaults to pending_review" in prompt
    assert "server runs in Labrastro backend" in prompt
    assert "peer means the user's local VS Code side" in prompt


def test_revision_prompt_uses_public_draft_without_skill_content() -> None:
    control = _control_plane()
    runner = CapabilityPackagerRunner(control)
    task = runner.start(
        evidence_bundle=EvidenceBundle(
            source={"type": "project_notes"},
            documents=[{"title": "Project notes", "content": "Review code changes."}],
            evidence=[{"title": "Project notes", "excerpt": "Review code changes."}],
        ),
        revision_instruction="rename the package",
        revision_draft={
            "id": "review",
            "name": "Review",
            "contributions": {
                "skills": [
                    {
                        "id": "skill:code-review",
                        "kind": "skill",
                        "name": "code-review",
                        "skill_content": "---\nname: code-review\n---\nlarge body",
                    }
                ]
            },
        },
    )
    assert '"skill_content":' not in task.prompt
    assert "skill_content_chars" in task.prompt


def test_github_repo_ingest_sets_repo_url_for_sandbox_worktree() -> None:
    class FakeFetchCapabilitiesTool:
        def execute(self, **kwargs: object) -> str:
            return json.dumps(
                {
                    "ok": True,
                    "url": kwargs["url"],
                    "title": "Example Tool",
                    "sections": [],
                    "links": [],
                    "evidence": [],
                    "errors": [],
                }
            )

    control = _control_plane()
    service = CapabilityPackageIngestService(
        control,
        collector=CapabilitySourceCollector(fetch_tool=FakeFetchCapabilitiesTool()),
    )

    result = service.start({"repoUrl": "https://github.com/acme/example-tool"})

    assert result.agent_run.metadata["worker_kind"] == "sandbox_worker"
    assert result.agent_run.metadata["repo_url"] == "https://github.com/acme/example-tool"
    assert result.agent_run.metadata["capability_source"]["type"] == "github_repo"
    assert result.agent_run.metadata["capability_source"]["url"] == "https://github.com/acme/example-tool"


def test_source_collector_uses_fetch_capabilities_for_url_sources() -> None:
    class FakeFetchCapabilitiesTool:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def execute(self, **kwargs: object) -> str:
            self.calls.append(kwargs)
            return json.dumps(
                {
                    "ok": True,
                    "url": kwargs["url"],
                    "final_url": kwargs["url"],
                    "source_kind": "docs_site",
                    "title": "Example Tool",
                    "sections": [
                        {
                            "heading": "Install",
                            "source_url": f"{kwargs['url']}#install",
                            "text": "Install with npm.",
                            "code_blocks": ["npm install -g example-tool"],
                        }
                    ],
                    "links": [
                        {
                            "title": "Repository",
                            "url": "https://github.com/acme/example-tool",
                            "kind": "github_repo",
                        }
                    ],
                    "evidence": [
                        {
                            "title": "Install",
                            "source_url": f"{kwargs['url']}#install",
                            "excerpt": "Install with npm.",
                            "content_hash": "abc123",
                            "fetched_at": "2026-05-22T00:00:00Z",
                        }
                    ],
                    "content_hash": "abc123",
                    "fetched_at": "2026-05-22T00:00:00Z",
                    "errors": [],
                }
            )

    fetch_tool = FakeFetchCapabilitiesTool()
    collector = CapabilitySourceCollector(fetch_tool=fetch_tool)

    bundle = collector.collect(
        {
            "type": "docs_url",
            "url": "https://docs.example.com/example-tool",
            "notes": "Prefer global CLI install.",
        }
    )

    assert fetch_tool.calls == [
        {
            "url": "https://docs.example.com/example-tool",
                "focus": "install setup configure authentication requirements runtime sdk executable mcp skill",
            "source_hint": "docs_url",
            "max_chars": 36000,
        }
    ]
    assert bundle.source["type"] == "docs_url"
    assert bundle.documents[0]["title"] == "Project notes"
    assert bundle.documents[1]["title"] == "Example Tool"
    assert "npm install -g example-tool" in bundle.documents[1]["content"]
    assert any(item.get("content_hash") == "abc123" for item in bundle.evidence)
    assert bundle.links[0]["kind"] == "github_repo"


def test_ingest_service_only_orchestrates_collector_and_runner() -> None:
    class FakeCollector:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def collect(self, payload: dict[str, object]) -> EvidenceBundle:
            self.payloads.append(payload)
            return EvidenceBundle(
                source={"type": "project_notes", "notes": "Use gh."},
                documents=[
                    {
                        "title": "Project notes",
                        "url": "",
                        "content": "Use gh.",
                    }
                ],
                evidence=[
                    {
                        "title": "Project notes",
                        "source_url": "",
                        "excerpt": "Use gh.",
                    }
                ],
            )

    class FakeRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def start(
            self,
            *,
            evidence_bundle: EvidenceBundle,
            workspace_root: str = "",
            agent_run_metadata: dict[str, object] | None = None,
            revision_draft: dict[str, object] | None = None,
            revision_instruction: str = "",
        ) -> AgentRunRecord:
            self.calls.append(
                {
                    "evidence_bundle": evidence_bundle,
                    "workspace_root": workspace_root,
                    "agent_run_metadata": agent_run_metadata or {},
                    "revision_draft": revision_draft,
                    "revision_instruction": revision_instruction,
                }
            )
            return AgentRunRecord(
                id="run-1",
                issue_id="capability-package-ingest",
                agent_id="custom-packager",
                source="capability_ingest",
                metadata={"source_bundle": evidence_bundle.to_dict()},
            )

    collector = FakeCollector()
    runner = FakeRunner()
    service = CapabilityPackageIngestService(collector=collector, packager_runner=runner)

    result = service.start(
        {
            "source": {"type": "project_notes", "notes": "Use gh."},
            "workspace_root": "D:/repo",
        }
    )

    assert collector.payloads == [{"type": "project_notes", "notes": "Use gh."}]
    assert runner.calls[0]["workspace_root"] == "D:/repo"
    assert result.agent_run.agent_id == "custom-packager"
    assert result.source_bundle["evidence"][0]["excerpt"] == "Use gh."


def test_draft_validator_requires_valid_components_and_evidence() -> None:
    validator = CapabilityDraftValidator()
    bundle = EvidenceBundle(
        source={"type": "project_notes"},
        documents=[{"title": "Project notes", "url": "", "content": "Install gh."}],
        evidence=[{"title": "Project notes", "source_url": "", "excerpt": "Install gh."}],
    )

    result = validator.validate(
        {
            "id": "review",
            "components": [{"id": "shell:gh", "kind": "shell", "name": "gh"}],
            "install_plan": ["Install gh."],
            "usage": ["Use gh."],
        },
        bundle,
    )

    assert result.ok is False
    assert any(message.startswith("component.kind must be one of ") for message in result.messages)
    assert "draft.evidence is required" in result.messages
    assert "risk_level is required" in result.messages


def test_draft_validator_requires_configure_command_evidence() -> None:
    validator = CapabilityDraftValidator()
    bundle = EvidenceBundle(
        source={"type": "project_notes"},
        documents=[{"title": "Project notes", "url": "", "content": "Install gh."}],
        evidence=[{"title": "Project notes", "source_url": "", "excerpt": "Install gh."}],
    )

    result = validator.validate(
        {
            "id": "review",
            "components": [
                {
                    "id": "envreq:executable:gh",
                    "kind": "environment_requirement",
                    "name": "gh",
                    "config": {
                        "kind": "executable",
                        "configure": "gh auth login",
                    },
                }
            ],
            "install_plan": ["Install gh."],
            "usage": ["Use gh."],
            "evidence": [{"title": "Project notes", "excerpt": "Install gh."}],
            "risk_level": "low",
        },
        bundle,
    )

    assert result.ok is False
    assert "envreq:executable:gh command lacks evidence: gh auth login" in result.messages


def test_package_installer_preserves_environment_requirement_requirements() -> None:
    data: dict[str, object] = {}
    result = CapabilityPackageInstaller().install_draft(
        data,
        {
            "id": "dotnet-sdk",
            "components": [
                {
                    "kind": "environment_requirement",
                    "name": "dotnet",
                    "resource_kind": "sdk",
                    "requirements": {"version": ">=8"},
                }
            ],
        },
    )

    assert result.component_ids == ["envreq:sdk:dotnet"]
    requirement = data["environment"]["requirements"]["envreq:sdk:dotnet"]
    assert requirement["kind"] == "sdk"
    assert requirement["requirements"] == {"version": ">=8"}


def test_package_installer_infers_executable_requirement_from_command() -> None:
    data: dict[str, object] = {}
    result = CapabilityPackageInstaller().install_draft(
        data,
        {
            "id": "github-cli",
            "components": [
                {
                    "kind": "environment_requirement",
                    "name": "gh",
                    "command": "gh",
                }
            ],
        },
    )

    assert result.component_ids == ["envreq:executable:gh"]
    requirement = data["environment"]["requirements"]["envreq:executable:gh"]
    assert requirement["kind"] == "executable"
    assert requirement["command"] == "gh"
    assert requirement["runtime_footprint"]["runs_on"] == "local_peer"


def test_package_installer_writes_component_and_package_runtime_footprint() -> None:
    data: dict[str, object] = {}
    result = CapabilityPackageInstaller().install_draft(
        data,
        {
            "id": "review",
            "components": [
                {
                    "kind": "mcp_server",
                    "name": "github",
                    "command": "github-mcp-server",
                    "runtime_footprint": {
                        "runs_on": "server",
                        "install_required_on": ["server"],
                        "config_required_on": ["server"],
                    },
                },
                {
                    "kind": "environment_requirement",
                    "name": "gh",
                    "command": "gh",
                    "placement": "peer",
                },
            ],
        },
    )

    assert result.component_ids == ["mcp_server:github", "envreq:executable:gh"]
    mcp_component = data["capability_components"]["mcp_server:github"]
    env_component = data["capability_components"]["envreq:executable:gh"]
    package = data["capability_packages"]["review"]
    assert mcp_component["runtime_footprint"]["runs_on"] == "server"
    assert env_component["runtime_footprint"]["runs_on"] == "local_peer"
    assert package["runtime_footprint"] == {
        "runs_on": "both",
        "install_required_on": ["server", "local_peer"],
        "config_required_on": ["server", "local_peer"],
        "user_message": "服务端和本地端都需要配置",
    }


def test_package_installer_aggregates_skill_runtime_from_environment_refs() -> None:
    data: dict[str, object] = {}

    CapabilityPackageInstaller().install_draft(
        data,
        {
            "id": "review",
            "components": [
                {
                    "kind": "skill",
                    "name": "code-review",
                    "skill_content": "---\nname: code-review\ndescription: Review code.\n---\nReview code.\n",
                    "environment_requirement_refs": ["envreq:executable:gh"],
                },
                {
                    "kind": "environment_requirement",
                    "name": "gh",
                    "command": "gh",
                    "placement": "peer",
                },
            ],
        },
    )

    component = data["capability_components"]["skill:code-review"]
    skill = data["skills"]["items"]["code-review"]
    package = data["capability_packages"]["review"]
    assert component["runtime_footprint"]["runs_on"] == "local_peer"
    assert component["config"]["environment_requirement_refs"] == ["envreq:executable:gh"]
    assert skill["environment_requirement_refs"] == ["envreq:executable:gh"]
    assert skill["runtime_footprint"]["runs_on"] == "local_peer"
    assert package["runtime_footprint"]["runs_on"] == "local_peer"


def test_package_installer_materializes_skill_to_canonical_server_path(tmp_path) -> None:
    install_root = tmp_path / "skills" / "packages"
    skill_content = (
        "---\n"
        "name: code-review\n"
        "description: Review code changes.\n"
        "---\n"
        "Use the repository review checklist.\n"
    )
    data: dict[str, object] = {}

    installer = CapabilityPackageInstaller(skill_install_root=install_root)
    result = installer.install_draft(
        data,
        {
            "id": "review",
            "components": [
                {
                    "kind": "skill",
                    "name": "code-review",
                    "display_name": "Code review",
                    "summary": "Review repository changes before merging.",
                    "description": "Review code changes.",
                    "source_path": "skills/code-review/SKILL.md",
                    "skill_content": skill_content,
                }
            ],
        },
    )

    installed_path = install_root / "components" / "skill-code-review" / "SKILL.md"
    assert result.component_ids == ["skill:code-review"]
    assert not installed_path.exists()
    installer.apply_skill_file_operations(result.skill_file_operations)
    assert installed_path.read_text(encoding="utf-8") == skill_content
    skill = data["skills"]["items"]["code-review"]
    component = data["capability_components"]["skill:code-review"]
    assert component["display_name"] == "Code review"
    assert component["summary"] == "Review repository changes before merging."
    assert skill["display_name"] == "Code review"
    assert skill["summary"] == "Review repository changes before merging."
    assert skill["path_hint"] == str(installed_path)
    assert skill["source_path"] == "skills/code-review/SKILL.md"
    assert skill["managed_by"] == "capability_package"
    assert "skill_content" not in skill


def test_package_installer_preserves_lifecycle_hooks_for_package_components_and_resources(tmp_path) -> None:
    skill_content = (
        "---\n"
        "name: code-review\n"
        "description: Review code changes.\n"
        "---\n"
        "Use the repository review checklist.\n"
    )
    package_hooks = [
        {
            "event": "UserPromptSubmit",
            "handler_type": "prompt",
            "handler_ref": "package:review/prompt",
            "display_name": "Review package prompt",
            "summary": "Adds review prompt context.",
            "permissions": [],
            "trust": "trusted",
        }
    ]
    skill_hooks = [
        {
            "event": "UserPromptSubmit",
            "handler_type": "prompt",
            "handler_ref": "skills/code-review/SKILL.md",
            "display_name": "Code review prompt context",
            "summary": "Adds code review context.",
            "permissions": [],
            "trust": "trusted",
        }
    ]
    mcp_hooks = [
        {
            "event": "PostToolUse",
            "handler_type": "mcp_tool",
            "handler_ref": "github.audit",
            "display_name": "GitHub audit",
            "summary": "Records GitHub MCP tool results.",
            "permissions": ["audit.write"],
            "trust": "trusted",
        }
    ]
    data: dict[str, object] = {}

    CapabilityPackageInstaller(skill_install_root=tmp_path).install_draft(
        data,
        {
            "id": "review",
            "hooks": package_hooks,
            "components": [
                {
                    "kind": "skill",
                    "name": "code-review",
                    "skill_content": skill_content,
                    "runtime_footprint": {
                        "runs_on": "local_peer",
                        "install_required_on": ["local_peer"],
                        "config_required_on": ["local_peer"],
                    },
                    "hooks": skill_hooks,
                },
                {
                    "kind": "mcp_server",
                    "name": "github",
                    "command": "github-mcp-server",
                    "runtime_footprint": {
                        "runs_on": "local_peer",
                        "install_required_on": ["local_peer"],
                        "config_required_on": ["local_peer"],
                    },
                    "hooks": mcp_hooks,
                },
            ],
        },
    )

    expected_package_hooks = [dict(package_hooks[0], placement="server", trust="pending_review")]
    expected_skill_hooks = [dict(skill_hooks[0], placement="server", trust="pending_review")]
    expected_mcp_hooks = [dict(mcp_hooks[0], placement="server", trust="pending_review")]
    assert data["capability_packages"]["review"]["hooks"] == expected_package_hooks
    assert data["capability_components"]["skill:code-review"]["hooks"] == expected_skill_hooks
    assert data["capability_components"]["mcp_server:github"]["hooks"] == expected_mcp_hooks
    assert data["skills"]["items"]["code-review"]["hooks"] == expected_skill_hooks
    assert data["mcp"]["servers"]["github"]["hooks"] == expected_mcp_hooks
    assert data["capability_packages"]["review"]["runtime_footprint"]["runs_on"] == "local_peer"
    assert data["capability_components"]["skill:code-review"]["runtime_footprint"]["runs_on"] == "local_peer"
    assert data["capability_components"]["mcp_server:github"]["runtime_footprint"]["runs_on"] == "local_peer"
    assert data["skills"]["items"]["code-review"]["runtime_footprint"]["runs_on"] == "local_peer"
    assert data["mcp"]["servers"]["github"]["runtime_footprint"]["runs_on"] == "local_peer"
    assert data["mcp"]["servers"]["github"]["placement"] == "peer"


def test_package_installer_does_not_auto_grant_agent_capability_refs(tmp_path) -> None:
    data: dict[str, object] = {
        "agents": {
            "coder": {
                "id": "coder",
                "capability_refs": [],
            }
        }
    }

    CapabilityPackageInstaller(skill_install_root=tmp_path).install_draft(
        data,
        {
            "id": "review",
            "components": [
                {
                    "kind": "mcp_server",
                    "name": "github",
                    "command": "github-mcp-server",
                    "args": ["stdio"],
                    "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                },
            ],
        },
    )

    assert data["agents"]["coder"]["capability_refs"] == []
    assert "review" in data["capability_packages"]
    assert "github" in data["mcp"]["servers"]


def test_package_installer_rejects_invalid_lifecycle_hook_before_config_write(tmp_path) -> None:
    data: dict[str, object] = {}

    with pytest.raises(CapabilityPackageIngestError) as exc_info:
        CapabilityPackageInstaller(skill_install_root=tmp_path).install_draft(
            data,
            {
                "id": "review",
                "hooks": [
                    {
                        "event": "PreToolUse",
                        "placement": "local_peer",
                        "handler_type": "prompt",
                        "display_name": "Review package startup",
                        "summary": "Adds review startup context.",
                        "permissions": [],
                    }
                ],
                "components": [
                    {
                        "kind": "mcp_server",
                        "name": "github",
                        "command": "github-mcp-server",
                    }
                ],
            },
        )

    assert exc_info.value.error == "invalid_lifecycle_hook"
    assert "placement" in exc_info.value.message
    assert data == {}


def test_package_installer_rejects_internal_lifecycle_handler_before_config_write(
    tmp_path,
) -> None:
    data: dict[str, object] = {}

    with pytest.raises(CapabilityPackageIngestError) as exc_info:
        CapabilityPackageInstaller(skill_install_root=tmp_path).install_draft(
            data,
            {
                "id": "review",
                "hooks": [
                    {
                        "event": "PreToolUse",
                        "handler_type": "internal",
                        "handler_ref": "memory_context",
                        "display_name": "Unsafe internal hook",
                        "summary": "Attempts to call Python hooks from package config.",
                        "permissions": [],
                    }
                ],
                "components": [
                    {
                        "kind": "mcp_server",
                        "name": "github",
                        "command": "github-mcp-server",
                    }
                ],
            },
        )

    assert exc_info.value.error == "invalid_lifecycle_hook"
    assert "internal handlers" in exc_info.value.message
    assert data == {}


def test_package_installer_rejects_legacy_lifecycle_tool_matcher_before_config_write(tmp_path) -> None:
    data: dict[str, object] = {}
    legacy_field = "mcp_" + "server"

    with pytest.raises(CapabilityPackageIngestError) as exc_info:
        CapabilityPackageInstaller(skill_install_root=tmp_path).install_draft(
            data,
            {
                "id": "review",
                "hooks": [
                    {
                        "event": "PostToolUse",
                        "handler_type": "prompt",
                        "display_name": "Review package tool hook",
                        "summary": "Adds review context for matching tools.",
                        "permissions": [],
                        "matcher": {legacy_field: "github"},
                    }
                ],
                "components": [
                    {
                        "kind": "mcp_server",
                        "name": "github",
                        "command": "github-mcp-server",
                    }
                ],
            },
        )

    assert exc_info.value.error == "invalid_lifecycle_hook"
    assert legacy_field in exc_info.value.message
    assert data == {}


def test_package_installer_accepts_lifecycle_tool_names_matcher(tmp_path) -> None:
    data: dict[str, object] = {}

    CapabilityPackageInstaller(skill_install_root=tmp_path).install_draft(
        data,
        {
            "id": "review",
            "hooks": [
                {
                    "event": "PostToolUse",
                    "handler_type": "prompt",
                    "display_name": "Review package tool hook",
                    "summary": "Adds review context for matching tools.",
                    "permissions": [],
                    "matcher": {"tool_names": "read_file"},
                }
            ],
            "components": [
                {
                    "kind": "mcp_server",
                    "name": "github",
                    "command": "github-mcp-server",
                }
            ],
        },
    )

    assert data["capability_packages"]["review"]["hooks"][0]["matcher"] == {
        "tool_names": "read_file"
    }


def test_package_installer_rejects_package_skill_without_installable_content(tmp_path) -> None:
    data: dict[str, object] = {}

    with pytest.raises(CapabilityPackageIngestError) as exc_info:
        CapabilityPackageInstaller(skill_install_root=tmp_path).install_draft(
            data,
            {
                "id": "review",
                "components": [
                    {
                        "kind": "skill",
                        "name": "code-review",
                        "path_hint": "/external/skills/code-review/SKILL.md",
                    }
                ],
            },
        )

    assert exc_info.value.error == "capability_package_skill_content_required"
    assert "code-review" not in data.get("skills", {}).get("items", {})


def test_package_installer_keeps_shared_skill_path_stable_when_owner_changes(tmp_path) -> None:
    install_root = tmp_path / "skills" / "packages"
    installer = CapabilityPackageInstaller(skill_install_root=install_root)
    data: dict[str, object] = {}
    draft = {
        "components": [
            {
                "kind": "skill",
                "name": "code-review",
                "skill_content": "Review code changes.\n",
            }
        ],
    }

    first_result = installer.install_draft(data, {"id": "review-a", **draft})
    installer.apply_skill_file_operations(first_result.skill_file_operations)
    first_path = Path(data["skills"]["items"]["code-review"]["path_hint"])
    second_result = installer.install_draft(data, {"id": "review-b", **draft})
    installer.apply_skill_file_operations(second_result.skill_file_operations)
    second_path = Path(data["skills"]["items"]["code-review"]["path_hint"])

    assert first_path == install_root / "components" / "skill-code-review" / "SKILL.md"
    assert second_path == first_path
    assert first_path.exists()
    assert not (install_root / "review-a").exists()
    assert not (install_root / "review-b").exists()

    component = CapabilityComponentConfig.from_dict(
        "skill:code-review",
        data["capability_components"]["skill:code-review"],
    )
    component.package_ids = [
        package_id for package_id in component.package_ids if package_id != "review-a"
    ]
    data["capability_components"]["skill:code-review"] = component.to_dict()
    installer.materialize_component(data, component)
    installer.apply_skill_file_operations(installer.skill_file_operations)

    assert Path(data["skills"]["items"]["code-review"]["path_hint"]) == first_path
    assert first_path.exists()

    installer.skill_file_operations = []
    component.package_ids = []
    installer.remove_materialized_component(data, component)
    installer.apply_skill_file_operations(installer.skill_file_operations)

    assert "code-review" not in data["skills"]["items"]
    assert not first_path.exists()
    assert not first_path.parent.exists()


def test_package_installer_delete_cleans_canonical_skill_path(tmp_path) -> None:
    install_root = tmp_path / "skills" / "packages"
    data: dict[str, object] = {}
    installer = CapabilityPackageInstaller(skill_install_root=install_root)
    result = installer.install_draft(
        data,
        {
            "id": "review",
            "components": [
                {
                    "kind": "skill",
                    "name": "code-review",
                    "skill_content": "Review code changes.\n",
                }
            ],
        },
    )
    installed_path = install_root / "components" / "skill-code-review" / "SKILL.md"
    installer.apply_skill_file_operations(result.skill_file_operations)
    assert installed_path.exists()
    component = CapabilityComponentConfig.from_dict(
        "skill:code-review",
        data["capability_components"]["skill:code-review"],
    )

    installer.skill_file_operations = []
    installer.remove_materialized_component(
        data,
        component,
    )
    installer.apply_skill_file_operations(installer.skill_file_operations)

    assert "code-review" not in data["skills"]["items"]
    assert not installed_path.exists()
    assert not installed_path.parent.exists()


def test_ingest_status_extracts_completed_draft_json() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        }
    )
    draft = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "contributions": {
            "environment_requirements": [
                {
                    "id": "envreq:executable:gh",
                    "kind": "executable",
                    "name": "gh",
                    "command": "gh",
                    "check": "gh --version",
                }
            ]
        },
        "install_plan": ["Install GitHub CLI."],
        "usage": ["Use gh pr view."],
        "evidence": [{"title": "Project notes", "excerpt": "Install gh and run gh --version"}],
        "credentials": ["GITHUB_TOKEN"],
        "risk_level": "low",
    }

    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=f"```json\n{json.dumps(draft)}\n```",
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["agent_run"]["status"] == "completed"
    assert status["draft"]["id"] == "review"
    assert (
        status["draft"]["contributions"]["environment_requirements"][0]["id"]
        == "envreq:executable:gh"
    )
    assert status["source_bundle"]["evidence"][0]["excerpt"] == (
        "Install gh, then use gh pr view for review."
    )
    assert status["validation"]["ok"] is True
    run_state = status["capability_run_state"]
    assert run_state["field_generation"]["patch_count"] == 1
    assert run_state["field_generation"]["field_state"]["full_draft"]["status"] == "filled"
    assert run_state["draft_assembly"]["field_state"]["full_draft"]["status"] == "filled"


def test_ingest_status_final_output_patch_overrides_earlier_event_patch() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        }
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent.text_event(_capability_patch_json("risk_level", "high")),
    )
    output_patches: list[tuple[str, object]] = [
        ("id", "review"),
        ("name", "Review"),
        (
            "contributions.environment_requirements",
            [
                {
                    "id": "envreq:executable:gh",
                    "kind": "executable",
                    "name": "gh",
                    "command": "gh",
                    "check": "gh --version",
                }
            ],
        ),
        ("install_plan", []),
        ("usage", []),
        (
            "evidence",
            [{"title": "Project notes", "excerpt": "Install gh and run gh --version"}],
        ),
        ("risk_level", "low"),
    ]
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=_capability_patch_stream(output_patches),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["draft"]["risk_level"] == "low"
    assert status["validation"]["ok"] is True
    risk_state = status["capability_run_state"]["field_generation"]["field_state"]["risk_level"]
    assert risk_state["value"] == "low"
    assert risk_state["producer_event_refs"][-1]["type"] == "agent_run_output"


def test_ingest_status_builds_skill_content_from_source_bundle() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this review skill.",
            }
        }
    )
    skill_content = "---\nname: code-review\ndescription: Review code changes.\n---\n\nReview code changes.\n"
    source_bundle = {
        "source": {"type": "github_repo", "url": "https://github.com/acme/review"},
        "documents": [
            {
                "title": "skills/code-review/SKILL.md",
                "source_path": "skills/code-review/SKILL.md",
                "content": skill_content,
            }
        ],
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "errors": [],
    }
    control.get_agent_run(result.agent_run.id).metadata["source_bundle"] = source_bundle
    draft_decision = {
        "id": "review",
        "name": "Review",
        "source": {"type": "github_repo", "url": "https://github.com/acme/review"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:code-review",
                    "kind": "skill",
                    "name": "code-review",
                    "source_path": "skills/code-review/SKILL.md",
                    "summary": "Review code changes.",
                }
            ]
        },
        "install_plan": ["Install the packaged skill."],
        "usage": ["Use the review skill."],
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    assert skill["config"]["skill_content"] == skill_content.strip()
    source_document_id = status["source_bundle"]["documents"][0]["source_document_id"]
    assert source_document_id.startswith("cap-src-doc-")
    assert skill["source_document_id"] == source_document_id
    assert skill["config"]["source_document_id"] == source_document_id
    assert status["validation"]["ok"] is True


def test_ingest_status_resolves_skill_content_by_source_document_id() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this review skill by source document id.",
            }
        }
    )
    skill_content = "---\nname: code-review\n---\n\nReview code changes.\n"
    source_document_id = "cap-src-doc-review-skill"
    control.get_agent_run(result.agent_run.id).metadata["source_bundle"] = {
        "source": {"type": "project_notes"},
        "documents": [
            {
                "source_document_id": source_document_id,
                "title": "skills/code-review/SKILL.md",
                "source_path": "skills/code-review/SKILL.md",
                "content": skill_content,
            }
        ],
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "errors": [],
    }
    draft_decision = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "materialization_plan": [
            {
                "component_id": "skill:code-review",
                "source_document_id": source_document_id,
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:code-review",
                    "kind": "skill",
                    "name": "code-review",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    assert skill["source_document_id"] == source_document_id
    assert skill["config"]["source_document_id"] == source_document_id
    assert status["validation"]["ok"] is True


def test_ingest_status_restores_source_bundle_from_seed_artifact_when_metadata_missing() -> None:
    class _Collector:
        def collect(self, source_payload: dict[str, object]) -> EvidenceBundle:
            del source_payload
            return EvidenceBundle(
                source={"type": "project_notes"},
                documents=[
                    {
                        "title": "skills/code-review/SKILL.md",
                        "source_path": "skills/code-review/SKILL.md",
                        "content": "---\nname: code-review\n---\n\nReview code changes.\n",
                    }
                ],
                evidence=[{"title": "Skill", "excerpt": "Review code changes."}],
            )

    control = _control_plane()
    service = CapabilityPackageIngestService(control, collector=_Collector())
    result = service.start({"source": {"type": "project_notes"}})
    source_document_id = result.source_bundle["documents"][0]["source_document_id"]
    seed_artifact_id = f"capability-source-seed-bundle:{result.agent_run.id}"
    task = control.get_agent_run(result.agent_run.id)
    task.metadata.pop("source_bundle", None)
    draft_decision = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "materialization_plan": [
            {
                "component_id": "skill:code-review",
                "source_document_id": source_document_id,
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:code-review",
                    "kind": "skill",
                    "name": "code-review",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == "---\nname: code-review\n---\n\nReview code changes."
    assert status["source_bundle"]["documents"][0]["source_document_id"] == source_document_id
    assert status["capability_run_state"]["seed_source_bundle_artifact_id"] == seed_artifact_id
    assert status["capability_run_state"]["materialization_source"] == "artifact"
    assert status["validation"]["ok"] is True


def test_ingest_status_invalid_source_document_id_does_not_fallback_to_source_path() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this review skill with a bad source document id.",
            }
        }
    )
    skill_content = "---\nname: code-review\n---\n\nReview code changes.\n"
    control.get_agent_run(result.agent_run.id).metadata["source_bundle"] = {
        "source": {"type": "project_notes"},
        "documents": [
            {
                "source_document_id": "cap-src-doc-real",
                "title": "skills/code-review/SKILL.md",
                "source_path": "skills/code-review/SKILL.md",
                "content": skill_content,
            }
        ],
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "errors": [],
    }
    draft_decision = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "materialization_plan": [
            {
                "component_id": "skill:code-review",
                "source_document_id": "cap-src-doc-invented",
                "source_path": "skills/code-review/SKILL.md",
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:code-review",
                    "kind": "skill",
                    "name": "code-review",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert "skill_content" not in skill
    assert status["validation"]["ok"] is False
    assert any(
        "source_document_id did not match any complete source document" in message
        for message in status["validation"]["messages"]
    )


def test_ingest_status_builds_skill_content_from_workspace_root(tmp_path: Path) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    skill_path = tmp_path / "skills" / "code-review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: code-review\ndescription: Review code changes.\n---\n\nReview code changes.\n",
        encoding="utf-8",
    )
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this review skill.",
            },
            "workspace_root": str(tmp_path),
        }
    )
    control.get_agent_run(result.agent_run.id).metadata["source_bundle"] = {
        "source": {"type": "project_notes"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "errors": [],
    }
    draft_decision = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:code-review",
                    "kind": "skill",
                    "name": "code-review",
                    "source_path": "skills/code-review/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    document = next(
        item
        for item in status["source_bundle"]["documents"]
        if item.get("source_path") == "skills/code-review/SKILL.md"
    )
    assert skill["skill_content"].startswith("---\nname: code-review")
    assert skill["source_document_id"] == document["source_document_id"]
    assert status["capability_run_state"]["seed_source_bundle_artifact_id"]
    assert status["validation"]["ok"] is True


def test_ingest_start_elides_workspace_skill_content_from_prompt_and_metadata(
    tmp_path: Path,
) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    marker = "WORKSPACE_SKILL_FULL_BODY_MARKER"
    skill_path = tmp_path / "skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_content = (
        "---\nname: review\ndescription: Review code changes.\n---\n\n"
        + ("A" * 5000)
        + marker
    )
    skill_path.write_text(skill_content, encoding="utf-8")
    dependency_skill = tmp_path / "node_modules" / "third-party" / "SKILL.md"
    dependency_skill.parent.mkdir(parents=True)
    dependency_skill.write_text("THIRD_PARTY_SKILL_SHOULD_NOT_BE_SCANNED", encoding="utf-8")

    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install the local review skill.",
            },
            "workspace_root": str(tmp_path),
        }
    )

    source_docs = {
        item["source_path"]: item
        for item in result.source_bundle["documents"]
        if item.get("source_path")
    }
    assert "skills/review/SKILL.md" in source_docs
    assert "node_modules/third-party/SKILL.md" not in source_docs
    assert marker in source_docs["skills/review/SKILL.md"]["content"]
    seed_artifact_id = f"capability-source-seed-bundle:{result.agent_run.id}"
    seed_artifact = next(
        item
        for item in control.artifacts_to_dict(result.agent_run.id)
        if item["id"] == seed_artifact_id
    )
    assert marker in seed_artifact["content"]
    assert marker not in result.agent_run.prompt

    metadata_docs = {
        item["source_path"]: item
        for item in result.agent_run.metadata["source_bundle"]["documents"]
        if item.get("source_path")
    }
    metadata_doc = metadata_docs["skills/review/SKILL.md"]
    assert metadata_doc["source_document_id"] == source_docs["skills/review/SKILL.md"][
        "source_document_id"
    ]
    assert metadata_doc["content_omitted_from_prompt"] is True
    assert marker not in json.dumps(metadata_doc)


def test_ingest_status_does_not_read_skill_content_from_agent_run_workdir(tmp_path: Path) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    skill_path = tmp_path / "SKILL.md"
    skill_content = (
        "---\n"
        "name: stop-slop\n"
        "description: Detect vague AI writing.\n"
        "---\n\n"
        "Detect vague AI writing.\n"
    )
    skill_path.write_text(skill_content, encoding="utf-8")
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install the stop-slop skill from the checked out worktree.",
            }
        }
    )
    task = control.get_agent_run(result.agent_run.id)
    task.workdir = str(tmp_path)
    task.metadata["source_bundle"] = {
        "source": {"type": "github_repo", "url": "https://github.com/hardikpandya/stop-slop"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "Detect vague AI writing."}],
        "errors": [],
    }
    draft_decision = {
        "id": "stop-slop",
        "name": "Stop Slop",
        "source": {"type": "github_repo", "url": "https://github.com/hardikpandya/stop-slop"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:stop-slop",
                    "kind": "skill",
                    "name": "stop-slop",
                    "source_path": "skills/stop-slop/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Detect vague AI writing."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert "skill_content" not in skill
    assert status["validation"]["ok"] is False
    assert any(
        "source bundle does not contain a complete source document" in message
        for message in status["validation"]["messages"]
    )


def test_ingest_status_does_not_read_skill_content_from_sandbox_container_workdir() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install the stop-slop skill from the checked out sandbox worktree.",
            }
        }
    )
    task = control.get_agent_run(result.agent_run.id)
    task.workdir = "/workspace/.rcoder/agent-runs/workspace/task/workdir/stop-slop"
    task.metadata["sandbox_container_id"] = "sandbox-container-1"
    task.metadata["source_bundle"] = {
        "source": {"type": "github_repo", "url": "https://github.com/hardikpandya/stop-slop"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "Detect vague AI writing."}],
        "errors": [],
    }
    draft_decision = {
        "id": "stop-slop",
        "name": "Stop Slop",
        "source": {"type": "github_repo", "url": "https://github.com/hardikpandya/stop-slop"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:stop-slop",
                    "kind": "skill",
                    "name": "stop-slop",
                    "source_path": "skills/stop-slop/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Detect vague AI writing."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert "skill_content" not in skill
    assert status["validation"]["ok"] is False
    assert any(
        "source bundle does not contain a complete source document" in message
        for message in status["validation"]["messages"]
    )


def test_ingest_status_requires_source_document_instead_of_scanning_ambiguous_workdir(tmp_path: Path) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    for name in ("one", "two"):
        path = tmp_path / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\n---\n\n{name}\n", encoding="utf-8")
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install one generated skill.",
            }
        }
    )
    task = control.get_agent_run(result.agent_run.id)
    task.workdir = str(tmp_path)
    task.metadata["source_bundle"] = {
        "source": {"type": "project_notes"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "Generated skill."}],
        "errors": [],
    }
    draft_decision = {
        "id": "ambiguous-skill",
        "name": "Ambiguous Skill",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:ambiguous",
                    "kind": "skill",
                    "name": "ambiguous",
                    "source_path": "SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Generated skill."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["validation"]["ok"] is False
    assert any(
        "source bundle does not contain a complete source document" in message
        for message in status["validation"]["messages"]
    )


def test_ingest_status_builds_skill_content_from_agent_run_read_file_event() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this review skill.",
            }
        }
    )
    skill_content = "---\nname: code-review\ndescription: Review code changes.\n---\n\nReview code changes.\n"
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "input": {"path": "skills/code-review/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "output": skill_content,
            },
        ),
    )
    draft_decision = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:code-review",
                    "kind": "skill",
                    "name": "code-review",
                    "source_path": "skills/code-review/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "Review code changes."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    document = next(
        item
        for item in status["source_bundle"]["documents"]
        if item.get("source_path") == "skills/code-review/SKILL.md"
    )
    assert skill["skill_content"] == skill_content.strip()
    assert skill["source_document_id"] == document["source_document_id"]
    assert skill["config"]["source_document_id"] == document["source_document_id"]
    assert status["validation"]["ok"] is True


def test_ingest_status_builds_source_inventory_from_session_tool_call_events() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    source_path = "skills/gsap-core/SKILL.md"
    skill_content = (
        "---\n"
        "name: gsap-core\n"
        "description: Use GSAP core animation APIs.\n"
        "---\n\n"
        "Use GSAP core animation APIs with source-backed guidance.\n"
    )
    _append_raw_agent_run_event(
        control,
        result.agent_run.id,
        "tool_call_start",
        {
            "tool_name": "read_file",
            "tool_call_id": "read-gsap-core",
            "tool_args": {"file_path": source_path},
        },
    )
    _append_raw_agent_run_event(
        control,
        result.agent_run.id,
        "tool_call_end",
        {
            "tool_name": "read_file",
            "tool_call_id": "read-gsap-core",
            "tool_result": skill_content,
        },
    )
    patches: list[tuple[str, object]] = [
        ("id", "gsap-skills"),
        ("name", "GSAP Skills"),
        (
            "description",
            "Installable GSAP animation guidance from repository Skill files.",
        ),
        (
            "source_inventory",
            {"skill_files": [{"source_path": source_path}], "docs": [], "files": []},
        ),
        (
            "contributions.skills",
            [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                    "display_name": "GSAP core",
                    "summary": "Use GSAP core animation APIs.",
                    "source_path": source_path,
                }
            ],
        ),
        ("install_plan", []),
        ("usage", []),
        (
            "evidence",
            [{"title": "GSAP core", "excerpt": "Use GSAP core animation APIs."}],
        ),
        ("risk_level", "low"),
    ]
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=_capability_patch_stream(patches, source_path=source_path),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["failure"] is None
    assert status["validation"]["ok"] is True
    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    inventory = status["source_bundle"]["source_inventory"]
    assert {item["source_path"] for item in inventory["documents"]} == {source_path}
    assert {item["path"] for item in inventory["skill_files"]} == {source_path}
    assert {item["event_type"] for item in inventory["tool_calls"]} == {
        "tool_use",
        "tool_result",
    }


def test_ingest_status_prefers_full_read_after_paged_read_file_same_source_path() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this skill after completing a paged read.",
            }
        }
    )
    source_path = "skills/partial/SKILL.md"
    paged_output = "\n".join(
        [
            "1\t---",
            "2\tname: partial",
            "3\t---",
            "4\t",
            "5\tPartial preview.",
            "... (12 lines total, showing 1-5; use override=true to read full file)",
        ]
    )
    full_content = "---\nname: partial\n---\n\nComplete skill content.\n"
    for call_id, output, input_payload in (
        ("read-partial", paged_output, {"file_path": source_path, "limit": 5}),
        ("read-full", full_content, {"file_path": source_path, "override": True}),
    ):
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent(
                type="tool_use",
                data={
                    "tool_name": "read_file",
                    "tool_call_id": call_id,
                    "input": input_payload,
                },
            ),
        )
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent(
                type="tool_result",
                data={
                    "tool_name": "read_file",
                    "tool_call_id": call_id,
                    "output": output,
                },
            ),
        )
    draft_decision = {
        "id": "partial",
        "name": "Partial",
        "source": {"type": "project_notes"},
        "materialization_plan": [
            {
                "component_id": "skill:partial",
                "source_path": source_path,
                "content_ref": "read-full",
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:partial",
                    "kind": "skill",
                    "name": "partial",
                }
            ]
        },
        "evidence": [{"title": "Partial skill", "excerpt": source_path}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == full_content.strip()
    assert status["validation"]["ok"] is True
    inventory = status["source_bundle"]["source_inventory"]
    document = next(item for item in inventory["documents"] if item["source_path"] == source_path)
    assert document["content_complete"] is True
    assert document["tool_call_id"] == "read-full"
    assert document["content_ref"] == "read-full"


def test_ingest_status_treats_list_file_as_inventory_not_skill_content() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    source_path = "skills/gsap-core/SKILL.md"
    skill_content = "---\nname: gsap-core\n---\n\nUse GSAP core.\n"
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "list_file",
                "tool_call_id": "list-skill-dir",
                "input": {"path": "skills/gsap-core"},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "list_file",
                "tool_call_id": "list-skill-dir",
                "output": source_path,
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "input": {"path": source_path},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "output": skill_content,
            },
        ),
    )
    draft_decision = {
        "id": "gsap-skills",
        "name": "GSAP Skills",
        "source": {"type": "github_repo", "url": "https://github.com/greensock/gsap-skills"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                    "source_path": source_path,
                }
            ]
        },
        "evidence": [{"title": "GSAP", "excerpt": source_path}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    inventory = status["source_bundle"]["source_inventory"]
    assert {item["path"] for item in inventory["skill_files"]} == {source_path}
    assert {item["source_path"] for item in inventory["documents"]} == {source_path}
    assert all(item.get("tool_name") != "list_file" for item in inventory["documents"])
    assert status["validation"]["ok"] is True


def test_ingest_status_materializes_gsap_style_skill_repo_from_agent_run_reads() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    skill_contents = {
        "skills/gsap-core/SKILL.md": (
            "---\n"
            "name: gsap-core\n"
            "description: Use GSAP core animation APIs.\n"
            "---\n\n"
            "Use GSAP core animation APIs with source-backed guidance.\n"
        ),
        "skills/gsap-timeline/SKILL.md": (
            "---\n"
            "name: gsap-timeline\n"
            "description: Build GSAP timelines.\n"
            "---\n\n"
            "Build coordinated GSAP timelines.\n"
        ),
    }
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "glob",
                "tool_call_id": "glob-skills",
                "input": {"pattern": "skills/**/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "glob",
                "tool_call_id": "glob-skills",
                "output": "\n".join(skill_contents),
            },
        ),
    )
    for index, (path, content) in enumerate(skill_contents.items(), start=1):
        call_id = f"read-skill-{index}"
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent(
                type="tool_use",
                data={
                    "tool_name": "read_file",
                    "tool_call_id": call_id,
                    "input": {"path": path},
                },
            ),
        )
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent(
                type="tool_result",
                data={
                    "tool_name": "read_file",
                    "tool_call_id": call_id,
                    "output": content,
                },
            ),
        )
    draft_decision = {
        "id": "gsap-skills",
        "name": "GSAP Skills",
        "source": {"type": "github_repo", "url": "https://github.com/greensock/gsap-skills"},
        "source_inventory": {
            "skill_files": list(skill_contents),
        },
        "materialization_plan": [
            {
                "component_id": "skill:gsap-core",
                "source_path": "skills/gsap-core/SKILL.md",
                "content_ref": "read-skill-1",
            },
            {
                "component_id": "skill:gsap-timeline",
                "source_path": "skills/gsap-timeline/SKILL.md",
                "content_ref": "read-skill-2",
            },
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                    "summary": "Use GSAP core animation APIs.",
                },
                {
                    "id": "skill:gsap-timeline",
                    "kind": "skill",
                    "name": "gsap-timeline",
                    "summary": "Build GSAP timelines.",
                },
            ]
        },
        "evidence": [{"title": "GSAP skills", "excerpt": "skills/gsap-core/SKILL.md"}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skills = {
        item["name"]: item
        for item in status["draft"]["contributions"]["skills"]
    }
    assert skills["gsap-core"]["skill_content"] == skill_contents["skills/gsap-core/SKILL.md"].strip()
    assert skills["gsap-timeline"]["skill_content"] == skill_contents["skills/gsap-timeline/SKILL.md"].strip()
    assert status["validation"]["ok"] is True
    inventory = status["source_bundle"]["source_inventory"]
    assert {item["path"] for item in inventory["skill_files"]} == set(skill_contents)
    assert all("content" not in item for item in inventory["documents"])
    assert inventory["raw_event_refs"]


def test_ingest_status_reports_incomplete_model_output_after_source_discovery() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    source_path = "skills/gsap-core/SKILL.md"
    skill_content = (
        "---\n"
        "name: gsap-core\n"
        "description: Use GSAP core animation APIs.\n"
        "---\n\n"
        "Use GSAP core animation APIs with source-backed guidance.\n"
    )
    for index in range(1005):
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent(
                type="status",
                data={"status": "thinking", "index": index},
            ),
        )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-gsap-core",
                "input": {"file_path": source_path},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-gsap-core",
                "output": skill_content,
            },
        ),
    )
    truncated_output = (
        '{"id": "gsap-skills", "name": "GSAP Skills", '
        '"source": {"type": "github_repo", "url": "https://github.com/greensock/gsap-skills"}, '
        '"contributions": {"skills": [{"id": "skill:gsap-core", "kind": "skill", '
        '"name": "gsap-core", "runs_on": "server'
    )
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=truncated_output,
            events=[
                ExecutorEvent.status(
                    "model_output_interrupted",
                    stream_status="interrupted",
                    classification="text_interrupted",
                    message="peer closed connection without sending complete message body",
                    recovery={"attempted": True, "failed": True, "max_attempts": 1},
                )
            ],
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["draft"] is None
    assert status["failure"]["code"] == "model_output_incomplete"
    assert status["failure"]["code"] != "source_discovery_incomplete"
    inventory = status["source_bundle"]["source_inventory"]
    assert {item["source_path"] for item in inventory["documents"]} == {source_path}
    assert {item["path"] for item in inventory["skill_files"]} == {source_path}
    assert status["capability_run_state"]["materialization_source"] == "artifact"
    assert status["capability_run_state"]["source_summary"]["skill_files"] == 1


def test_ingest_status_diagnoses_interrupted_output_beyond_display_event_window() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install a skill from interrupted model output.",
            }
        }
    )
    for index in range(1005):
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent.status("thinking", index=index),
        )
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output="",
            events=[
                ExecutorEvent.status(
                    "model_output_interrupted",
                    stream_status="interrupted",
                    classification="text_interrupted",
                    message="peer closed connection without sending complete message body",
                    recovery={"attempted": True, "failed": True, "max_attempts": 1},
                )
            ],
        ),
    )

    status = service.status(result.agent_run.id)

    assert len(status["events"]) == 1000
    assert status["draft"] is None
    assert status["failure"]["code"] == "draft_generation_interrupted"
    assert status["failure"]["code"] not in {
        "draft_not_produced",
        "source_discovery_incomplete",
    }


def test_ingest_status_records_field_patch_without_draft_ready() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    source_path = "skills/gsap-core/SKILL.md"
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-gsap-core",
                "input": {"file_path": source_path},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-gsap-core",
                "output": "---\nname: gsap-core\n---\n\nUse GSAP core.\n",
            },
        ),
    )
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output="",
            events=[
                ExecutorEvent.text_event(
                    json.dumps(
                        {
                            "capability_draft_patch": {
                                "field_path": "repo_summary",
                                "value": "GSAP skill repository with animation guidance.",
                                "source_refs": [
                                    {"source_path": source_path},
                                ],
                            }
                        }
                    )
                )
            ],
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["draft"] is None
    assert status["failure"]["code"] == "field_generation_incomplete"
    run_state = status["capability_run_state"]
    assert run_state["phase"] == "draft_missing"
    assert run_state["source_evidence"]["source_summary"]["skill_files"] == 1
    assert run_state["ingest_state"]["phase"] == "draft_missing"
    assert run_state["ingest_state"]["field_generation_state"]["patch_count"] == 1
    assert run_state["field_generation"]["patch_count"] == 1
    assert run_state["field_generation"]["field_state"]["repo_summary"]["status"] == "filled"
    assert run_state["draft_assembly"]["missing_fields"]
    assert "contributions" in run_state["draft_assembly"]["missing_fields"]


def test_ingest_status_assembles_completed_draft_from_field_patches() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    source_path = "skills/gsap-core/SKILL.md"
    skill_content = (
        "---\n"
        "name: gsap-core\n"
        "description: Use GSAP core animation APIs.\n"
        "---\n\n"
        "Use GSAP core animation APIs with source-backed guidance.\n"
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-gsap-core",
                "input": {"file_path": source_path},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-gsap-core",
                "output": skill_content,
            },
        ),
    )

    def patch_event(field_path: str, value: object) -> ExecutorEvent:
        return ExecutorEvent.text_event(
            json.dumps(
                {
                    "capability_draft_patch": {
                        "field_path": field_path,
                        "value": value,
                        "source_refs": [{"source_path": source_path}],
                    }
                }
            )
        )

    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output="",
            events=[
                patch_event("id", "gsap-skills"),
                patch_event("name", "GSAP Skills"),
                patch_event(
                    "description",
                    "Installable GSAP animation guidance from repository Skill files.",
                ),
                patch_event(
                    "source_inventory",
                    {
                        "skill_files": [
                            {
                                "source_path": source_path,
                            }
                        ],
                        "docs": [],
                        "files": [],
                    },
                ),
                patch_event(
                    "contributions.skills",
                    [
                        {
                            "id": "skill:gsap-core",
                            "kind": "skill",
                            "name": "gsap-core",
                            "display_name": "GSAP core",
                            "summary": "Use GSAP core animation APIs.",
                            "source_path": source_path,
                        }
                    ],
                ),
                patch_event("install_plan", ["Install the gsap-core Skill file."]),
                patch_event("usage", ["Use gsap-core when authoring GSAP animations."]),
                patch_event(
                    "evidence",
                    [{"title": "GSAP core", "excerpt": "Use GSAP core animation APIs."}],
                ),
                patch_event("risk_level", "low"),
            ],
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["failure"] is None
    assert status["validation"]["ok"] is True
    draft = status["draft"]
    assert draft["source_inventory"]["skill_files"][0]["source_path"] == source_path
    skill = draft["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    run_state = status["capability_run_state"]
    assert run_state["field_generation"]["patch_count"] == 9
    assert run_state["draft_assembly"]["draft_present"] is True
    assert run_state["draft_assembly"]["missing_fields"] == []


def test_ingest_status_assembles_completed_draft_from_agent_run_output_patch_stream() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    source_path = "skills/gsap-core/SKILL.md"
    skill_content = (
        "---\n"
        "name: gsap-core\n"
        "description: Use GSAP core animation APIs.\n"
        "---\n\n"
        "Use GSAP core animation APIs with source-backed guidance.\n"
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent.tool_use(
            tool_name="read_file",
            tool_call_id="read-gsap-core",
            tool_args={"file_path": source_path},
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent.tool_result(
            tool_name="read_file",
            tool_call_id="read-gsap-core",
            output=skill_content,
        ),
    )
    patches: list[tuple[str, object]] = [
        ("id", "gsap-skills"),
        ("name", "GSAP Skills"),
        (
            "description",
            "Installable GSAP animation guidance from repository Skill files.",
        ),
        (
            "source_inventory",
            {"skill_files": [{"source_path": source_path}], "docs": [], "files": []},
        ),
        (
            "contributions.skills",
            [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                    "display_name": "GSAP core",
                    "summary": "Use GSAP core animation APIs.",
                    "source_path": source_path,
                }
            ],
        ),
        ("install_plan", []),
        ("usage", []),
        (
            "evidence",
            [{"title": "GSAP core", "excerpt": "Use GSAP core animation APIs."}],
        ),
        ("risk_level", "low"),
    ]
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=_capability_patch_stream(patches, source_path=source_path),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["failure"] is None
    assert status["validation"]["ok"] is True
    assert status["draft"]["install_plan"] == []
    assert status["draft"]["usage"] == []
    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    run_state = status["capability_run_state"]
    assert run_state["field_generation"]["patch_count"] == len(patches)
    assert run_state["draft_assembly"]["draft_present"] is True
    assert run_state["draft_assembly"]["missing_fields"] == []


def test_ingest_status_loads_patch_stream_event_beyond_event_window() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    source_path = "skills/gsap-core/SKILL.md"
    skill_content = (
        "---\n"
        "name: gsap-core\n"
        "description: Use GSAP core animation APIs.\n"
        "---\n\n"
        "Use GSAP core animation APIs with source-backed guidance.\n"
    )
    for index in range(1005):
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent.status("discovering", index=index),
        )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent.tool_use(
            tool_name="read_file",
            tool_call_id="read-gsap-core",
            tool_args={"file_path": source_path},
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent.tool_result(
            tool_name="read_file",
            tool_call_id="read-gsap-core",
            output=skill_content,
        ),
    )
    patches: list[tuple[str, object]] = [
        ("id", "gsap-skills"),
        ("name", "GSAP Skills"),
        (
            "source_inventory",
            {"skill_files": [{"source_path": source_path}], "docs": [], "files": []},
        ),
        (
            "contributions.skills",
            [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                    "source_path": source_path,
                }
            ],
        ),
        ("install_plan", []),
        ("usage", []),
        (
            "evidence",
            [{"title": "GSAP core", "excerpt": "Use GSAP core animation APIs."}],
        ),
        ("risk_level", "low"),
    ]
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent.text_event(_capability_patch_stream(patches, source_path=source_path)),
    )
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output="",
        ),
    )

    status = service.status(result.agent_run.id)

    assert len(status["events"]) == 1000
    assert status["failure"] is None
    assert status["validation"]["ok"] is True
    assert status["draft"]["contributions"]["skills"][0]["skill_content"] == skill_content.strip()
    run_state = status["capability_run_state"]
    assert run_state["field_generation"]["patch_count"] == len(patches)
    assert run_state["draft_assembly"]["missing_fields"] == []
    assert run_state["materialization_source"] == "artifact"


def test_ingest_status_materializes_source_documents_beyond_event_window_with_runtime_paths() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    task = control.get_agent_run(result.agent_run.id)
    task.workdir = "/workspace/.rcoder/agent-runs/workspace/task-abc/workdir/gsap-skills"
    core_runtime_path = (
        "/workspace/.rcoder/agent-runs/workspace/task-abc/workdir/"
        "gsap-skills/skills/gsap-core/SKILL.md"
    )
    timeline_runtime_path = (
        "/workspace/.rcoder/agent-runs/workspace/task-abc/workdir/"
        "gsap-skills/skills/gsap-timeline/SKILL.md"
    )
    core_source_path = "skills/gsap-core/SKILL.md"
    timeline_source_path = "skills/gsap-timeline/SKILL.md"
    core_skill_content = (
        "---\n"
        "name: gsap-core\n"
        "description: Use GSAP core animation APIs.\n"
        "---\n\n"
        "Use GSAP core animation APIs with source-backed guidance.\n"
    )
    timeline_skill_content = (
        "---\n"
        "name: gsap-timeline\n"
        "description: Build GSAP timelines.\n"
        "---\n\n"
        "Build coordinated GSAP timelines.\n"
    )
    for index in range(1005):
        control.append_executor_event(
            result.agent_run.id,
            ExecutorEvent(
                type="status",
                data={"status": "discovering", "index": index},
            ),
        )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "call-read-core",
                "input": {"file_path": core_runtime_path},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "call-read-core",
                "output": core_skill_content,
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "call-read-timeline",
                "input": {"file_path": timeline_runtime_path},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "call-read-timeline",
                "output": timeline_skill_content,
            },
        ),
    )
    draft_decision = {
        "id": "gsap-skills",
        "name": "GSAP Skills",
        "source": {"type": "github_repo", "url": "https://github.com/greensock/gsap-skills"},
        "materialization_plan": [
            {
                "component_id": "skill:gsap-core",
                "source_path": core_source_path,
                "content_ref": "read-file:skills/gsap-core/SKILL.md:round-03",
            },
            {
                "component_id": "skill:gsap-timeline",
                "source_path": timeline_source_path,
                "content_ref": "read-file:skills/gsap-timeline/SKILL.md:round-03",
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                },
                {
                    "id": "skill:gsap-timeline",
                    "kind": "skill",
                    "name": "gsap-timeline",
                }
            ]
        },
        "evidence": [{"title": "GSAP skills", "excerpt": core_source_path}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skills = {
        item["name"]: item
        for item in status["draft"]["contributions"]["skills"]
    }
    assert skills["gsap-core"]["skill_content"] == core_skill_content.strip()
    assert skills["gsap-core"]["source_path"] == core_source_path
    assert skills["gsap-timeline"]["skill_content"] == timeline_skill_content.strip()
    assert skills["gsap-timeline"]["source_path"] == timeline_source_path
    assert status["validation"]["ok"] is True
    inventory = status["source_bundle"]["source_inventory"]
    assert {item["path"] for item in inventory["skill_files"]} == {
        core_source_path,
        timeline_source_path,
    }
    assert {
        item["source_path"]
        for item in inventory["documents"]
        if item["source_path"].startswith("skills/gsap-")
    } == {core_source_path, timeline_source_path}
    document_ids = {
        item["source_path"]: item["source_document_id"]
        for item in inventory["documents"]
        if item["source_path"].startswith("skills/gsap-")
    }
    assert set(document_ids) == {core_source_path, timeline_source_path}
    assert all(value.startswith("cap-src-doc-") for value in document_ids.values())
    assert status["capability_run_state"]["phase"] == "draft_ready"
    assert status["capability_run_state"]["materialization_source"] == "artifact"
    artifact_id = f"capability-source-bundle:{result.agent_run.id}"
    assert status["capability_run_state"]["source_bundle_artifact_id"] == artifact_id
    artifacts = [
        item
        for item in control.artifacts_to_dict(result.agent_run.id)
        if item["id"] == artifact_id
    ]
    assert len(artifacts) == 1
    assert artifacts[0]["type"] == "document"
    assert artifacts[0]["metadata"]["kind"] == "capability_source_bundle"
    persisted_bundle = json.loads(artifacts[0]["content"])
    persisted_inventory = persisted_bundle["source_inventory"]
    assert {item["path"] for item in persisted_inventory["skill_files"]} == {
        core_source_path,
        timeline_source_path,
    }
    assert {
        item["source_path"]: item["source_document_id"]
        for item in persisted_inventory["documents"]
        if item["source_path"].startswith("skills/gsap-")
    } == document_ids

    control._events[result.agent_run.id] = []
    replayed_status = service.status(result.agent_run.id)

    replayed_skills = {
        item["name"]: item
        for item in replayed_status["draft"]["contributions"]["skills"]
    }
    assert replayed_status["events"] == []
    assert replayed_status["capability_run_state"]["materialization_source"] == "artifact"
    assert replayed_skills["gsap-core"]["skill_content"] == core_skill_content.strip()
    assert replayed_skills["gsap-timeline"]["skill_content"] == timeline_skill_content.strip()
    replayed_inventory = replayed_status["source_bundle"]["source_inventory"]
    assert {
        item["source_path"]: item["source_document_id"]
        for item in replayed_inventory["documents"]
        if item["source_path"].startswith("skills/gsap-")
    } == document_ids
    artifacts_after_replay = [
        item
        for item in control.artifacts_to_dict(result.agent_run.id)
        if item["id"] == artifact_id
    ]
    assert len(artifacts_after_replay) == 1


def test_ingest_status_strips_read_file_line_numbers_from_skill_content() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this numbered skill.",
            }
        }
    )
    source_path = "skills/numbered/SKILL.md"
    skill_content = "---\nname: numbered\n---\n\nUse numbered output safely.\n"
    numbered_output = "\n".join(
        f"{index}\t{line}"
        for index, line in enumerate(skill_content.splitlines(), start=1)
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-numbered",
                "input": {"file_path": source_path},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-numbered",
                "output": numbered_output,
            },
        ),
    )
    draft_decision = {
        "id": "numbered",
        "name": "Numbered",
        "source": {"type": "project_notes"},
        "materialization_plan": [
            {
                "component_id": "skill:numbered",
                "source_path": source_path,
                "content_ref": "read-numbered",
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:numbered",
                    "kind": "skill",
                    "name": "numbered",
                }
            ]
        },
        "evidence": [{"title": "Numbered skill", "excerpt": source_path}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert skill["skill_content"] == skill_content.strip()
    assert status["validation"]["ok"] is True


def test_ingest_status_does_not_resolve_fake_content_ref_by_component_name() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this skill with an invalid ref.",
            }
        }
    )
    skill_content = "---\nname: gsap-core\n---\n\nUse GSAP core.\n"
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-real-core",
                "input": {"file_path": "skills/gsap-core/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-real-core",
                "output": skill_content,
            },
        ),
    )
    draft_decision = {
        "id": "fake-ref",
        "name": "Fake Ref",
        "source": {"type": "project_notes"},
        "materialization_plan": [
            {
                "component_id": "skill:gsap-core",
                "content_ref": "read-file:skills/gsap-core/SKILL.md:round-03",
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                }
            ]
        },
        "evidence": [{"title": "GSAP skill", "excerpt": "skills/gsap-core/SKILL.md"}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert "skill_content" not in skill
    assert status["validation"]["ok"] is False
    assert status["capability_run_state"]["phase"] == "validation_failed"
    assert any("requires skill_content" in message for message in status["validation"]["messages"])


def test_ingest_status_rejects_paged_read_file_skill_content_without_complete_source() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this partially read skill.",
            }
        }
    )
    source_path = "skills/partial/SKILL.md"
    paged_output = "\n".join(
        [
            "1\t---",
            "2\tname: partial",
            "3\t---",
            "4\t",
            "5\tPartial content.",
            "... (12 lines total, showing 1-5; use override=true to read full file)",
        ]
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-partial",
                "input": {"file_path": source_path, "limit": 5},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-partial",
                "output": paged_output,
            },
        ),
    )
    draft_decision = {
        "id": "partial",
        "name": "Partial",
        "source": {"type": "project_notes"},
        "materialization_plan": [
            {
                "component_id": "skill:partial",
                "source_path": source_path,
                "content_ref": "read-partial",
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:partial",
                    "kind": "skill",
                    "name": "partial",
                }
            ]
        },
        "evidence": [{"title": "Partial skill", "excerpt": source_path}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert "skill_content" not in skill
    assert status["validation"]["ok"] is False
    assert status["capability_run_state"]["phase"] == "validation_failed"
    inventory = status["source_bundle"]["source_inventory"]
    document = next(item for item in inventory["documents"] if item["source_path"] == source_path)
    assert document["content_complete"] is False
    assert document["content_incomplete_reason"] == "read_file_paged_output"
    assert any("source document is incomplete" in message for message in status["validation"]["messages"])


def test_ingest_status_requires_source_document_even_with_exact_workdir_path(
    tmp_path: Path,
) -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    for name in ("one", "two"):
        path = tmp_path / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\n---\n\n{name} skill\n", encoding="utf-8")
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install one exact skill from a multi-skill worktree.",
            }
        }
    )
    task = control.get_agent_run(result.agent_run.id)
    task.workdir = str(tmp_path)
    task.metadata["source_bundle"] = {
        "source": {"type": "project_notes"},
        "documents": [],
        "evidence": [{"title": "Skill", "excerpt": "two skill"}],
        "errors": [],
    }
    draft_decision = {
        "id": "two-skill",
        "name": "Two Skill",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:two",
                    "kind": "skill",
                    "name": "two",
                    "source_path": "skills/two/SKILL.md",
                }
            ]
        },
        "evidence": [{"title": "Skill", "excerpt": "two skill"}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    skill = status["draft"]["contributions"]["skills"][0]
    assert "skill_content" not in skill
    assert status["validation"]["ok"] is False
    assert status["capability_run_state"]["phase"] == "validation_failed"
    assert any(
        "source bundle does not contain a complete source document" in message
        for message in status["validation"]["messages"]
    )


def test_ingest_status_classifies_draft_invalid_as_validation_failed() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start(
        {
            "source": {
                "type": "project_notes",
                "notes": "Install this invalid skill draft.",
            }
        }
    )
    draft_decision = {
        "id": "invalid-skill",
        "name": "Invalid Skill",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:invalid",
                    "kind": "skill",
                    "name": "invalid",
                    "skill_content": "---\nname: invalid\n---\n\nInvalid skill.\n",
                    "access": "admin",
                }
            ]
        },
        "evidence": [{"title": "Invalid skill", "excerpt": "Invalid skill."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["validation"]["ok"] is False
    assert status["failure"]["result_type"] == "draft_invalid"
    assert status["capability_run_state"]["phase"] == "validation_failed"
    assert any(
        "component.access must be read, write, or both" in message
        for message in status["validation"]["messages"]
    )


def test_ingest_status_reports_unsupported_external_install_envreq() -> None:
    control = _control_plane()
    service = CapabilityPackageIngestService(control)
    result = service.start({"repoUrl": "https://github.com/greensock/gsap-skills"})
    skill_content = "---\nname: gsap-core\n---\n\nUse GSAP core.\n"
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-core",
                "input": {"path": "skills/gsap-core/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        result.agent_run.id,
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-core",
                "output": skill_content,
            },
        ),
    )
    draft_decision = {
        "id": "gsap-skills",
        "name": "GSAP Skills",
        "source": {"type": "github_repo", "url": "https://github.com/greensock/gsap-skills"},
        "materialization_plan": [
            {
                "component_id": "skill:gsap-core",
                "source_path": "skills/gsap-core/SKILL.md",
            }
        ],
        "contributions": {
            "skills": [
                {
                    "id": "skill:gsap-core",
                    "kind": "skill",
                    "name": "gsap-core",
                }
            ],
            "environment_requirements": [
                {
                    "id": "envreq:executable:npx",
                    "kind": "executable",
                    "name": "npx",
                    "command": "npx",
                    "check": "npx --version",
                    "install": "Install Node.js (https://nodejs.org) which includes npx",
                }
            ],
        },
        "evidence": [{"title": "GSAP", "excerpt": "Use GSAP core."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        result.agent_run.id,
        ExecutorRunResult(
            task_id=result.agent_run.id,
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    status = service.status(result.agent_run.id)

    assert status["validation"]["ok"] is False
    assert status["draft"]["contributions"]["environment_requirements"][0]["id"] == "envreq:executable:npx"
    assert status["validation"]["draft"]["contributions"]["environment_requirements"][0]["id"] == "envreq:executable:npx"
    assert status["failure"]["result_type"] == "command_evidence_missing"
    assert status["capability_run_state"]["phase"] == "validation_failed"
    assert any(
        "envreq:executable:npx command lacks evidence: npx --version" in message
        for message in status["validation"]["messages"]
    )


def test_capability_package_session_reports_structured_skill_content_failure(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("invalid draft must not install")

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-skill-content-failure",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Generate one missing skill.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    draft_decision = {
        "id": "missing-skill",
        "name": "Missing Skill",
        "source": {"type": "project_notes"},
        "contributions": {
            "skills": [
                {
                    "id": "skill:missing",
                    "kind": "skill",
                    "name": "missing",
                }
            ]
        },
        "evidence": [{"title": "Project notes", "excerpt": "Generate one missing skill."}],
        "risk_level": "low",
    }
    control.complete_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output=json.dumps(draft_decision),
        ),
    )

    _wait_for(lambda: session.done)

    result = next(
        event["payload"]
        for event in session.events
        if event["type"] == "workflow_result"
        and event["payload"].get("result_type") == "skill_content_unresolved"
    )
    assert result["status"] == "error"
    assert result["result"]["code"] == "skill_content_unresolved"
    assert any(
        "requires skill_content" in message
        for message in result["result"]["messages"]
    )
    error = next(event["payload"] for event in session.events if event["type"] == "error")
    assert error["code"] == "skill_content_unresolved"


def test_capability_package_session_reports_incomplete_field_generation_without_approval(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("incomplete field generation must not install")

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-field-incomplete",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        locale="en",
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {"repoUrl": "https://github.com/greensock/gsap-skills"},
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-gsap-core",
                "input": {"file_path": "skills/gsap-core/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-gsap-core",
                "output": "---\nname: gsap-core\n---\n\nUse GSAP core.\n",
            },
        ),
    )
    control.complete_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output="",
            events=[
                ExecutorEvent.text_event(
                    json.dumps(
                        {
                            "capability_draft_patch": {
                                "field_path": "repo_summary",
                                "value": "GSAP skill repository.",
                            }
                        }
                    )
                )
            ],
        ),
    )

    _wait_for(lambda: session.done)

    assert not any(event["type"] == "workflow_decision" for event in session.events)
    assert not any(event["type"] == "workflow_artifact" for event in session.events)
    result = next(
        event["payload"]
        for event in session.events
        if event["type"] == "workflow_result"
        and event["payload"].get("result_type") == "field_generation_incomplete"
    )
    assert result["status"] == "error"
    assert result["result"]["code"] == "field_generation_incomplete"
    failed_event = next(
        event["payload"]
        for event in session.events
        if event["type"] == "session_run_failed"
    )
    assert failed_event["code"] == "field_generation_incomplete"


def test_capability_package_session_reports_interrupted_output_beyond_display_event_window(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("interrupted draft must not install")

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-output-interrupted",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        locale="en",
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Generate one interrupted skill.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    for index in range(1005):
        control.append_executor_event(
            str(agent_run_id),
            ExecutorEvent.status("thinking", index=index),
        )
    control.complete_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output="",
            events=[
                ExecutorEvent.status(
                    "model_output_interrupted",
                    stream_status="interrupted",
                    classification="text_interrupted",
                    message="peer closed connection without sending complete message body",
                    recovery={"attempted": True, "failed": True, "max_attempts": 1},
                )
            ],
        ),
    )

    _wait_for(lambda: session.done)

    assert not any(event["type"] == "workflow_decision" for event in session.events)
    assert not any(event["type"] == "workflow_artifact" for event in session.events)
    result = next(
        event["payload"]
        for event in session.events
        if event["type"] == "workflow_result"
        and event["payload"].get("result_type") == "draft_generation_interrupted"
    )
    assert result["status"] == "error"
    assert result["result"]["code"] == "draft_generation_interrupted"
    failed_event = next(
        event["payload"]
        for event in session.events
        if event["type"] == "session_run_failed"
    )
    assert failed_event["code"] == "draft_generation_interrupted"


def test_capability_package_session_requests_approval_from_patch_stream_with_empty_lists(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def accept_capability_package_draft(self, payload: dict[str, object]):
            self.payloads.append(payload)

            class Result:
                ok = True
                status = 200
                payload = {"ok": True, "package_id": "review-empty-plan"}

            return Result()

    control = _control_plane()
    admin = FakeAdminManager()
    session = _RemoteSessionRun(
        session_run_id="session-run-empty-list-patches",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        locale="en",
    )
    service = CapabilityPackageSessionRunService(
        control,
        admin,
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    patches: list[tuple[str, object]] = [
        ("id", "review-empty-plan"),
        ("name", "Review Empty Plan"),
        (
            "contributions.environment_requirements",
            [
                {
                    "id": "envreq:executable:gh",
                    "kind": "executable",
                    "name": "gh",
                    "command": "gh",
                    "check": "gh --version",
                }
            ],
        ),
        ("install_plan", []),
        ("usage", []),
        (
            "evidence",
            [
                {
                    "title": "Project notes",
                    "excerpt": "Install gh and run gh --version.",
                }
            ],
        ),
        ("risk_level", "low"),
    ]
    control.complete_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output=_capability_patch_stream(patches),
        ),
    )

    approval = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "workflow_decision"
            ),
            None,
        )
    )
    draft_event = next(event for event in session.events if event["type"] == "workflow_artifact")
    assert draft_event["payload"]["artifact_type"] == "capability_package_draft"
    assert draft_event["payload"]["artifact"]["package_id"] == "review-empty-plan"
    assert approval["tool_name"] == "install_capability_package"
    assert approval["decision_type"] == "capability_package_install"
    assert approval["review"]["package_id"] == "review-empty-plan"
    session.resolve_approval(str(approval["approval_id"]), "allow_once", None)
    _wait_for(lambda: session.done)

    assert admin.payloads
    draft = admin.payloads[0]["draft"]  # type: ignore[index]
    assert draft["id"] == "review-empty-plan"  # type: ignore[index]
    assert draft["install_plan"] == []  # type: ignore[index]
    assert draft["usage"] == []  # type: ignore[index]


def test_capability_package_session_run_requests_install_approval_and_installs(tmp_path: Path) -> None:
    class FakeAdminManager:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def accept_capability_package_draft(self, payload: dict[str, object]):
            self.payloads.append(payload)

            class Result:
                ok = True
                status = 200
                payload = {"ok": True, "package_id": "review"}

            return Result()

    control = _control_plane()
    admin = FakeAdminManager()
    document: dict[str, object] | None = None

    def trace_sink(
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        nonlocal document
        document = apply_session_event(
            document,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            session_event_seq=(int(document.get("last_event_seq") or 0) + 1)
            if isinstance(document, dict)
            else 1,
            session_run_id=session_run_id,
            session_run_seq=session_run_seq,
        )
        return int(document.get("last_event_seq") or 0)

    session = _RemoteSessionRun(
        session_run_id="session-run-1",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        locale="en",
        trace_event_sink=trace_sink,
    )
    session.enable_trace_persistence("session-1")
    session.append_event(
        "session_run_start",
        {
            "prompt": "Create capability package",
            "mode": "capability_package",
            "workflow_mode": "capability_package_ingest",
            "locale": "en",
        },
    )
    session.mark_running()
    service = CapabilityPackageSessionRunService(
        control,
        admin,
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent(
            type="tool_use",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "input": {"path": "skills/review/SKILL.md"},
            },
        ),
    )
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent(
            type="tool_result",
            data={
                "tool_name": "read_file",
                "tool_call_id": "read-skill",
                "output": "Review package skill content.",
                "path": "skills/review/SKILL.md",
            },
        ),
    )
    draft = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "contributions": {
            "environment_requirements": [
                {
                    "id": "envreq:executable:gh",
                    "kind": "executable",
                    "name": "gh",
                    "command": "gh",
                    "check": "gh --version",
                }
            ]
        },
        "install_plan": ["Install GitHub CLI."],
        "usage": ["Use gh pr view."],
        "evidence": [{"title": "Project notes", "excerpt": "Install gh and run gh --version"}],
        "credentials": ["GITHUB_TOKEN"],
        "risk_level": "low",
    }
    control.complete_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(draft)}\n```",
        ),
    )
    approval = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "workflow_decision"
            ),
            None,
        )
    )
    draft_event = next(event for event in session.events if event["type"] == "workflow_artifact")
    assert draft_event["payload"]["artifact_type"] == "capability_package_draft"
    assert draft_event["payload"]["artifact"]["package_id"] == "review"
    assert approval["tool_name"] == "install_capability_package"
    assert approval["tool_call_id"]
    assert approval["intent"] == "Confirm installing capability package review"
    assert approval["sections"][0]["title"] == "Capability package"
    assert approval["sections"][1]["title"] == "Component summary"
    assert approval["sections"][2]["title"] == "Runtime footprint"
    assert approval["sections"][2]["items"][0]["value"] == "需要在本机安装/配置"
    assert approval["sections"][2]["items"][1]["value"] == "Local client"
    assert approval["decision_type"] == "capability_package_install"
    assert approval["review"]["package_id"] == "review"
    status_approvals = session.status_payload()["approvals"]
    assert len(status_approvals) == 1
    assert status_approvals[0]["approval_id"] == approval["approval_id"]
    assert status_approvals[0]["decision_type"] == "capability_package_install"
    assert status_approvals[0]["tool_name"] == "install_capability_package"
    assert status_approvals[0]["review"]["package_id"] == "review"
    assert status_approvals[0]["state"] == "requested"
    session.append_event("reasoning_delta", {"content": "Installing package."})
    session.resolve_approval(str(approval["approval_id"]), "allow_once", None)
    _wait_for(lambda: session.done)

    assert admin.payloads
    assert admin.payloads[0]["draft"]["id"] == "review"  # type: ignore[index]
    assert not any(event["type"] in {"tool_call_start", "tool_call_end"} for event in session.events)
    tool_steps = [
        event["payload"]
        for event in session.events
        if event["type"] == "workflow_step"
        and event["payload"].get("details", {}).get("tool_call_id") == "read-skill"
    ]
    assert {step["status"] for step in tool_steps} == {"running", "done"}
    assert all(step["stage"] == "read_source" for step in tool_steps)
    done_tool_step = next(step for step in tool_steps if step["status"] == "done")
    assert done_tool_step["details"]["tool_name"] == "read_file"
    assert done_tool_step["details"]["tool_call_id"] == "read-skill"
    assert done_tool_step["details"]["raw_event_refs"][0]["type"] == "tool_result"
    assert "tool_result" not in done_tool_step
    assert "tool_result" not in done_tool_step["details"]
    assert any(event["type"] == "workflow_result" for event in session.events)
    assert session.events[-1]["type"] in {"session_run_end", "approval_resolved", "workflow_result"}
    assert any(
        event["type"] == "session_run_end"
        and event["payload"].get("response") == "Capability package review installed."
        and event["payload"].get("response_rendered") is True
        for event in session.events
    )
    assert isinstance(document, dict)
    assert document["stats"]["runStatus"] == "done"  # type: ignore[index]
    parts = document["turns"][0]["assistantMessages"][0]["parts"]  # type: ignore[index]
    assert not any(
        part.get("type") == "thinking" and part.get("active") is True
        for part in parts
    )


def test_capability_package_session_process_text_follows_english_locale(tmp_path: Path) -> None:
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-1",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        locale="en",
    )
    service = CapabilityPackageSessionRunService(
        control,
        object(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="sandbox_worker",
        executors=["fake"],
        peer_id="peer-1",
    )
    assert claim is not None
    control.complete_claimed_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="failed",
            output="",
            error="No model provider/profile is configured.",
        ),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    _wait_for(lambda: session.done)

    messages = [
        str(event["payload"].get("message") or "")
        for event in session.events
        if event["type"] == "workflow_step"
    ]
    assert "Starting capability package draft generation" in messages
    assert "Capability package generation task entered capability_packager" in messages
    assert "Capability package generation task queued" in messages
    assert "Capability package generation task accepted by sandbox worker" in messages
    assert not any("能力包" in message for message in messages)


def test_capability_package_session_unknown_failure_uses_session_locale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_start(self, payload, *, agent_run_metadata=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(CapabilityPackageIngestService, "start", fail_start)
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-1",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        locale="zh-CN",
    )
    service = CapabilityPackageSessionRunService(
        control,
        object(),
        poll_timeout_sec=0.05,
    )

    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    _wait_for(lambda: session.done)

    error_event = next(event for event in session.events if event["type"] == "error")
    failed_event = next(event for event in session.events if event["type"] == "session_run_failed")
    assert error_event["payload"]["message"] == "能力包流程执行失败。"
    assert error_event["payload"]["message_key"] == "capability_package.session_failed"
    assert error_event["payload"]["diagnostic_message"] == "boom"
    assert failed_event["payload"]["message"] == "能力包流程执行失败。"


def test_capability_package_session_follow_up_revises_pending_draft(tmp_path: Path) -> None:
    class FakeAdminManager:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def accept_capability_package_draft(self, payload: dict[str, object]):
            self.payloads.append(payload)
            raise AssertionError("draft revision should not install the previous approval")

    control = _control_plane()
    admin = FakeAdminManager()
    session = _RemoteSessionRun(
        session_run_id="session-run-revise",
        peer_id="peer-1",
        session_hint="session-1",
        locale="zh-CN",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(control, admin, poll_timeout_sec=0.05)
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install hub, then use hub pr show for review.",
            }
        },
    )
    first_agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    first_agent_run = control.agent_run_to_dict(str(first_agent_run_id))
    assert first_agent_run["metadata"]["locale"] == "zh-CN"
    assert "所有用户可见的生成内容都必须使用简体中文" in first_agent_run["prompt"]
    assert "生成草案中的自然语言字段" in first_agent_run["prompt"]
    assert "你是 capability_packager" in first_agent_run["prompt"]
    first_draft = _review_draft(command="hub")
    control.complete_agent_run(
        str(first_agent_run_id),
        ExecutorRunResult(
            task_id=str(first_agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(first_draft)}\n```",
        ),
    )
    first_approval = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "workflow_decision"
            ),
            None,
        )
    )

    session.submit_follow_up(
        "把依赖改成 gh，不要用 hub",
        followup_id="follow-revise",
        client_request_id="pending-revise",
    )
    second_agent_run_id = _wait_for(
        lambda: next(
            (
                run["id"]
                for run in control.list_agent_runs(agent_id="capability_packager")
                if run["id"] != first_agent_run_id
            ),
            "",
        )
    )
    second_agent_run = control.agent_run_to_dict(str(second_agent_run_id))
    assert second_agent_run["metadata"]["session_run_id"] == "session-run-revise"
    assert second_agent_run["metadata"]["locale"] == "zh-CN"
    assert second_agent_run["metadata"]["revision_of_agent_run_id"] == first_agent_run_id
    assert second_agent_run["metadata"]["revision_followup_id"] == "follow-revise"
    assert second_agent_run["metadata"]["revision_instruction"] == "把依赖改成 gh，不要用 hub"
    assert second_agent_run["parent_task_id"] == first_agent_run_id
    assert "用户意见：" in second_agent_run["prompt"]
    assert "把依赖改成 gh，不要用 hub" in second_agent_run["prompt"]
    assert '"command": "hub"' in second_agent_run["prompt"]
    assert any(
        event["type"] == "approval_resolved"
        and event["payload"].get("approval_id") == first_approval["approval_id"]
        and event["payload"].get("reason") == "收到修改意见，重新生成草案。"
        for event in session.events
    )
    assert any(
        event["type"] == "session_run_follow_up_consumed"
        and event["payload"].get("followup_id") == "follow-revise"
        for event in session.events
    )
    assert any(
        event["type"] == "workflow_step"
        and event["payload"].get("details", {}).get("phase") == "capability_package_revision_requested"
        and "把依赖改成 gh，不要用 hub" in event["payload"].get("message", "")
        and event["payload"].get("details", {}).get("instruction") == "把依赖改成 gh，不要用 hub"
        for event in session.events
    )

    second_draft = _review_draft(command="gh")
    control.complete_agent_run(
        str(second_agent_run_id),
        ExecutorRunResult(
            task_id=str(second_agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(second_draft)}\n```",
        ),
    )
    approvals = _wait_for(
        lambda: [
            event["payload"]
            for event in session.events
            if event["type"] == "workflow_decision"
        ] if len([event for event in session.events if event["type"] == "workflow_decision"]) >= 2 else []
    )
    second_approval = approvals[-1]
    assert second_approval["approval_id"] != first_approval["approval_id"]
    assert second_approval["tool_name"] == "install_capability_package"
    assert second_approval["tool_args"]["agent_run_id"] == second_agent_run_id

    session.resolve_approval(str(second_approval["approval_id"]), "deny_once", "test_cleanup")
    _wait_for(lambda: session.done)
    assert admin.payloads == []


def test_peer_shutdown_keeps_capability_package_session_run_active(tmp_path: Path) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("peer shutdown must not install")

    control = _control_plane()
    http_service = object.__new__(RemoteRelayHTTPService)
    http_service._session_runs_lock = threading.Lock()
    http_service._session_runs = {}
    session = _RemoteSessionRun(
        session_run_id="session-run-peer-shutdown",
        peer_id="peer-1",
        session_hint="session-1",
        mode="capability_package",
        workflow_mode="capability_package_ingest",
        runtime_state={"mode": "capability_package", "workflow_mode": "capability_package_ingest"},
        artifact_root=tmp_path,
    )
    http_service._session_runs[session.session_run_id] = session
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )

    http_service._abort_peer_session_runs("peer-1", "peer_disconnected: peer_shutdown")
    time.sleep(0.1)

    task = control.agent_run_to_dict(str(agent_run_id))
    assert task["status"] in {"queued", "running"}
    assert session.done is False
    assert session.status == "running"
    assert not any(
        event["type"] == "error"
        and event["payload"].get("message") == "peer_disconnected: peer_shutdown"
        for event in session.events
    )

    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)


def test_capability_package_session_stays_attached_when_agent_run_lease_expires(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("lease recovery test should stop at approval")

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-lease-recover",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="sandbox_worker",
        executors=["fake"],
        peer_id="peer-1",
        lease_sec=1,
    )
    assert claim is not None
    assert claim.task.id == agent_run_id

    recovered = control.recover_stale_agent_runs(now=time.time() + 2)
    assert agent_run_id in recovered
    assert control.agent_run_to_dict(str(agent_run_id))["status"] == "queued"

    draft = _review_draft(command="gh")
    control.complete_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(draft)}\n```",
        ),
    )
    approval = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "workflow_decision"
            ),
            None,
        )
    )
    assert approval["tool_name"] == "install_capability_package"
    assert approval["tool_args"]["agent_run_id"] == agent_run_id
    assert session.done is False

    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)


def test_capability_package_session_cancel_cancels_agent_run(tmp_path: Path) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("cancelled session must not install")

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-2",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    session.request_cancel("user_cancelled")
    _wait_for(lambda: session.done)

    task = control.agent_run_to_dict(str(agent_run_id))
    assert task["status"] == "cancelled"


def test_capability_package_session_cancel_during_install_approval_does_not_append_install_terminal_events(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def accept_capability_package_draft(self, payload: dict[str, object]):
            self.payloads.append(payload)
            raise AssertionError("cancelled session must not install")

    control = _control_plane()
    admin = FakeAdminManager()
    session = _RemoteSessionRun(
        session_run_id="session-run-approval-cancel",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        admin,
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    draft = {
        "id": "review",
        "name": "Review",
        "source": {"type": "project_notes"},
        "contributions": {
            "environment_requirements": [
                {
                    "id": "envreq:executable:gh",
                    "kind": "executable",
                    "name": "gh",
                    "command": "gh",
                    "check": "gh --version",
                }
            ]
        },
        "install_plan": ["Install GitHub CLI."],
        "usage": ["Use gh pr view."],
        "evidence": [{"title": "Project notes", "excerpt": "Install gh and run gh --version"}],
        "credentials": ["GITHUB_TOKEN"],
        "risk_level": "low",
    }
    control.complete_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="completed",
            output=f"```json\n{json.dumps(draft)}\n```",
        ),
    )
    approval = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "workflow_decision"
            ),
            None,
        )
    )
    assert approval["tool_name"] == "install_capability_package"

    first_request, resolved_approvals = session.request_cancel("user_cancelled")
    assert first_request is True
    session.append_event("session_run_cancel_requested", {"reason": "user_cancelled"})
    for event_payload in resolved_approvals:
        session.append_event("approval_resolved", event_payload)
    session.append_event("session_run_cancelled", {"reason": "user_cancelled"})
    session.mark_done("user_cancelled")
    time.sleep(0.2)

    events = session.events
    assert admin.payloads == []
    assert any(event["type"] == "session_run_cancelled" for event in events)
    assert not any(event["type"] == "session_run_end" for event in events)
    assert not any(
        event["type"] == "workflow_result"
        and event["payload"].get("result_type") == "capability_package_install"
        for event in events
    )


def test_capability_package_session_persists_agent_run_progress_and_failure(
    tmp_path: Path,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("failed draft generation must not install")

    persisted: list[dict[str, object]] = []

    def trace_sink(
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        persisted.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "payload": dict(payload),
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return len(persisted)

    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-trace",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
        trace_event_sink=trace_sink,
    )
    session.enable_trace_persistence("session-1")
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {
            "source": {
                "type": "project_notes",
                "notes": "Install gh, then use gh pr view for review.",
            }
        },
    )
    agent_run_id = _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    claim = control.claim_agent_run(
        worker_id="worker-1",
        worker_kind="sandbox_worker",
        executors=["fake"],
        peer_id="peer-1",
    )
    assert claim is not None
    assert claim.task.id == agent_run_id
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent.status("preparing_worktree"),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    control.append_executor_event(
        str(agent_run_id),
        ExecutorEvent.status("worktree_ready", workdir="/tmp/work"),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )
    for idx in range(250):
        control.append_executor_event(
            str(agent_run_id),
            ExecutorEvent.text_event(f"progress line {idx}"),
            request_id=claim.request_id,
            worker_id="worker-1",
            peer_id="peer-1",
        )
    control.complete_claimed_agent_run(
        str(agent_run_id),
        ExecutorRunResult(
            task_id=str(agent_run_id),
            status="failed",
            output="",
            error="No model provider/profile is configured.",
        ),
        request_id=claim.request_id,
        worker_id="worker-1",
        peer_id="peer-1",
    )

    _wait_for(lambda: session.done)

    phases = [
        event["payload"].get("details", {}).get("phase")
        for event in persisted
        if event["event_type"] == "workflow_step"
    ]
    assert "agent_run_queued" in phases
    assert "agent_run_claimed" in phases
    assert "agent_run_worktree_ready" in phases
    assert "agent_run_failed" in phases
    assistant_deltas = [
        event for event in persisted if event["event_type"] == "assistant_delta"
    ]
    assert len(assistant_deltas) < 250
    assert any(
        event["event_type"] == "error"
        and "No model provider/profile is configured."
        in str(event["payload"].get("message") or "")
        for event in persisted
    )


def test_remote_session_run_replays_pending_trace_events_when_sink_is_attached(
    tmp_path: Path,
) -> None:
    http_service = object.__new__(RemoteRelayHTTPService)
    http_service._session_runs_lock = threading.Lock()
    http_service._session_runs = {}
    http_service.session_trace_event_sink = None
    session = _RemoteSessionRun(
        session_run_id="session-run-late-sink",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    http_service._session_runs[session.session_run_id] = session
    session.enable_trace_persistence("session-1")
    session.append_event(
        "context_event",
        {"message": "sink not attached yet", "phase": "late_sink"},
    )
    persisted: list[dict[str, object]] = []

    def trace_sink(
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        persisted.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "payload": dict(payload),
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return len(persisted)

    http_service.set_session_trace_event_sink(trace_sink)

    assert persisted == [
        {
            "session_id": "session-1",
            "event_type": "context_event",
            "payload": {"message": "sink not attached yet", "phase": "late_sink"},
            "session_run_id": "session-run-late-sink",
            "session_run_seq": 1,
            "source": "remote_session_run",
            "replayable": True,
        }
    ]


def test_remote_session_run_does_not_persist_when_sink_is_attached_without_trace_enable(
    tmp_path: Path,
) -> None:
    http_service = object.__new__(RemoteRelayHTTPService)
    http_service._session_runs_lock = threading.Lock()
    http_service._session_runs = {}
    http_service.session_trace_event_sink = None
    session = _RemoteSessionRun(
        session_run_id="session-run-memory-only",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    http_service._session_runs[session.session_run_id] = session
    session.append_event(
        "context_event",
        {"message": "memory only", "phase": "memory_only"},
    )
    persisted: list[dict[str, object]] = []

    def trace_sink(
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        session_run_id: str | None,
        session_run_seq: int | None,
        source: str,
        replayable: bool,
    ) -> int:
        persisted.append(
            {
                "session_id": session_id,
                "event_type": event_type,
                "payload": dict(payload),
                "session_run_id": session_run_id,
                "session_run_seq": session_run_seq,
                "source": source,
                "replayable": replayable,
            }
        )
        return len(persisted)

    http_service.set_session_trace_event_sink(trace_sink)

    assert persisted == []

    session.enable_trace_persistence("session-1")

    assert persisted == [
        {
            "session_id": "session-1",
            "event_type": "context_event",
            "payload": {"message": "memory only", "phase": "memory_only"},
            "session_run_id": "session-run-memory-only",
            "session_run_seq": 1,
            "source": "remote_session_run",
            "replayable": True,
        }
    ]


def test_capability_package_session_surfaces_source_bundle_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("source warning test should not install")

    def fake_fetch(self, **kwargs: object) -> str:
        return json.dumps(
            {
                "ok": False,
                "url": kwargs["url"],
                "title": "",
                "sections": [],
                "links": [],
                "evidence": [],
                "errors": [
                    {
                        "code": "fetch_failed",
                        "message": "The read operation timed out",
                        "url": kwargs["url"],
                    }
                ],
            }
        )

    monkeypatch.setattr(
        "labrastro_server.services.capability_packages.FetchCapabilitiesTool.execute",
        fake_fetch,
    )
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-source-warning",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(
        session,
        {"docsUrl": "https://docs.example.com/example-tool"},
    )

    warning = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "output"
                and event["payload"].get("level") == "warning"
            ),
            None,
        )
    )
    assert warning["code"] == "fetch_failed"
    assert warning["content"] == "未能从仓库或文档中抓取到可用于能力包生成的资料。"
    assert "The read operation timed out" not in warning["content"]
    assert warning["source_error"]["message"] == "The read operation timed out"
    assert warning["source_errors"][0]["message"] == "The read operation timed out"

    _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)


def test_capability_package_session_softens_partial_source_fetch_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("partial source warning test should not install")

    def fake_fetch(self, **kwargs: object) -> str:
        return json.dumps(
            {
                "ok": True,
                "url": kwargs["url"],
                "title": "Example Tool",
                "sections": [
                    {
                        "heading": "Install",
                        "source_url": f"{kwargs['url']}#install",
                        "text": "Install with npm.",
                    }
                ],
                "links": [],
                "evidence": [
                    {
                        "title": "Install",
                        "source_url": f"{kwargs['url']}#install",
                        "excerpt": "Install with npm.",
                    }
                ],
                "errors": [
                    {
                        "code": "network_error",
                        "message": "Remote end closed connection without response",
                        "url": kwargs["url"],
                        "attempts": 3,
                        "retryable": True,
                    }
                ],
            }
        )

    monkeypatch.setattr(
        "labrastro_server.services.capability_packages.FetchCapabilitiesTool.execute",
        fake_fetch,
    )
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-partial-source-warning",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(session, {"docsUrl": "https://docs.example.com/example-tool"})

    warning = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "output"
                and event["payload"].get("level") == "warning"
            ),
            None,
        )
    )
    assert warning["code"] == "network_error"
    assert warning["content"] == "部分在线资料读取失败，已继续使用可读取内容生成草案。"
    assert "Remote end closed" not in warning["content"]
    assert warning["source_error"]["message"] == "Remote end closed connection without response"
    assert warning["source_errors"][0]["attempts"] == 3

    _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)


def test_capability_package_session_surfaces_empty_source_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdminManager:
        def accept_capability_package_draft(self, payload: dict[str, object]):
            raise AssertionError("empty evidence test should not install")

    def fake_fetch(self, **kwargs: object) -> str:
        return json.dumps(
            {
                "ok": True,
                "url": kwargs["url"],
                "title": "Empty docs",
                "sections": [],
                "links": [],
                "evidence": [],
                "errors": [],
            }
        )

    monkeypatch.setattr(
        "labrastro_server.services.capability_packages.FetchCapabilitiesTool.execute",
        fake_fetch,
    )
    control = _control_plane()
    session = _RemoteSessionRun(
        session_run_id="session-run-empty-evidence",
        peer_id="peer-1",
        session_hint="session-1",
        artifact_root=tmp_path,
    )
    service = CapabilityPackageSessionRunService(
        control,
        FakeAdminManager(),
        poll_timeout_sec=0.05,
    )
    service.start(session, {"docsUrl": "https://docs.example.com/empty-tool"})

    warning = _wait_for(
        lambda: next(
            (
                event["payload"]
                for event in session.events
                if event["type"] == "output"
                and event["payload"].get("level") == "warning"
            ),
            None,
        )
    )
    assert warning["code"] == "source_evidence_empty"
    assert "未能从仓库或文档中抓取到可用于能力包生成的资料" in warning["content"]

    _wait_for(
        lambda: next(
            (
                event["payload"].get("agent_run_id")
                for event in session.events
                if event["type"] == "workflow_step"
                and event["payload"].get("agent_run_id")
            ),
            "",
        )
    )
    session.request_cancel("test_cleanup")
    _wait_for(lambda: session.done)
