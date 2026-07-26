from __future__ import annotations

import base64
import importlib.util
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


archive_validator = load_module(
    "archive_validator", "scripts/validate-tar-archive.py"
)
environment_validator = load_module(
    "environment_validator", "scripts/validate-elder-env.py"
)
network_health = load_module(
    "network_health", "network/k1-network-health.py"
)
nav_engine = load_module("nav_engine_test", "voice/nav_engine.py")
incoming_sms = load_module(
    "incoming_sms_test", "voice/incoming_sms_to_tts.py"
)


class ArchiveValidationTests(unittest.TestCase):
    def write_archive(self, member_name: str) -> str:
        handle = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        handle.close()
        with tarfile.open(handle.name, "w:gz") as archive:
            payload = b"safe"
            member = tarfile.TarInfo(member_name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return handle.name

    def test_accepts_relative_regular_member(self):
        archive_validator.validate_archive(self.write_archive("package/file.txt"))

    def test_rejects_parent_traversal(self):
        with self.assertRaises(ValueError):
            archive_validator.validate_archive(self.write_archive("../escape"))


class NetworkHealthTests(unittest.TestCase):
    def test_default_route_parser_preserves_owner(self):
        routes = network_health.parse_default_routes(
            "default via 198.51.100.1 dev enx001122334455 proto dhcp metric 100",
            network_health.socket.AF_INET,
        )
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].interface, "enx001122334455")
        self.assertEqual(routes[0].metric, 100)

    def test_action_cooldown(self):
        self.assertFalse(network_health.action_allowed(100.0, 219.0))
        self.assertTrue(network_health.action_allowed(100.0, 220.0))


class EnvironmentValidationTests(unittest.TestCase):
    def write_environment(self, extra: str = "") -> Path:
        phone = "13" + "8000" + "00000"
        device_key = base64.b64encode(b"0123456789").decode("ascii")
        product_key = base64.b64encode(b"abcdefghij").decode("ascii")
        map_key = "Map" + "Key_" + "123456"
        content = (
            "ONENET_PRODUCT_ID=product-1\n"
            "ONENET_DEVICE_NAME=device-1\n"
            f"ONENET_DEVICE_KEY_B64={device_key}\n"
            f"ONENET_PRODUCT_KEY_B64={product_key}\n"
            f"TENCENT_MAP_KEY={map_key}\n"
            f"ELDER_SMS_PHONE={phone}\n"
            "ELDER_SMS_CONTACT_NAME=TrustedContact\n"
            + extra
        )
        handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
        handle.write(content)
        handle.close()
        path = Path(handle.name)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_accepts_exact_schema(self):
        environment_validator.validate_environment(self.write_environment())

    def test_rejects_environment_injection_key(self):
        with self.assertRaises(environment_validator.ConfigurationError):
            environment_validator.validate_environment(
                self.write_environment("LD_" + "PRELOAD=/tmp/untrusted.so\n")
            )


class InputBoundaryTests(unittest.TestCase):
    def test_navigation_rejects_non_https_url_before_io(self):
        with self.assertRaises(ValueError):
            nav_engine._load_https_json("http://apis.map.qq.com/test", timeout=1)

    def test_navigation_rejects_unapproved_host_before_io(self):
        with self.assertRaises(ValueError):
            nav_engine._load_https_json("https://example.invalid/test", timeout=1)

    def test_navigation_rejects_nonstandard_port_before_io(self):
        with self.assertRaises(ValueError):
            nav_engine._load_https_json(
                "https://apis.map.qq.com:8443/test", timeout=1
            )

    def test_mqtt_peer_certificate_is_pinned(self):
        source = (ROOT / "network/lbs_service.py").read_text(encoding="utf-8")
        self.assertIn("getpeercert(binary_form=True)", source)
        self.assertIn(
            "e08b69e2e3f8d5a67084a557eb2c1486c9a5b6a3fabd2eab6051a03077916930",
            source,
        )

    def test_sms_decoder_rejects_malformed_hex(self):
        with self.assertRaises(incoming_sms.SmsDecodeError):
            incoming_sms.decode_sms_deliver_pdu("not-hex")


if __name__ == "__main__":
    unittest.main()
