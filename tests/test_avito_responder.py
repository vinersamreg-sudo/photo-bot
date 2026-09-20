from __future__ import annotations

import http.client
import io
import json
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import httpx

from app.avito_responder.api import AvitoApiError
from app.avito_responder.api import AvitoApiClient
from app.avito_responder.config import AvitoResponderSettings
from app.avito_responder.models import ChatMessage, IncomingEvent, ReplyContext
from app.avito_responder.repository import AvitoRepository
from app.avito_responder.responder import AvitoReplyGenerator, GeneratedReply
from app.avito_responder.service import AvitoResponderService
from app.avito_responder.webhook import AvitoWebhookServer
from app.avito_responder.cli import main as avito_cli_main


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
WEBHOOK_SECRET = "a" * 43


def payload(
    event_id: str = "event-1",
    message_id: str = "message-1",
    *,
    chat_id: str = "chat-1",
    author_id: int = 20,
    user_id: int = 10,
    item_id: int = 77,
    direction: str = "in",
    message_type: str = "text",
    created: int = 1_790_000_000,
) -> dict:
    return {
        "id": event_id,
        "timestamp": created,
        "payload": {
            "type": "message",
            "value": {
                "id": message_id,
                "chat_id": chat_id,
                "author_id": author_id,
                "user_id": user_id,
                "created": created,
                "direction": direction,
                "type": message_type,
                "context": {"type": "item", "value": {"id": item_id}},
            },
        },
    }


class FakeApi:
    def __init__(self, messages: tuple[ChatMessage, ...]) -> None:
        self.messages = messages
        self.sent: list[tuple[int, str, str]] = []
        self.send_calls = 0
        self.item_id = 77
        self.send_error: AvitoApiError | None = None

    def get_chat(self, user_id: int, chat_id: str) -> dict:
        return {"context": {"type": "item", "value": {"id": self.item_id}}}

    def get_messages(self, user_id: int, chat_id: str, *, limit: int):
        return self.messages

    def send_message(self, user_id: int, chat_id: str, text: str) -> str:
        self.send_calls += 1
        if self.send_error:
            raise self.send_error
        self.sent.append((user_id, chat_id, text))
        return "out-1"


class FakeGenerator:
    model = "gpt-5.4-mini"

    def __init__(self, text: str = "Здравствуйте! Фото получили, задачу можно рассмотреть.") -> None:
        self.text = text
        self.contexts: list[ReplyContext] = []

    def generate(self, context: ReplyContext) -> GeneratedReply:
        self.contexts.append(context)
        return GeneratedReply(self.text, "response-1")


class AvitoResponderTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = AvitoResponderSettings.from_environment(
            self.root,
            {
                "AVITO_AUTO_REPLY_ENABLED": "true",
                "AVITO_CLIENT_ID": "client",
                "AVITO_CLIENT_SECRET": "secret",
                "AVITO_ACCOUNT_USER_ID": "10",
                "AVITO_ALLOWED_ITEM_IDS": "77",
                "AVITO_WEBHOOK_SECRET": WEBHOOK_SECRET,
                "OPENAI_API_KEY": "openai",
            },
        )
        self.repository = AvitoRepository(self.settings.database_path, debounce_seconds=8)
        self.repository.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_configuration_is_disabled_by_default_and_debounce_is_fixed(self) -> None:
        settings = AvitoResponderSettings.from_environment(self.root, {})
        self.assertFalse(settings.auto_reply_enabled)
        self.assertEqual(settings.debounce_seconds, 8)
        self.assertEqual(settings.reply_model, "gpt-5.4-mini")
        with self.assertRaises(ValueError):
            settings.require_runtime_webhook()
        with self.assertRaises(ValueError):
            AvitoResponderSettings.from_environment(
                self.root, {"AVITO_AUTO_REPLY_ENABLED": "true"}
            )

    def test_each_new_message_resets_eight_second_debounce(self) -> None:
        first = IncomingEvent.from_webhook(payload())
        second = IncomingEvent.from_webhook(payload("event-2", "message-2"))
        self.repository.record_event(first, eligible=True, now=NOW)
        self.repository.record_event(second, eligible=True, now=NOW + timedelta(seconds=6))
        self.assertIsNone(self.repository.claim_due(now=NOW + timedelta(seconds=9)))
        claim = self.repository.claim_due(now=NOW + timedelta(seconds=15))
        self.assertIsNotNone(claim)
        self.assertEqual(claim.revision, 2)
        self.assertEqual(claim.last_message_id, "message-2")

    def test_two_workers_can_claim_one_chat_only_once(self) -> None:
        event = IncomingEvent.from_webhook(payload())
        self.repository.record_event(event, eligible=True, now=NOW)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(
                pool.map(
                    lambda _value: self.repository.claim_due(
                        now=NOW + timedelta(seconds=9)
                    ),
                    range(2),
                )
            )
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_new_webhook_does_not_clear_active_processing_lease(self) -> None:
        self.repository.record_event(
            IncomingEvent.from_webhook(payload()), eligible=True, now=NOW
        )
        claim = self.repository.claim_due(now=NOW + timedelta(seconds=9))
        self.assertIsNotNone(claim)
        inserted, scheduled = self.repository.record_event(
            IncomingEvent.from_webhook(payload("event-2", "message-2")),
            eligible=True,
            now=NOW + timedelta(seconds=10),
        )
        self.assertTrue(inserted)
        self.assertTrue(scheduled)
        self.assertEqual(self.repository.status(), {"processing": 1})
        self.assertIsNone(self.repository.claim_due(now=NOW + timedelta(seconds=20)))
        self.assertFalse(self.repository.revision_is_current(claim))

    def test_expired_processing_lease_is_reclaimed_after_restart(self) -> None:
        repository = AvitoRepository(
            self.settings.database_path, debounce_seconds=8, lease_seconds=5
        )
        repository.record_event(
            IncomingEvent.from_webhook(payload()), eligible=True, now=NOW
        )
        first = repository.claim_due(now=NOW + timedelta(seconds=9))
        self.assertIsNotNone(first)
        restarted = AvitoRepository(
            self.settings.database_path, debounce_seconds=8, lease_seconds=5
        )
        second = restarted.claim_due(now=NOW + timedelta(seconds=15))
        self.assertIsNotNone(second)
        self.assertNotEqual(first.lock_token, second.lock_token)

    def test_diagnostic_cleanup_is_bounded_and_preserves_pending(self) -> None:
        old = NOW - timedelta(days=31)
        completed = IncomingEvent.from_webhook(payload())
        pending = IncomingEvent.from_webhook(payload("event-2", "message-2", chat_id="chat-2"))
        self.repository.record_event(completed, eligible=False, now=old)
        self.repository.record_event(pending, eligible=True, now=old)
        result = self.repository.cleanup(NOW - timedelta(days=30))
        self.assertEqual(result["events"], 1)
        self.assertIsNotNone(self.repository.claim_due(now=NOW))

    def test_two_messages_and_image_are_aggregated_into_one_reply(self) -> None:
        messages = (
            ChatMessage("message-1", 20, "in", "image", "", 1, 1),
            ChatMessage("message-2", 20, "in", "text", "Сделайте фон светлее", 0, 2),
        )
        api = FakeApi(messages)
        generator = FakeGenerator()
        service = AvitoResponderService(self.settings, self.repository, api, generator)
        service.accept_webhook(payload())
        service.accept_webhook(payload("event-2", "message-2", created=1_790_000_001))
        result = service.process_one_due(now=datetime.now(timezone.utc) + timedelta(seconds=9))
        self.assertEqual(result, "sent")
        self.assertEqual(len(api.sent), 1)
        self.assertEqual(generator.contexts[0].image_count, 1)
        self.assertEqual(generator.contexts[0].customer_text, "Сделайте фон светлее")
        self.assertEqual(service.process_one_due(now=datetime.now(timezone.utc) + timedelta(seconds=20)), "idle")

    def test_own_outgoing_system_and_other_item_never_schedule(self) -> None:
        api = FakeApi(())
        service = AvitoResponderService(self.settings, self.repository, api, FakeGenerator())
        values = (
            payload(author_id=10, direction="out"),
            payload("event-2", "message-2", message_type="system"),
            payload("event-3", "message-3", item_id=88),
        )
        for value in values:
            self.assertFalse(service.accept_webhook(value)["scheduled"])
        self.assertEqual(self.repository.status(), {})

    def test_event_for_different_account_never_schedules(self) -> None:
        service = AvitoResponderService(
            self.settings, self.repository, FakeApi(()), FakeGenerator()
        )
        self.assertFalse(service.accept_webhook(payload(user_id=999))["scheduled"])

    def test_existing_seller_reply_prevents_automatic_reply(self) -> None:
        messages = (
            ChatMessage("old-out", 10, "out", "text", "Здравствуйте", 0, 1),
            ChatMessage("message-1", 20, "in", "text", "Нужна обработка", 0, 2),
        )
        api = FakeApi(messages)
        service = AvitoResponderService(self.settings, self.repository, api, FakeGenerator())
        service.accept_webhook(payload())
        result = service.process_one_due(now=datetime.now(timezone.utc) + timedelta(seconds=9))
        self.assertEqual(result, "existing_outbound")
        self.assertEqual(api.sent, [])
        service.accept_webhook(payload("event-2", "message-2", created=1_790_000_100))
        self.assertEqual(
            service.process_one_due(now=datetime.now(timezone.utc) + timedelta(seconds=30)),
            "idle",
        )

    def test_duplicate_webhook_and_replay_send_once(self) -> None:
        messages = (ChatMessage("message-1", 20, "in", "text", "Ретушь", 0, 1),)
        api = FakeApi(messages)
        service = AvitoResponderService(self.settings, self.repository, api, FakeGenerator())
        self.assertFalse(service.accept_webhook(payload())["duplicate"])
        self.assertTrue(service.accept_webhook(payload())["duplicate"])
        self.assertEqual(service.process_one_due(now=datetime.now(timezone.utc) + timedelta(seconds=9)), "sent")
        service.accept_webhook(payload("event-2", "message-2", created=1_790_000_100))
        self.assertEqual(service.process_one_due(now=datetime.now(timezone.utc) + timedelta(seconds=30)), "idle")
        self.assertEqual(len(api.sent), 1)

    def test_duplicate_message_id_with_new_event_id_is_not_scheduled(self) -> None:
        service = AvitoResponderService(
            self.settings, self.repository, FakeApi(()), FakeGenerator()
        )
        self.assertFalse(service.accept_webhook(payload())["duplicate"])
        replay = service.accept_webhook(payload("event-2", "message-1"))
        self.assertTrue(replay["duplicate"])

    def test_message_arriving_during_generation_discards_stale_draft(self) -> None:
        api = FakeApi((ChatMessage("message-1", 20, "in", "text", "Ретушь", 0, 1),))
        service_holder = {}

        class InterruptingGenerator(FakeGenerator):
            def generate(inner_self, context):
                result = super().generate(context)
                service_holder["service"].accept_webhook(
                    payload("event-2", "message-2", created=1_790_000_010)
                )
                return result

        service = AvitoResponderService(
            self.settings, self.repository, api, InterruptingGenerator()
        )
        service_holder["service"] = service
        service.accept_webhook(payload())
        result = service.process_one_due(now=datetime.now(timezone.utc) + timedelta(seconds=9))
        self.assertEqual(result, "superseded")
        self.assertEqual(api.sent, [])

    def test_failure_from_stale_claim_does_not_close_newer_bundle(self) -> None:
        api = FakeApi((ChatMessage("message-1", 20, "in", "text", "Ретушь", 0, 1),))
        service_holder = {}

        class InterruptingGenerator(FakeGenerator):
            def generate(inner_self, context):
                service_holder["service"].accept_webhook(
                    payload("event-2", "message-2", created=1_790_000_010)
                )
                raise RuntimeError("synthetic failure")

        service = AvitoResponderService(
            self.settings, self.repository, api, InterruptingGenerator()
        )
        service_holder["service"] = service
        service.accept_webhook(payload())
        self.assertEqual(
            service.process_one_due(
                now=datetime.now(timezone.utc) + timedelta(seconds=9)
            ),
            "failed",
        )
        self.assertEqual(self.repository.status(), {"pending": 1})

    def test_unverified_webhook_cannot_trigger_reply(self) -> None:
        api = FakeApi((ChatMessage("other", 20, "in", "text", "Привет", 0, 1),))
        service = AvitoResponderService(self.settings, self.repository, api, FakeGenerator())
        service.accept_webhook(payload())
        result = service.process_one_due(now=datetime.now(timezone.utc) + timedelta(seconds=9))
        self.assertEqual(result, "unverified_event")
        self.assertEqual(api.sent, [])

    def test_uncertain_send_is_reconciled_without_retry(self) -> None:
        reply = "Здравствуйте! Фото получили, задачу можно рассмотреть."
        messages = (
            ChatMessage("message-1", 20, "in", "text", "Ретушь", 0, 1),
            ChatMessage("out-confirmed", 10, "out", "text", reply, 0, 2),
        )
        api = FakeApi((messages[0],))
        api.send_error = AvitoApiError("send_message", None, uncertain=True)
        generator = FakeGenerator(reply)
        service = AvitoResponderService(self.settings, self.repository, api, generator)
        service.accept_webhook(payload())
        original = api.get_messages
        calls = 0

        def get_messages(user_id: int, chat_id: str, *, limit: int):
            nonlocal calls
            calls += 1
            return (messages[0],) if calls == 1 else messages

        api.get_messages = get_messages
        result = service.process_one_due(now=datetime.now(timezone.utc) + timedelta(seconds=9))
        self.assertEqual(result, "sent_reconciled")
        self.assertEqual(api.sent, [])

    def test_uncertain_timeout_never_blindly_retries(self) -> None:
        messages = (ChatMessage("message-1", 20, "in", "text", "Ретушь", 0, 1),)
        api = FakeApi(messages)
        api.send_error = AvitoApiError("send_message", None, uncertain=True)
        service = AvitoResponderService(
            self.settings, self.repository, api, FakeGenerator()
        )
        service.accept_webhook(payload())
        result = service.process_one_due(
            now=datetime.now(timezone.utc) + timedelta(seconds=9)
        )
        self.assertEqual(result, "uncertain")
        self.assertEqual(api.send_calls, 1)
        self.assertEqual(
            service.process_one_due(
                now=datetime.now(timezone.utc) + timedelta(seconds=120)
            ),
            "idle",
        )
        self.assertEqual(api.send_calls, 1)

    def test_restart_reconciles_sending_after_remote_success_without_resend(self) -> None:
        reply = "Здравствуйте! Фото получили, задачу можно рассмотреть."
        inbound = ChatMessage("message-1", 20, "in", "text", "Ретушь", 0, 1)
        api = FakeApi((inbound,))
        service = AvitoResponderService(
            self.settings, self.repository, api, FakeGenerator(reply)
        )
        self.repository.record_event(
            IncomingEvent.from_webhook(payload()), eligible=True, now=NOW
        )
        claim = self.repository.claim_due(now=NOW + timedelta(seconds=9))
        self.assertTrue(
            self.repository.prepare_reply(
                claim, reply, self.settings.reply_model, "response-1", now=NOW + timedelta(seconds=9)
            )
        )
        self.assertTrue(
            self.repository.mark_sending(claim, now=NOW + timedelta(seconds=9))
        )
        api.messages = (
            inbound,
            ChatMessage("out-confirmed", 10, "out", "text", reply, 0, 2),
        )
        restarted = AvitoResponderService(
            self.settings,
            AvitoRepository(self.settings.database_path, lease_seconds=60),
            api,
            FakeGenerator(reply),
        )
        result = restarted.recover_one_sending(now=NOW + timedelta(seconds=70))
        self.assertEqual(result, "sent_reconciled")
        self.assertEqual(api.send_calls, 0)
        self.assertEqual(self.repository.status(), {"sent": 1})

    def test_restart_retries_once_only_after_confirmed_absence_and_grace(self) -> None:
        reply = "Здравствуйте! Фото получили, задачу можно рассмотреть."
        inbound = ChatMessage("message-1", 20, "in", "text", "Ретушь", 0, 1)
        api = FakeApi((inbound,))
        service = AvitoResponderService(
            self.settings, self.repository, api, FakeGenerator(reply)
        )
        self.repository.record_event(
            IncomingEvent.from_webhook(payload()), eligible=True, now=NOW
        )
        claim = self.repository.claim_due(now=NOW + timedelta(seconds=9))
        self.repository.prepare_reply(
            claim, reply, self.settings.reply_model, "response-1", now=NOW + timedelta(seconds=9)
        )
        self.repository.mark_sending(claim, now=NOW + timedelta(seconds=9))
        self.assertEqual(
            service.recover_one_sending(now=NOW + timedelta(seconds=70)),
            "reconciliation_wait",
        )
        self.assertEqual(api.send_calls, 0)
        self.assertEqual(
            service.recover_one_sending(now=NOW + timedelta(seconds=140)),
            "sent_retried",
        )
        self.assertEqual(api.send_calls, 1)
        self.assertEqual(self.repository.status(), {"sent": 1})

    def test_restart_with_ambiguous_history_closes_without_resend(self) -> None:
        reply = "Здравствуйте! Фото получили, задачу можно рассмотреть."
        api = FakeApi(())
        service = AvitoResponderService(
            self.settings, self.repository, api, FakeGenerator(reply)
        )
        self.repository.record_event(
            IncomingEvent.from_webhook(payload()), eligible=True, now=NOW
        )
        claim = self.repository.claim_due(now=NOW + timedelta(seconds=9))
        self.repository.prepare_reply(
            claim, reply, self.settings.reply_model, "response-1", now=NOW + timedelta(seconds=9)
        )
        self.repository.mark_sending(claim, now=NOW + timedelta(seconds=9))
        self.assertEqual(
            service.recover_one_sending(now=NOW + timedelta(seconds=140)),
            "uncertain",
        )
        self.assertEqual(api.send_calls, 0)
        self.assertEqual(self.repository.status(), {"uncertain": 1})


class ReplyGeneratorTests(TestCase):
    def test_model_receives_text_metadata_only_and_reasoning_none(self) -> None:
        class Responses:
            def __init__(self) -> None:
                self.kwargs = {}

            def create(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(output_text="Фото получили. Уточните задачу.", id="r1")

        responses = Responses()
        context = ReplyContext(
            "chat", 77,
            (ChatMessage("m", 20, "in", "image", "", 2, 1),),
            2, "",
        )
        result = AvitoReplyGenerator(responses, "gpt-5.4-mini").generate(context)
        self.assertEqual(result.text, "Фото получили. Уточните задачу.")
        self.assertEqual(responses.kwargs["reasoning"], {"effort": "none"})
        self.assertNotIn("image_url", str(responses.kwargs))

    def test_price_or_repeated_photo_request_is_replaced_by_safe_fallback(self) -> None:
        class Responses:
            def create(self, **kwargs):
                return SimpleNamespace(output_text="Пришлите фото, цена 500 ₽", id="r1")

        context = ReplyContext(
            "chat", 77,
            (ChatMessage("m", 20, "in", "image", "", 1, 1),),
            1, "",
        )
        text = AvitoReplyGenerator(Responses(), "gpt-5.4-mini").generate(context).text
        self.assertIn("Фотография получена", text)
        self.assertNotIn("500", text)


class WebhookTests(TestCase):
    def test_webhook_returns_quick_200_and_deduplicates(self) -> None:
        class Service:
            def __init__(self) -> None:
                self.calls = 0

            def accept_webhook(self, value):
                self.calls += 1
                return {"accepted": True, "duplicate": self.calls > 1, "scheduled": True}

        service = Service()
        server = AvitoWebhookServer(service, "127.0.0.1", 0, f"/hook/{WEBHOOK_SECRET}")
        built = server.build()
        thread = threading.Thread(target=built.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(payload()).encode()
            for expected in (False, True):
                connection = http.client.HTTPConnection("127.0.0.1", built.server_port, timeout=2)
                connection.request(
                    "POST", f"/hook/{WEBHOOK_SECRET}", body,
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                result = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(result["duplicate"], expected)
                connection.close()
        finally:
            built.shutdown()
            built.server_close()
            thread.join(timeout=2)

    def test_wrong_webhook_secret_path_returns_404_without_service_call(self) -> None:
        class Service:
            calls = 0

            def accept_webhook(self, value):
                self.calls += 1
                return {"duplicate": False}

        service = Service()
        server = AvitoWebhookServer(
            service, "127.0.0.1", 0, f"/integrations/avito/{WEBHOOK_SECRET}/messages"
        )
        built = server.build()
        thread = threading.Thread(target=built.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1", built.server_port, timeout=2
            )
            connection.request("POST", "/integrations/avito/wrong/messages", b"{}")
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 404)
            self.assertEqual(service.calls, 0)
        finally:
            built.shutdown()
            built.server_close()
            thread.join(timeout=2)

    def test_disabled_mode_records_no_runnable_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = AvitoResponderSettings.from_environment(Path(directory), {})
            repository = AvitoRepository(settings.database_path)
            repository.initialize()
            service = AvitoResponderService(
                settings, repository, FakeApi(()), FakeGenerator()
            )
            result = service.accept_webhook(payload())
            self.assertFalse(result["scheduled"])
            self.assertEqual(repository.status(), {})

    def test_systemd_service_is_isolated_from_photo_bot(self) -> None:
        root = Path(__file__).resolve().parents[1]
        avito_unit = (root / "ops" / "ravuna-avito-responder.service").read_text(encoding="utf-8")
        photo_unit = (root / "ops" / "photo-bot.service").read_text(encoding="utf-8")
        self.assertIn("/opt/ravuna-avito", avito_unit)
        self.assertNotIn("photo-bot.service", avito_unit)
        self.assertNotIn("avito", photo_unit.lower())
        for restriction in (
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=true",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
            "MemoryDenyWriteExecute=true",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "CapabilityBoundingSet=",
            "AmbientCapabilities=",
            "DevicePolicy=closed",
            "PrivateDevices=true",
            "UMask=0077",
        ):
            self.assertIn(restriction, avito_unit)

    def test_nginx_route_is_narrow_rate_limited_and_does_not_forward_headers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        location = (root / "ops" / "nginx" / "ravuna-avito-location.conf").read_text(
            encoding="utf-8"
        )
        rate = (root / "ops" / "nginx" / "ravuna-avito-rate.conf").read_text(
            encoding="utf-8"
        )
        site = (root / "site" / "nginx" / "ravuna.ru.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("[A-Za-z0-9_-]{43}", location)
        self.assertIn("limit_req zone=ravuna_avito_webhook", location)
        self.assertIn("client_max_body_size 128k", location)
        self.assertIn("proxy_pass_request_headers off", location)
        self.assertIn("proxy_connect_timeout 2s", location)
        self.assertIn("error_log /dev/null crit", location)
        self.assertIn("rate=10r/m", rate)
        self.assertIn("include /etc/nginx/snippets/ravuna-avito-location*.conf", site)

    def test_avito_deploy_is_manual_fail_closed_and_photo_bot_read_only(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "deploy-avito.yml").read_text(
            encoding="utf-8"
        )
        deploy = (root / "ops" / "deploy_ravuna_avito.sh").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("push:", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn('"AVITO_AUTO_REPLY_ENABLED": "false"', workflow)
        self.assertIn('"AVITO_ALLOWED_ITEM_IDS": "8191967914"', workflow)
        self.assertNotIn("systemctl restart photo-bot", deploy)
        self.assertNotIn("systemctl reload photo-bot", deploy)
        self.assertNotIn('. "$ENV_FILE"', deploy)
        self.assertIn("PHOTO_BOT_UNCHANGED=true", deploy)
        self.assertNotIn("webhook subscription", workflow.lower())

    def test_sudoers_scope_contains_only_avito_and_nginx_operations(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sudoers = (root / "ops" / "ravuna-avito-deploy.sudoers").read_text(
            encoding="utf-8"
        )
        self.assertIn("ravuna-avito-responder.service", sudoers)
        self.assertNotIn("restart photo-bot", sudoers)
        self.assertNotIn("/bin/bash", sudoers)

    def test_deploy_cli_probe_is_release_rooted_from_arbitrary_cwd(self) -> None:
        root = Path(__file__).resolve().parents[1]
        deploy = (root / "ops" / "deploy_ravuna_avito.sh").read_text(encoding="utf-8")
        self.assertIn('STATUS=$(cd "$FINAL_RELEASE" &&', deploy)
        self.assertIn('DRY_RUN=$(cd "$FINAL_RELEASE" &&', deploy)
        self.assertNotIn('"$ROOT/current/venv/bin/python" -m app.avito_responder.cli', deploy)
        environment = os.environ.copy()
        for name in tuple(environment):
            if name.startswith("AVITO_") or name == "OPENAI_API_KEY":
                environment.pop(name)
        with tempfile.TemporaryDirectory() as caller_directory:
            completed = subprocess.run(
                [
                    os.fspath(Path(os.sys.executable)),
                    "-c",
                    (
                        "import os, runpy, sys; "
                        "os.chdir(sys.argv[1]); "
                        "sys.argv=['app.avito_responder.cli', 'status']; "
                        "runpy.run_module('app.avito_responder.cli', run_name='__main__')"
                    ),
                    str(root),
                ],
                cwd=caller_directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["enabled"], False)

    def test_current_helpers_cover_first_and_second_release_rollbacks(self) -> None:
        if os.name == "nt":
            self.skipTest("symlink shell contract is exercised on Linux CI")
        root = Path(__file__).resolve().parents[1]
        helper = root / "ops" / "ravuna_avito_current.sh"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            releases = base / "releases"
            first = releases / ("a" * 40)
            second = releases / ("b" * 40)
            first.mkdir(parents=True)
            second.mkdir()
            env_file = base / ".env"
            data = base / "data"
            env_file.write_text("secret\n", encoding="utf-8")
            data.mkdir()
            command = f'''set -eu
. "$1"
ravuna_avito_inspect_current "$2" "$3"
test "$RAVUNA_AVITO_CURRENT_STATE" = absent
ravuna_avito_set_current "$2" "$3"
test "$(readlink -e "$2/current")" = "$(readlink -e "$3")"
ravuna_avito_remove_current "$2" "$3"
test ! -e "$2/current" && test ! -L "$2/current"
test -f "$2/.env" && test -d "$2/data"
ravuna_avito_set_current "$2" "$3"
ravuna_avito_inspect_current "$2" "$4"
test "$RAVUNA_AVITO_CURRENT_STATE" = previous
test "$RAVUNA_AVITO_PREVIOUS_RELEASE" = "$(readlink -e "$3")"
ravuna_avito_set_current "$2" "$4"
ravuna_avito_set_current "$2" "$RAVUNA_AVITO_PREVIOUS_RELEASE"
test "$(readlink -e "$2/current")" = "$(readlink -e "$3")"
test "$(readlink "$2/current")" != "$2/current"
'''
            completed = subprocess.run(
                ["bash", "-c", command, "bash", str(helper), str(base), str(first), str(second)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_current_helpers_fail_closed_for_broken_or_non_symlink_current(self) -> None:
        if os.name == "nt":
            self.skipTest("symlink shell contract is exercised on Linux CI")
        root = Path(__file__).resolve().parents[1]
        helper = root / "ops" / "ravuna_avito_current.sh"
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            release = base / "releases" / ("a" * 40)
            release.mkdir(parents=True)
            current = base / "current"
            current.symlink_to(current)
            cyclic = subprocess.run(
                ["bash", "-c", '. "$1"; ravuna_avito_inspect_current "$2" "$3"', "bash", str(helper), str(base), str(release)],
                capture_output=True,
                text=True,
                check=False,
            )
            current.unlink()
            current.write_text("not a symlink", encoding="utf-8")
            regular = subprocess.run(
                ["bash", "-c", '. "$1"; ravuna_avito_inspect_current "$2" "$3"', "bash", str(helper), str(base), str(release)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(cyclic.returncode, 0)
        self.assertNotEqual(regular.returncode, 0)


class AvitoApiClientTests(TestCase):
    def test_client_credentials_messages_and_send_contract(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/token/":
                body = request.content.decode()
                self.assertIn("grant_type=client_credentials", body)
                return httpx.Response(200, json={"access_token": "private-token", "expires_in": 86400})
            self.assertEqual(request.headers["Authorization"], "Bearer private-token")
            if request.method == "GET":
                return httpx.Response(200, json={"messages": [{
                    "id": "m1", "author_id": 20, "direction": "in", "type": "image",
                    "created": 1, "content": {"image": {"sizes": {"640x480": "https://example.invalid/i"}}},
                }]})
            self.assertEqual(json.loads(request.content)["type"], "text")
            return httpx.Response(200, json={"id": "sent-1"})

        client = AvitoApiClient(
            "client", "secret", "https://api.avito.test",
            transport=httpx.MockTransport(handler),
        )
        try:
            messages = client.get_messages(10, "chat", limit=6)
            sent = client.send_message(10, "chat", "Здравствуйте")
        finally:
            client.close()
        self.assertEqual(messages[0].image_count, 1)
        self.assertEqual(sent, "sent-1")
        self.assertEqual(sum(request.url.path == "/token/" for request in requests), 1)


class AvitoCliTests(TestCase):
    def test_synthetic_dry_run_generates_but_never_sends_to_avito(self) -> None:
        root = Path(__file__).resolve().parents[1]
        fixture = root / "tests" / "fixtures" / "avito_first_reply.json"

        class Responses:
            def __init__(self) -> None:
                self.kwargs = {}

            def create(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(
                    output_text="Здравствуйте! Фото получили, фон сделать светлее можно.",
                    id="response-1",
                )

        responses = Responses()
        fake_openai = SimpleNamespace(responses=responses)
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic"}, clear=True),
            patch("app.avito_responder.cli.load_dotenv"),
            patch("app.avito_responder.cli.OpenAI", return_value=fake_openai),
            redirect_stdout(output),
        ):
            result = avito_cli_main(["dry-run", "--fixture", str(fixture)])
        report = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertFalse(report["sent"])
        self.assertEqual(report["image_count"], 1)
        self.assertEqual(responses.kwargs["reasoning"], {"effort": "none"})
