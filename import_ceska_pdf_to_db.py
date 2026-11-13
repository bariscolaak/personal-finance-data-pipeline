"""Import Ceska sporitelna investment PDFs into the existing staging table.

This script intentionally does not create or change database tables.  It reads
PDF files from pdf/ceska_pdf and upserts parsed events into
staging_investment_ceska_events.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
PDF_FOLDER = ROOT / "pdf" / "ceska_pdf"
DATABASE_PATH = ROOT / "DataBase" / "financials.db"
TABLE_NAME = "staging_investment_ceska_events"


@dataclass
class CeskaEvent:
    source_key: str
    account_number: str
    event_reference: str | None
    event_date: str
    settlement_date: str | None
    event_type: str
    asset_name: str | None
    isin: str | None
    market: str | None
    quantity: str | None
    unit_price: str | None
    gross_amount: str | None
    commission: str
    tax: str
    total_amount: str
    trade_currency: str | None
    settlement_currency: str
    exchange_rate: str | None
    source_file: str
    raw_text: str


def pdf_text(path: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def clean_lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]


def iso_date(value: str) -> str:
    return datetime.strptime(value, "%d.%m.%Y").date().isoformat()


def iso_datetime(value: str) -> str:
    return datetime.strptime(value, "%d.%m.%Y %H:%M:%S").isoformat(sep=" ")


def decimal_text(value: str) -> str:
    return str(Decimal(value.replace(" ", "").replace(",", ".")))


def account_number(text: str) -> str:
    match = re.search(r"Číslo majetkového účtu:\s*(\d+)", text)
    if not match:
        raise ValueError("Investment account number was not found")
    return match.group(1)


def stable_key(*parts: object) -> str:
    raw = "|".join("" if part is None else str(part).strip() for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def completed_section(text: str) -> str | None:
    marker = "CENNÉ PAPÍRY OBCHODOVANÉ NA BURZÁCH A JINÝCH TRZÍCH - Provedené pokyny a transakce"
    if marker not in text:
        return None
    section = text.split(marker, 1)[1]
    for ending in (
        "CENNÉ PAPÍRY OBCHODOVANÉ NA BURZÁCH A JINÝCH TRZÍCH - Podané",
        "CENNÉ PAPÍRY OBCHODOVANÉ NA BURZÁCH A JINÝCH TRZÍCH - Neprovedené",
        "VÝPLATY -",
        "SPRÁVA MAJETKOVÉHO ÚČTU",
        "VYSVĚTLIVKY",
    ):
        section = section.split(ending, 1)[0]
    return section


def parse_trades(text: str, source_file: str) -> list[CeskaEvent]:
    section = completed_section(text)
    if not section:
        return []

    lines = clean_lines(section)
    account = account_number(text)
    events: list[CeskaEvent] = []
    isin_pattern = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")

    for index, line in enumerate(lines):
        if not isin_pattern.fullmatch(line):
            continue
        if index < 6 or index + 5 >= len(lines):
            raise ValueError(f"Unexpected trade layout near ISIN {line}")

        order_date = iso_datetime(lines[index - 6])
        service = lines[index - 5]
        execution_date = iso_datetime(lines[index - 4])
        settlement_date = iso_date(lines[index - 3])
        reference = lines[index - 2]
        asset_name = lines[index - 1]
        trade_currency = lines[index + 1]
        market = lines[index + 2]
        quantity = decimal_text(lines[index + 3])
        unit_price = decimal_text(lines[index + 4])

        tail: list[str] = []
        cursor = index + 5
        while cursor < len(lines) and len(tail) < 7:
            if re.fullmatch(r"-?\d+(?:[.,]\d+)?", lines[cursor]):
                tail.append(decimal_text(lines[cursor]))
            cursor += 1
        if len(tail) != 7:
            raise ValueError(f"Expected 7 trade totals near order {reference}, got {tail}")

        total_amount = tail[2]
        gross_amount = tail[4]
        commission = tail[5] if Decimal(tail[5]) else tail[6]
        event_type = "BUY" if service.startswith("Nákup") else "SELL"
        raw = "\n".join(lines[index - 6 : cursor])
        key = stable_key("CESKA", account, "TRADE", reference)

        events.append(
            CeskaEvent(
                source_key=key,
                account_number=account,
                event_reference=reference,
                event_date=execution_date,
                settlement_date=settlement_date,
                event_type=event_type,
                asset_name=asset_name,
                isin=line,
                market=market,
                quantity=quantity,
                unit_price=unit_price,
                gross_amount=gross_amount,
                commission=commission,
                tax="0",
                total_amount=total_amount,
                trade_currency=trade_currency,
                settlement_currency=trade_currency,
                exchange_rate=None,
                source_file=source_file,
                raw_text=raw,
            )
        )
    return events


def parse_dividends(text: str, source_file: str) -> list[CeskaEvent]:
    marker = "VÝPLATY - Provedené transakce"
    if marker not in text:
        return []
    section = text.split(marker, 1)[1].split("VYSVĚTLIVKY", 1)[0]
    lines = clean_lines(section)
    account = account_number(text)
    events: list[CeskaEvent] = []
    isin_pattern = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")

    for index, line in enumerate(lines):
        if not isin_pattern.fullmatch(line):
            continue
        if index < 6 or index + 11 >= len(lines):
            raise ValueError(f"Unexpected dividend layout near ISIN {line}")

        record_date = iso_date(lines[index - 5])
        payment_date = iso_date(lines[index - 4])
        reference = lines[index - 3]
        asset_name = lines[index - 1]
        quantity = decimal_text(lines[index + 1])
        trade_currency = lines[index + 2]
        gross_amount = decimal_text(lines[index + 5])
        tax = decimal_text(lines[index + 7])
        # Empty/zero transaction-fee cells are omitted by PDF text extraction.
        commission = "0"
        settlement_currency = lines[index + 9]
        exchange_rate = decimal_text(lines[index + 10])
        total_amount = decimal_text(lines[index + 11])
        raw = "\n".join(lines[index - 6 : index + 12])
        key = stable_key("CESKA", account, "DIVIDEND", reference)

        events.append(
            CeskaEvent(
                source_key=key,
                account_number=account,
                event_reference=reference,
                event_date=payment_date,
                settlement_date=payment_date,
                event_type="DIVIDEND",
                asset_name=asset_name,
                isin=line,
                market=None,
                quantity=quantity,
                unit_price=None,
                gross_amount=gross_amount,
                commission=commission,
                tax=tax,
                total_amount=total_amount,
                trade_currency=trade_currency,
                settlement_currency=settlement_currency,
                exchange_rate=exchange_rate,
                source_file=source_file,
                raw_text=raw,
            )
        )
    return events


def parse_account_fee(text: str, source_file: str) -> list[CeskaEvent]:
    marker = "SPRÁVA MAJETKOVÉHO ÚČTU"
    if marker not in text:
        return []
    section = text.split(marker, 1)[1].split("VYSVĚTLIVKY", 1)[0]
    lines = clean_lines(section)
    account = account_number(text)

    date_pair = next(
        (i for i, line in enumerate(lines) if re.fullmatch(r"\d{2}\.\d{2}\.\d{4} \d{2}\.\d{2}\.\d{4} .+", line)),
        None,
    )
    if date_pair is None:
        raise ValueError("Account-fee row was not found")
    match = re.fullmatch(r"(\d{2}\.\d{2}\.\d{4}) (\d{2}\.\d{2}\.\d{4}) (.+)", lines[date_pair])
    assert match
    record_date, due_date, service_start = match.groups()
    service_detail = lines[date_pair + 1]
    amount = decimal_text(lines[date_pair + 2])
    currency = lines[date_pair + 3]
    status = lines[date_pair + 4]
    if status.casefold() == "neuhrazeno":
        # Unpaid fees have no payment date and are not completed cash movements.
        print(
            f"WARNING: {source_file}: skipped unpaid account fee "
            f"{amount} {currency} (due {iso_date(due_date)})."
        )
        return []
    payment_date = iso_date(lines[date_pair + 5])
    description = f"{service_start} - {service_detail}"
    raw = "\n".join(lines[date_pair : date_pair + 6])
    key = stable_key("CESKA", account, "ACCOUNT_FEE", record_date, due_date, amount, description)

    return [
        CeskaEvent(
            source_key=key,
            account_number=account,
            event_reference=None,
            event_date=payment_date,
            settlement_date=iso_date(due_date),
            event_type="ACCOUNT_FEE",
            asset_name=None,
            isin=None,
            market=None,
            quantity=None,
            unit_price=None,
            gross_amount=amount,
            commission=amount,
            tax="0",
            total_amount=amount,
            trade_currency=currency,
            settlement_currency=currency,
            exchange_rate=None,
            source_file=source_file,
            raw_text=raw,
        )
    ]


def parse_pdf(path: Path) -> list[CeskaEvent]:
    text = pdf_text(path)
    events = parse_trades(text, path.name)
    events.extend(parse_dividends(text, path.name))
    events.extend(parse_account_fee(text, path.name))
    return events


def validate_table(connection: sqlite3.Connection) -> None:
    required = set(CeskaEvent.__annotations__)
    actual = {row[1] for row in connection.execute(f"PRAGMA table_info({TABLE_NAME})")}
    if not actual:
        raise RuntimeError(f"Table {TABLE_NAME!r} does not exist. Create it in DBeaver first.")
    missing = required - actual
    if missing:
        raise RuntimeError(f"Table {TABLE_NAME!r} is missing columns: {sorted(missing)}")


def upsert_event(connection: sqlite3.Connection, event: CeskaEvent) -> None:
    values = asdict(event)
    columns = list(values)
    placeholders = ", ".join(f":{column}" for column in columns)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in columns
        if column != "source_key"
    )
    connection.execute(
        f"""
        INSERT INTO {TABLE_NAME} ({', '.join(columns)})
        VALUES ({placeholders})
        ON CONFLICT(source_key) DO UPDATE SET
            {updates},
            updated_at = CURRENT_TIMESTAMP
        """,
        values,
    )


def main() -> None:
    pdf_files = sorted(PDF_FOLDER.glob("*.pdf"))
    if not pdf_files:
        raise RuntimeError(f"No PDF files found in {PDF_FOLDER}")

    parsed: list[CeskaEvent] = []
    for path in pdf_files:
        events = parse_pdf(path)
        parsed.extend(events)
        print(f"{path.name}: {len(events)} event(s)")

    with sqlite3.connect(DATABASE_PATH) as connection:
        validate_table(connection)
        before = connection.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]
        for event in parsed:
            upsert_event(connection, event)
        connection.commit()
        after = connection.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0]

    print(f"Parsed: {len(parsed)} event(s)")
    print(f"Rows before: {before}; rows after: {after}; new rows: {after - before}")


if __name__ == "__main__":
    main()
