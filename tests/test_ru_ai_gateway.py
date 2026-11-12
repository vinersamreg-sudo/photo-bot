import base64
import io
import json
import shutil
import ssl
import subprocess
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import Request, urlopen

from PIL import Image

from app.ai_gateway import Gateway, RemoteImageProvider, mock_openai, serve
from app.config import Settings
from app.domain import PolicyRejectedError
from app.image_provider import OpenAIImageProvider
from app.openai_client import OpenAIConfigurationError
from app.provider_router import select_image_provider


def png():
    out = io.BytesIO()
    Image.new("RGB", (320, 480), "blue").save(out, "PNG")
    return out.getvalue()


class RuAiGatewayTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.journal = self.root / "receipts.sqlite"
        self.mock = mock_openai()
        self.mock.images.edit = Mock(wraps=self.mock.images.edit)
        self.provider = OpenAIImageProvider(self.mock, "fixture-model", quality="medium")

    def request(self):
        return json.dumps({"id": f"{int(time.time())}-{uuid.uuid4().hex}",
                           "prompt": "Точный тестовый запрос 😀", "images": [
                               base64.b64encode(png()).decode("ascii")]}).encode()

    def test_identical_retry_different_body_and_restart(self):
        gateway = Gateway(self.journal, self.provider)
        raw = self.request()
        first = gateway.edit(raw)
        self.assertEqual(first[0], 200)
        self.assertEqual(first, gateway.edit(raw))
        self.assertEqual(gateway.edit(raw + b" ")[0], 409)
        self.assertEqual(Gateway(self.journal, self.provider).edit(raw)[0], 409)
        self.assertEqual(self.mock.images.edit.call_count, 1)
        args = self.mock.images.edit.call_args.kwargs
        self.assertEqual(args["prompt"], "Точный тестовый запрос 😀")
        self.assertEqual((args["quality"], args["size"], args["output_format"]), ("medium", "1024x1536", "png"))
        self.assertNotIn(base64.b64encode(png()), self.journal.read_bytes())

    def test_policy_error_is_typed_and_no_provider_message_is_returned(self):
        provider = Mock()
        provider.edit_bytes.side_effect = PolicyRejectedError("sensitive original")
        gateway = Gateway(self.journal, provider)
        self.assertEqual(gateway.edit(self.request()), (422, b'{"error":"policy"}'))

    def test_mock_is_never_selected_in_public_or_paid_mode(self):
        settings = Settings(base_dir=self.root, image_provider="openai", openai_image_model="fixture",
                            openai_api_key="", app_env="test",
                            ai_gateway_url="https://127.0.0.1", max_public_access_enabled=True)
        with self.assertRaises(OpenAIConfigurationError):
            select_image_provider(settings)
        with self.assertRaises(OpenAIConfigurationError):
            select_image_provider(replace(settings, max_public_access_enabled=False, payments_enabled=True))

    @skipUnless(shutil.which("openssl"), "OpenSSL CLI required")
    def test_mtls_real_transport_with_mock_upstream(self):
        def run(*args):
            return subprocess.run(["openssl", *args], cwd=self.root, check=True, capture_output=True)

        run("req", "-new", "-x509", "-nodes", "-newkey", "rsa:2048", "-days", "1",
            "-subj", "/CN=synthetic-root", "-out", "ca.pem", "-keyout", "ca.key")
        for name, ext in (("server", "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth"),
                          ("client", "subjectAltName=DNS:ravuna-core.internal\nextendedKeyUsage=clientAuth"),
                          ("wrong", "subjectAltName=DNS:wrong.internal\nextendedKeyUsage=clientAuth")):
            run("req", "-new", "-nodes", "-newkey", "rsa:2048", "-subj", f"/CN={name}",
                "-out", f"{name}.csr", "-keyout", f"{name}.key")
            (self.root / "ext").write_text(ext, encoding="ascii")
            run("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                "-CAcreateserial", "-out", f"{name}.pem", "-days", "1", "-extfile", "ext")
        server = serve(Gateway(self.journal, self.provider), "127.0.0.1", 0,
                       ca=str(self.root / "ca.pem"), cert=str(self.root / "server.pem"),
                       key=str(self.root / "server.key"))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        settings = Settings(base_dir=self.root, openai_image_model="fixture-model", openai_api_key="",
                            app_env="test", image_provider="openai", demo_min_request_interval_seconds=0,
                            ai_gateway_url=f"https://127.0.0.1:{server.server_port}",
                            ai_gateway_ca=str(self.root / "ca.pem"),
                            ai_gateway_cert=str(self.root / "client.pem"), ai_gateway_key=str(self.root / "client.key"))
        source = self.root / "synthetic.png"
        source.write_bytes(png())
        self.assertTrue(RemoteImageProvider(settings).edit(source, "Пример").image_bytes.startswith(b"\x89PNG"))
        from app.domain import ProviderUnavailableError
        wrong = replace(settings, ai_gateway_cert=str(self.root / "wrong.pem"), ai_gateway_key=str(self.root / "wrong.key"))
        with self.assertRaises(ProviderUnavailableError):
            RemoteImageProvider(wrong).edit(source, "Пример")
        with self.assertRaises(URLError):
            urlopen(Request(settings.ai_gateway_url + "/v1/edit", data=self.request()), timeout=2,
                    context=ssl.create_default_context(cafile=settings.ai_gateway_ca))
        self.assertEqual(self.mock.images.edit.call_count, 1)

        from app.database import Database
        from app.demo_service import DemoService
        from app.storage import PrivateStorage
        from app.watermark import WatermarkService

        remote = RemoteImageProvider(settings)
        demo = DemoService(settings, Database(settings.database_path), PrivateStorage(settings.users_dir, 1024**2),
                           WatermarkService("ОБРАЗЕЦ", 1024, "JPEG", 82), remote)
        session = demo.start_session("max", "fixture-owner", source)
        initial = demo.commerce.balance(session.user_id).available
        with patch.object(self.provider, "edit_bytes", side_effect=PolicyRejectedError("not logged")):
            with self.assertRaises(PolicyRejectedError):
                demo.generate(session.session_id, "Первый запрос", "fixture-policy")
        self.assertEqual(demo.commerce.balance(session.user_id).available, initial)
        first = demo.generate(session.session_id, "Второй запрос", "fixture-success")
        replay = demo.generate(session.session_id, "Второй запрос", "fixture-success")
        self.assertEqual(first.attempt_id, replay.attempt_id)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(demo.commerce.balance(session.user_id).available, initial - 1)
        self.assertEqual(self.mock.images.edit.call_count, 2)
