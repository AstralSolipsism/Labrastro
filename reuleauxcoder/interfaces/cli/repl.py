"""Interactive REPL loop."""

import time
from pathlib import Path

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory

from reuleauxcoder import __version__
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.runtime.session_state import (
    build_session_runtime_state,
    get_session_fingerprint,
)
from reuleauxcoder.infrastructure.fs.paths import ensure_user_dirs
from labrastro_server.infrastructure.persistence.factory import (
    create_session_store as create_configured_session_store,
)
from reuleauxcoder.interfaces.cli.commands import handle_command
from reuleauxcoder.interfaces.cli.render import show_banner
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.ui_registry import UIProfile


def _auto_save_session(
    agent,
    config,
    sessions_dir: Path | None,
    current_session_id: str | None,
    ui_bus: UIEventBus,
    *,
    is_exit: bool = False,
) -> str | None:
    if not getattr(config, "session_auto_save", True):
        return current_session_id
    if not getattr(agent, "messages", None):
        return current_session_id

    sid = create_configured_session_store(config, sessions_dir).save(
        agent.messages,
        getattr(agent.llm, "model", ""),
        current_session_id,
        is_exit=is_exit,
        total_prompt_tokens=agent.state.total_prompt_tokens,
        total_completion_tokens=agent.state.total_completion_tokens,
        active_mode=getattr(agent, "active_mode", None),
        runtime_state=build_session_runtime_state(config, agent),
        fingerprint=get_session_fingerprint(config, agent),
    )
    setattr(agent, "current_session_id", sid)
    ui_bus.info(
        f"Session auto-saved: {sid}",
        kind=UIEventKind.SESSION,
        session_id=sid,
    )
    return sid


def run_repl(
    agent,
    config,
    ui_bus: UIEventBus,
    ui_profile: UIProfile,
    action_registry: ActionRegistry,
    current_session_id: str = None,
    sessions_dir: Path | None = None,
    session_exit_time: str | None = None,
    skills_service=None,
) -> None:
    ensure_user_dirs()
    show_banner(
        getattr(agent.llm, "model", ""),
        getattr(agent.llm, "base_url", None),
        __version__,
    )

    hist_path = (
        str(Path(config.history_file).expanduser())
        if getattr(config, "history_file", None)
        else None
    )
    history = (
        FileHistory(hist_path)
        if hist_path
        else FileHistory(str(Path.cwd() / ".rcoder" / "history"))
    )
    setattr(agent, "current_session_id", current_session_id)

    pending_resume_prefix: str | None = None
    if session_exit_time is not None:
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        pending_resume_prefix = (
            f"[SESSION_RESUME] User returned to the session at {current_time} "
            f"(last left at {session_exit_time}).\n\n"
        )

    while True:
        try:
            user_input = pt_prompt("You > ", history=history).strip()
        except (EOFError, KeyboardInterrupt):
            ui_bus.info("\nBye!")
            current_session_id = _auto_save_session(
                agent,
                config,
                sessions_dir,
                current_session_id,
                ui_bus,
                is_exit=True,
            )
            break

        if not user_input:
            continue

        result = handle_command(
            user_input,
            agent,
            config,
            current_session_id,
            ui_bus,
            ui_profile,
            action_registry,
            sessions_dir,
            skills_service,
        )
        prev_session_id = current_session_id
        current_session_id = result["session_id"]
        setattr(agent, "current_session_id", current_session_id)

        resumed_exit_time = result.get("session_exit_time")
        if resumed_exit_time is not None:
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            pending_resume_prefix = (
                f"[SESSION_RESUME] User returned to the session at {current_time} "
                f"(last left at {resumed_exit_time}).\n\n"
            )
        elif result["action"] == "continue" and current_session_id != prev_session_id:
            # Session switched/reset (e.g. /new, /session): stale resume marker should not leak.
            pending_resume_prefix = None

        if result["action"] == "exit":
            break
        if result["action"] == "continue":
            continue

        chat_input = user_input
        if pending_resume_prefix is not None:
            chat_input = pending_resume_prefix + chat_input
            pending_resume_prefix = None

        try:
            agent.chat(chat_input)
            current_session_id = _auto_save_session(
                agent,
                config,
                sessions_dir,
                current_session_id,
                ui_bus,
            )
        except KeyboardInterrupt:
            ui_bus.warning("Interrupted.")
            current_session_id = _auto_save_session(
                agent,
                config,
                sessions_dir,
                current_session_id,
                ui_bus,
            )
        except Exception as e:
            diagnostic_path = getattr(e, "llm_diagnostic_path", None)
            if diagnostic_path and current_session_id:
                create_configured_session_store(config, sessions_dir).append_system_message(
                    current_session_id,
                    getattr(agent.llm, "model", ""),
                    f"[LLM_ERROR_DIAGNOSTIC] path={diagnostic_path} error={type(e).__name__}: {e}",
                    active_mode=getattr(agent, "active_mode", None),
                )
            if diagnostic_path:
                ui_bus.error(
                    f"Error: {e}\nDiagnostic saved to: {diagnostic_path}",
                    kind=UIEventKind.SYSTEM,
                    diagnostic_path=diagnostic_path,
                )
            else:
                ui_bus.error(f"Error: {e}")
            current_session_id = _auto_save_session(
                agent,
                config,
                sessions_dir,
                current_session_id,
                ui_bus,
            )
