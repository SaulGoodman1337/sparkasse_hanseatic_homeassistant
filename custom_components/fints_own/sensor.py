"""FinTS SEPA and online Hanseatic Bank sensors for Home Assistant.

Creates one main sensor per configured FinTS user.

On update, the main user sensor:
- loads the persisted python-fints client state
- opens one FinTS dialog
- discovers all available SEPA accounts via get_sepa_accounts()
- fetches the balance for each SEPA account
- optionally fetches transactions per SEPA account
- updates dynamically created child account sensors
- exposes the total balance and aggregated transaction data
- updates monitoring sensors per user
- enforces a Home Assistant update timeout around the whole FinTS sync

The child account sensors do not poll the bank themselves.
They are updated only by the main user sensor.

The FinTS backend intentionally does not use credit cards / UPD / get_information().
The Hanseatic backend uses Meine Hanseatic Bank private JSON API with app/SMS SCA.

transaction_days:
- user-level default: users[].transaction_days
- account-level override: users[].accounts[].transaction_days
- 0 = do not fetch transactions
- 1..90 = fetch transactions without expected additional TAN for this bank setup
- >90 = allowed, but may trigger additional pushTAN depending on bank policy

sync_timeout_seconds:
- max runtime for one full user sync from the Home Assistant sensor perspective
- if exceeded, the sensor update returns with an error
- a stuck underlying worker thread may continue until the bank/network call returns
- the next manual/automatic sync is allowed to start again
"""

from __future__ import annotations

from collections import namedtuple
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from functools import partial
from datetime import date, datetime, timedelta
import hashlib
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable, cast

from fints.client import FinTS3PinTanClient, NeedTANResponse
from fints.exceptions import FinTSClientError
from fints.models import SEPAAccount
import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    SensorEntity,
)
from homeassistant.const import CONF_NAME, CONF_PIN, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .hanseatic import (
    HanseaticBankClient, HanseaticSnapshot, HanseaticError,
    HanseaticApprovalRequired, HanseaticOtpRequired,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=1)
ICON = "mdi:bank"

BankCredentials = namedtuple("BankCredentials", "blz login pin url product_id")

CONF_USERS = "users"
CONF_BIN = "bank_identification_number"
CONF_PRODUCT_ID = "product_id"
CONF_TRANSACTION_DAYS = "transaction_days"
CONF_ACCOUNTS = "accounts"
CONF_ACCOUNT = "account"
CONF_SYNC_TIMEOUT_SECONDS = "sync_timeout_seconds"
CONF_PROVIDER = "provider"
CONF_LOGIN_ID = "login_id"
CONF_PASSWORD = "password"
CONF_ACCOUNT_NUMBER = "account_number"
CONF_AUTO_REAUTHENTICATE = "auto_reauthenticate"
CONF_REAUTH_RETRY_HOURS = "reauth_retry_hours"
CONF_SCA_TIMEOUT_SECONDS = "sca_timeout_seconds"
PROVIDER_FINTS = "fints"
PROVIDER_HANSEATIC = "hanseatic"
HANSEATIC_TRANSACTION_DAYS = 0
HANSEATIC_MAX_ATTRIBUTE_TRANSACTIONS = 15

ATTR_USER = "user"
ATTR_ACCOUNT = "account"
ATTR_IBAN = "iban"
ATTR_ACCOUNT_NUMBER = "account_number"
ATTR_ACCOUNT_TYPE = "account_type"
ATTR_ACCOUNT_COUNT = "account_count"
ATTR_ACCOUNTS = "accounts"
ATTR_LAST_UPDATE_STATUS = "last_update_status"

DEFAULT_TRANSACTION_DAYS = 0
TRANSACTION_DAYS_EXTRA_TAN_THRESHOLD = 90
MAX_TRANSACTION_DAYS = 365

DEFAULT_SYNC_TIMEOUT_SECONDS = 240
MIN_SYNC_TIMEOUT_SECONDS = 30
MAX_SYNC_TIMEOUT_SECONDS = 1800

PUSHTAN_WAIT_SECONDS = 35
CLIENT_STATE_DIR = Path("/config/.fints_own")


class FinTsSyncTimeoutError(TimeoutError):
    """Raised when a FinTS sync exceeds the configured timeout."""


SCHEMA_ACCOUNT = vol.Schema(
    {
        vol.Required(CONF_ACCOUNT): str,
        vol.Optional(CONF_TRANSACTION_DAYS): vol.All(
            vol.Coerce(int),
            vol.Range(min=0, max=MAX_TRANSACTION_DAYS),
        ),
    }
)

SCHEMA_FINTS_USER = vol.Schema(
    {
        vol.Optional(CONF_PROVIDER, default=PROVIDER_FINTS): vol.In([PROVIDER_FINTS]),
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_BIN): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PIN): str,
        vol.Required(CONF_URL): str,
        vol.Optional(CONF_PRODUCT_ID): str,
        vol.Optional(CONF_TRANSACTION_DAYS, default=DEFAULT_TRANSACTION_DAYS): vol.All(
            vol.Coerce(int),
            vol.Range(min=0, max=MAX_TRANSACTION_DAYS),
        ),
        vol.Optional(CONF_SYNC_TIMEOUT_SECONDS, default=DEFAULT_SYNC_TIMEOUT_SECONDS): vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_SYNC_TIMEOUT_SECONDS, max=MAX_SYNC_TIMEOUT_SECONDS),
        ),
        vol.Optional(CONF_ACCOUNTS, default=[]): vol.All(list, [SCHEMA_ACCOUNT]),
    }
)

SCHEMA_HANSEATIC_USER = vol.Schema(
    {
        vol.Required(CONF_PROVIDER): vol.In([PROVIDER_HANSEATIC]),
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_LOGIN_ID): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_ACCOUNT_NUMBER): str,
        vol.Optional(CONF_TRANSACTION_DAYS, default=HANSEATIC_TRANSACTION_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_TRANSACTION_DAYS),
        ),
        vol.Optional(CONF_AUTO_REAUTHENTICATE, default=True): bool,
        vol.Optional(CONF_REAUTH_RETRY_HOURS, default=12): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=72),
        ),
        vol.Optional(CONF_SCA_TIMEOUT_SECONDS, default=90): vol.All(
            vol.Coerce(int), vol.Range(min=30, max=180),
        ),
    }
)


def _validate_user_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise vol.Invalid("each user must be a mapping")
    provider = value.get(CONF_PROVIDER, PROVIDER_FINTS)
    if provider == PROVIDER_FINTS:
        return SCHEMA_FINTS_USER(value)
    if provider == PROVIDER_HANSEATIC:
        return SCHEMA_HANSEATIC_USER(value)
    raise vol.Invalid(f"Unknown bank provider: {provider}")


PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERS): vol.All(list, [_validate_user_config]),
    }
)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_identifier(value: Any) -> str:
    return str(value or "").replace(" ", "").strip().upper()


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = value.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_") or "unknown"


def _safe_account_label(account: SEPAAccount) -> str:
    accountnumber = getattr(account, "accountnumber", None)
    iban = getattr(account, "iban", None)

    if accountnumber:
        return str(accountnumber)

    if iban:
        iban_str = str(iban)
        if len(iban_str) > 8:
            return f"{iban_str[:4]}...{iban_str[-4:]}"
        return iban_str

    return "unknown_account"


def _account_unique_key(account: SEPAAccount) -> str:
    iban = _normalize_identifier(getattr(account, "iban", None))
    accountnumber = _normalize_identifier(getattr(account, "accountnumber", None))
    subaccount = _normalize_identifier(getattr(account, "subaccount", None))
    blz = _normalize_identifier(getattr(account, "blz", None))

    if iban:
        return iban

    raw = "|".join([blz, accountnumber, subaccount])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _account_identifier_candidates(account: SEPAAccount) -> set[str]:
    values = {
        getattr(account, "iban", None),
        getattr(account, "accountnumber", None),
        getattr(account, "subaccount", None),
        _account_unique_key(account),
    }
    return {_normalize_identifier(value) for value in values if _normalize_identifier(value)}


def _format_account_debug(account: SEPAAccount) -> str:
    return (
        f"iban={getattr(account, 'iban', None)} "
        f"accountnumber={getattr(account, 'accountnumber', None)} "
        f"subaccount={getattr(account, 'subaccount', None)} "
        f"blz={getattr(account, 'blz', None)}"
    )


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value))
    except Exception:
        return None


def _date_to_iso(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date().isoformat()

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

        try:
            return datetime.fromisoformat(value).date().isoformat()
        except Exception:
            return value

    return str(value)


def _safe_str(value: Any, max_len: int = 500) -> str | None:
    if value is None:
        return None

    try:
        text = str(value)
    except Exception:
        return None

    text = text.strip()

    if not text:
        return None

    if len(text) > max_len:
        return text[:max_len]

    return text


def _extract_transaction_data(tx: Any) -> dict[str, Any] | None:
    data = getattr(tx, "data", None)

    if not isinstance(data, dict):
        return None

    amount_obj = data.get("amount")

    if amount_obj is None:
        return None

    amount = _as_float(getattr(amount_obj, "amount", None))

    if amount is None:
        return None

    currency = _safe_str(getattr(amount_obj, "currency", None), 20) or "EUR"

    booking_date = (
        data.get("date")
        or data.get("booking_date")
        or data.get("bdate")
        or data.get("entry_date")
    )
    valuta_date = (
        data.get("valutadate")
        or data.get("valuta_date")
        or data.get("value_date")
    )

    purpose = _safe_str(data.get("purpose"), 1000)
    applicant_name = _safe_str(data.get("applicant_name"), 300)
    applicant_iban = _safe_str(data.get("applicant_iban"), 100)
    applicant_bin = _safe_str(data.get("applicant_bin"), 100)

    posting_text = (
        _safe_str(data.get("posting_text"), 300)
        or _safe_str(data.get("transaction_type"), 300)
        or _safe_str(data.get("type"), 300)
    )

    customer_reference = _safe_str(data.get("customer_reference"), 300)
    end_to_end_reference = _safe_str(data.get("end_to_end_reference"), 300)
    prima_nota = _safe_str(data.get("prima_nota"), 100)

    return {
        "date": _date_to_iso(booking_date),
        "valuta_date": _date_to_iso(valuta_date),
        "amount": amount,
        "currency": currency,
        "direction": "income" if amount >= 0 else "expense",
        "name": applicant_name,
        "applicant_iban": applicant_iban,
        "applicant_bin": applicant_bin,
        "purpose": purpose,
        "posting_text": posting_text,
        "customer_reference": customer_reference,
        "end_to_end_reference": end_to_end_reference,
        "prima_nota": prima_nota,
    }


def _transaction_sort_key(tx: dict[str, Any]) -> str:
    return str(tx.get("date") or tx.get("valuta_date") or "")


def _summarize_transactions(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    income_total = 0.0
    expense_total = 0.0
    net_total = 0.0

    for tx in transactions:
        amount = _as_float(tx.get("amount"))

        if amount is None:
            continue

        net_total += amount

        if amount >= 0:
            income_total += amount
        else:
            expense_total += abs(amount)

    return {
        "transaction_count": len(transactions),
        "income_total": round(income_total, 2),
        "expense_total": round(expense_total, 2),
        "net_total": round(net_total, 2),
    }


def _run_with_timeout(
    func: Callable[[], Any],
    timeout_seconds: int,
    description: str,
) -> Any:
    executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="fints_own_sync",
    )
    future = executor.submit(func)

    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as e:
        future.cancel()
        raise FinTsSyncTimeoutError(
            f"{description} exceeded timeout of {timeout_seconds} seconds. "
            "The Home Assistant update was aborted. "
            "A stuck FinTS worker may still finish in the background, "
            "but it will not block the next sync attempt."
        ) from e
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


@dataclass
class SyncState:
    user_name: str
    last_successful_sync: str | None = None
    last_sync_started: str | None = None
    last_sync_finished: str | None = None
    last_sync_status: str = "not_updated"
    waiting_for_pushtan: bool = False
    last_error: str | None = None


@dataclass
class AccountConfig:
    identifier: str
    transaction_days: int | None = None


@dataclass
class AccountBalanceSnapshot:
    account_key: str
    user_name: str
    account: SEPAAccount
    account_label: str
    amount: float | None
    currency: str | None
    available: bool
    transaction_days: int = 0
    transaction_period_start: str | None = None
    transaction_period_end: str | None = None
    transaction_count: int = 0
    income_total: float = 0.0
    expense_total: float = 0.0
    net_total: float = 0.0
    transactions: list[dict[str, Any]] = field(default_factory=list)
    transaction_error: str | None = None
    error: str | None = None


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    fints_entities: list[SensorEntity] = []
    hanseatic_entities: list[SensorEntity] = []
    managers: dict[str, HanseaticBankSensorManager] = hass.data.setdefault(
        "fints_own_hanseatic_managers", {}
    )

    for user_config in config[CONF_USERS]:
        user_name = cast(str, user_config[CONF_NAME])

        if user_config.get(CONF_PROVIDER, PROVIDER_FINTS) == PROVIDER_HANSEATIC:
            if user_name in managers:
                raise vol.Invalid(f"Duplicate Hanseatic user name: {user_name}")
            client = HanseaticBankClient(
                login_id=str(user_config[CONF_LOGIN_ID]),
                password=str(user_config[CONF_PASSWORD]),
                state_dir=CLIENT_STATE_DIR,
                account_number=user_config.get(CONF_ACCOUNT_NUMBER),
                transaction_days=int(user_config.get(CONF_TRANSACTION_DAYS, HANSEATIC_TRANSACTION_DAYS)),
                auto_reauthenticate=bool(user_config.get(CONF_AUTO_REAUTHENTICATE, True)),
                reauth_retry_hours=int(user_config.get(CONF_REAUTH_RETRY_HOURS, 12)),
                sca_timeout_seconds=int(user_config.get(CONF_SCA_TIMEOUT_SECONDS, 90)),
            )
            manager = HanseaticBankSensorManager(user_name, client)
            managers[user_name] = manager
            hanseatic_entities.extend(manager.sensors)
            _LOGGER.info("Configured Hanseatic online bank provider for '%s'.", user_name)
            continue

        user_transaction_days = int(user_config.get(CONF_TRANSACTION_DAYS, DEFAULT_TRANSACTION_DAYS))
        sync_timeout_seconds = int(
            user_config.get(CONF_SYNC_TIMEOUT_SECONDS, DEFAULT_SYNC_TIMEOUT_SECONDS)
        )

        account_configs: list[AccountConfig] = []
        for account_entry in user_config.get(CONF_ACCOUNTS, []):
            account_configs.append(
                AccountConfig(
                    identifier=str(account_entry[CONF_ACCOUNT]),
                    transaction_days=(
                        int(account_entry[CONF_TRANSACTION_DAYS])
                        if CONF_TRANSACTION_DAYS in account_entry
                        else None
                    ),
                )
            )

        credentials = BankCredentials(
            user_config[CONF_BIN],
            user_config[CONF_USERNAME],
            user_config[CONF_PIN],
            user_config[CONF_URL],
            user_config.get(CONF_PRODUCT_ID, None),
        )

        client = FinTsClient(
            credentials=credentials,
            user_name=user_name,
            user_transaction_days=user_transaction_days,
            account_configs=account_configs,
            sync_timeout_seconds=sync_timeout_seconds,
        )

        manager = FinTsUserSensorManager(
            user_name=user_name,
            client=client,
            add_entities=add_entities,
        )
        client.set_status_callback(manager.update_sync_state)
        fints_entities.append(manager.main_sensor)
        fints_entities.extend(manager.monitoring_sensors)
        _LOGGER.info("Configured FinTS user sensor for '%s'.", user_name)

    # Preserve the original FinTS startup behavior (no bank call on setup).
    if fints_entities:
        add_entities(fints_entities, False)

    if hanseatic_entities:
        _register_hanseatic_services(hass, managers)
        # Avoid blocking HA platform setup while the bank waits for App-SCA.
        # The main sensor starts its initial refresh after it has been added.
        add_entities(hanseatic_entities, False)


def _register_hanseatic_services(
    hass: HomeAssistant, managers: dict[str, "HanseaticBankSensorManager"]
) -> None:
    domain = "fints_own"
    if not hass.services.has_service(domain, "hanseatic_submit_otp"):
        def submit_otp(call: Any) -> None:
            manager = managers.get(call.data[CONF_NAME])
            if manager is None:
                raise ValueError("Unknown Hanseatic Bank user name")
            manager.submit_otp(call.data["otp"])

        hass.services.register(
            domain, "hanseatic_submit_otp", submit_otp,
            schema=vol.Schema({vol.Required(CONF_NAME): str, vol.Required("otp"): str}),
        )

    if not hass.services.has_service(domain, "hanseatic_reauthenticate"):
        def reauthenticate(call: Any) -> None:
            manager = managers.get(call.data[CONF_NAME])
            if manager is None:
                raise ValueError("Unknown Hanseatic Bank user name")
            manager.refresh(force_reauthenticate=True)

        hass.services.register(
            domain, "hanseatic_reauthenticate", reauthenticate,
            schema=vol.Schema({vol.Required(CONF_NAME): str}),
        )


class FinTsClient:
    def __init__(
        self,
        credentials: BankCredentials,
        user_name: str,
        user_transaction_days: int,
        account_configs: list[AccountConfig],
        sync_timeout_seconds: int,
    ) -> None:
        self._credentials = credentials
        self.user_name = user_name
        self.user_transaction_days = user_transaction_days
        self.account_configs = account_configs
        self.sync_timeout_seconds = sync_timeout_seconds
        self._status_callback: Callable[..., None] | None = None

        state_key_raw = "|".join(
            [
                str(credentials.blz),
                str(credentials.login),
                str(credentials.url),
                str(credentials.product_id or ""),
            ]
        )
        state_key = hashlib.sha256(state_key_raw.encode("utf-8")).hexdigest()[:16]
        self._state_file = CLIENT_STATE_DIR / f"client_state_{state_key}.bin"

    def set_status_callback(self, callback: Callable[..., None]) -> None:
        self._status_callback = callback

    def _publish_status(
        self,
        status: str | None = None,
        waiting_for_pushtan: bool | None = None,
        last_error: str | None = None,
    ) -> None:
        if self._status_callback is None:
            return

        try:
            self._status_callback(
                status=status,
                waiting_for_pushtan=waiting_for_pushtan,
                last_error=last_error,
            )
        except Exception as e:
            _LOGGER.error(
                "Failed to publish FinTS sync status for user '%s': %s",
                self.user_name,
                e,
            )

    def _transaction_days_for_account(self, account: SEPAAccount) -> int:
        candidates = _account_identifier_candidates(account)

        for account_config in self.account_configs:
            configured = _normalize_identifier(account_config.identifier)

            if configured in candidates and account_config.transaction_days is not None:
                return int(account_config.transaction_days)

        return int(self.user_transaction_days)

    def _load_client_state(self) -> bytes | None:
        try:
            if self._state_file.exists():
                data = self._state_file.read_bytes()

                if data:
                    _LOGGER.warning(
                        "Loaded persisted FinTS client state for user '%s' from %s (%s bytes).",
                        self.user_name,
                        self._state_file,
                        len(data),
                    )
                    return data

                _LOGGER.warning(
                    "FinTS client state file exists but is empty for user '%s': %s",
                    self.user_name,
                    self._state_file,
                )
        except Exception as e:
            _LOGGER.error(
                "Failed to load persisted FinTS client state for user '%s' from %s: %s",
                self.user_name,
                self._state_file,
                e,
            )

        return None

    def _save_client_state(self, client: FinTS3PinTanClient) -> None:
        try:
            CLIENT_STATE_DIR.mkdir(parents=True, exist_ok=True)

            data = client.deconstruct(including_private=True)

            if not data:
                _LOGGER.warning(
                    "FinTS client deconstruct() returned empty state for user '%s'.",
                    self.user_name,
                )
                return

            tmp_file = self._state_file.with_suffix(".tmp")
            tmp_file.write_bytes(data)
            tmp_file.replace(self._state_file)

            _LOGGER.warning(
                "Saved persisted FinTS client state for user '%s' to %s (%s bytes).",
                self.user_name,
                self._state_file,
                len(data),
            )
        except Exception as e:
            _LOGGER.error(
                "Failed to save persisted FinTS client state for user '%s' to %s: %s",
                self.user_name,
                self._state_file,
                e,
            )

    def _create_client(self) -> FinTS3PinTanClient:
        from_data = self._load_client_state()

        if from_data:
            _LOGGER.warning(
                "Creating FinTS client with persisted client state for user '%s'.",
                self.user_name,
            )
        else:
            _LOGGER.warning(
                "Creating FinTS client without persisted client state for user '%s'.",
                self.user_name,
            )

        return FinTS3PinTanClient(
            self._credentials.blz,
            self._credentials.login,
            self._credentials.pin,
            self._credentials.url,
            product_id=self._credentials.product_id,
            from_data=from_data,
        )

    def _handle_push_tan_once(
        self,
        bank: FinTS3PinTanClient,
        result: Any,
        operation: str,
        wait_seconds: int = PUSHTAN_WAIT_SECONDS,
    ) -> Any:
        if not isinstance(result, NeedTANResponse):
            return result

        challenge = getattr(result, "challenge", None)
        decoupled = bool(getattr(result, "decoupled", False))

        _LOGGER.warning(
            "FinTS TAN required for user '%s', operation %s. decoupled=%s challenge=%s. "
            "Waiting %s seconds for app approval.",
            self.user_name,
            operation,
            decoupled,
            challenge,
            wait_seconds,
        )

        self._publish_status(
            status="waiting_for_pushtan",
            waiting_for_pushtan=True,
            last_error=None,
        )

        if not decoupled:
            error_message = (
                f"FinTS operation '{operation}' for user '{self.user_name}' requires a TAN, "
                "but this TAN method is not decoupled/pushTAN."
            )
            self._publish_status(
                status="error",
                waiting_for_pushtan=False,
                last_error=error_message,
            )
            raise RuntimeError(error_message)

        try:
            time.sleep(wait_seconds)

            confirmed_result = bank.send_tan(result, "")

            _LOGGER.warning(
                "FinTS pushTAN confirmation returned for user '%s', operation %s: %s",
                self.user_name,
                operation,
                type(confirmed_result).__name__,
            )

            self._publish_status(
                status="running",
                waiting_for_pushtan=False,
                last_error=None,
            )

            return confirmed_result

        except FinTSClientError as e:
            _LOGGER.error(
                "FinTS pushTAN confirmation failed for user '%s', operation %s after waiting %s seconds: %s",
                self.user_name,
                operation,
                wait_seconds,
                e,
            )
            self._publish_status(
                status="error",
                waiting_for_pushtan=False,
                last_error=str(e),
            )
            raise

        except Exception as e:
            self._publish_status(
                status="error",
                waiting_for_pushtan=False,
                last_error=str(e),
            )
            raise

    def _run_with_tan(
        self,
        operation: str,
        func: Callable[[FinTS3PinTanClient], Any],
    ) -> Any:
        bank = self._create_client()

        try:
            with bank:
                init_tan_response = getattr(bank, "init_tan_response", None)

                if isinstance(init_tan_response, NeedTANResponse):
                    self._handle_push_tan_once(
                        bank,
                        init_tan_response,
                        "dialog initialization",
                    )

                result = func(bank)

                if isinstance(result, NeedTANResponse):
                    result = self._handle_push_tan_once(
                        bank,
                        result,
                        operation,
                    )

                return result

        finally:
            self._publish_status(waiting_for_pushtan=False)
            self._save_client_state(bank)

    def _fetch_transactions_for_account(
        self,
        bank: FinTS3PinTanClient,
        account: SEPAAccount,
        account_label: str,
        transaction_days: int,
        start_date: date,
        end_date: date,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if transaction_days <= 0:
            return [], None

        if transaction_days > TRANSACTION_DAYS_EXTRA_TAN_THRESHOLD:
            _LOGGER.warning(
                "Fetching %s transaction days for user '%s', account '%s'. "
                "This is above %s days and may trigger additional pushTAN.",
                transaction_days,
                self.user_name,
                account_label,
                TRANSACTION_DAYS_EXTRA_TAN_THRESHOLD,
            )

        try:
            transactions_raw = bank.get_transactions(
                account,
                start_date,
                end_date,
                True,
            )

            if isinstance(transactions_raw, NeedTANResponse):
                transactions_raw = self._handle_push_tan_once(
                    bank,
                    transactions_raw,
                    f"get_transactions {account_label}",
                )

            if transactions_raw is None:
                return [], "bank.get_transactions() returned None"

            parsed_transactions: list[dict[str, Any]] = []

            for tx in transactions_raw:
                parsed = _extract_transaction_data(tx)

                if parsed is not None:
                    parsed_transactions.append(parsed)

            parsed_transactions.sort(
                key=_transaction_sort_key,
                reverse=True,
            )

            return parsed_transactions, None

        except Exception as e:
            _LOGGER.error(
                "FinTS transaction fetch failed for user '%s', account %s: %s",
                self.user_name,
                _format_account_debug(account),
                e,
            )
            return [], str(e)

    def get_all_account_balances(self) -> list[AccountBalanceSnapshot]:
        def operation(bank: FinTS3PinTanClient) -> list[AccountBalanceSnapshot]:
            accounts = bank.get_sepa_accounts()

            if isinstance(accounts, NeedTANResponse):
                accounts = self._handle_push_tan_once(
                    bank,
                    accounts,
                    "get_sepa_accounts",
                )

            if accounts is None:
                raise RuntimeError("bank.get_sepa_accounts() returned None.")

            if not accounts:
                _LOGGER.warning(
                    "Bank returned no SEPA accounts for user '%s'.",
                    self.user_name,
                )
                return []

            _LOGGER.warning(
                "FinTS returned %s SEPA accounts for user '%s'.",
                len(accounts),
                self.user_name,
            )

            today = date.today()
            snapshots: list[AccountBalanceSnapshot] = []

            for account in accounts:
                account_key = _account_unique_key(account)
                account_label = _safe_account_label(account)
                transaction_days = self._transaction_days_for_account(account)

                if transaction_days > 0:
                    start_date = today - timedelta(days=transaction_days)
                    end_date = today
                    transaction_period_start = start_date.isoformat()
                    transaction_period_end = end_date.isoformat()
                else:
                    start_date = today
                    end_date = today
                    transaction_period_start = None
                    transaction_period_end = None

                _LOGGER.warning(
                    "Available FinTS SEPA account for user '%s': %s transaction_days=%s",
                    self.user_name,
                    _format_account_debug(account),
                    transaction_days,
                )

                amount: float | None = None
                currency: str | None = None
                available = False
                balance_error: str | None = None

                try:
                    balance = bank.get_balance(account)

                    if isinstance(balance, NeedTANResponse):
                        balance = self._handle_push_tan_once(
                            bank,
                            balance,
                            f"get_balance {account_label}",
                        )

                    if balance is None:
                        balance_error = "bank.get_balance() returned None"
                    else:
                        amount_obj = getattr(balance, "amount", None)

                        if amount_obj is None:
                            balance_error = f"balance has no amount attribute: {balance!r}"
                        else:
                            amount = float(str(amount_obj.amount))
                            currency = str(amount_obj.currency)
                            available = True

                except Exception as e:
                    _LOGGER.error(
                        "FinTS balance fetch failed for user '%s', account %s: %s",
                        self.user_name,
                        _format_account_debug(account),
                        e,
                    )
                    balance_error = str(e)

                if transaction_days > 0:
                    transactions, transaction_error = self._fetch_transactions_for_account(
                        bank=bank,
                        account=account,
                        account_label=account_label,
                        transaction_days=transaction_days,
                        start_date=start_date,
                        end_date=end_date,
                    )
                else:
                    transactions = []
                    transaction_error = None

                transaction_summary = _summarize_transactions(transactions)

                snapshots.append(
                    AccountBalanceSnapshot(
                        account_key=account_key,
                        user_name=self.user_name,
                        account=account,
                        account_label=account_label,
                        amount=amount,
                        currency=currency,
                        available=available,
                        transaction_days=transaction_days,
                        transaction_period_start=transaction_period_start,
                        transaction_period_end=transaction_period_end,
                        transaction_count=transaction_summary["transaction_count"],
                        income_total=transaction_summary["income_total"],
                        expense_total=transaction_summary["expense_total"],
                        net_total=transaction_summary["net_total"],
                        transactions=transactions,
                        transaction_error=transaction_error,
                        error=balance_error,
                    )
                )

            return snapshots

        return self._run_with_tan(
            "get_all_account_balances",
            operation,
        )


class FinTsUserSensorManager:
    def __init__(
        self,
        user_name: str,
        client: FinTsClient,
        add_entities: AddEntitiesCallback,
    ) -> None:
        self.user_name = user_name
        self.client = client
        self._add_entities = add_entities
        self._account_sensors: dict[str, FinTsAccountBalanceSensor] = {}
        self.sync_state = SyncState(user_name=user_name)

        self.main_sensor = FinTsUserTotalBalanceSensor(
            manager=self,
            user_name=user_name,
            client=client,
        )

        self.last_successful_sync_sensor = FinTsLastSuccessfulSyncSensor(
            manager=self,
            user_name=user_name,
        )
        self.last_sync_status_sensor = FinTsLastSyncStatusSensor(
            manager=self,
            user_name=user_name,
        )
        self.waiting_for_pushtan_sensor = FinTsWaitingForPushTanSensor(
            manager=self,
            user_name=user_name,
        )

        self.monitoring_sensors: list[SensorEntity] = [
            self.last_successful_sync_sensor,
            self.last_sync_status_sensor,
            self.waiting_for_pushtan_sensor,
        ]

    def update_sync_state(
        self,
        status: str | None = None,
        waiting_for_pushtan: bool | None = None,
        last_error: str | None = None,
        mark_started: bool = False,
        mark_finished: bool = False,
        mark_success: bool = False,
    ) -> None:
        if mark_started:
            self.sync_state.last_sync_started = _now_iso()

        if mark_finished:
            self.sync_state.last_sync_finished = _now_iso()

        if mark_success:
            self.sync_state.last_successful_sync = _now_iso()

        if status is not None:
            self.sync_state.last_sync_status = status

        if waiting_for_pushtan is not None:
            self.sync_state.waiting_for_pushtan = waiting_for_pushtan

        if last_error is not None:
            self.sync_state.last_error = last_error
        elif status in ("running", "waiting_for_pushtan", "success"):
            self.sync_state.last_error = None

        self.schedule_monitoring_update()

    def schedule_monitoring_update(self) -> None:
        for sensor in self.monitoring_sensors:
            if sensor.hass is not None:
                sensor.schedule_update_ha_state(force_refresh=False)

    def update_from_snapshots(self, snapshots: list[AccountBalanceSnapshot]) -> None:
        new_entities: list[SensorEntity] = []

        for snapshot in snapshots:
            if snapshot.account_key not in self._account_sensors:
                sensor = FinTsAccountBalanceSensor(
                    user_name=self.user_name,
                    snapshot=snapshot,
                )
                self._account_sensors[snapshot.account_key] = sensor
                new_entities.append(sensor)

                _LOGGER.warning(
                    "Created dynamic FinTS SEPA account sensor for user '%s', account %s.",
                    self.user_name,
                    _format_account_debug(snapshot.account),
                )

            self._account_sensors[snapshot.account_key].update_from_snapshot(snapshot)

        if new_entities:
            _LOGGER.warning(
                "Adding %s dynamic FinTS SEPA account sensors for user '%s'.",
                len(new_entities),
                self.user_name,
            )
            self._add_entities(new_entities, False)


class FinTsLastSuccessfulSyncSensor(SensorEntity):
    def __init__(self, manager: FinTsUserSensorManager, user_name: str) -> None:
        self._manager = manager
        self._user_name = user_name
        self._attr_name = f"FinTS {user_name} Letzter erfolgreicher Sync"
        self._attr_icon = "mdi:clock-check-outline"

    @property
    def unique_id(self) -> str:
        return f"fints_own_last_successful_sync_{_slugify(self._user_name)}"

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> str | None:
        return self._manager.sync_state.last_successful_sync

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_USER: self._user_name,
            ATTR_ACCOUNT_TYPE: "monitoring",
            "last_sync_started": self._manager.sync_state.last_sync_started,
            "last_sync_finished": self._manager.sync_state.last_sync_finished,
            "last_sync_status": self._manager.sync_state.last_sync_status,
            "waiting_for_pushtan": self._manager.sync_state.waiting_for_pushtan,
            "last_error": self._manager.sync_state.last_error,
        }


class FinTsLastSyncStatusSensor(SensorEntity):
    def __init__(self, manager: FinTsUserSensorManager, user_name: str) -> None:
        self._manager = manager
        self._user_name = user_name
        self._attr_name = f"FinTS {user_name} Status letzter Sync"
        self._attr_icon = "mdi:list-status"

    @property
    def unique_id(self) -> str:
        return f"fints_own_last_sync_status_{_slugify(self._user_name)}"

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> str:
        return self._manager.sync_state.last_sync_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_USER: self._user_name,
            ATTR_ACCOUNT_TYPE: "monitoring",
            "last_successful_sync": self._manager.sync_state.last_successful_sync,
            "last_sync_started": self._manager.sync_state.last_sync_started,
            "last_sync_finished": self._manager.sync_state.last_sync_finished,
            "waiting_for_pushtan": self._manager.sync_state.waiting_for_pushtan,
            "last_error": self._manager.sync_state.last_error,
        }


class FinTsWaitingForPushTanSensor(SensorEntity):
    def __init__(self, manager: FinTsUserSensorManager, user_name: str) -> None:
        self._manager = manager
        self._user_name = user_name
        self._attr_name = f"FinTS {user_name} Warte auf PushTAN"
        self._attr_icon = "mdi:cellphone-key"

    @property
    def unique_id(self) -> str:
        return f"fints_own_waiting_for_pushtan_{_slugify(self._user_name)}"

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> bool:
        return self._manager.sync_state.waiting_for_pushtan

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_USER: self._user_name,
            ATTR_ACCOUNT_TYPE: "monitoring",
            "last_successful_sync": self._manager.sync_state.last_successful_sync,
            "last_sync_started": self._manager.sync_state.last_sync_started,
            "last_sync_finished": self._manager.sync_state.last_sync_finished,
            "last_sync_status": self._manager.sync_state.last_sync_status,
            "last_error": self._manager.sync_state.last_error,
        }


class FinTsUserTotalBalanceSensor(SensorEntity):
    def __init__(
        self,
        manager: FinTsUserSensorManager,
        user_name: str,
        client: FinTsClient,
    ) -> None:
        self._manager = manager
        self._client = client
        self._user_name = user_name

        self._attr_name = f"FinTS {user_name} Gesamt"
        self._attr_icon = "mdi:bank"
        self._attr_native_value = None
        self._attr_native_unit_of_measurement = "EUR"

        self._attr_extra_state_attributes = {
            ATTR_USER: user_name,
            ATTR_ACCOUNT_TYPE: "user_total",
            ATTR_ACCOUNT_COUNT: 0,
            "balance_account_count": 0,
            "failed_account_count": 0,
            "default_transaction_days": self._client.user_transaction_days,
            "sync_timeout_seconds": self._client.sync_timeout_seconds,
            "transaction_count": 0,
            "income_total": 0.0,
            "expense_total": 0.0,
            "net_total": 0.0,
            "transactions": [],
            "last_successful_sync": None,
            "last_sync_started": None,
            "last_sync_finished": None,
            "waiting_for_pushtan": False,
            ATTR_ACCOUNTS: [],
            ATTR_LAST_UPDATE_STATUS: "not_updated",
        }

    @property
    def unique_id(self) -> str:
        return f"fints_own_user_total_{_slugify(self._user_name)}"

    def update(self) -> None:
        self._manager.update_sync_state(
            status="running",
            waiting_for_pushtan=False,
            last_error=None,
            mark_started=True,
        )

        try:
            _LOGGER.warning(
                "Updating FinTS user total sensor for user '%s' with timeout=%s seconds.",
                self._user_name,
                self._client.sync_timeout_seconds,
            )

            snapshots = _run_with_timeout(
                func=self._client.get_all_account_balances,
                timeout_seconds=self._client.sync_timeout_seconds,
                description=f"FinTS sync for user '{self._user_name}'",
            )

            self._manager.update_from_snapshots(snapshots)

            currencies = {
                snapshot.currency
                for snapshot in snapshots
                if snapshot.available and snapshot.currency
            }

            if not snapshots:
                self._attr_native_value = None
                self._attr_extra_state_attributes.update(
                    {
                        ATTR_ACCOUNT_COUNT: 0,
                        "balance_account_count": 0,
                        "failed_account_count": 0,
                        "sync_timeout_seconds": self._client.sync_timeout_seconds,
                        "transaction_count": 0,
                        "income_total": 0.0,
                        "expense_total": 0.0,
                        "net_total": 0.0,
                        "transactions": [],
                        "last_successful_sync": self._manager.sync_state.last_successful_sync,
                        "last_sync_started": self._manager.sync_state.last_sync_started,
                        "last_sync_finished": self._manager.sync_state.last_sync_finished,
                        "waiting_for_pushtan": self._manager.sync_state.waiting_for_pushtan,
                        ATTR_ACCOUNTS: [],
                        ATTR_LAST_UPDATE_STATUS: "no_accounts",
                    }
                )

                self._manager.update_sync_state(
                    status="success",
                    waiting_for_pushtan=False,
                    mark_success=True,
                    mark_finished=True,
                )
                return

            if len(currencies) > 1:
                _LOGGER.error(
                    "Multiple currencies found for user '%s': %s. Total balance will not be calculated.",
                    self._user_name,
                    sorted(currencies),
                )
                total = None
                unit = None
                status = "multiple_currencies"
            else:
                total = sum(
                    snapshot.amount
                    for snapshot in snapshots
                    if snapshot.available and snapshot.amount is not None
                )
                unit = next(iter(currencies), "EUR")
                status = "ok"

            self._attr_native_value = total
            self._attr_native_unit_of_measurement = unit or "EUR"

            accounts_attr = []
            all_transactions = []

            balance_account_count = 0
            failed_account_count = 0
            total_transaction_count = 0
            total_income = 0.0
            total_expenses = 0.0
            total_net = 0.0

            for snapshot in snapshots:
                if snapshot.available:
                    balance_account_count += 1
                else:
                    failed_account_count += 1

                total_transaction_count += snapshot.transaction_count
                total_income += snapshot.income_total
                total_expenses += snapshot.expense_total
                total_net += snapshot.net_total

                for tx in snapshot.transactions:
                    merged_tx = dict(tx)
                    merged_tx["account_key"] = snapshot.account_key
                    merged_tx["account_label"] = snapshot.account_label
                    merged_tx["iban"] = getattr(snapshot.account, "iban", None)
                    merged_tx["accountnumber"] = getattr(snapshot.account, "accountnumber", None)
                    all_transactions.append(merged_tx)

                accounts_attr.append(
                    {
                        "account_key": snapshot.account_key,
                        "iban": getattr(snapshot.account, "iban", None),
                        "accountnumber": getattr(snapshot.account, "accountnumber", None),
                        "subaccount": getattr(snapshot.account, "subaccount", None),
                        "blz": getattr(snapshot.account, "blz", None),
                        "label": snapshot.account_label,
                        "amount": snapshot.amount,
                        "currency": snapshot.currency,
                        "available": snapshot.available,
                        "transaction_days": snapshot.transaction_days,
                        "transaction_period_start": snapshot.transaction_period_start,
                        "transaction_period_end": snapshot.transaction_period_end,
                        "transaction_count": snapshot.transaction_count,
                        "income_total": snapshot.income_total,
                        "expense_total": snapshot.expense_total,
                        "net_total": snapshot.net_total,
                        "transaction_error": snapshot.transaction_error,
                        "error": snapshot.error,
                    }
                )

            all_transactions.sort(key=_transaction_sort_key, reverse=True)

            self._manager.update_sync_state(
                status="success",
                waiting_for_pushtan=False,
                mark_success=True,
                mark_finished=True,
            )

            self._attr_extra_state_attributes.update(
                {
                    ATTR_ACCOUNT_COUNT: len(snapshots),
                    "balance_account_count": balance_account_count,
                    "failed_account_count": failed_account_count,
                    "default_transaction_days": self._client.user_transaction_days,
                    "sync_timeout_seconds": self._client.sync_timeout_seconds,
                    "transaction_count": total_transaction_count,
                    "income_total": round(total_income, 2),
                    "expense_total": round(total_expenses, 2),
                    "net_total": round(total_net, 2),
                    "transactions": all_transactions,
                    "last_successful_sync": self._manager.sync_state.last_successful_sync,
                    "last_sync_started": self._manager.sync_state.last_sync_started,
                    "last_sync_finished": self._manager.sync_state.last_sync_finished,
                    "waiting_for_pushtan": self._manager.sync_state.waiting_for_pushtan,
                    ATTR_ACCOUNTS: accounts_attr,
                    ATTR_LAST_UPDATE_STATUS: status,
                }
            )

            _LOGGER.warning(
                "Updated FinTS user total sensor for user '%s': total=%s %s, accounts=%s, balance_accounts=%s, failed_accounts=%s, transactions=%s.",
                self._user_name,
                self._attr_native_value,
                self._attr_native_unit_of_measurement,
                len(snapshots),
                balance_account_count,
                failed_account_count,
                total_transaction_count,
            )

        except Exception as e:
            _LOGGER.error(
                "FinTS user total update failed for user '%s': %s",
                self._user_name,
                e,
            )

            self._attr_native_value = None

            self._manager.update_sync_state(
                status="error",
                waiting_for_pushtan=False,
                last_error=str(e),
                mark_finished=True,
            )

            self._attr_extra_state_attributes.update(
                {
                    "sync_timeout_seconds": self._client.sync_timeout_seconds,
                    "last_successful_sync": self._manager.sync_state.last_successful_sync,
                    "last_sync_started": self._manager.sync_state.last_sync_started,
                    "last_sync_finished": self._manager.sync_state.last_sync_finished,
                    "waiting_for_pushtan": self._manager.sync_state.waiting_for_pushtan,
                    ATTR_LAST_UPDATE_STATUS: f"error: {e}",
                }
            )


class FinTsAccountBalanceSensor(SensorEntity):
    def __init__(
        self,
        user_name: str,
        snapshot: AccountBalanceSnapshot,
    ) -> None:
        self._user_name = user_name
        self._account_key = snapshot.account_key
        self._account_label = snapshot.account_label

        self._attr_name = f"FinTS {user_name} {snapshot.account_label}"
        self._attr_icon = "mdi:bank-outline"
        self._attr_native_value = None
        self._attr_native_unit_of_measurement = "EUR"

        self._attr_extra_state_attributes = {
            ATTR_USER: user_name,
            ATTR_ACCOUNT_TYPE: "sepa_account_balance",
            ATTR_ACCOUNT: snapshot.account_label,
            ATTR_IBAN: getattr(snapshot.account, "iban", None),
            ATTR_ACCOUNT_NUMBER: getattr(snapshot.account, "accountnumber", None),
            ATTR_LAST_UPDATE_STATUS: "not_updated",
        }

        self.update_from_snapshot(snapshot, schedule=False)

    @property
    def unique_id(self) -> str:
        return f"fints_own_sepa_account_balance_{_slugify(self._user_name)}_{_normalize_identifier(self._account_key)}"

    @property
    def should_poll(self) -> bool:
        return False

    def update_from_snapshot(
        self,
        snapshot: AccountBalanceSnapshot,
        schedule: bool = True,
    ) -> None:
        self._account_key = snapshot.account_key
        self._account_label = snapshot.account_label

        self._attr_native_value = snapshot.amount
        self._attr_native_unit_of_measurement = snapshot.currency or "EUR"

        self._attr_extra_state_attributes.update(
            {
                ATTR_USER: self._user_name,
                ATTR_ACCOUNT_TYPE: "sepa_account_balance",
                ATTR_ACCOUNT: snapshot.account_label,
                ATTR_IBAN: getattr(snapshot.account, "iban", None),
                ATTR_ACCOUNT_NUMBER: getattr(snapshot.account, "accountnumber", None),
                "subaccount": getattr(snapshot.account, "subaccount", None),
                "blz": getattr(snapshot.account, "blz", None),
                "available": snapshot.available,
                "transaction_days": snapshot.transaction_days,
                "transaction_period_start": snapshot.transaction_period_start,
                "transaction_period_end": snapshot.transaction_period_end,
                "transaction_count": snapshot.transaction_count,
                "income_total": snapshot.income_total,
                "expense_total": snapshot.expense_total,
                "net_total": snapshot.net_total,
                "transactions": snapshot.transactions,
                "transaction_error": snapshot.transaction_error,
                "error": snapshot.error,
                ATTR_LAST_UPDATE_STATUS: "ok" if snapshot.available else "error",
            }
        )

        if schedule and self.hass is not None:
            self.schedule_update_ha_state(force_refresh=False)

    def update(self) -> None:
        return

class HanseaticBankSensorManager:
    """Refresh one online Hanseatic bank account and expose shared sensor state."""

    def __init__(self, user_name: str, client: HanseaticBankClient) -> None:
        self.user_name = user_name
        self.client = client
        self.transaction_days = client.transaction_days
        self.snapshot: HanseaticSnapshot | None = None
        self.last_successful_sync: str | None = None
        self.last_error: str | None = None
        self.last_status: str = "not_updated"
        self._lock = Lock()
        self.main_sensor = HanseaticBalanceSensor(self)
        self.available_sensor = HanseaticValueSensor(
            self, "available_credit", "Verfügbarer Kredit", "mdi:credit-card-check-outline"
        )
        self.limit_sensor = HanseaticValueSensor(
            self, "credit_limit", "Kreditlimit", "mdi:credit-card-settings-outline"
        )
        self.status_sensor = HanseaticOnlineStatusSensor(self)
        self.sensors: list[SensorEntity] = [
            self.main_sensor, self.available_sensor, self.limit_sensor, self.status_sensor,
        ]

    def refresh(self, *, force_reauthenticate: bool = False, update_main: bool = False) -> None:
        with self._lock:
            try:
                self.snapshot = self.client.fetch(force_reauthenticate=force_reauthenticate)
                self.last_successful_sync = _now_iso()
                self.last_status = "success"
                self.last_error = None
            except HanseaticOtpRequired as exc:
                self.snapshot = None
                self.last_status = "sms_otp_required"
                self.last_error = str(exc)
            except HanseaticApprovalRequired as exc:
                self.snapshot = None
                self.last_status = "approval_required"
                self.last_error = str(exc)
            except (HanseaticError, OSError, ValueError) as exc:
                self.snapshot = None
                self.last_status = "error"
                self.last_error = str(exc)
                _LOGGER.warning("Hanseatic online sync failed for '%s': %s", self.user_name, exc)
        self._update_sensors(include_main=update_main)

    def submit_otp(self, otp: str) -> None:
        with self._lock:
            self.client.submit_otp(otp)
        self.refresh(update_main=True)

    def _update_sensors(self, *, include_main: bool = False) -> None:
        for sensor in self.sensors:
            if (include_main or sensor is not self.main_sensor) and sensor.hass is not None:
                sensor.schedule_update_ha_state(force_refresh=False)


class HanseaticBalanceSensor(SensorEntity):
    """Balance as returned by Hanseatic's API (no guessed sign inversion)."""

    def __init__(self, manager: HanseaticBankSensorManager) -> None:
        self._manager = manager
        self._attr_name = f"Hanseatic {manager.user_name} Saldo"
        self._attr_icon = "mdi:credit-card"

    @property
    def unique_id(self) -> str:
        return f"fints_own_hanseatic_balance_{_slugify(self._manager.user_name)}"

    @property
    def native_unit_of_measurement(self) -> str:
        return self._manager.snapshot.currency if self._manager.snapshot else "EUR"

    @property
    def native_value(self) -> float | None:
        return self._manager.snapshot.balance if self._manager.snapshot else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._manager.snapshot
        attrs: dict[str, Any] = {
            ATTR_USER: self._manager.user_name,
            "provider": PROVIDER_HANSEATIC,
            "source": "online_api",
            ATTR_LAST_UPDATE_STATUS: self._manager.last_status,
            "last_error": self._manager.last_error,
            "last_successful_sync": self._manager.last_successful_sync,
            "waiting_for_sms": self._manager.client.waiting_for_sms,
            "retry_after": self._manager.client.retry_after,
            "default_transaction_days": self._manager.transaction_days,
        }
        if snapshot is not None:
            attrs.update({
                ATTR_ACCOUNT_TYPE: snapshot.account_type,
                ATTR_ACCOUNT_NUMBER: snapshot.account_number,
                "account_label": snapshot.account_label,
                "retrieved_at": snapshot.retrieved_at,
                "available_credit": snapshot.available_credit,
                "credit_limit": snapshot.credit_limit,
                "transaction_period_start": snapshot.period_start,
                "transaction_period_end": snapshot.period_end,
                "transaction_count": snapshot.transaction_count,
                "income_total": snapshot.income_total,
                "expense_total": snapshot.expense_total,
                "net_total": snapshot.net_total,
                "transactions": snapshot.transactions[:HANSEATIC_MAX_ATTRIBUTE_TRANSACTIONS],
                "transactions_truncated": snapshot.transaction_count > HANSEATIC_MAX_ATTRIBUTE_TRANSACTIONS,
                "transaction_error": snapshot.transaction_error,
                "history_requires_approval": snapshot.history_requires_approval,
            })
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The API may await a secure-app approval for up to 180s.
        # Run the first request in HA's executor, outside startup setup.
        async def do_initial_refresh() -> None:
            await self.hass.async_add_executor_job(
                partial(self._manager.refresh, update_main=True)
            )

        self.hass.async_create_task(do_initial_refresh())

    def update(self) -> None:
        self._manager.refresh()


class HanseaticValueSensor(SensorEntity):
    def __init__(
        self, manager: HanseaticBankSensorManager, key: str, label: str, icon: str,
    ) -> None:
        self._manager = manager
        self._key = key
        self._attr_name = f"Hanseatic {manager.user_name} {label}"
        self._attr_icon = icon

    @property
    def unique_id(self) -> str:
        return f"fints_own_hanseatic_{self._key}_{_slugify(self._manager.user_name)}"

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def native_unit_of_measurement(self) -> str:
        return self._manager.snapshot.currency if self._manager.snapshot else "EUR"

    @property
    def native_value(self) -> float | None:
        return getattr(self._manager.snapshot, self._key) if self._manager.snapshot else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_USER: self._manager.user_name,
            "provider": PROVIDER_HANSEATIC,
            ATTR_LAST_UPDATE_STATUS: self._manager.last_status,
            "retrieved_at": self._manager.snapshot.retrieved_at if self._manager.snapshot else None,
        }


class HanseaticOnlineStatusSensor(SensorEntity):
    def __init__(self, manager: HanseaticBankSensorManager) -> None:
        self._manager = manager
        self._attr_name = f"Hanseatic {manager.user_name} Onlinestatus"
        self._attr_icon = "mdi:cloud-check-outline"

    @property
    def unique_id(self) -> str:
        # Re-use the old import status entity ID for users who installed v1.1.
        return f"fints_own_hanseatic_import_status_{_slugify(self._manager.user_name)}"

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def native_value(self) -> str:
        return self._manager.last_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_USER: self._manager.user_name,
            "provider": PROVIDER_HANSEATIC,
            "last_successful_sync": self._manager.last_successful_sync,
            "last_error": self._manager.last_error,
            "waiting_for_sms": self._manager.client.waiting_for_sms,
            "retry_after": self._manager.client.retry_after,
        }