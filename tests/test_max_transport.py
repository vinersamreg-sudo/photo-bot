import tempfile
from pathlib import Path
from unittest import TestCase

import httpx

from app.max_adapter import Button
from app.max_transport import (
    MaxApiClient,
    MaxTransportError,
    SingleInstanceLock,
    parse_update,
)


class MaxTransportTests(TestCase):
    def test_parses_message_image_callback_and_start_ids(self) -> None:
        message = parse_update({
            "update_type": "message_created",
            "timestamp": 10,
            "message": {
                "sender": {"user_id": 42},
                "recipient": {"chat_id": 77},
                "body": {
                    "mid": "mid-1",
                    "text": "hello",
                    "attachments": [{
                        "type": "image",
                        "payload": {"url": "https://iu.oneme.ru/image"},
                    }],
                },
            },
        })
        self.assertEqual(message.event_key, "message:mid-1")
        self.assertEqual(message.user_id, "42")
        self.assertEqual(message.chat_id, "77")
        self.assertEqual(message.image_url, "https://iu.oneme.ru/image")

        callback = parse_update({
            "update_type": "message_callback",
            "timestamp": 11,
            "callback": {"callback_id": "cb-1", "payload": "custom", "user": {"user_id": 42}},
            "message": {"recipient": {"chat_id": 77}, "body": {"mid": "mid-2"}},
        })
        self.assertEqual(callback.event_key, "callback:cb-1")
        self.assertEqual(callback.callback_payload, "custom")

        started = parse_update({
            "update_type": "bot_started", "timestamp": 12,
            "chat_id": 77, "user": {"user_id": 42},
        })
        self.assertEqual(started.event_key, "start:77:42:12")

    def test_official_api_headers_updates_messages_and_media(self) -> None:
        api_requests = []
        media_requests = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            api_requests.append(request)
            self.assertEqual(request.headers["Authorization"], "max-secret-test")
            if request.url.path == "/updates":
                return httpx.Response(200, json={"updates": [], "marker": 9})
            if request.url.path == "/uploads":
                return httpx.Response(200, json={"url": "https://iu.oneme.ru/upload"})
            if request.url.path == "/messages":
                return httpx.Response(200, json={"message": {"body": {"mid": "sent-1"}}})
            if request.url.path == "/answers":
                return httpx.Response(200, json={"success": True})
            return httpx.Response(200, json={"success": True})

        image_bytes = b"image-bytes"

        def media_handler(request: httpx.Request) -> httpx.Response:
            media_requests.append(request)
            self.assertNotIn("Authorization", request.headers)
            if request.method == "GET":
                return httpx.Response(200, content=image_bytes)
            return httpx.Response(200, json={"token": "media-token"})

        api = httpx.Client(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(api_handler),
            headers={"Authorization": "max-secret-test"},
        )
        media = httpx.Client(transport=httpx.MockTransport(media_handler))
        client = MaxApiClient("max-secret-test", client=api, media_client=media)
        updates, marker = client.get_updates(None, timeout=30)
        self.assertEqual(updates, [])
        self.assertEqual(marker, 9)
        updates_request = next(
            request for request in api_requests if request.url.path == "/updates"
        )
        self.assertEqual(
            updates_request.url.params["types"],
            "bot_started,message_created,message_callback",
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "preview.jpg"
            source.write_bytes(image_bytes)
            token = client.upload_image(source)
            self.assertEqual(token, "media-token")
            mid = client.send_message(
                "42", "result", (Button("OK", "ok"),), image_token=token
            )
            self.assertEqual(mid, "sent-1")
            downloaded = Path(directory) / "incoming.bin"
            client.download_image("https://iu.oneme.ru/input", downloaded, 1024)
            self.assertEqual(downloaded.read_bytes(), image_bytes)
        client.answer_callback("cb-1", "Принято")
        client.edit_message("sent-1", "changed")
        client.delete_message("sent-1")
        self.assertTrue(api_requests)
        self.assertTrue(media_requests)

    def test_rejects_unknown_media_host_and_redacts_api_error(self) -> None:
        api = httpx.Client(
            base_url="https://platform-api2.max.ru",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(401, text="max-secret-test body")
            ),
            headers={"Authorization": "max-secret-test"},
        )
        client = MaxApiClient("max-secret-test", client=api, media_client=api)
        with self.assertRaises(MaxTransportError) as caught:
            client.get_me()
        self.assertNotIn("max-secret-test", str(caught.exception))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MaxTransportError):
                client.download_image(
                    "https://example.com/private", Path(directory) / "x", 100
                )

    def test_polling_single_instance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "max.lock"
            with SingleInstanceLock(path):
                with self.assertRaises(MaxTransportError):
                    with SingleInstanceLock(path):
                        pass
