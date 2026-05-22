"""VS Code ChatView UI profile."""

from __future__ import annotations

from reuleauxcoder.interfaces.ui_registry import UICapability, UIProfile


VSCODE_CHAT_PROFILE = UIProfile(
    ui_id="vscode",
    display_name="VS Code ChatView",
    capabilities=frozenset(
        {
            UICapability.TEXT_INPUT,
            UICapability.STREAM_OUTPUT,
            UICapability.BUTTONS,
            UICapability.MENUS,
            UICapability.TABS,
            UICapability.MODAL,
            UICapability.TEXT_SELECT,
            UICapability.DIFF_REVIEW,
        }
    ),
)
