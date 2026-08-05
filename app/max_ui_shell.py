"""Durable single-message UI shell for MAX conversations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

from app.database import Database
from app.max_adapter import Button
from app.max_conversation import MaxConversationStore
from app.max_transport import MaxTransportError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def versioned_action(action: str, revision: int) -> str:
    if action.startswith("https://"):
        return action
    return f"ui:{revision}:{action}"


def parse_versioned_action(payload: str) -> tuple[Optional[int], str]:
    if not payload.startswith("ui:"):
        return None, payload
    parts = payload.split(":", 2)
    if len(parts) != 3:
        return -1, payload
    _prefix, revision, action = parts
    try:
        return int(revision), action
    except ValueError:
        return -1, action


@dataclass(frozen=True)
class ActiveUiSession:
    platform_user_id: str
    chat_id: Optional[str]
    message_id: Optional[str]
    screen: str
    context: dict[str, Any]
    revision: int


@dataclass(frozen=True)
class RenderResult:
    applied: bool
    message_id: Optional[str]
    revision: int
    used_fallback: bool = False


class UiTransport(Protocol):
    def send_message(
        self, user_id: str, text: str, buttons: Sequence[Button] = (), **kwargs: Any
    ) -> str: ...

    def edit_message(
        self,
        message_id: str,
        text: str,
        buttons: Sequence[Button] = (),
        *,
        notify: bool = False,
    ) -> None: ...

    def send_image(
        self,
        user_id: str,
        image: Path,
        caption: str,
        buttons: Sequence[Button],
        *,
        notify: bool = True,
    ) -> Optional[str]: ...

    def edit_image(
        self,
        message_id: str,
        image: Path,
        caption: str,
        buttons: Sequence[Button],
        *,
        notify: bool = False,
    ) -> None: ...

    def delete_message(self, message_id: str) -> None: ...


class MaxUiShell:
    """Render each logical screen into one bot-owned MAX message."""

    def __init__(
        self,
        database: Database,
        transport: UiTransport,
        keyboards: MaxConversationStore,
    ) -> None:
        self.database = database
        self.transport = transport
        self.keyboards = keyboards

    @staticmethod
    def _session(row: Any) -> ActiveUiSession:
        try:
            context = json.loads(row["active_screen_context"] or "{}")
        except (TypeError, ValueError):
            context = {}
        if not isinstance(context, dict):
            context = {}
        return ActiveUiSession(
            row["platform_user_id"],
            row["chat_id"],
            row["active_ui_message_id"],
            row["active_screen"],
            context,
            int(row["ui_revision"]),
        )

    def current(self, platform_user_id: str) -> Optional[ActiveUiSession]:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM active_ui_sessions WHERE platform_user_id=?",
                (platform_user_id,),
            ).fetchone()
        return self._session(row) if row else None

    def callback_is_current(
        self, platform_user_id: str, message_id: Optional[str], revision: Optional[int]
    ) -> bool:
        current = self.current(platform_user_id)
        if current is None or not message_id or current.message_id != message_id:
            return False
        return revision is None or revision == current.revision

    def update_context(
        self,
        platform_user_id: str,
        context: dict[str, Any],
        *,
        expected_revision: Optional[int] = None,
    ) -> bool:
        encoded = json.dumps(
            context, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self.database.transaction() as connection:
            if expected_revision is None:
                updated = connection.execute(
                    """UPDATE active_ui_sessions SET active_screen_context=?,updated_at=?
                       WHERE platform_user_id=?""",
                    (encoded, _now(), platform_user_id),
                ).rowcount
            else:
                updated = connection.execute(
                    """UPDATE active_ui_sessions SET active_screen_context=?,updated_at=?
                       WHERE platform_user_id=? AND ui_revision=?""",
                    (encoded, _now(), platform_user_id, expected_revision),
                ).rowcount
        return bool(updated)

    def render(
        self,
        platform_user_id: str,
        *,
        text: str,
        buttons: Sequence[Button] = (),
        screen: str,
        context: Optional[dict[str, Any]] = None,
        chat_id: Optional[str] = None,
        image: Optional[Path] = None,
        expected_revision: Optional[int] = None,
        notify: bool = False,
    ) -> RenderResult:
        now = _now()
        encoded_context = json.dumps(
            context or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO active_ui_sessions(
                       platform_user_id,chat_id,active_screen,active_screen_context,
                       ui_revision,created_at,updated_at
                   ) VALUES(?,?,'none','{}',0,?,?)""",
                (platform_user_id, chat_id, now, now),
            )
            row = connection.execute(
                "SELECT * FROM active_ui_sessions WHERE platform_user_id=?",
                (platform_user_id,),
            ).fetchone()
            current_revision = int(row["ui_revision"])
            if expected_revision is not None and current_revision != expected_revision:
                return RenderResult(False, row["active_ui_message_id"], current_revision)
            revision = current_revision + 1
            connection.execute(
                """UPDATE active_ui_sessions
                   SET chat_id=COALESCE(?,chat_id),active_screen=?,
                       active_screen_context=?,ui_revision=?,updated_at=?
                   WHERE platform_user_id=?""",
                (
                    chat_id,
                    screen,
                    encoded_context,
                    revision,
                    now,
                    platform_user_id,
                ),
            )
            previous_message_id = row["active_ui_message_id"]

        rendered_buttons = tuple(
            Button(button.text, versioned_action(button.action, revision), button.row)
            for button in buttons
        )
        message_id = previous_message_id
        used_fallback = False
        if previous_message_id:
            try:
                if image is None:
                    self.transport.edit_message(
                        previous_message_id,
                        text,
                        rendered_buttons,
                        notify=notify,
                    )
                else:
                    self.transport.edit_image(
                        previous_message_id,
                        image,
                        text,
                        rendered_buttons,
                        notify=notify,
                    )
            except MaxTransportError:
                used_fallback = True
                message_id = self._send(
                    platform_user_id,
                    text,
                    rendered_buttons,
                    image=image,
                    notify=notify,
                )
                try:
                    self.transport.delete_message(previous_message_id)
                except MaxTransportError:
                    pass
                self.keyboards.clear_keyboard(platform_user_id, previous_message_id)
        else:
            message_id = self._send(
                platform_user_id,
                text,
                rendered_buttons,
                image=image,
                notify=notify,
            )

        with self.database.transaction() as connection:
            updated = connection.execute(
                """UPDATE active_ui_sessions SET active_ui_message_id=?,updated_at=?
                   WHERE platform_user_id=? AND ui_revision=?""",
                (message_id, _now(), platform_user_id, revision),
            ).rowcount
        if not updated:
            if message_id and message_id != previous_message_id:
                try:
                    self.transport.delete_message(message_id)
                except MaxTransportError:
                    pass
            current = self.current(platform_user_id)
            return RenderResult(
                False,
                current.message_id if current else None,
                current.revision if current else revision,
                used_fallback,
            )
        if rendered_buttons and message_id:
            self.keyboards.register_keyboard(platform_user_id, message_id, text)
        elif message_id:
            self.keyboards.clear_keyboard(platform_user_id, message_id)
        return RenderResult(True, message_id, revision, used_fallback)

    def _send(
        self,
        platform_user_id: str,
        text: str,
        buttons: Sequence[Button],
        *,
        image: Optional[Path],
        notify: bool,
    ) -> str:
        if image is None:
            return self.transport.send_message(
                platform_user_id, text, buttons, notify=notify
            )
        message_id = self.transport.send_image(
            platform_user_id, image, text, buttons, notify=notify
        )
        if not message_id:
            raise MaxTransportError("MAX image screen delivery failed")
        return message_id
