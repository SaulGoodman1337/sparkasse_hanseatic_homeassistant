"""Read-only online client for Hanseatic Bank's *unofficial* Meine Hanseatic API.

Based on the documented flow of the MIT-licensed community MoneyMoney extension:
https://github.com/davyd15/moneymoney-hanseaticbank

Not endorsed or supported by Hanseatic Bank. No payment endpoints are used.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

_LOGGER = logging.getLogger(__name__)

# Static OAuth *web application* identifiers (not customer credentials).
# Source: Meine Hanseatic Bank web app, as documented by the MoneyMoney extension.
_API_ROOT = "https://connecthb.hanseaticbank.de"
_PORTAL = "https://meine.hanseaticbank.de"
_BASIC_AUTH = "Basic bTZLVnV4ZVhoY1FYV0RHNWM5VWNDYVo1QnA0YTo0alhIUWRxMGhqdG9ibUNWZW11NlFWcGliX3dh"
_CLIENT_ID = "5bnQTsZSz_IixlE0YqX4CrVCjPca"
_CLIENT_SECRET = "cNLo9jjW9kpDkcf3VRnfmRXmXFoa"
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

MAX_TRANSACTION_PAGES = 25
MAX_RESPONSE_BYTES = 4_000_000
DEFAULT_HTTP_TIMEOUT = 15


class HanseaticError(Exception):
    """Public, sanitized error safe for Home Assistant state/logging."""


class HanseaticHttpError(HanseaticError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Hanseatic HTTP {status}")


class HanseaticApprovalRequired(HanseaticError):
    """The bank requires app approval, or a prior approval attempt expired."""


class HanseaticOtpRequired(HanseaticApprovalRequired):
    """The bank sent an SMS code; submit it using a Home Assistant service."""


@dataclass(frozen=True)
class HanseaticSnapshot:
    account_number: str
    account_label: str
    account_type: str
    balance: float
    currency: str
    retrieved_at: str
    available_credit: float | None = None
    credit_limit: float | None = None
    transactions: list[dict[str, Any]] = field(default_factory=list)
    period_start: str | None = None
    period_end: str | None = None
    transaction_error: str | None = None
    history_requires_approval: bool = False

    @property
    def transaction_count(self) -> int:
        return len(self.transactions)

    @property
    def income_total(self) -> float:
        return round(sum(tx["amount"] for tx in self.transactions if tx["amount"] > 0), 2)

    @property
    def expense_total(self) -> float:
        return round(-sum(tx["amount"] for tx in self.transactions if tx["amount"] < 0), 2)

    @property
    def net_total(self) -> float:
        return round(sum(tx["amount"] for tx in self.transactions), 2)


def _money(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("Bank returned a missing or invalid balance")
    raw = str(value).strip()
    if not raw:
        raise ValueError("Bank returned an empty balance")
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        decimal = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Bank returned an invalid monetary amount") from exc
    if not decimal.is_finite():
        raise ValueError("Bank returned a non-finite monetary amount")
    return float(decimal)


def _optional_money(*values: Any) -> float | None:
    for value in values:
        if value is not None and value != "":
            try:
                return _money(value)
            except ValueError:
                _LOGGER.debug("Ignoring unrecognized optional Hanseatic balance field")
    return None


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        decoded = urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except (IndexError, ValueError, UnicodeDecodeError):
        return {}


def _api_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    for pattern in (r"^\d{4}-\d{2}-\d{2}", r"^\d{1,2}\.\d{1,2}\.\d{4}"):
        match = re.match(pattern, value)
        if not match:
            continue
        fragment = match.group(0)
        try:
            if "." in fragment:
                return datetime.strptime(fragment, "%d.%m.%Y").date().isoformat()
            return date.fromisoformat(fragment).isoformat()
        except ValueError:
            return None
    return None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # Never forward Authorization to a redirect target.
        return None


def _http_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    form: bool,
) -> dict[str, Any]:
    if not path.startswith("/") or path.startswith("//") or "#" in path:
        raise HanseaticError("Invalid internal API path")
    request_headers = {
        "Accept": "application/json",
        "Accept-Language": "de-DE,de;q=0.9",
        "Origin": _PORTAL,
        "Referer": _PORTAL + "/",
        "User-Agent": _USER_AGENT,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8" if form else "application/json",
        **headers,
    }
    body = None
    if payload is not None:
        body = (urlencode(payload) if form else json.dumps(payload, separators=(",", ":"))).encode("utf-8")
    req = Request(_API_ROOT + path, data=body, headers=request_headers, method=method)
    try:
        with build_opener(_NoRedirect).open(req, timeout=DEFAULT_HTTP_TIMEOUT) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise HanseaticError("Hanseatic API response exceeds the safety limit")
    except HTTPError as exc:
        raise HanseaticHttpError(exc.code) from None
    except (URLError, TimeoutError, OSError) as exc:
        # Intentionally do not include request headers, body or exception URL.
        raise HanseaticError(f"Hanseatic bank connection failed ({type(exc).__name__})") from None
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise HanseaticError("Hanseatic Bank returned a non-JSON response") from None
    if not isinstance(parsed, dict):
        raise HanseaticError("Hanseatic Bank returned an unexpected response")
    if parsed.get("error") or parsed.get("error_code"):
        # No raw bank response: it may contain login or token details.
        raise HanseaticError("Hanseatic Bank API rejected the request")
    return parsed


RequestFn = Callable[
    [str, str, dict[str, Any] | None, dict[str, str], bool], dict[str, Any]
]


class HanseaticBankClient:
    """Online read-only API, cached bearer session, and explicit SCA handling."""

    def __init__(
        self,
        login_id: str,
        password: str,
        state_dir: Path,
        *,
        account_number: str | None = None,
        transaction_days: int = 0,
        auto_reauthenticate: bool = True,
        reauth_retry_hours: int = 12,
        sca_timeout_seconds: int = 90,
        sca_poll_seconds: float = 3,
        requester: RequestFn = _http_request,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.login_id = str(login_id).strip()
        self._password = password
        if not re.fullmatch(r"\d{10}", self.login_id):
            raise ValueError("Hanseatic login_id must contain exactly 10 digits")
        if not password:
            raise ValueError("Hanseatic password is empty")
        self.account_number = str(account_number).strip() if account_number else None
        self.transaction_days = transaction_days
        self.auto_reauthenticate = auto_reauthenticate
        self.reauth_retry_hours = reauth_retry_hours
        self.sca_timeout_seconds = sca_timeout_seconds
        self.sca_poll_seconds = sca_poll_seconds
        self._requester = requester
        self._sleep = sleeper
        self._clock = clock
        key = hashlib.sha256(self.login_id.encode("utf-8")).hexdigest()[:16]
        self._state_file = state_dir / f"hanseatic_session_{key}.json"
        self._access_token: str | None = None
        self._device_token: str | None = None
        self._retry_after: float = 0
        self._pending_sms: tuple[str, float] | None = None
        # Persist only bank-issued tokens and non-secret synchronization metadata.
        # The password is never written to the session file.
        self.last_successful_sync: str | None = None
        self.last_authenticated_at: str | None = None
        self.session_restored = False
        self.session_storage_error = False
        self._load_state()

    @property
    def has_cached_access_token(self) -> bool:
        """Whether a bearer token is cached (not proof of server validity)."""
        return bool(self._access_token)

    @property
    def has_saved_device_token(self) -> bool:
        return bool(self._device_token)

    @property
    def waiting_for_sms(self) -> bool:
        return bool(self._pending_sms and self._clock() < self._pending_sms[1])

    @property
    def retry_after(self) -> str | None:
        if self._retry_after <= self._clock():
            return None
        return datetime.fromtimestamp(self._retry_after, tz=timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _valid_timestamp(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed.isoformat(timespec="seconds")
        except ValueError:
            pass
        return None

    def _load_state(self) -> None:
        try:
            if not self._state_file.is_file():
                return
            state = json.loads(self._state_file.read_text("utf-8"))
            if not isinstance(state, dict):
                raise ValueError("Unexpected session data format")
            token = state.get("access_token")
            if isinstance(token, str) and token:
                self._access_token = token
            device = state.get("device_token")
            if isinstance(device, str) and device:
                self._device_token = device
            self._retry_after = float(state.get("retry_after") or 0)
            self.last_successful_sync = self._valid_timestamp(state.get("last_successful_sync"))
            self.last_authenticated_at = self._valid_timestamp(state.get("last_authenticated_at"))
            self.session_restored = self.has_cached_access_token or self.has_saved_device_token
            _LOGGER.info(
                "Hanseatic session restored from disk: bearer=%s, registered_device=%s",
                self.has_cached_access_token, self.has_saved_device_token,
            )
        except (OSError, ValueError, TypeError):
            self.session_storage_error = True
            _LOGGER.warning("Could not load saved Hanseatic session; new approval may be needed")

    def _save_state(self) -> None:
        folder = self._state_file.parent
        data = {
            "access_token": self._access_token,
            "device_token": self._device_token,
            "retry_after": self._retry_after,
            "last_successful_sync": self.last_successful_sync,
            "last_authenticated_at": self.last_authenticated_at,
        }
        temp_path: str | None = None
        try:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temp_path = tempfile.mkstemp(prefix="hanseatic_", suffix=".tmp", dir=folder)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(data, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._state_file)
            self.session_storage_error = False
            _LOGGER.debug("Saved Hanseatic bearer/device session state (tokens not logged)")
        except OSError as exc:
            # Keep the in-memory session usable but flag that a restart may need SCA.
            self.session_storage_error = True
            _LOGGER.error("Could not persist Hanseatic session (%s); restart may require a new login", type(exc).__name__)
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    _LOGGER.warning("Could not remove temporary Hanseatic session file")

    def _call(
        self, method: str, path: str, payload: dict[str, Any] | None = None,
        *, bearer: str | None = None, basic: bool = False, device_token: str | None = None,
        form: bool = False,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        if basic:
            headers["Authorization"] = _BASIC_AUTH
        if device_token:
            headers["devicetoken"] = device_token
        return self._requester(method, path, payload, headers, form)

    def _save_access_token(self, token: str) -> None:
        if not isinstance(token, str) or not token:
            raise HanseaticError("Hanseatic Bank did not provide an access token")
        self._access_token = token
        self.last_authenticated_at = datetime.fromtimestamp(
            self._clock(), tz=timezone.utc
        ).isoformat(timespec="seconds")
        self._retry_after = 0
        self._save_state()

    def _mark_successful_fetch(self, snapshot: HanseaticSnapshot) -> HanseaticSnapshot:
        self.last_successful_sync = datetime.fromtimestamp(
            self._clock(), tz=timezone.utc
        ).isoformat(timespec="seconds")
        self._save_state()
        return snapshot

    def _start_login(self) -> None:
        values = {
            "grant_type": "hbSCACustomPassword",
            "password": self._password,
            "loginId": self.login_id,
        }
        if self._device_token:
            try:
                response = self._call("POST", "/token", values, basic=True, form=True, device_token=self._device_token)
            except HanseaticHttpError as exc:
                if exc.status not in (401, 403):
                    # A server/network error does not invalidate a registered device.
                    raise
                self._device_token = None
                self._save_state()
                # Only an authentication rejection can justify removing the key.
                response = self._call("POST", "/token", values, basic=True, form=True)
        else:
            response = self._call("POST", "/token", values, basic=True, form=True)

        if response.get("access_token"):
            self._save_access_token(response["access_token"])
            return
        claims = _jwt_payload(response.get("id_token", ""))
        sca_id = claims.get("sca_id")
        sca_type = str(claims.get("sca_type") or "APP").upper()
        if not isinstance(sca_id, str) or not sca_id:
            raise HanseaticError("Login response has neither access token nor SCA challenge")

        if sca_type == "SMS":
            self._pending_sms = (sca_id, self._clock() + max(self.sca_timeout_seconds, 180))
            # A missed SMS must not trigger a new SMS at every hourly poll.
            # The explicit reauthenticate service bypasses this cooldown.
            self._retry_after = self._clock() + 3600 * self.reauth_retry_hours
            self._save_state()
            raise HanseaticOtpRequired(
                "SMS-TAN erforderlich: Dienst fints_own.hanseatic_submit_otp aufrufen"
            )

        self._complete_app_approval(sca_id)

    def _client_token(self) -> str:
        response = self._call(
            "POST", "/token",
            {"grant_type": "client_credentials", "client_id": _CLIENT_ID, "client_secret": _CLIENT_SECRET},
            form=True,
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            raise HanseaticError("Hanseatic Bank did not provide an SCA status token")
        return token

    def _complete_app_approval(self, sca_id: str) -> None:
        client_token = self._client_token()
        end = time.monotonic() + self.sca_timeout_seconds
        while True:
            response = self._call(
                "GET", "/openScaBroker/1.0/customer/"
                + quote(self.login_id, safe="") + "/status/" + quote(sca_id, safe=""),
                bearer=client_token,
            )
            status = str(response.get("status") or "").lower()
            if status == "complete":
                result = response.get("resultData")
                device_token = result.get("DEVICETOKEN") if isinstance(result, dict) else None
                if not isinstance(device_token, str) or not device_token:
                    device_token = self._device_token
                headers_device_token = device_token if device_token else None
                exchange = self._call(
                    "POST", "/token",
                    {"grant_type": "hbSCACustomPassword", "password": self._password, "loginId": self.login_id},
                    basic=True, form=True, device_token=headers_device_token,
                )
                token = exchange.get("access_token")
                if not token:
                    raise HanseaticError("Bank did not issue an access token after app approval")
                if device_token:
                    self._device_token = device_token
                self._save_access_token(token)
                return
            if status not in ("open", "pending", "accepted"):
                raise HanseaticApprovalRequired("App-Freigabe abgelehnt oder abgelaufen")
            if time.monotonic() + self.sca_poll_seconds >= end:
                raise HanseaticApprovalRequired("App-Freigabe nicht rechtzeitig bestätigt")
            self._sleep(self.sca_poll_seconds)

    def submit_otp(self, otp: str) -> None:
        if not self.waiting_for_sms:
            self._pending_sms = None
            raise HanseaticOtpRequired("Keine gültige SMS-TAN-Anfrage. Anmeldung erneut auslösen")
        clean = otp.strip()
        if not re.fullmatch(r"[0-9]{4,12}", clean):
            raise HanseaticError("Die SMS-TAN hat ein ungültiges Format")
        sca_id = self._pending_sms[0]
        response = self._call(
            "POST", "/token",
            {"grant_type": "hbSCACustomPassword", "loginId": self.login_id, "otp": clean, "scaId": sca_id},
            basic=True, form=True,
        )
        token = response.get("access_token")
        if not token:
            raise HanseaticError("SMS-TAN abgelehnt oder abgelaufen")
        self._pending_sms = None
        self._save_access_token(token)

    def _customer_data(self) -> dict[str, Any]:
        if not self._access_token:
            raise HanseaticApprovalRequired("Keine Hanseatic-Bank-Sitzung vorhanden")
        response = self._call(
            "POST", "/customerinfo/1.0/initCustomer",
            {"initiator": "MHB", "language": "de"}, bearer=self._access_token,
        )
        accounts = response.get("accounts")
        if not isinstance(accounts, dict):
            raise HanseaticError("Hanseatic Bank returned no account information")
        return accounts

    def _select_account(self, accounts: dict[str, Any]) -> tuple[dict[str, Any], str]:
        account_types = (
            ("creditAccounts", "credit_card"),
            ("overnightAccounts", "savings"),
            ("loanAccounts", "loan"),
        )
        if self.account_number:
            for group_name, label in account_types:
                for account in accounts.get(group_name) or []:
                    if isinstance(account, dict) and str(account.get("accountNumber", "")) == self.account_number:
                        return account, label
            raise HanseaticError("Configured Hanseatic account_number was not found")
        for group_name, label in account_types:
            group = [a for a in accounts.get(group_name) or [] if isinstance(a, dict)]
            if len(group) == 1:
                return group[0], label
            if len(group) > 1:
                raise HanseaticError("Multiple Hanseatic accounts found; please configure account_number")
        raise HanseaticError("No Hanseatic accounts found")

    def _transactions(self, account_number: str) -> tuple[list[dict[str, Any]], str | None, bool]:
        if self.transaction_days <= 0:
            return [], None, False
        start_date = date.today() - timedelta(days=self.transaction_days)
        result: list[dict[str, Any]] = []
        more_with_sca = False
        error: str | None = None
        seen: set[tuple[Any, ...]] = set()
        for page in range(1, MAX_TRANSACTION_PAGES + 1):
            try:
                body = self._call(
                    "GET", "/transaction/1.0/transactionsEnriched/"
                    + quote(account_number, safe="")
                    + f"?page={page}&withReservations=true&withEnrichments=true",
                    bearer=self._access_token,
                )
                rows = body.get("transactions") or []
                if not isinstance(rows, list):
                    raise HanseaticError("Unexpected transactions response")
                found_recent = False
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    when = _api_date(raw.get("transactionDate") or raw.get("transactionDateTime"))
                    if not when:
                        continue
                    if date.fromisoformat(when) < start_date:
                        continue
                    found_recent = True
                    try:
                        amount = _money(raw.get("amount"))
                    except ValueError:
                        continue
                    merchant = raw.get("merchantData")
                    if not isinstance(merchant, dict):
                        merchant = {}
                    purpose = str(raw.get("description") or "")
                    if raw.get("city"):
                        purpose += (" " if purpose else "") + f"[{raw['city']}]"
                    name = str(raw.get("merchantName") or raw.get("recipientName") or merchant.get("name") or "")
                    identifier = (when, amount, name, purpose, str(raw.get("id") or ""))
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    result.append({
                        "date": when,
                        "amount": amount,
                        "currency": "EUR",
                        "direction": "income" if amount >= 0 else "expense",
                        "name": name[:300],
                        "purpose": purpose[:500],
                        "booked": raw.get("booked") is not False,
                    })
                more_with_sca = more_with_sca or body.get("moreWithSCA") is True
                if body.get("more") is not True or (rows and not found_recent):
                    break
            except HanseaticError as exc:
                error = str(exc)
                break
        result.sort(key=lambda tx: tx["date"], reverse=True)
        return result, error, more_with_sca

    def _read_snapshot(self) -> HanseaticSnapshot:
        accounts = self._customer_data()
        account, account_type = self._select_account(accounts)
        number = str(account.get("accountNumber") or "")
        if not number:
            raise HanseaticError("Bank account has no account number")
        try:
            balance = _money(account.get("saldo"))
        except ValueError as exc:
            raise HanseaticError(str(exc)) from None
        card_details = account.get("creditcard")
        if not isinstance(card_details, dict):
            card_details = {}
        transactions, transaction_error, history_more = self._transactions(number)
        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        return HanseaticSnapshot(
            account_number=number,
            account_label=str(account.get("productLabel") or "Hanseatic Bank"),
            account_type=account_type,
            balance=balance,
            currency="EUR",
            retrieved_at=now,
            available_credit=_optional_money(account.get("availableAmount"), account.get("availableCredit"), account.get("availableLimit"), card_details.get("availableAmount")),
            credit_limit=_optional_money(account.get("creditLimit"), account.get("creditLine"), account.get("limit"), card_details.get("creditLimit")),
            transactions=transactions,
            period_start=(date.today() - timedelta(days=self.transaction_days)).isoformat() if self.transaction_days else None,
            period_end=date.today().isoformat() if self.transaction_days else None,
            transaction_error=transaction_error,
            history_requires_approval=history_more,
        )

    def fetch(self, *, force_reauthenticate: bool = False) -> HanseaticSnapshot:
        """Poll bank directly. Reuse existing bearer token; log in when necessary."""
        if self._access_token and not force_reauthenticate:
            try:
                return self._mark_successful_fetch(self._read_snapshot())
            except HanseaticHttpError as exc:
                if exc.status not in (401, 403):
                    raise
                self._access_token = None
                self._save_state()
            except HanseaticError:
                # Non-auth errors must not trigger a new SCA push.
                raise

        if self.waiting_for_sms and not force_reauthenticate:
            raise HanseaticOtpRequired("SMS-TAN ausstehend: fints_own.hanseatic_submit_otp")
        if self._pending_sms:
            # The explicit reauthenticate service may replace a pending SMS challenge.
            self._pending_sms = None
        if not self.auto_reauthenticate and not force_reauthenticate:
            raise HanseaticApprovalRequired("Keine gültige Sitzung; fints_own.hanseatic_reauthenticate aufrufen")
        if self._retry_after > self._clock() and not force_reauthenticate:
            raise HanseaticApprovalRequired("Freigabe erforderlich; erneuter Anmeldeversuch nach " + (self.retry_after or ""))
        try:
            self._start_login()
        except HanseaticOtpRequired:
            # SMS can be entered via the HA service, no retry cooldown now.
            raise
        except (HanseaticError, ValueError):
            self._retry_after = self._clock() + 3600 * self.reauth_retry_hours
            self._save_state()
            raise
        try:
            return self._mark_successful_fetch(self._read_snapshot())
        except HanseaticHttpError as exc:
            if exc.status in (401, 403):
                self._access_token = None
                self._retry_after = self._clock() + 3600 * self.reauth_retry_hours
                self._save_state()
            raise
