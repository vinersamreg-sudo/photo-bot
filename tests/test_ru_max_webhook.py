import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx

from app.database import Database
from app.max_transport import MaxApiClient
from app.max_webhook import PATH, MaxInbox, MaxWebhookServer


class RuMaxWebhookTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = Database(self.root / "main.sqlite3")
        self.inbox = MaxInbox(self.database)
        self.secret = "sample-webhook-value".ljust(40, "x")
        server = MaxWebhookServer(self.inbox, 0, self.secret)
        server.start()
        self.addCleanup(server.stop)
        self.url = f"http://127.0.0.1:{server.server.server_port}{PATH}"
        self.event = {
            "update_type": "message_created", "timestamp": 10,
            "message": {"sender": {"user_id": 123, "ignored": "not needed"},
                        "body": {"mid": "fixture", "text": "Пример",
                                 "attachments": [{"type": "image", "payload": {
                                     "url": "https://iu.oneme.ru/synthetic"}}]}},
        }

    def submit(self, secret=None):
        return httpx.post(self.url, json=self.event,
                          headers={"X-Max-Bot-Api-Secret": secret or self.secret}).status_code

    def test_auth_bounds_and_failed_commit_allow_retry(self):
        self.assertEqual(self.submit("wrong"), 403)
        self.assertEqual(httpx.post(self.url, content=b"x" * 131073,
                                    headers={"X-Max-Bot-Api-Secret": self.secret}).status_code, 413)
        with patch.object(self.inbox, "accept", side_effect=sqlite3.OperationalError()):
            self.assertEqual(self.submit(), 503)
        self.assertEqual(self.submit(), 200)
        with self.database.read() as conn:
            data = json.loads(conn.execute("SELECT payload FROM max_webhook_inbox").fetchone()[0])
            self.assertEqual(data["text"], "Пример")
            self.assertNotIn("ignored", data)

    def test_duplicate_after_restart_executes_once(self):
        self.assertEqual(self.submit(), 200)
        self.assertEqual(self.submit(), 200)
        restarted = MaxInbox(self.database)
        app = Mock()
        self.assertTrue(restarted.handle_next(app))
        self.assertEqual(self.submit(), 200)
        self.assertFalse(restarted.handle_next(app))
        app.handle.assert_called_once()

    def test_ambiguous_claim_does_not_redo_external_effect(self):
        self.assertEqual(self.submit(), 200)
        self.assertIsNotNone(self.inbox.take())
        restarted = MaxInbox(self.database)
        restarted.recover()
        self.assertEqual(self.submit(), 200)
        self.assertFalse(restarted.handle_next(Mock()))

    def test_webhook_payload_photo_download_and_result_delivery(self):
        from io import BytesIO
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (32, 32), "green").save(buffer, "PNG")
        photo = buffer.getvalue()
        observed = []

        def api(request):
            observed.append(request.url.path)
            if request.url.path == "/uploads":
                return httpx.Response(200, json={"url": "https://iu.oneme.ru/upload"})
            return httpx.Response(200, json={"message": {"body": {"mid": "fixture-result"}}})

        def media(request):
            observed.append(request.url.path)
            self.assertNotIn("Authorization", request.headers)
            if request.url.path == "/upload":
                self.assertIn(photo, request.content)
                return httpx.Response(200, json={"photos": {"fixture": {"token": "fixture-image"}}})
            return httpx.Response(200, headers={"Content-Type": "image/png"}, content=photo)

        with httpx.Client(base_url="https://platform-api2.max.ru",
                          transport=httpx.MockTransport(api)) as api_client:
            with httpx.Client(transport=httpx.MockTransport(media)) as media_client:
                client = MaxApiClient("fixture", client=api_client, media_client=media_client)

                def handle(event):
                    file = client.download_image(event.image_urls[0], self.root / "source.png", 8192)
                    self.assertEqual(file.read_bytes(), photo)
                    self.assertEqual(client.send_image(event.user_id, file, "Готово", ()), "fixture-result")

                self.assertEqual(self.submit(), 200)
                self.assertTrue(self.inbox.handle_next(Mock(handle=handle)))
                self.assertEqual(observed, ["/synthetic", "/uploads", "/upload", "/messages"])
