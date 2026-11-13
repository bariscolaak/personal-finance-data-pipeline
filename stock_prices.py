from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen
import yfinance as yf
import pandas as pd


ROOT = Path(__file__).resolve().parent
DATA_BASE_PATH = ROOT / "DataBase" / "financials.db"


ticker_map = {
    "ALTNY": "ALTNY.IS",
    "ASELS": "ASELS.IS",
    "Advanced Micro Devices Inc": "AMD",
    "Alphabet Inc A": "GOOGL",
    "Apple": "AAPL",
    "BTC": "BTC-USD",
    "Nvidia Corp": "NVDA",
    "Northrop Grumman Corp": "NOC",
    "Tesla Motors Inc": "TSLA",
    "Leonardo SpA": "LDO.MI",
    # Same BYD H-share ISIN CNE100000296, quoted in EUR in Frankfurt.
    # Valuation proxy: the bank trades on Tradegate, not Frankfurt.
    "BYD Co Ltd": "BY6.F",
    # ISIN KYG3777B1032; EUR Frankfurt price proxy for the Tradegate holding.
    "Geely Automobile": "GRU.F",
    "SPACE EXPLORATION TECHN-CL A": "SPCX",
    "ONRYT": "ONRYT.IS",
    "OTKAR": "OTKAR.IS",
    "SDTTR": "SDTTR.IS",
    "Taiwan Semiconductor ADR": "TSM",
    "XAU": "GC=F",
}

trackers = [ticker_map[name] for name in ticker_map]


hist_frames = []

for symbol in trackers:
    ticker = yf.Ticker(symbol)

    # SpaceX listed on 2026-06-12; exclude any earlier ticker history.
    history_options = {"start": "2026-06-12"} if symbol == "SPCX" else {"period": "2y"}
    df = ticker.history(**history_options)[["Close", "Volume"]].reset_index()
    df["Currency"] = ticker.fast_info.get("currency")
    df["Symbol"] = symbol
    hist_frames.append(df)

hist_df = pd.concat(hist_frames, ignore_index=True)

hist_df["Date"] = pd.to_datetime(
    hist_df["Date"],
    utc=True
).dt.date.astype(str)


## ticker mapi tersine cevirip onu bir dictionary olarak kullanarak reversse ticker map a kaydediyoruz sonra symbol dan karsiliklarini aliyoruz.
reverse_ticker_map = {symbol: name for name, symbol in ticker_map.items()}

hist_df["asset_name"] = hist_df["Symbol"].map(reverse_ticker_map)


rows = list(
    hist_df[["Date", "Close", "Volume", "Symbol", "Currency", "asset_name"]]
    .itertuples(index=False, name=None)
)


with sqlite3.connect(DATA_BASE_PATH) as connection:
    connection.executemany(
    """
    INSERT INTO stock_price_overtime (
        Date,
        Close,
        Volume,
        Symbol,
        Currency,
        asset_name
    )
    VALUES (?, ?, ?, ?, ?, ?)

    ON CONFLICT(Date, Symbol)
    DO UPDATE SET
        Close = excluded.Close,
        Volume = excluded.Volume,
        Currency = excluded.Currency,
        asset_name = excluded.asset_name
    """,
    rows,
)

connection.commit()

