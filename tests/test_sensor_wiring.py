"""Offline HA wiring tests. Sensor platform is loaded with minimal HA/FinTS stubs."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest

try:
    import voluptuous as real_vol
except ImportError:
    real_vol = None

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "fints_own"


def stub_module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def load_sensor_with_stubs():
    names = (
        "fints", "fints.client", "fints.exceptions", "fints.models",
        "homeassistant", "homeassistant.components", "homeassistant.components.sensor",
        "homeassistant.const", "homeassistant.core", "homeassistant.helpers",
        "homeassistant.helpers.entity_platform", "homeassistant.helpers.typing",
        "custom_components", "custom_components.fints_own",
    )
    for name in names:
        stub_module(name)

    if real_vol is None:
        # The offline wiring tests can run without installed third-party packages.
        # CI installs real Voluptuous to exercise Home Assistant's config schema.
        vol = stub_module("voluptuous")

        class MockSchema:
            def __init__(self, *args, **kwargs):
                pass
            def extend(self, *args):
                return self
            def __call__(self, value):
                return value

        vol.Schema = MockSchema
        vol.Required = lambda name, **kwargs: name
        vol.Optional = lambda name, **kwargs: name
        vol.All = lambda *args: (lambda v: v)
        vol.Coerce = lambda cls: (lambda v: cls(v))
        vol.Range = lambda **kwargs: (lambda v: v)
        vol.In = lambda values: (lambda v: v)
        vol.Invalid = ValueError
    else:
        vol = real_vol

    fints_client = sys.modules["fints.client"]
    fints_client.FinTS3PinTanClient = type("FinTS3PinTanClient", (), {})
    fints_client.NeedTANResponse = type("NeedTANResponse", (), {})
    sys.modules["fints.exceptions"].FinTSClientError = type("FinTSClientError", (Exception,), {})
    sys.modules["fints.models"].SEPAAccount = type("SEPAAccount", (), {})

    module = sys.modules["homeassistant.components.sensor"]
    class FakeSensorEntity:
        hass = None
        async def async_added_to_hass(self):
            return None
        def schedule_update_ha_state(self, **kwargs):
            pass
    module.SensorEntity = FakeSensorEntity
    module.PLATFORM_SCHEMA = (
        vol.Schema({vol.Optional("platform"): str}, extra=vol.ALLOW_EXTRA)
        if real_vol is not None else MockSchema()
    )

    const = sys.modules["homeassistant.const"]
    const.CONF_NAME = "name"
    const.CONF_PIN = "pin"
    const.CONF_URL = "url"
    const.CONF_USERNAME = "username"
    sys.modules["homeassistant.core"].HomeAssistant = type("HomeAssistant", (), {})
    sys.modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = type("AddEntitiesCallback", (), {})
    sys.modules["homeassistant.helpers.typing"].ConfigType = dict
    sys.modules["homeassistant.helpers.typing"].DiscoveryInfoType = dict

    package = sys.modules["custom_components.fints_own"]
    package.__path__ = [str(ROOT)]
    spec = importlib.util.spec_from_file_location("custom_components.fints_own.sensor", ROOT / "sensor.py")
    sensor = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = sensor
    spec.loader.exec_module(sensor)
    return sensor


class Services:
    def __init__(self):
        self.handlers = {}
    def has_service(self, domain, name):
        return (domain, name) in self.handlers
    def register(self, domain, name, func, schema=None):
        self.handlers[(domain, name)] = func


class FakeHass:
    def __init__(self):
        self.data = {}
        self.services = Services()
        self.jobs = []
    def async_add_executor_job(self, func):
        return asyncio.to_thread(func)
    def async_create_task(self, coro):
        task = asyncio.create_task(coro)
        self.jobs.append(task)
        return task


class FakeBankClient:
    def __init__(self, **kwargs):
        self.login_id = kwargs["login_id"]
        self.transaction_days = kwargs["transaction_days"]
        self.auto_reauthenticate = kwargs["auto_reauthenticate"]
        self.retry_after = None
        self.waiting_for_sms = False
        self.calls = []
        self.balance = 18.3
        self.session_restored = True
        self.has_cached_access_token = True
        self.has_saved_device_token = True
        self.session_storage_error = False
        self.last_authenticated_at = "2026-10-09T12:00:00+00:00"
        self.last_successful_sync = "2026-10-09T12:01:00+00:00"
        self.access_token_expires_at = "2026-10-09T13:00:00+00:00"
        self.access_token_expiry_source = "expires_in"
        self.refresh_token_offered = False
        self.last_token_rejected_at = "2026-10-09T11:30:00+00:00"
        self.last_token_rejection_http_status = 401
        self.last_login_attempt_at = "2026-10-09T12:00:00+00:00"
        self.last_login_reason = "bank_rejected_access_token"
        self.last_login_outcome = "success"
        self.last_login_http_status = None
    def fetch(self, *, force_reauthenticate=False):
        self.calls.append(("fetch", force_reauthenticate))
        from custom_components.fints_own.hanseatic import HanseaticSnapshot
        return HanseaticSnapshot(
            account_number="123", account_label="GenialCard", account_type="credit_card",
            balance=self.balance, currency="EUR", retrieved_at="2026-10-09T12:00:00+00:00"
        )
    def submit_otp(self, otp):
        self.calls.append(("otp", otp))


class TestHAWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_sensor_with_stubs()
        cls.real_client = cls.module.HanseaticBankClient
        cls.module.HanseaticBankClient = FakeBankClient

    @classmethod
    def tearDownClass(cls):
        cls.module.HanseaticBankClient = cls.real_client

    def add_entities(self, entities, update_first):
        self.entities.extend(entities)
        self.update_flags.append(update_first)
        for ent in entities:
            ent.hass = self.hass

    def perform_initial_refresh(self):
        async def refresh():
            await self.entities[-4].async_added_to_hass()
            await asyncio.gather(*self.hass.jobs)
        asyncio.run(refresh())

    def setUp(self):
        self.entities = []
        self.update_flags = []
        self.hass = FakeHass()

    def test_fints_configuration_and_entity_ids_unchanged(self):
        self.module.setup_platform(
            self.hass,
            {"users": [{"name": "Sparkasse", "bank_identification_number": "10000000",
                        "username": "dummy", "pin": "dummy", "url": "https://test.invalid/fints"}]},
            self.add_entities,
        )
        self.assertEqual(len(self.entities), 4)
        self.assertEqual(self.entities[0].unique_id, "fints_own_user_total_sparkasse")
        self.assertEqual(self.entities[1].unique_id, "fints_own_last_successful_sync_sparkasse")
        self.assertEqual(self.update_flags, [False])

    def test_online_balance_updated_and_services_registered(self):
        self.module.setup_platform(
            self.hass,
            {"users": [{"name": "Visa", "provider": "hanseatic", "login_id": "1234567890", "password": "dummy"}]},
            self.add_entities,
        )
        self.assertEqual(len(self.entities), 4)
        self.perform_initial_refresh()
        self.assertEqual(self.entities[0].native_value, 18.3)
        self.assertEqual(self.entities[0].unique_id, "fints_own_hanseatic_balance_visa")
        self.assertEqual(self.entities[3].native_value, "success")
        self.assertEqual(self.entities[0].extra_state_attributes["source"], "online_api")
        self.assertEqual(self.update_flags, [False])
        manager = self.hass.data["fints_own_hanseatic_managers"]["Visa"]
        self.assertEqual(manager.client.calls, [("fetch", False)])

        class ServiceCall:
            def __init__(self, data):
                self.data = data
        self.hass.services.handlers[("fints_own", "hanseatic_submit_otp")](
            ServiceCall({"name": "Visa", "otp": "123456"})
        )
        self.assertEqual(manager.client.calls[-2:], [("otp", "123456"), ("fetch", False)])
        self.hass.services.handlers[("fints_own", "hanseatic_reauthenticate")](
            ServiceCall({"name": "Visa"})
        )
        self.assertEqual(manager.client.calls[-1], ("fetch", True))

    @unittest.skipIf(real_vol is None, "Real Voluptuous not installed")
    def test_schema_accepts_hanseatic_and_sparkasse_together(self):
        validated = self.module.PLATFORM_SCHEMA({
            "platform": "fints_own",
            "users": [
                {"name": "Sparkasse", "bank_identification_number": "10000000",
                 "username": "spk-user", "pin": "1234", "url": "https://bank.invalid/fints"},
                {"name": "Visa", "provider": "hanseatic", "login_id": "1234567890",
                 "password": "secret", "auto_reauthenticate": True,
                 "sca_timeout_seconds": 90, "reauth_retry_hours": 12},
            ],
        })
        self.assertEqual(validated["users"][0]["provider"], "fints")
        self.assertEqual(validated["users"][1]["provider"], "hanseatic")
        self.assertEqual(validated["users"][1]["login_id"], "1234567890")

    @unittest.skipIf(real_vol is None, "Real Voluptuous not installed")
    def test_schema_accepts_hanseatic_without_sparkasse_fields(self):
        validated = self.module.PLATFORM_SCHEMA({
            "platform": "fints_own",
            "users": [{"name": "Visa", "provider": "hanseatic", "login_id": "1234567890",
                       "password": "secret"}],
        })
        self.assertEqual(validated["users"][0]["provider"], "hanseatic")

    @unittest.skipIf(real_vol is None, "Real Voluptuous not installed")
    def test_schema_still_rejects_incomplete_sparkasse_config(self):
        with self.assertRaises(real_vol.Invalid):
            self.module.PLATFORM_SCHEMA({
                "platform": "fints_own",
                "users": [{"name": "Sparkasse", "username": "spk-user"}],
            })

    def test_status_reports_persisted_session_without_exposing_tokens(self):
        self.module.setup_platform(
            self.hass,
            {"users": [{"name": "Visa", "provider": "hanseatic", "login_id": "1234567890", "password": "dummy"}]},
            self.add_entities,
        )
        status = self.entities[3]
        attrs = status.extra_state_attributes
        self.assertEqual(attrs["last_successful_sync"], "2026-10-09T12:01:00+00:00")
        self.assertEqual(attrs["last_authenticated_at"], "2026-10-09T12:00:00+00:00")
        self.assertTrue(attrs["session_restored"])
        self.assertTrue(attrs["access_token_cached"])
        self.assertTrue(attrs["device_token_cached"])
        self.assertFalse(attrs["session_storage_error"])
        self.assertEqual(attrs["access_token_expires_at"], "2026-10-09T13:00:00+00:00")
        self.assertEqual(attrs["access_token_expiry_source"], "expires_in")
        self.assertFalse(attrs["refresh_token_offered"])
        self.assertEqual(attrs["last_token_rejection_http_status"], 401)
        self.assertEqual(attrs["last_login_reason"], "bank_rejected_access_token")
        self.assertEqual(attrs["last_login_outcome"], "success")
        self.assertIsNone(attrs["last_login_http_status"])
        self.assertNotIn("access_token", attrs)
        self.assertNotIn("device_token", attrs)

    def test_sparkasse_and_hanseatic_in_one_config(self):
        self.module.setup_platform(
            self.hass,
            {"users": [
                {"name": "Sparkasse", "bank_identification_number": "12345678",
                 "username": "test", "pin": "test", "url": "https://test.invalid/fints"},
                {"name": "Visa", "provider": "hanseatic", "login_id": "1234567890", "password": "test"},
            ]},
            self.add_entities,
        )
        self.assertEqual(len(self.entities), 8)
        self.assertEqual(self.update_flags, [False, False])


if __name__ == "__main__":
    unittest.main()
