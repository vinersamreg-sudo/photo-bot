import json
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
            "message": {
                "recipient": {"chat_id": 77},
                "body": {"mid": "mid-2", "text": "Result card"},
            },
        })
        self.assertEqual(callback.event_key, "callback:cb-1")
        self.assertEqual(callback.callback_payload, "custom")
        self.assertEqual(callback.text, "Result card")

        started = parse_update({
            "update_type": "bot_started", "timestamp": 12,
            "chat_id": 77, "user": {"user_id": 42},
            "payload": "src_site",
        })
        self.assertEqual(started.event_key, "start:77:42:12")
        self.assertEqual(started.start_payload, "src_site")

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
                "42", "result",
                (
                    Button("OK", "ok"),
                    Button("Оплатить", "https://pay.example/order"),
                    Button("Копировать", "Текст Юникод", kind="clipboard"),
                ),
                image_token=token,
            )
            self.assertEqual(mid, "sent-1")
            message_request = next(
                request for request in api_requests
                if request.url.path == "/messages" and request.method == "POST"
            )
            keyboard = json.loads(message_request.content)["attachments"][-1]["payload"]["buttons"]
            self.assertEqual(keyboard[0][0]["type"], "callback")
            self.assertEqual(keyboard[1][0]["type"], "link")
            self.assertEqual(keyboard[1][0]["url"], "https://pay.example/order")
            self.assertEqual(keyboard[2][0]["type"], "clipboard")
            self.assertEqual(keyboard[2][0]["payload"], "Текст Юникод")
            downloaded = Path(directory) / "incoming.bin"
            client.download_image("https://iu.oneme.ru/input", downloaded, 1024)
            self.assertEqual(downloaded.read_bytes(), image_bytes)
        client.answer_callback("cb-1", "Принято")
        client.edit_message("sent-1", "changed")
        edit_request = next(
            request for request in api_requests
            if request.url.path == "/messages" and request.method == "PUT"
        )
        self.assertEqual(json.loads(edit_request.content)["attachments"], [])
        client.delete_message("sent-1")
        self.assertTrue(api_requests)
        self.assertTrue(media_requests)

    def test_edit_image_replaces_media_and_groups_number_buttons(self) -> None:
        requests = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/uploads":
                return httpx.Response(200, json={"url": "https://iu.oneme.ru/upload"})
            return httpx.Response(200, json={"success": True})

        client = MaxApiClient(
            "max-secret-test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
                headers={"Authorization": "max-secret-test"},
            ),
            media_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json={"token": "photo-token"})
                )
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "sheet.jpg"
            image.write_bytes(b"image")
            client.edit_image(
                "bot-message",
                image,
                "Страница",
                (
                    Button("1", "open:1", 0),
                    Button("2", "open:2", 0),
                    Button("Назад", "back", 1),
                ),
            )
        request = next(
            request for request in requests
            if request.url.path == "/messages" and request.method == "PUT"
        )
        body = json.loads(request.content)
        self.assertFalse(body["notify"])
        self.assertEqual(body["attachments"][0]["type"], "image")
        rows = body["attachments"][1]["payload"]["buttons"]
        self.assertEqual([button["text"] for button in rows[0]], ["1", "2"])
        self.assertEqual([button["text"] for button in rows[1]], ["Назад"])

    def test_each_image_edit_uploads_and_uses_the_new_attachment_token(self) -> None:
        requests = []
        token_number = 0

        def api_handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/uploads":
                return httpx.Response(200, json={"url": "https://iu.oneme.ru/upload"})
            return httpx.Response(200, json={"success": True})

        def media_handler(_request: httpx.Request) -> httpx.Response:
            nonlocal token_number
            token_number += 1
            return httpx.Response(200, json={"token": f"page-token-{token_number}"})

        client = MaxApiClient(
            "max-secret-test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
                headers={"Authorization": "max-secret-test"},
            ),
            media_client=httpx.Client(transport=httpx.MockTransport(media_handler)),
        )
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "page-1.jpg"
            second = Path(directory) / "page-2.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            client.edit_image("bot-message", first, "1/2", ())
            client.edit_image("bot-message", second, "2/2", ())

        edits = [
            json.loads(request.content)
            for request in requests
            if request.url.path == "/messages" and request.method == "PUT"
        ]
        self.assertEqual(len(edits), 2)
        self.assertEqual(
            [body["attachments"][0]["payload"]["token"] for body in edits],
            ["page-token-1", "page-token-2"],
        )

    def test_image_upload_accepts_the_documented_photo_token_map(self) -> None:
        sent_messages = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/uploads":
                return httpx.Response(200, json={"url": "https://iu.oneme.ru/upload"})
            if request.url.path == "/messages":
                sent_messages.append(request.read().decode("utf-8"))
                return httpx.Response(200, json={"message": {"body": {"mid": "sent-photo"}}})
            return httpx.Response(200, json={"success": True})

        media = httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={"photos": {"photo-id": {"token": "photo-token"}}},
                )
            )
        )
        client = MaxApiClient(
            "max-secret-test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
                headers={"Authorization": "max-secret-test"},
            ),
            media_client=media,
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "preview.jpg"
            source.write_bytes(b"image-bytes")
            self.assertTrue(
                client.send_image("42", source, "preview", (Button("Open", "open"),))
            )
        self.assertEqual(len(sent_messages), 1)
        sent = json.loads(sent_messages[0])
        self.assertEqual(sent["attachments"][0]["type"], "image")
        self.assertEqual(sent["attachments"][0]["payload"]["token"], "photo-token")

    def test_image_delivery_retries_attachment_not_ready_with_bounded_backoff(self) -> None:
        message_attempts = 0
        delays = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            nonlocal message_attempts
            if request.url.path == "/uploads":
                return httpx.Response(200, json={"url": "https://iu.oneme.ru/upload"})
            if request.url.path == "/messages":
                message_attempts += 1
                if message_attempts < 3:
                    return httpx.Response(
                        400, json={"code": "attachment.not.ready"}
                    )
                return httpx.Response(
                    200, json={"message": {"body": {"mid": "image-ready"}}}
                )
            return httpx.Response(200, json={"success": True})

        client = MaxApiClient(
            "max-secret-test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
                headers={"Authorization": "max-secret-test"},
            ),
            media_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200, json={"token": "image-token"}
                    )
                )
            ),
            sleeper=delays.append,
        )
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.png"
            preview.write_bytes(b"preview")
            self.assertEqual(
                client.send_image("42", preview, "Preview", ()),
                "image-ready",
            )
        self.assertEqual(message_attempts, 3)
        self.assertEqual(delays, [0.5, 1.0])

    def test_image_delivery_stops_after_three_not_ready_responses(self) -> None:
        message_attempts = 0
        delays = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            nonlocal message_attempts
            if request.url.path == "/uploads":
                return httpx.Response(200, json={"url": "https://iu.oneme.ru/upload"})
            if request.url.path == "/messages":
                message_attempts += 1
                return httpx.Response(
                    400, json={"code": "attachment.not.ready"}
                )
            return httpx.Response(200, json={"success": True})

        client = MaxApiClient(
            "max-secret-test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
                headers={"Authorization": "max-secret-test"},
            ),
            media_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200, json={"token": "image-token"}
                    )
                )
            ),
            sleeper=delays.append,
        )
        with tempfile.TemporaryDirectory() as directory:
            preview = Path(directory) / "preview.png"
            preview.write_bytes(b"preview")
            self.assertIsNone(client.send_image("42", preview, "Preview", ()))
        self.assertEqual(message_attempts, 3)
        self.assertEqual(delays, [0.5, 1.0])

    def test_paid_original_is_uploaded_and_sent_as_a_file(self) -> None:
        api_requests = []
        media_requests = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            api_requests.append(request)
            if request.url.path == "/uploads":
                self.assertEqual(request.url.params["type"], "file")
                return httpx.Response(200, json={"url": "https://iu.oneme.ru/upload"})
            if request.url.path == "/messages":
                return httpx.Response(
                    200, json={"message": {"body": {"mid": "sent-file"}}}
                )
            return httpx.Response(200, json={"success": True})

        def media_handler(request: httpx.Request) -> httpx.Response:
            media_requests.append(request)
            self.assertIn('filename="ravuna-original.png"', request.content.decode("latin-1"))
            self.assertIn("Content-Type: image/png", request.content.decode("latin-1"))
            return httpx.Response(200, json={"token": "file-token"})

        client = MaxApiClient(
            "max-secret-test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
                headers={"Authorization": "max-secret-test"},
            ),
            media_client=httpx.Client(transport=httpx.MockTransport(media_handler)),
            sleeper=lambda _seconds: None,
        )
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "private-uuid.png"
            original.write_bytes(b"original-png-bytes")
            self.assertEqual(
                client.send_file("42", original, "Original", ()),
                "sent-file",
            )
        message_request = next(
            request for request in api_requests
            if request.url.path == "/messages" and request.method == "POST"
        )
        attachment = json.loads(message_request.content)["attachments"][0]
        self.assertEqual(attachment["type"], "file")
        self.assertEqual(attachment["payload"]["token"], "file-token")
        self.assertEqual(len(media_requests), 1)

    def test_file_delivery_retries_documented_attachment_not_ready(self) -> None:
        api_requests = []
        delays = []
        message_attempts = 0

        def api_handler(request: httpx.Request) -> httpx.Response:
            nonlocal message_attempts
            api_requests.append(request)
            if request.url.path == "/uploads":
                return httpx.Response(
                    200, json={"url": "https://fu.oneme.ru/upload"}
                )
            if request.url.path == "/messages":
                message_attempts += 1
                if message_attempts == 1:
                    return httpx.Response(
                        400,
                        json={
                            "code": "attachment.not.ready",
                            "message": "Key: errors.process.attachment.file.not.processed",
                        },
                    )
                return httpx.Response(
                    200, json={"message": {"body": {"mid": "sent-after-retry"}}}
                )
            return httpx.Response(200, json={"success": True})

        client = MaxApiClient(
            "max-secret-test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
                headers={"Authorization": "max-secret-test"},
            ),
            media_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200, json={"token": "file-token"}
                    )
                )
            ),
            sleeper=delays.append,
        )
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.png"
            original.write_bytes(b"original")
            self.assertEqual(
                client.send_file("42", original, "Original", ()),
                "sent-after-retry",
            )
        self.assertEqual(message_attempts, 2)
        self.assertEqual(
            sum(request.url.path == "/uploads" for request in api_requests), 1
        )
        self.assertEqual(delays, [0.5, 0.5])

    def test_file_delivery_does_not_retry_permanent_http_400(self) -> None:
        message_attempts = 0
        delays = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            nonlocal message_attempts
            if request.url.path == "/uploads":
                return httpx.Response(
                    200, json={"url": "https://fu.oneme.ru/upload"}
                )
            if request.url.path == "/messages":
                message_attempts += 1
                return httpx.Response(
                    400,
                    json={"code": "invalid.request", "message": "invalid file"},
                )
            return httpx.Response(200, json={"success": True})

        client = MaxApiClient(
            "max-secret-test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
                headers={"Authorization": "max-secret-test"},
            ),
            media_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200, json={"token": "file-token"}
                    )
                )
            ),
            sleeper=delays.append,
        )
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.png"
            original.write_bytes(b"original")
            self.assertIsNone(client.send_file("42", original, "Original", ()))
        self.assertEqual(message_attempts, 1)
        self.assertEqual(delays, [0.5])

    def test_file_delivery_stops_after_three_bounded_attempts(self) -> None:
        message_attempts = 0
        delays = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            nonlocal message_attempts
            if request.url.path == "/uploads":
                return httpx.Response(
                    200, json={"url": "https://fu.oneme.ru/upload"}
                )
            if request.url.path == "/messages":
                message_attempts += 1
                return httpx.Response(
                    400, json={"code": "attachment.not.ready"}
                )
            return httpx.Response(200, json={"success": True})

        client = MaxApiClient(
            "max-secret-test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
                headers={"Authorization": "max-secret-test"},
            ),
            media_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200, json={"token": "file-token"}
                    )
                )
            ),
            sleeper=delays.append,
        )
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.png"
            original.write_bytes(b"original")
            self.assertIsNone(client.send_file("42", original, "Original", ()))
        self.assertEqual(message_attempts, 3)
        self.assertEqual(delays, [0.5, 0.5, 1.0])

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
        self.assertEqual(caught.exception.kind, "invalid_token")
        self.assertEqual(caught.exception.http_status, 401)
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

    def test_timeout_network_rate_limit_and_forbidden_are_classified(self) -> None:
        cases = (
            (httpx.ReadTimeout("slow"), "timeout"),
            (httpx.ConnectError("offline"), "network"),
        )
        for failure, kind in cases:
            client = MaxApiClient(
                "test",
                client=httpx.Client(
                    base_url="https://platform-api2.max.ru",
                    transport=httpx.MockTransport(lambda _request, exc=failure: (_ for _ in ()).throw(exc)),
                ),
            )
            with self.assertRaises(MaxTransportError) as caught:
                client.get_me()
            self.assertEqual(caught.exception.kind, kind)

    def test_missing_ca_bundle_and_inactive_bot_are_classified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MaxTransportError) as caught:
                MaxApiClient("test", ca_bundle=Path(directory) / "missing.crt")
            self.assertEqual(caught.exception.kind, "configuration_missing")
        client = MaxApiClient(
            "test",
            client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        403, json={"code": "bot_not_active"}
                    )
                ),
            ),
        )
        with self.assertRaises(MaxTransportError) as caught:
            client.get_me()
        self.assertEqual(caught.exception.kind, "bot_not_active")
        for status, kind in ((403, "forbidden"), (429, "rate_limit")):
            client = MaxApiClient(
                "test",
                client=httpx.Client(
                    base_url="https://platform-api2.max.ru",
                    transport=httpx.MockTransport(
                        lambda _request, code=status: httpx.Response(code, json={})
                    ),
                ),
            )
            with self.assertRaises(MaxTransportError) as caught:
                client.get_me()
            self.assertEqual(caught.exception.kind, kind)
