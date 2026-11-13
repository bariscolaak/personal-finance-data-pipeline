"""Import supported Turkish bank PDF statements into SQLite.

The importer currently understands the İş Bankası investment-account statement
and the VakıfBank gold-account statement stored in the project's pdf folder.
It uses a stable fingerprint per statement row, so importing the same PDFs again
does not create duplicate database rows.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
PDF_FOLDER = ROOT / "pdf"
DATABASE_PATH = ROOT / "DataBase" / "financials.db"


@dataclass
class PdfAccount:
    bank_name: str
    account_number: str
    iban: str | None
    account_type: str | None
    account_label: str | None
    balance_value: str | None
    balance_unit: str | None


@dataclass
class PdfTransaction:
    transaction_datetime: str
    transaction_number: str
    channel: str | None
    amount_value: str
    amount_unit: str
    balance_value: str | None
    balance_unit: str | None
    security_symbol: str | None
    security_quantity: str | None
    transaction_code: str | None
    transaction_name: str | None
    tl_amount: str | None
    exchange_rate: str | None
    description: str
    raw_text: str


def pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def decimal_text(value: str | None) -> str | None:
    """Convert Turkish-formatted decimal text to a database-friendly decimal."""
    if value is None:
        return None
    return value.replace(".", "").replace(",", ".")


def iso_datetime(value: str, source_format: str) -> str:
    return datetime.strptime(value, source_format).isoformat(sep=" ")


def parse_isbank(text: str) -> tuple[PdfAccount, list[PdfTransaction]]:
    account_number = re.search(r"Hesap Numarası\s+İşlem Yeri.*?\n.*?\n(\d+-\d+)", text, re.S)
    iban = re.search(r"\bTR\d{2}(?:\s?\d{4}){5}\s?\d{2}\b", text)
    if not account_number:
        raise ValueError("İş Bankası account number was not found")

    account = PdfAccount(
        bank_name="Türkiye İş Bankası",
        account_number=account_number.group(1),
        iban=iban.group(0).replace(" ", "") if iban else None,
        account_type="Yatırım",
        account_label=None,
        balance_value="0.00",
        balance_unit="TRY",
    )

    row_pattern = re.compile(
        r"^(\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2})\s+"
        r"(\d{8})\s+(Sistem|İşCep|Diğer)\s+"
        r"(-?[\d.]+,\d{2})\s+(-?[\d.]+,\d{2})"
        r"(?:\s+([\d.]+,\d{2}))?\s+([A-Z]{2})(?:\s+(.*))?$",
        re.MULTILINE,
    )
    matches = list(row_pattern.finditer(text))
    transactions: list[PdfTransaction] = []

    for index, match in enumerate(matches):
        following_start = match.end()
        following_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        continuation = text[following_start:following_end].strip()
        first_description = (match.group(8) or "").strip()
        description = " ".join(
            part.strip() for part in (first_description, continuation) if part.strip()
        )
        description = re.sub(r"\s+", " ", description)
        # Do not include the document footer in the final transaction description.
        description = description.split("(*) Bloke dahil", 1)[0].strip()
        # Repeated page furniture must not change the transaction fingerprint.
        description = re.sub(r"\bSayfa\s+\d+\s*/\s*\d+\b", " ", description)
        description = re.sub(
            r"İşlem Tarihi\s+İşlem No\s+Kanal/Şube\s+İşlem Tutarı\s+"
            r"Bakiye \(TL\)\s+Kıymet Bakiye\s+İşlem Açıklama",
            " ",
            description,
        )
        description = re.sub(r"\s+", " ", description).strip()

        symbol_match = re.match(r"([A-Z0-9]+)\s+-", description)
        quantity_match = re.search(r"Adet:\s*([+-]?\s*\d[\d.]*)", description)

        transactions.append(
            PdfTransaction(
                transaction_datetime=iso_datetime(match.group(1), "%d/%m/%Y-%H:%M:%S"),
                transaction_number=match.group(2),
                channel=match.group(3),
                amount_value=decimal_text(match.group(4)) or "0",
                amount_unit="TRY",
                balance_value=decimal_text(match.group(5)),
                balance_unit="TRY",
                security_symbol=symbol_match.group(1) if symbol_match else None,
                security_quantity=(
                    re.sub(r"\s+", "", quantity_match.group(1))
                    if quantity_match else None
                ),
                transaction_code=match.group(7),
                transaction_name=None,
                tl_amount=None,
                exchange_rate=None,
                description=description,
                raw_text=" ".join(part for part in (match.group(0), continuation) if part),
            )
        )

    return account, transactions


def parse_vakifbank(text: str) -> tuple[PdfAccount, list[PdfTransaction]]:
    account_number = re.search(r"Hesap No:\s*(\d+)", text)
    iban = re.search(r"IBAN\s*:\s*(TR[\d ]+)", text)
    account_type = re.search(r"Hesap Türü\s*:\s*([^\n]+?)\s+Rumuz", text)
    label = re.search(r"Rumuz\s*:\s*([^\n]+)", text)
    balance = re.search(r"Bakiye\s*:\s*([\d.,]+)\s+([A-Z]+)", text)
    if not account_number:
        raise ValueError("VakıfBank account number was not found")

    account = PdfAccount(
        bank_name="VakıfBank",
        account_number=account_number.group(1),
        iban=re.sub(r"\s+", "", iban.group(1)) if iban else None,
        account_type=account_type.group(1).strip() if account_type else "VADESİZ ALT",
        account_label=label.group(1).strip() if label else None,
        balance_value=decimal_text(balance.group(1)) if balance else None,
        balance_unit=balance.group(2) if balance else "ALT",
    )

    row_pattern = re.compile(
        r"^(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})\s+(\d+)\s+"
        r"(-?[\d.]+,\d+)\s+(-?[\d.]+,\d+)\s+([^\n]+)$",
        re.MULTILINE,
    )
    matches = list(row_pattern.finditer(text))
    transactions: list[PdfTransaction] = []

    for index, match in enumerate(matches):
        following_start = match.end()
        following_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        continuation = text[following_start:following_end].strip()
        continuation = continuation.split("***Dekont yerine", 1)[0].strip()
        description = re.sub(r"\s+", " ", continuation)
        tl_amount = re.search(r"\(([\d.,]+)\s*TL", description)
        exchange_rate = re.search(r"İşlem Kuru\s*:\s*([\d.,]+)", description)

        transactions.append(
            PdfTransaction(
                transaction_datetime=iso_datetime(
                    f"{match.group(1)} {match.group(2)}", "%d.%m.%Y %H:%M"
                ),
                transaction_number=match.group(3),
                channel=None,
                amount_value=decimal_text(match.group(4)) or "0",
                amount_unit="ALT",
                balance_value=decimal_text(match.group(5)),
                balance_unit="ALT",
                security_symbol="ALT",
                security_quantity=decimal_text(match.group(4)),
                transaction_code=None,
                transaction_name=match.group(6).strip(),
                tl_amount=decimal_text(tl_amount.group(1)) if tl_amount else None,
                exchange_rate=decimal_text(exchange_rate.group(1)) if exchange_rate else None,
                description=description,
                raw_text=" ".join(part for part in (match.group(0), continuation) if part),
            )
        )

    return account, transactions


def create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS staging_investment_pdf_accounts (
            pdf_account_id INTEGER PRIMARY KEY,
            bank_name TEXT NOT NULL,
            account_number TEXT NOT NULL,
            iban TEXT,
            account_type TEXT,
            account_label TEXT,
            balance_value TEXT,
            balance_unit TEXT,
            source_file TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (bank_name, account_number)
        );

        CREATE TABLE IF NOT EXISTS staging_investment_pdf_transactions (
            pdf_transaction_id INTEGER PRIMARY KEY,
            pdf_account_id INTEGER NOT NULL,
            source_key TEXT NOT NULL,
            transaction_datetime TEXT NOT NULL,
            transaction_number TEXT NOT NULL,
            channel TEXT,
            amount_value TEXT NOT NULL,
            amount_unit TEXT NOT NULL,
            balance_value TEXT,
            balance_unit TEXT,
            security_symbol TEXT,
            security_quantity TEXT,
            transaction_code TEXT,
            transaction_name TEXT,
            tl_amount TEXT,
            exchange_rate TEXT,
            description TEXT,
            source_file TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (pdf_account_id) REFERENCES staging_investment_pdf_accounts(pdf_account_id),
            UNIQUE (pdf_account_id, source_key)
        );
        """
    )


def source_key(transaction: PdfTransaction, occurrence: int) -> str:
    stable_value = "|".join(
        (
            transaction.transaction_datetime,
            transaction.transaction_number,
            transaction.amount_value,
            transaction.amount_unit,
            transaction.transaction_code or "",
            transaction.description,
        )
    )
    base_key = hashlib.sha256(stable_value.encode("utf-8")).hexdigest()
    return f"{base_key}:{occurrence}"


def upsert_account(
    connection: sqlite3.Connection, account: PdfAccount, source_file: str
) -> int:
    connection.execute(
        """
        INSERT INTO staging_investment_pdf_accounts (
            bank_name, account_number, iban, account_type, account_label,
            balance_value, balance_unit, source_file
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bank_name, account_number) DO UPDATE SET
            iban = excluded.iban,
            account_type = excluded.account_type,
            account_label = excluded.account_label,
            balance_value = excluded.balance_value,
            balance_unit = excluded.balance_unit,
            source_file = excluded.source_file,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            account.bank_name,
            account.account_number,
            account.iban,
            account.account_type,
            account.account_label,
            account.balance_value,
            account.balance_unit,
            source_file,
        ),
    )
    row = connection.execute(
        "SELECT pdf_account_id FROM staging_investment_pdf_accounts WHERE bank_name=? AND account_number=?",
        (account.bank_name, account.account_number),
    ).fetchone()
    if row is None:
        raise RuntimeError("Imported PDF account could not be found")
    return int(row[0])


def upsert_transaction(
    connection: sqlite3.Connection,
    pdf_account_id: int,
    transaction: PdfTransaction,
    source_file: str,
    occurrence: int,
) -> None:
    connection.execute(
        """
        INSERT INTO staging_investment_pdf_transactions (
            pdf_account_id, source_key, transaction_datetime,
            transaction_number, channel, amount_value, amount_unit,
            balance_value, balance_unit, security_symbol, security_quantity,
            transaction_code, transaction_name, tl_amount, exchange_rate,
            description, source_file, raw_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pdf_account_id, source_key) DO UPDATE SET
            balance_value = excluded.balance_value,
            balance_unit = excluded.balance_unit,
            security_symbol = excluded.security_symbol,
            security_quantity = excluded.security_quantity,
            transaction_name = excluded.transaction_name,
            tl_amount = excluded.tl_amount,
            exchange_rate = excluded.exchange_rate,
            description = excluded.description,
            source_file = excluded.source_file,
            raw_text = excluded.raw_text,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            pdf_account_id,
            source_key(transaction, occurrence),
            transaction.transaction_datetime,
            transaction.transaction_number,
            transaction.channel,
            transaction.amount_value,
            transaction.amount_unit,
            transaction.balance_value,
            transaction.balance_unit,
            transaction.security_symbol,
            transaction.security_quantity,
            transaction.transaction_code,
            transaction.transaction_name,
            transaction.tl_amount,
            transaction.exchange_rate,
            transaction.description,
            source_file,
            transaction.raw_text,
        ),
    )


def import_pdf(connection: sqlite3.Connection, path: Path) -> int:
    text = pdf_text(path)
    lowered = path.name.lower()
    if "isbank" in lowered:
        account, transactions = parse_isbank(text)
    elif "vakifbank" in lowered:
        account, transactions = parse_vakifbank(text)
    else:
        raise ValueError(f"Unsupported PDF format: {path.name}")

    account_id = upsert_account(connection, account, path.name)
    occurrences: dict[str, int] = {}
    for transaction in transactions:
        base_key = source_key(transaction, 0).split(":", 1)[0]
        occurrences[base_key] = occurrences.get(base_key, 0) + 1
        upsert_transaction(
            connection,
            account_id,
            transaction,
            path.name,
            occurrences[base_key],
        )
    return len(transactions)


def main() -> None:
    pdf_paths = sorted(PDF_FOLDER.glob("*.pdf"))
    if not pdf_paths:
        raise RuntimeError(f"No PDF files found in {PDF_FOLDER}")

    with sqlite3.connect(DATABASE_PATH) as connection:
        create_tables(connection)
        for path in pdf_paths:
            count = import_pdf(connection, path)
            print(f"{path.name}: {count} rows processed")

        account_count = connection.execute("SELECT COUNT(*) FROM staging_investment_pdf_accounts").fetchone()[0]
        transaction_count = connection.execute(
            "SELECT COUNT(*) FROM staging_investment_pdf_transactions"
        ).fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    print(f"PDF accounts in database: {account_count}")
    print(f"PDF transactions in database: {transaction_count}")
    print(f"Foreign-key errors: {len(foreign_key_errors)}")


if __name__ == "__main__":
    main()
