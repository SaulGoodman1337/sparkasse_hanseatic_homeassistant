"""Offline contract tests for the Hanseatic online API (no real credentials)."""
import base64
from datetime import date, timedelta
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "custom_components" / "fints_own" / "hanseatic.py"
spec = importlib.util.spec_from_file_location("hanseatic_test_api", MODULE)
hb = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hb
spec.loader.exec_module(hb)


def id_token(sca_type="APP", sca_id="challenge-42"):
    body = base64.urlsafe_b64encode(json.dumps({"sca_id": sca_id, "sca_type": sca_type}).encode()).decode().rstrip("=")
    return "header." + body + ".signature"


class StubTransport:
    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def __call__(self, method, path, payload, headers, form):
        self.calls.append((method, path, payload, headers, form))
        for condition, callback in self.handlers:
            if condition(method, path, payload, headers):
                if callable(callback):
                    return callback(method, path, payload, headers)
                if isinstance(callback, list):
                    item = callback.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    return item
                if isinstance(callback, Exception):
                    raise callback
                return callback
        raise AssertionError(f"Unexpected {method} {path}")


def path_is(value, method=None):
    return lambda m, path, payload, headers: path == value and (method is None or method == m)


def customer_response(saldo="-123.45", **extra):
    return {"accounts": {"creditAccounts": [{"accountNumber": "CARD1", "productLabel": "GenialCard", "saldo": saldo, **extra}]}}


class HanseaticOnlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def client(self, stub, **kwargs):
        return hb.HanseaticBankClient(
            "1234567890", "test-password", Path(self.tmp.name),
            requester=stub, sca_poll_seconds=0, **kwargs,
        )

    def test_direct_token_online_balance_and_optional_limits(self):
        stub = StubTransport([
            (path_is("/token"), {"access_token": "auth-token"}),
            (path_is("/customerinfo/1.0/initCustomer"), customer_response(
                "-123,45", availableAmount="876,55", creditLimit="1000,00"
            )),
        ])
        client = self.client(stub)
        snap = client.fetch()
        self.assertEqual(snap.balance, -123.45)
        self.assertEqual(snap.available_credit, 876.55)
        self.assertEqual(snap.credit_limit, 1000)
        self.assertEqual(snap.account_type, "credit_card")
        self.assertEqual(snap.transaction_count, 0)
        self.assertEqual(stub.calls[0][2]["password"], "test-password")
        self.assertEqual(stub.calls[0][3]["Authorization"], hb._BASIC_AUTH)
        state = next(Path(self.tmp.name).glob("hanseatic_session_*.json"))
        self.assertEqual(stat.S_IMODE(os.stat(state).st_mode), 0o600)
        self.assertNotIn("test-password", state.read_text("utf-8"))
        self.assertNotIn("1234567890", state.name)

    def test_stored_token_is_reused_after_restart_without_new_push(self):
        first = StubTransport([
            (path_is("/token"), {"access_token": "cached-token"}),
            (path_is("/customerinfo/1.0/initCustomer"), customer_response("4.2")),
        ])
        self.client(first).fetch()
        second = StubTransport([(path_is("/customerinfo/1.0/initCustomer"), customer_response("5.5"))])
        client = self.client(second)
        self.assertEqual(client.fetch().balance, 5.5)
        self.assertEqual(len(second.calls), 1)
        self.assertEqual(second.calls[0][3]["Authorization"], "Bearer cached-token")

    def test_app_push_sca_completes_and_saves_device_token(self):
        attempts = [
            {"id_token": id_token()},
            {"access_token": "client-status-token"},
            {"access_token": "logged-in"},
        ]
        status = [{"status": "open"}, {"status": "complete", "resultData": {"DEVICETOKEN": "device-42"}}]
        stub = StubTransport([
            (path_is("/token"), attempts),
            (lambda m, p, b, h: p.startswith("/openScaBroker/"), status),
            (path_is("/customerinfo/1.0/initCustomer"), customer_response(19)),
        ])
        client = self.client(stub)
        self.assertEqual(client.fetch().balance, 19)
        self.assertEqual([c[1] for c in stub.calls].count("/token"), 3)
        self.assertEqual(stub.calls[2][3]["Authorization"], "Bearer client-status-token")
        self.assertEqual(stub.calls[4][3]["devicetoken"], "device-42")
        state = next(Path(self.tmp.name).glob("hanseatic_session_*.json"))
        self.assertEqual(json.loads(state.read_text())["device_token"], "device-42")

    def test_sms_otp_completion_no_manual_file(self):
        stub = StubTransport([
            (path_is("/token"), [{"id_token": id_token("SMS")}, {"access_token": "sms-token"}]),
            (path_is("/customerinfo/1.0/initCustomer"), customer_response(25)),
        ])
        client = self.client(stub)
        with self.assertRaises(hb.HanseaticOtpRequired):
            client.fetch()
        self.assertTrue(client.waiting_for_sms)
        with self.assertRaises(hb.HanseaticError):
            client.submit_otp("not-a-code")
        client.submit_otp("123456")
        self.assertFalse(client.waiting_for_sms)
        self.assertEqual(client.fetch().balance, 25)
        self.assertEqual(stub.calls[1][2]["otp"], "123456")
        self.assertNotIn("123456", next(Path(self.tmp.name).glob("hanseatic_session_*.json")).read_text())

    def test_auth_failure_rate_limited_without_endless_pushes(self):
        stub = StubTransport([(path_is("/token"), hb.HanseaticHttpError(401))])
        client = self.client(stub, reauth_retry_hours=24)
        with self.assertRaises(hb.HanseaticHttpError):
            client.fetch()
        self.assertIsNotNone(client.retry_after)
        calls = len(stub.calls)
        with self.assertRaises(hb.HanseaticApprovalRequired):
            client.fetch()
        self.assertEqual(len(stub.calls), calls)
        with self.assertRaises(hb.HanseaticHttpError):
            client.fetch(force_reauthenticate=True)
        self.assertEqual(len(stub.calls), calls + 1)

    def test_expired_bearer_token_triggers_new_login(self):
        initial = StubTransport([
            (path_is("/token"), {"access_token": "token1"}),
            (path_is("/customerinfo/1.0/initCustomer"), customer_response(10)),
        ])
        client = self.client(initial)
        client.fetch()
        subsequent = StubTransport([
            (path_is("/customerinfo/1.0/initCustomer"), [hb.HanseaticHttpError(401), customer_response(20)]),
            (path_is("/token"), {"access_token": "token2"}),
        ])
        client._requester = subsequent
        self.assertEqual(client.fetch().balance, 20)
        self.assertEqual([c[1] for c in subsequent.calls], [
            "/customerinfo/1.0/initCustomer", "/token", "/customerinfo/1.0/initCustomer"
        ])

    def test_transactions_are_fetched_live_and_filtered(self):
        today = date.today()
        older = (today - timedelta(days=120)).isoformat()
        recent = (today - timedelta(days=2)).isoformat()
        stub = StubTransport([
            (path_is("/token"), {"access_token": "token"}),
            (path_is("/customerinfo/1.0/initCustomer"), customer_response(45)),
            (lambda m, p, b, h: p.startswith("/transaction/"), {
                "transactions": [
                    {"transactionDate": recent, "amount": -7.40, "merchantName": "Shop", "description": "Purchase", "booked": True},
                    {"transactionDate": recent, "amount": 2.00, "merchantName": "Refund", "booked": False},
                    {"transactionDate": older, "amount": -99.0},
                ], "more": False,
            }),
        ])
        snap = self.client(stub, transaction_days=30).fetch()
        self.assertEqual(snap.transaction_count, 2)
        self.assertEqual(snap.income_total, 2.0)
        self.assertEqual(snap.expense_total, 7.4)
        self.assertEqual(snap.transactions[0]["booked"], True)

    def test_transaction_failure_does_not_hide_bank_balance(self):
        stub = StubTransport([
            (path_is("/token"), {"access_token": "token"}),
            (path_is("/customerinfo/1.0/initCustomer"), customer_response(-80)),
            (lambda m, p, b, h: p.startswith("/transaction/"), hb.HanseaticHttpError(500)),
        ])
        snap = self.client(stub, transaction_days=5).fetch()
        self.assertEqual(snap.balance, -80)
        self.assertIn("500", snap.transaction_error)

    def test_more_than_one_card_requires_account_selection(self):
        stub = StubTransport([
            (path_is("/token"), {"access_token": "token"}),
            (path_is("/customerinfo/1.0/initCustomer"), {"accounts": {"creditAccounts": [
                {"accountNumber": "1", "saldo": 1}, {"accountNumber": "2", "saldo": 2}
            ]}}),
        ])
        client = self.client(stub)
        with self.assertRaisesRegex(hb.HanseaticError, "account_number"):
            client.fetch()
        client.account_number = "2"
        self.assertEqual(client.fetch().balance, 2)

    def test_unauthorized_requests_never_send_secrets_to_arbitrary_urls(self):
        with self.assertRaises(hb.HanseaticError):
            hb._http_request("POST", "//evil.invalid/", {"password": "secret"}, {}, True)
        with self.assertRaises(ValueError):
            hb.HanseaticBankClient("not-ten-digits", "test", Path(self.tmp.name))

    def test_bank_does_not_need_manual_import_file(self):
        stub = StubTransport([
            (path_is("/token"), {"access_token": "token"}),
            (path_is("/customerinfo/1.0/initCustomer"), customer_response(99)),
        ])
        snap = self.client(stub).fetch()
        self.assertEqual(snap.balance, 99)
        self.assertTrue(any(c[1].endswith("initCustomer") for c in stub.calls))

    def test_automatic_reauthentication_can_be_disabled(self):
        stub = StubTransport([])
        client = self.client(stub, auto_reauthenticate=False)
        with self.assertRaises(hb.HanseaticApprovalRequired):
            client.fetch()
        self.assertEqual(stub.calls, [])


if __name__ == "__main__":
    unittest.main()
