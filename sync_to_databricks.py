"""Copy dashboard-ready SQLite tables to Databricks Delta tables.

SQLite remains the local source of truth. Each run replaces the contents of
the corresponding Databricks table through a temporary Delta staging table.
Authentication uses Databricks OAuth user login, so no token is stored here.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from databricks import sql


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = ROOT / "DataBase" / "financials.db"
ENV_PATH = ROOT / ".env"


@dataclass(frozen=True)
class TableSpec:
    sqlite_name: str
    databricks_name: str
    columns: tuple[tuple[str, str], ...]
    databricks_schema: str | None = None


TABLES = (
    TableSpec(
        sqlite_name="money_flow_transactions",
        databricks_name="money_flow_transactions",
        databricks_schema="money_flow",
        columns=(
            ("transaction_id", "BIGINT"),
            ("account_id", "STRING"),
            ("entry_reference", "STRING"),
            ("reservation_id", "STRING"),
            ("amount_minor", "BIGINT"),
            ("currency", "STRING"),
            ("direction", "STRING"),
            ("status", "STRING"),
            ("booking_date", "STRING"),
            ("value_date", "STRING"),
            ("transaction_code", "STRING"),
            ("transaction_issuer", "STRING"),
            ("counterparty_name", "STRING"),
            ("counterparty_iban", "STRING"),
            ("payment_reference", "STRING"),
            ("description", "STRING"),
            ("source_currency", "STRING"),
            ("target_currency", "STRING"),
            ("exchange_rate", "STRING"),
            ("raw_json", "STRING"),
            ("created_at", "STRING"),
            ("updated_at", "STRING"),
        ),
    ),
    TableSpec(
        sqlite_name="core_investment_assets",
        databricks_name="investment_assets",
        columns=(
            ("asset_id", "BIGINT"),
            ("asset_name", "STRING"),
            ("asset_type", "STRING"),
            ("asset_quantity_unit", "STRING"),
        ),
    ),
    TableSpec(
        sqlite_name="core_investment_transactions",
        databricks_name="investment_transactions",
        columns=(
            ("investment_transaction_id", "BIGINT"),
            ("pdf_transaction_id", "BIGINT"),
            ("asset_id", "BIGINT"),
            ("transaction_date", "STRING"),
            ("transaction_type", "STRING"),
            ("quantity", "DOUBLE"),
            ("unit_price", "DOUBLE"),
            ("commission", "DOUBLE"),
            ("tax", "DOUBLE"),
            ("total_amount", "DOUBLE"),
            ("currency", "STRING"),
            ("total_amount_czk", "DOUBLE"),
            ("total_amount_usd", "DOUBLE"),
        ),
    ),
    TableSpec(
        sqlite_name="core_currency_rates",
        databricks_name="currency_rates",
        columns=(
            ("fx_rate_id", "BIGINT"),
            ("rate_date", "STRING"),
            ("base_currency", "STRING"),
            ("quote_currency", "STRING"),
            ("rate", "DOUBLE"),
            ("source_col", "STRING"),
            ("imported_at", "STRING"),
        ),
    ),
    TableSpec(
        sqlite_name="stock_price_overtime",
        databricks_name="stock_price_overtime",
        columns=(
            ("asset_id", "BIGINT"),
            ("asset_name", "STRING"),
            ("Date", "STRING"),
            ("Close", "DOUBLE"),
            ("Volume", "BIGINT"),
            ("Symbol", "STRING"),
            ("Currency", "STRING"),
        ),
    ),
)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Set {name} in {ENV_PATH}")
    return value


def safe_identifier(value: str, env_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise RuntimeError(
            f"{env_name} must contain only letters, numbers, and underscores"
        )
    return value


def qualified_name(catalog: str, schema: str, table: str) -> str:
    return f"`{catalog}`.`{schema}`.`{table}`"


def sqlite_rows(
    connection: sqlite3.Connection,
    spec: TableSpec,
) -> list[tuple[object, ...]]:
    columns = ", ".join(name for name, _ in spec.columns)
    return connection.execute(
        f"SELECT {columns} FROM {spec.sqlite_name}"
    ).fetchall()


def sync_table(
    cursor,
    catalog: str,
    schema: str,
    spec: TableSpec,
    rows: list[tuple[object, ...]],
) -> None:
    target = qualified_name(catalog, schema, spec.databricks_name)
    staging = qualified_name(catalog, schema, f"_sync_{spec.databricks_name}")
    definition = ", ".join(
        f"`{name}` {data_type}" for name, data_type in spec.columns
    )
    column_list = ", ".join(f"`{name}`" for name, _ in spec.columns)
    row_placeholders = "(" + ", ".join("?" for _ in spec.columns) + ")"

    cursor.execute(f"CREATE TABLE IF NOT EXISTS {target} ({definition}) USING DELTA")
    cursor.execute(f"DESCRIBE TABLE {target}")
    existing_columns = {
        row[0]
        for row in cursor.fetchall()
        if row[0] and not str(row[0]).startswith("#")
    }
    missing_columns = [
        (name, data_type)
        for name, data_type in spec.columns
        if name not in existing_columns
    ]
    if missing_columns:
        missing_definition = ", ".join(
            f"`{name}` {data_type}" for name, data_type in missing_columns
        )
        cursor.execute(f"ALTER TABLE {target} ADD COLUMNS ({missing_definition})")

    cursor.execute(f"CREATE OR REPLACE TABLE {staging} ({definition}) USING DELTA")

    # Send several rows per SQL statement. This is much faster than issuing one
    # remote INSERT for every FX-rate row.
    batch_size = 100
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        values_sql = ", ".join(row_placeholders for _ in batch)
        parameters = [value for row in batch for value in row]
        cursor.execute(
            f"INSERT INTO {staging} ({column_list}) VALUES {values_sql}",
            parameters=parameters,
        )

    cursor.execute(
        f"INSERT OVERWRITE TABLE {target} ({column_list}) "
        f"SELECT {column_list} FROM {staging}"
    )
    cursor.execute(f"DROP TABLE {staging}")
    print(f"{spec.sqlite_name} -> {target}: {len(rows)} row(s)")


def main() -> None:
    load_env_file(ENV_PATH)

    server_hostname = required_env("DATABRICKS_SERVER_HOSTNAME")
    http_path = required_env("DATABRICKS_HTTP_PATH")
    catalog = safe_identifier(
        required_env("DATABRICKS_CATALOG"),
        "DATABRICKS_CATALOG",
    )
    schema = safe_identifier(
        os.environ.get("DATABRICKS_SCHEMA", "portfolio").strip() or "portfolio",
        "DATABRICKS_SCHEMA",
    )

    with sqlite3.connect(DATABASE_PATH) as sqlite_connection:
        existing_tables = {
            row[0]
            for row in sqlite_connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        available_specs = [
            spec for spec in TABLES if spec.sqlite_name in existing_tables
        ]
        missing_tables = [
            spec.sqlite_name
            for spec in TABLES
            if spec.sqlite_name not in existing_tables
        ]
        for table_name in missing_tables:
            print(f"Skipping missing SQLite table: {table_name}")

        local_data = {
            spec.sqlite_name: sqlite_rows(sqlite_connection, spec)
            for spec in available_specs
        }

    with sql.connect(
        server_hostname=server_hostname,
        http_path=http_path,
        auth_type="databricks-oauth",
    ) as databricks_connection:
        with databricks_connection.cursor() as cursor:
            for spec in available_specs:
                target_schema = safe_identifier(
                    spec.databricks_schema or schema, "table schema"
                )
                cursor.execute(
                    f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{target_schema}`"
                )
                sync_table(
                    cursor,
                    catalog,
                    target_schema,
                    spec,
                    local_data[spec.sqlite_name],
                )

    print("Databricks synchronization completed.")


if __name__ == "__main__":
    main()
