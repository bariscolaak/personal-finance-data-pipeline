"""Download historical CNB exchange rates and store them in SQLite.

The Czech National Bank publishes yearly, public TXT files. No login, OAuth,
API key, or ngrok tunnel is required.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "DataBase" / "financials.db"
TABLE_NAME = "core_currency_rates"

CNB_YEAR_URL = (
    "https://www.cnb.cz/en/financial-markets/foreign-exchange-market/"
    "central-bank-exchange-rate-fixing/central-bank-exchange-rate-fixing/"
    "year.txt?year={year}"
)

# Always keep these currencies available, even before they appear in core data.
DEFAULT_CURRENCIES = {"USD", "TRY", "EUR"}


def validate_table(connection: sqlite3.Connection) -> None:
    required = {
        "rate_date",
        "base_currency",
        "quote_currency",
        "rate",
        "source_col",
        "imported_at",
    }
    actual = {
        row[1] for row in connection.execute(f"PRAGMA table_info({TABLE_NAME})")
    }
    if not actual:
        raise RuntimeError(
            f"Table {TABLE_NAME!r} does not exist. Create it in DBeaver first."
        )
    missing = required - actual
    if missing:
        raise RuntimeError(
            f"Table {TABLE_NAME!r} is missing columns: {sorted(missing)}"
        )


def required_years(connection: sqlite3.Connection) -> list[int]:
    """Return every transaction year plus the current year."""
    years = {date.today().year}
    rows = connection.execute(
        """
        SELECT DISTINCT substr(transaction_date, 1, 4)
        FROM core_investment_transactions
        WHERE transaction_date IS NOT NULL
        """
    )
    for (year_text,) in rows:
        if year_text and re.fullmatch(r"\d{4}", year_text):
            years.add(int(year_text))
    return sorted(years)


def required_currencies(connection: sqlite3.Connection) -> set[str]:
    """Return non-CZK currencies used by investment transactions."""
    currencies = set(DEFAULT_CURRENCIES)
    rows = connection.execute(
        """
        SELECT DISTINCT upper(currency)
        FROM core_investment_transactions
        WHERE currency IS NOT NULL
          AND upper(currency) <> 'CZK'
        """
    )
    currencies.update(currency for (currency,) in rows if currency)
    return currencies


def download_year(year: int) -> str:
    url = CNB_YEAR_URL.format(year=year)
    request = Request(
        url,
        headers={"User-Agent": "financials-x01/1.0 (personal finance project)"},
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read()

    # CNB files normally decode as UTF-8. The fallback supports older files.
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return payload.decode("cp1250")


def parse_year(
    text: str,
    wanted_currencies: set[str],
) -> list[tuple[str, str, str, str, str]]:
    """Parse CNB's yearly pipe-separated format.

    CNB may quote currencies in units such as "100 JPY". Stored rates are
    normalized so they always mean: 1 base_currency = rate CZK.
    """
    header: list[tuple[Decimal, str]] | None = None
    parsed: list[tuple[str, str, str, str, str]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]

        if parts[0].lower() == "date":
            new_header: list[tuple[Decimal, str]] = []
            for item in parts[1:]:
                match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s+([A-Z]{3})", item)
                if not match:
                    raise ValueError(f"Unexpected CNB header item: {item!r}")
                new_header.append((Decimal(match.group(1)), match.group(2)))
            header = new_header
            continue

        if header is None:
            continue

        try:
            rate_date = datetime.strptime(parts[0], "%d.%m.%Y").date().isoformat()
        except ValueError:
            continue

        values = parts[1:]
        if len(values) != len(header):
            raise ValueError(
                f"CNB row {parts[0]} has {len(values)} values; "
                f"the header has {len(header)} currencies"
            )

        for (amount, currency), value_text in zip(header, values):
            if currency not in wanted_currencies or not value_text:
                continue
            quoted_rate = Decimal(value_text.replace(",", "."))
            rate_per_one = quoted_rate / amount
            parsed.append(
                (
                    rate_date,
                    currency,
                    "CZK",
                    str(rate_per_one.normalize()),
                    "CNB",
                )
            )

    return parsed


def save_rates(
    connection: sqlite3.Connection,
    rates: list[tuple[str, str, str, str, str]],
) -> None:
    connection.executemany(
        f"""
        INSERT INTO {TABLE_NAME} (
            rate_date,
            base_currency,
            quote_currency,
            rate,
            source_col
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(rate_date, base_currency, quote_currency)
        DO UPDATE SET
            rate = excluded.rate,
            source_col = excluded.source_col,
            imported_at = CURRENT_TIMESTAMP
        """,
        rates,
    )


def main() -> None:
    with sqlite3.connect(DATABASE_PATH) as connection:
        validate_table(connection)
        years = required_years(connection)
        currencies = required_currencies(connection)

        print(f"Years: {', '.join(map(str, years))}")
        print(f"Currencies: {', '.join(sorted(currencies))}")

        total = 0
        for year in years:
            text = download_year(year)
            rates = parse_year(text, currencies)
            save_rates(connection, rates)
            total += len(rates)
            print(f"{year}: {len(rates)} rate row(s)")

        connection.commit()
        stored = connection.execute(
            f"SELECT COUNT(*) FROM {TABLE_NAME}"
        ).fetchone()[0]

    print(f"Processed: {total} rate row(s)")
    print(f"Stored in {TABLE_NAME}: {stored} row(s)")


if __name__ == "__main__":
    main()
