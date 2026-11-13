"""Import the latest locally saved Erste JSON snapshot into SQLite."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "DataBase" / "financials.db"
RAW_DATA_ROOT = ROOT / "DataBase" / "raw_json"


def nested(value: object, *keys: str, default: object = None) -> object:
    """Safely read keys from nested JSON objects."""
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def raw_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def money_to_minor(value: object) -> int:
    """Convert CZK/USD-style decimal amounts to integer minor units."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def latest_snapshot() -> Path:
    snapshots = sorted(path for path in RAW_DATA_ROOT.iterdir() if path.is_dir())
    if not snapshots:
        raise RuntimeError(f"No JSON snapshots found in {RAW_DATA_ROOT}")
    return snapshots[-1]


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path.name}")
    return value


def ensure_account_columns(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(money_flow_accounts)")}
    if "bic" not in columns:
        connection.execute("ALTER TABLE money_flow_accounts ADD COLUMN bic TEXT")
    if "raw_json" not in columns:
        connection.execute("ALTER TABLE money_flow_accounts ADD COLUMN raw_json TEXT")


def import_account(connection: sqlite3.Connection, account: dict) -> None:
    connection.execute(
        """
        INSERT INTO money_flow_accounts (
            account_id, account_name, product_name, currency, iban,
            account_number, bank_code, country_code, bic, is_owner, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id) DO UPDATE SET
            account_name = excluded.account_name,
            product_name = excluded.product_name,
            currency = excluded.currency,
            iban = excluded.iban,
            account_number = excluded.account_number,
            bank_code = excluded.bank_code,
            country_code = excluded.country_code,
            bic = excluded.bic,
            is_owner = excluded.is_owner,
            raw_json = excluded.raw_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            account["id"],
            account.get("nameI18N"),
            account.get("productI18N"),
            account.get("currency"),
            nested(account, "identification", "iban"),
            nested(account, "identification", "other"),
            nested(account, "servicer", "bankCode"),
            nested(account, "servicer", "countryCode"),
            nested(account, "servicer", "bic"),
            int(bool(nested(account, "relationship", "isOwner", default=False))),
            raw_json(account),
        ),
    )


def import_balance(connection: sqlite3.Connection, account_id: str, balance: dict) -> None:
    connection.execute(
        """
        INSERT INTO money_flow_balances (
            account_id, balance_type_code, amount_minor, currency,
            credit_debit_indicator, balance_datetime, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, balance_type_code, balance_datetime) DO UPDATE SET
            amount_minor = excluded.amount_minor,
            currency = excluded.currency,
            credit_debit_indicator = excluded.credit_debit_indicator,
            fetched_at = CURRENT_TIMESTAMP,
            raw_json = excluded.raw_json
        """,
        (
            account_id,
            nested(balance, "type", "codeOrProprietary", "code"),
            money_to_minor(nested(balance, "amount", "value")),
            nested(balance, "amount", "currency"),
            balance.get("creditDebitIndicator"),
            nested(balance, "date", "dateTime"),
            raw_json(balance),
        ),
    )


def transaction_reference(account_id: str, transaction: dict) -> str:
    reference = transaction.get("entryReference")
    if reference:
        return str(reference)
    fingerprint = hashlib.sha256(
        f"{account_id}:{raw_json(transaction)}".encode("utf-8")
    ).hexdigest()
    return f"generated:{fingerprint}"


def import_transaction(connection: sqlite3.Connection, account_id: str, transaction: dict) -> None:
    details = nested(transaction, "entryDetails", "transactionDetails", default={})
    parties = nested(details, "relatedParties", default={})
    direction = transaction.get("creditDebitIndicator")
    party_key = "creditor" if direction == "DBIT" else "debtor"
    party_account_key = "creditorAccount" if direction == "DBIT" else "debtorAccount"

    references = nested(details, "references", default={})
    remittance = nested(details, "remittanceInformation", default={})
    structured_refs = nested(
        remittance,
        "structured",
        "creditorReferenceInformation",
        "reference",
        default=[],
    )
    if not isinstance(structured_refs, list):
        structured_refs = [structured_refs]
    payment_reference = (
        nested(remittance, "unstructured")
        or " | ".join(str(item) for item in structured_refs if item)
        or nested(references, "endToEndIdentification")
        or nested(references, "accountServicerReference")
    )

    description = (
        nested(details, "additionalTransactionDescription")
        or nested(details, "additionalTransactionInformation")
        or nested(details, "additionalRemittanceInformation")
    )
    exchange = nested(
        details,
        "amountDetails",
        "counterValueAmount",
        "currencyExchange",
        default={},
    )

    connection.execute(
        """
        INSERT INTO money_flow_transactions (
            account_id, entry_reference, reservation_id, amount_minor,
            currency, direction, status, booking_date, value_date,
            transaction_code, transaction_issuer, counterparty_name,
            counterparty_iban, payment_reference, description,
            source_currency, target_currency, exchange_rate, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_id, entry_reference) DO UPDATE SET
            reservation_id = excluded.reservation_id,
            amount_minor = excluded.amount_minor,
            currency = excluded.currency,
            direction = excluded.direction,
            status = excluded.status,
            booking_date = excluded.booking_date,
            value_date = excluded.value_date,
            transaction_code = excluded.transaction_code,
            transaction_issuer = excluded.transaction_issuer,
            counterparty_name = excluded.counterparty_name,
            counterparty_iban = excluded.counterparty_iban,
            payment_reference = excluded.payment_reference,
            description = excluded.description,
            source_currency = excluded.source_currency,
            target_currency = excluded.target_currency,
            exchange_rate = excluded.exchange_rate,
            raw_json = excluded.raw_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            account_id,
            transaction_reference(account_id, transaction),
            transaction.get("reservationId"),
            money_to_minor(nested(transaction, "amount", "value")),
            nested(transaction, "amount", "currency"),
            direction,
            transaction.get("status"),
            nested(transaction, "bookingDate", "date"),
            nested(transaction, "valueDate", "date"),
            nested(transaction, "bankTransactionCode", "proprietary", "code"),
            nested(transaction, "bankTransactionCode", "proprietary", "issuer"),
            nested(parties, party_key, "name"),
            nested(parties, party_account_key, "identification", "iban"),
            payment_reference,
            description,
            nested(exchange, "sourceCurrency"),
            nested(exchange, "targetCurrency"),
            str(nested(exchange, "exchangeRate")) if nested(exchange, "exchangeRate") is not None else None,
            raw_json(transaction),
        ),
    )


def import_latest_snapshot() -> tuple[Path, int, int, int]:
    snapshot = latest_snapshot()
    accounts_payload = load_json(snapshot / "accounts.json")
    accounts = accounts_payload.get("accounts", [])

    account_count = 0
    balance_count = 0
    transaction_count = 0

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        ensure_account_columns(connection)

        for index, account in enumerate(accounts, start=1):
            if not isinstance(account, dict) or not account.get("id"):
                continue
            account_id = str(account["id"])
            import_account(connection, account)
            account_count += 1

            balance_payload = load_json(snapshot / f"account_{index:03d}_balance.json")
            for balance in balance_payload.get("balances", []):
                import_balance(connection, account_id, balance)
                balance_count += 1

            transaction_payload = load_json(snapshot / f"account_{index:03d}_transactions.json")
            for transaction in transaction_payload.get("transactions", []):
                import_transaction(connection, account_id, transaction)
                transaction_count += 1

    return snapshot, account_count, balance_count, transaction_count


if __name__ == "__main__":
    snapshot_path, accounts, balances, transactions = import_latest_snapshot()
    print(f"Imported snapshot: {snapshot_path.name}")
    print(f"Accounts processed: {accounts}")
    print(f"Balances processed: {balances}")
    print(f"Transactions processed: {transactions}")

