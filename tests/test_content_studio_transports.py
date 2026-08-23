from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image

from app.content_studio.config import ContentStudioSettings
from app.content_studio.transports import (
    ContentPublishingError,
    MaxContentApi,
    MaxChannelTransport,
    TelegramChannelTransport,
    VkCommunityTransport,
    build_publishers,
)


class FakeMaxClient:
    def __init__(self) -> None:
        self.calls = []

    def audit_permissions(self, channel_id):
        return {"channel_id": channel_id, "ready": True}

    def publish_media(self, channel_id, media, text, source):
        self.calls.append((channel_id, media, text, source))
        return "max-channel-message"

    def metrics(self, channel_id, external_id):
        return {"views": 0, "reactions": 0, "comments": 0}


class ContentStudioTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.media = Path(self.temp.name) / "post.png"
        Image.new("RGB", (64, 64), "purple").save(self.media)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, platform: str) -> dict[str, object]:
        return {
            "platform": platform,
            "post_id": "post-1",
            "text": "Демонстрационный пример Ravuna.",
            "media_path": str(self.media.resolve()),
            "utm_url": "https://max.ru/bot?utm_source=test",
            "source_code": "src_test-post-1",
            "demo_disclosure_present": True,
        }

    def test_max_transport_uses_channel_image_message(self) -> None:
        client = FakeMaxClient()
        transport = MaxChannelTransport(client, "channel-1", Path(self.temp.name))
        external_id = transport.publish(self.payload("max"))
        self.assertEqual(external_id, "max-channel-message")
        self.assertEqual(client.calls[0][0], "channel-1")
        self.assertEqual(client.calls[0][1], self.media.resolve())

    def test_telegram_transport_uploads_photo_with_caption(self) -> None:
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

        client = httpx.Client(
            base_url="https://api.telegram.org/bottest-token/",
            transport=httpx.MockTransport(handler),
        )
        transport = TelegramChannelTransport(
            "test-token", "@ravuna", Path(self.temp.name), client=client
        )
        self.assertEqual(transport.publish(self.payload("telegram")), "42")
        self.assertEqual(requests[0].url.path, "/bottest-token/sendPhoto")
        self.assertIn("multipart/form-data", requests[0].headers["content-type"])

    def test_vk_transport_uses_wall_upload_and_deterministic_wall_post(self) -> None:
        api_calls = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            api_calls.append(request)
            method = request.url.path.rsplit("/", 1)[-1]
            if method == "photos.getWallUploadServer":
                return httpx.Response(
                    200,
                    json={"response": {"upload_url": "https://upload.vk.com/image"}},
                )
            if method == "photos.saveWallPhoto":
                return httpx.Response(
                    200, json={"response": [{"owner_id": -123, "id": 9}]}
                )
            if method == "wall.post":
                return httpx.Response(200, json={"response": {"post_id": 77}})
            if method == "groups.getById":
                return httpx.Response(
                    200,
                    json={
                        "response": {
                            "groups": [
                                {"id": 123, "can_post": 1, "can_upload_video": 1}
                            ]
                        }
                    },
                )
            if method == "wall.get":
                return httpx.Response(200, json={"response": {"items": []}})
            return httpx.Response(404)

        api_client = httpx.Client(
            base_url="https://api.vk.com/method/",
            transport=httpx.MockTransport(api_handler),
        )
        upload_client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"server": 1, "photo": "[]", "hash": "hash"}
                )
            )
        )
        transport = VkCommunityTransport(
            "test-token",
            "123",
            Path(self.temp.name),
            api_client=api_client,
            upload_client=upload_client,
        )
        self.assertEqual(transport.publish(self.payload("vk")), "wall:77")
        self.assertEqual(
            [request.url.path.rsplit("/", 1)[-1] for request in api_calls],
            [
                "groups.getById",
                "wall.get",
                "photos.getWallUploadServer",
                "photos.saveWallPhoto",
                "wall.post",
            ],
        )
        first_wall_body = api_calls[-1].content
        second_transport = VkCommunityTransport(
            "test-token",
            "123",
            Path(self.temp.name),
            api_client=api_client,
            upload_client=upload_client,
        )
        second_transport.publish(self.payload("vk"))
        self.assertEqual(first_wall_body, api_calls[-1].content)

    def test_transports_reject_missing_disclosure_and_relative_media(self) -> None:
        transport = MaxChannelTransport(
            FakeMaxClient(), "channel-1", Path(self.temp.name)
        )
        payload = self.payload("max")
        payload["demo_disclosure_present"] = False
        with self.assertRaisesRegex(ContentPublishingError, "disclosure"):
            transport.publish(payload)
        payload = self.payload("max")
        payload["media_path"] = "relative.png"
        with self.assertRaisesRegex(ContentPublishingError, "unavailable"):
            transport.publish(payload)
        payload = self.payload("max")
        payload["text"] = "api_" + "key=" + "sk-" + "this-value-must-never-be-published"
        with self.assertRaisesRegex(ContentPublishingError, "secret gate"):
            transport.publish(payload)

    def test_max_video_uses_the_same_official_media_adapter(self) -> None:
        video = Path(self.temp.name) / "post.mp4"
        video.write_bytes(b"synthetic-placeholder")
        transport = MaxChannelTransport(
            FakeMaxClient(), "channel-1", Path(self.temp.name)
        )
        payload = self.payload("max")
        payload["media_path"] = str(video)
        self.assertEqual(transport.publish(payload), "max-channel-message")
        self.assertEqual(transport.client.calls[0][1], video.resolve())

    def test_max_api_retries_attachment_not_ready_with_a_bounded_backoff(self) -> None:
        message_attempts = 0

        def api_handler(request: httpx.Request) -> httpx.Response:
            nonlocal message_attempts
            if request.url.path == "/me":
                return httpx.Response(200, json={"user_id": 42})
            if request.url.path.endswith("/members/admins"):
                return httpx.Response(
                    200,
                    json={
                        "members": [
                            {
                                "user_id": 42,
                                "permissions": ["read_all_messages", "write"],
                            }
                        ]
                    },
                )
            if request.url.path == "/messages" and request.method == "GET":
                return httpx.Response(200, json={"messages": []})
            if request.url.path == "/uploads":
                return httpx.Response(
                    200, json={"url": "https://upload.max.ru/media"}
                )
            if request.url.path == "/messages" and request.method == "POST":
                message_attempts += 1
                if message_attempts < 3:
                    return httpx.Response(
                        400, json={"code": "attachment.not.ready"}
                    )
                return httpx.Response(
                    200, json={"message": {"body": {"mid": "posted-42"}}}
                )
            return httpx.Response(404, json={})

        delays = []
        api = MaxContentApi(
            "test-token",
            "https://platform-api2.max.ru",
            api_client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(api_handler),
            ),
            upload_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json={"photos": {"photo.png": {"token": "media-token"}}},
                    )
                )
            ),
            sleeper=delays.append,
        )
        self.assertEqual(
            api.publish_media("channel-1", self.media, "Demo", "src_max-test"),
            "posted-42",
        )
        self.assertEqual(message_attempts, 3)
        self.assertEqual(delays, [2, 4])

    def test_max_api_reuses_an_existing_attributed_post_without_upload(self) -> None:
        paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append((request.method, request.url.path))
            if request.url.path == "/me":
                return httpx.Response(200, json={"user_id": 42})
            if request.url.path.endswith("/members/admins"):
                return httpx.Response(
                    200,
                    json={
                        "members": [
                            {
                                "user_id": 42,
                                "permissions": ["read_all_messages", "write"],
                            }
                        ]
                    },
                )
            if request.url.path == "/messages":
                return httpx.Response(
                    200,
                    json={
                        "messages": [
                            {"body": {"mid": "existing-1", "text": "src_max-test"}}
                        ]
                    },
                )
            return httpx.Response(500, json={})

        api = MaxContentApi(
            "test-token",
            "https://platform-api2.max.ru",
            api_client=httpx.Client(
                base_url="https://platform-api2.max.ru",
                transport=httpx.MockTransport(handler),
            ),
        )
        self.assertEqual(
            api.publish_media("channel-1", self.media, "Demo", "src_max-test"),
            "existing-1",
        )
        self.assertNotIn(("POST", "/uploads"), paths)

    def test_vk_vertical_video_uses_official_save_upload_and_wall_post(self) -> None:
        methods = []

        def api_handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            methods.append(method)
            if method == "groups.getById":
                return httpx.Response(
                    200,
                    json={
                        "response": {
                            "groups": [
                                {"id": 123, "can_post": 1, "can_upload_video": 1}
                            ]
                        }
                    },
                )
            if method == "wall.get":
                return httpx.Response(200, json={"response": {"items": []}})
            if method == "video.save":
                return httpx.Response(
                    200,
                    json={
                        "response": {
                            "upload_url": "https://upload.vk.com/video",
                            "owner_id": -123,
                            "video_id": 9,
                        }
                    },
                )
            if method == "wall.post":
                return httpx.Response(200, json={"response": {"post_id": 77}})
            return httpx.Response(404)

        video = Path(self.temp.name) / "vertical.mp4"
        video.write_bytes(b"synthetic-test-video")
        transport = VkCommunityTransport(
            "test-token",
            "123",
            Path(self.temp.name),
            api_client=httpx.Client(
                base_url="https://api.vk.com/method/",
                transport=httpx.MockTransport(api_handler),
            ),
            upload_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json={"owner_id": -123, "video_id": 9}
                    )
                )
            ),
        )
        payload = self.payload("vk")
        payload["media_path"] = str(video)
        self.assertEqual(transport.publish(payload), "wall:77;video:9")
        self.assertEqual(methods, ["groups.getById", "wall.get", "video.save", "wall.post"])

    def test_settings_are_fail_closed_and_do_not_repr_tokens(self) -> None:
        root = Path(self.temp.name)
        settings = ContentStudioSettings.from_environment(
            root,
            {
                "CONTENT_STUDIO_TELEGRAM_BOT_TOKEN": "telegram-secret",
                "CONTENT_STUDIO_VK_ACCESS_TOKEN": "vk-secret",
            },
        )
        publishers = build_publishers(settings)
        self.assertEqual(sorted(publishers), ["max", "telegram", "vk"])
        self.assertFalse(settings.publishing_enabled)
        representation = repr(settings)
        self.assertNotIn("telegram-secret", representation)
        self.assertNotIn("vk-secret", representation)


if __name__ == "__main__":
    unittest.main()
