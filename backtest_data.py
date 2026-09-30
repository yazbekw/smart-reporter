"""
backtest_data.py — جلب وتخزين الشموع في Supabase
"""
import io
import time
import zipfile
import requests
from datetime import datetime, timezone, timedelta
from supabase import create_client
from backtest_config import CFG

VISION_BASE = "https://data.binance.vision/data/spot/monthly/klines"

_sb = None


def _sb_client():
    global _sb
    if _sb is None:
        _sb = create_client(CFG["SUPABASE_URL"], CFG["SUPABASE_KEY"])
    return _sb


def _normalize_ts(ts):
    ts = int(ts)
    if ts > 10**17: return ts // 1_000_000
    if ts > 10**14: return ts // 1_000_000_000
    if ts > 10**12: return ts // 1000
    return ts


# ============================================================
# قراءة من Supabase
# ============================================================
def _cache_count(symbol, days):
    """كم شمعة موجودة في cache؟"""
    sb = _sb_client()
    cutoff = int(
        (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    )
    res = (
        sb.table("candles")
        .select("open_time", count="exact")
        .eq("symbol", symbol)
        .gte("open_time", cutoff)
        .execute()
    )
    return res.count or 0


def cache_has_data(symbol, days):
    n = _cache_count(symbol, days)
    return n >= days * 90  # ~90 شمعة/يوم


def load_candles(symbol, days=None):
    """يقرأ الشموع من Supabase (مع pagination)"""
    if days is None:
        days = CFG["DAYS"]
    sb = _sb_client()
    cutoff = int(
        (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    )

    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        res = (
            sb.table("candles")
            .select("open_time,open,high,low,close,volume,quote_volume,taker_buy_base")
            .eq("symbol", symbol)
            .gte("open_time", cutoff)
            .order("open_time")
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    return all_rows


# ============================================================
# جلب من Binance Vision
# ============================================================
def fetch_vision_month(symbol, year, month):
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
                    rows.append({
                        "symbol": symbol,
                        "open_time": ts_sec * 1000,
                        "open": float(parts[1]),
                        "high": float(parts[2]),
                        "low": float(parts[3]),
                        "close": float(parts[4]),
                        "volume": float(parts[5]),
                        "quote_volume": float(parts[7]),
                        "taker_buy_base": float(parts[9]) if len(parts) > 9 else 0.0,
                        "interval": "15m",
                    })
                except Exception:
                    continue
        return rows
    except Exception as e:
        print(f"  ⚠️ {symbol} {year}-{month:02d}: {e}")
        return []


# ============================================================
# حفظ في Supabase (batches)
# ============================================================
def save_candles(rows, batch_size=500):
    """يُخزّن على دفعات لتجنب مشاكل payload"""
    if not rows:
        return 0
    sb = _sb_client()
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        try:
            sb.table("candles").upsert(batch).execute()
            total += len(batch)
        except Exception as e:
            print(f"  ❌ batch {i}: {e}")
    return total


# ============================================================
# التنسيق العام
# ============================================================
def fetch_and_store(symbol, days=None, progress_cb=None):
    """يجلب آخر N يوم من Binance Vision ويخزّنها"""
    if days is None:
        days = CFG["DAYS"]

    if cache_has_data(symbol, days):
        if progress_cb:
            progress_cb(f"  ✅ {symbol}: موجود في cache")
        return 0

    if progress_cb:
        progress_cb(f"  ⬇️  {symbol}: جلب من Binance Vision...")

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
            saved = save_candles(rows)
            total += saved
            if progress_cb:
                progress_cb(f"  📥 {symbol} {year}-{month:02d}: {saved} شمعة")
        time.sleep(0.3)

    return total


def prepare_all_data(progress_cb=None):
    """يجلب كل العملات"""
    total = 0
    for sym in CFG["SYMBOLS"]:
        total += fetch_and_store(sym, CFG["DAYS"], progress_cb)
    return total


# ============================================================
# Supabase Backtest runs
# ============================================================
def create_backtest_run(config_json):
    sb = _sb_client()
    res = sb.table("backtest_runs").insert({
        "status": "running",
        "config_json": config_json,
    }).execute()
    return res.data[0]["id"] if res.data else None


def update_backtest_run(run_id, **fields):
    sb = _sb_client()
    fields["finished_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("backtest_runs").update(fields).eq("id", run_id).execute()


def save_backtest_trades(run_id, trades):
    """يُخزّن الصفقات على دفعات"""
    if not trades:
        return
    sb = _sb_client()
    batch_size = 500
    for i in range(0, len(trades), batch_size):
        batch = []
        for t in trades[i:i + batch_size]:
            batch.append({
                "run_id": run_id,
                "symbol": t.get("symbol"),
                "direction": t.get("direction"),
                "entry_time": t.get("time_utc"),
                "entry_price": t.get("entry"),
                "exit_price": t.get("exit"),
                "exit_reason": t.get("exit_reason"),
                "sl_pct": t.get("sl_pct"),
                "tp_pct": t.get("tp_pct"),
                "rr": t.get("rr"),
                "position_usd": t.get("size_usd"),
                "pnl_usd": t.get("pnl_usd"),
                "pnl_pct": t.get("pnl_pct"),
                "hold_candles": t.get("hold_candles"),
                "classification": t.get("classification"),
                "final_conf": t.get("final_conf"),
                "composite": t.get("composite"),
            })
        try:
            sb.table("backtest_trades").insert(batch).execute()
        except Exception as e:
            print(f"  ❌ trades batch {i}: {e}")
