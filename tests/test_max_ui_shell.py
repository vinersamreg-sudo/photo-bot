import tempfile
from pathlib import Path
from unittest import TestCase

from PIL import Image

from app.database import Database
from app.max_adapter import Button
from app.max_conversation import MaxConversationStore
from app.max_transport import MaxTransportError
from app.max_ui_shell import MaxUiShell, parse_versioned_action


class FakeUiTransport:
    def __init__(self) -> None:
        self.sends = []
        self.edits = []
        self.image_sends = []
        self.image_edits = []
        self.deletes = []
        self.fail_edit = False

    def send_message(self, user_id, text, buttons=(), **kwargs):
        message_id = f"bot-{len(self.sends) + len(self.image_sends) + 1}"
        self.sends.append((user_id, text, tuple(buttons), kwargs, message_id))
        return message_id

    def edit_message(self, message_id, text, buttons=(), **kwargs):
        if self.fail_edit:
            self.fail_edit = False
            raise MaxTransportError("edit failed")
        self.edits.append((message_id, text, tuple(buttons), kwargs))

    def send_image(self, user_id, image, caption, buttons, **kwargs):
        message_id = f"bot-{len(self.sends) + len(self.image_sends) + 1}"
        self.image_sends.append(
            (user_id, Path(image), caption, tuple(buttons), kwargs, message_id)
        )
        return message_id

    def edit_image(self, message_id, image, caption, buttons, **kwargs):
        if self.fail_edit:
            self.fail_edit = False
            raise MaxTransportError("edit failed")
        self.image_edits.append(
            (message_id, Path(image), caption, tuple(buttons), kwargs)
        )

    def delete_message(self, message_id):
        self.deletes.append(message_id)


class MaxUiShellTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.database = Database(base / "ui.sqlite3")
        self.store = MaxConversationStore(self.database)
        self.transport = FakeUiTransport()
        self.shell = MaxUiShell(self.database, self.transport, self.store)
        self.image = base / "preview.jpg"
        Image.new("RGB", (64, 64), "#dddddd").save(self.image)

    def test_transition_edits_one_active_message_and_versions_callbacks(self) -> None:
        first = self.shell.render(
            "u1",
            text="Главное меню",
            buttons=(Button("Открыть", "works"),),
            screen="main",
        )
        second = self.shell.render(
            "u1",
            text="Мои работы",
            buttons=(Button("Назад", "menu"),),
            screen="works",
            image=self.image,
        )

        self.assertEqual(len(self.transport.sends), 1)
        self.assertEqual(len(self.transport.image_sends), 0)
        self.assertEqual(len(self.transport.image_edits), 1)
        self.assertEqual(first.message_id, second.message_id)
        revision, action = parse_versioned_action(
            self.transport.image_edits[0][3][0].action
        )
        self.assertEqual((revision, action), (2, "menu"))
        self.assertTrue(
            self.shell.callback_is_current("u1", second.message_id, second.revision)
        )

    def test_failed_edit_sends_once_then_deletes_only_old_bot_message(self) -> None:
        first = self.shell.render("u1", text="Первый", screen="main")
        self.transport.fail_edit = True
        second = self.shell.render("u1", text="Второй", screen="nested")

        self.assertEqual(len(self.transport.sends), 2)
        self.assertEqual(self.transport.deletes, [first.message_id])
        self.assertNotEqual(first.message_id, second.message_id)

    def test_stale_async_revision_does_not_overwrite_new_screen(self) -> None:
        processing = self.shell.render("u1", text="Обрабатываю", screen="processing")
        newer = self.shell.render("u1", text="Главное меню", screen="main")
        calls_before = len(self.transport.image_edits) + len(self.transport.edits)

        stale = self.shell.render(
            "u1",
            text="Готово",
            screen="result",
            image=self.image,
            expected_revision=processing.revision,
        )

        self.assertFalse(stale.applied)
        self.assertEqual(stale.revision, newer.revision)
        self.assertEqual(
            len(self.transport.image_edits) + len(self.transport.edits), calls_before
        )
