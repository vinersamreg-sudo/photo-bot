import hashlib
import ssl
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
MAX_CA = ROOT / "ops" / "certs" / "russian_trusted_root_ca_pem.crt"
EXPECTED_DER_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"


class MaxCertificateTests(TestCase):
    def test_official_russian_trusted_root_is_pinned(self) -> None:
        der = ssl.PEM_cert_to_DER_cert(MAX_CA.read_text(encoding="ascii"))
        self.assertEqual(hashlib.sha256(der).hexdigest(), EXPECTED_DER_SHA256)
