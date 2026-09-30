"""جلب وتخزين الشموع التاريخية"""
import io
import os
import time
import zipfile
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from backtest_config import CFG

VISION_BASE = "https://data.binance.vision/data/spot/monthly/klines"


def _normalize_ts(ts):
    ts = int(ts)
    if ts > 10**17: return ts // 1_000_000
    if ts > 10**14: return ts // 1_000_000_000
    if ts > 10**12: return ts // 1000
    return ts


def init_cache():
    con = sqlite3.connect(CFG["CACHE_DB"])
    con.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            symbol TEXT NOT NULL,
            open_time INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_volume REAL, taker_buy_base REAL,
            PRIMARY KEY (symbol, open_time)
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_sym_time ON candles(symbol, open_time)")
    con.commit()
    return con


def cache_has_data(con, symbol, days=180):
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    row = con.execute(
        "SELECT COUNT(*) FROM candles WHERE symbol=? AND open_time>=?",
        (symbol, cutoff)
    ).fetchone()
    return row[0] >= days * 90  # ~90 شمعة/يوم × 15 دقيقة


def fetch_vision_month(symbol, year, month):
    """يجلب ملف شهر واحد من Binance Vision"""
    fname = f"{symbol}-15m-{year}-{month:02d}.zip"
    url = f"{VISION_BASE}/{symbol}/15m/{fname}"
    try:
        r = requests.get(url, timeout=60)
        if r.status_code != 200:
            return []
        z = zipfile.ZipFile(io.BytesIO(r.content))
        rows = []
        with z.open(z.namelist()[0]) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.decode("utf-8").split(",")
                if not parts[0].isdigit():
                    continue
                try:
                    ts_sec = _normalize_ts(int(parts[0]))
                    rows.append((
                        symbol,
                        ts_sec * 1000,
                        float(parts[1]), float(parts[2]),
                        float(parts[3]), float(parts[4]),
                        float(parts[5]), float(parts[7]),
                        float(parts[9]) if len(parts) > 9 else 0.0,
                    ))
                except Exception:
                    continue
        return rows
    except Exception as e:
        print(f"  ⚠️ {symbol} {year}-{month:02d}: {e}")
        return []


def fetch_and_cache(con, symbol, days=180):
    """يجلب آخر N يوماً ويخزّنها"""
    if cache_has_data(con, symbol, days):
        print(f"  ✅ {symbol}: موجود في cache")
        return

    print(f"  ⬇️  {symbol}: جلب من Binance Vision...")
    now = datetime.now(timezone.utc)
    months = []
    for i in range(8):
        d = now.replace(day=1) - timedelta(days=i * 30)
        months.append((d.year, d.month))
    months = list(dict.fromkeys(months))

    total = 0
    for (year, month) in months:
        rows = fetch_vision_month(symbol, year, month)
        if rows:
            con.executemany(
                "INSERT OR IGNORE INTO candles VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            con.commit()
            total += len(rows)
        time.sleep(0.3)

    print(f"  ✅ {symbol}: {total} شمعة")


def load_candles(con, symbol, days=180):
    """يقرأ الشموع من cache"""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows = con.execute(
        """SELECT open_time, open, high, low, close, volume, quote_volume, taker_buy_base
           FROM candles WHERE symbol=? AND open_time>=?
           ORDER BY open_time""",
        (symbol, cutoff),
    ).fetchall()
    return [
        {
            "open_time": r[0], "open": r[1], "high": r[2],
            "low": r[3], "close": r[4], "volume": r[5],
            "quote_volume": r[6], "taker_buy_base": r[7],
        }
        for r in rows
    ]


def prepare_all_data():
    """يحمّل كل العملات"""
    con = init_cache()
    for sym in CFG["SYMBOLS"]:
        fetch_and_cache(con, sym, CFG["DAYS"])
    return con
