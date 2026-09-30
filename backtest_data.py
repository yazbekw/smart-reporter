"""
backtest_data.py — جلب وتخزين الشموع مع تشخيص كامل
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
        url = CFG["SUPABASE_URL"]
        key = CFG["SUPABASE_KEY"]
        print(f"[SB] URL: {url[:40]}...")
        print(f"[SB] Key: {key[:25]}... (len={len(key)})")
        _sb = create_client(url, key)
    return _sb


def _normalize_ts(ts):
    ts = int(ts)
    if ts > 10**17: return ts // 1_000_000
    if ts > 10**14: return ts // 1_000_000_000
    if ts > 10**12: return ts // 1000
    return ts


# ============================================================
# تشخيص الجدول
# ============================================================
def diagnose_table():
    """يتحقق من وجود جدول candles وإمكانية الكتابة"""
    sb = _sb_client()
    result = {"ok": False, "issues": [], "info": {}}

    # 1. هل الجدول موجود؟
    try:
        res = sb.table("candles").select("symbol").limit(1).execute()
        result["info"]["table_exists"] = True
        result["info"]["existing_rows"] = "unknown (res.data length)"
        print("[DIAG] ✅ جدول candles موجود")
    except Exception as e:
        result["issues"].append(f"جدول غير موجود أو خطأ: {e}")
        print(f"[DIAG] ❌ جدول candles: {e}")
        return result

    # 2. اختبار كتابة
    test_row = {
        "symbol": "__TEST__",
        "open_time": 0,
        "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0,
        "volume": 0.0, "quote_volume": 0.0, "taker_buy_base": 0.0,
        "interval": "15m",
    }
    try:
        sb.table("candles").upsert(test_row).execute()
        print("[DIAG] ✅ الكتابة تعمل")
        # نظّف
        sb.table("candles").delete().eq("symbol", "__TEST__").execute()
        result["ok"] = True
    except Exception as e:
        result["issues"].append(f"فشل الكتابة: {e}")
        print(f"[DIAG] ❌ الكتابة: {e}")

    return result


# ============================================================
# قراءة من Supabase
# ============================================================
def _cache_count(symbol, days):
    sb = _sb_client()
    cutoff = int(
        (datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000
    )
    try:
        res = (
            sb.table("candles")
            .select("open_time", count="exact")
            .eq("symbol", symbol)
            .gte("open_time", cutoff)
            .execute()
        )
        return res.count or 0
    except Exception as e:
        print(f"[cache_count] {symbol}: {e}")
        return 0


def cache_has_data(symbol, days):
    n = _cache_count(symbol, days)
    need = days * 90
    print(f"[cache] {symbol}: {n} شمعة (المطلوب ≥ {need})")
    return n >= need


def load_candles(symbol, days=None):
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
        try:
            res = (
                sb.table("candles")
                .select("open_time,open,high,low,close,volume,quote_volume,taker_buy_base")
                .eq("symbol", symbol)
                .gte("open_time", cutoff)
                .order("open_time")
                .range(offset, offset + page_size - 1)
                .execute()
            )
        except Exception as e:
            print(f"[load_candles] {symbol} page {offset}: {e}")
            break
        rows = res.data or []
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    print(f"[load_candles] {symbol}: {len(all_rows)} شمعة")
    return all_rows


# ============================================================
# جلب من Binance Vision
# ============================================================
def fetch_vision_month(symbol, year, month, verbose=True):
    """يجلب ملف شهر واحد — مع logging مفصّل"""
    fname = f"{symbol}-15m-{year}-{month:02d}.zip"
    url = f"{VISION_BASE}/{symbol}/15m/{fname}"

    if verbose:
        print(f"[VISION] GET {url}")

    try:
        r = requests.get(url, timeout=60)
        if verbose:
            print(f"[VISION] HTTP {r.status_code}, size={len(r.content)} bytes")

        if r.status_code != 200:
            return []

        try:
            z = zipfile.ZipFile(io.BytesIO(r.content))
        except zipfile.BadZipFile as e:
            print(f"[VISION] ❌ ZIP corrupt: {e}")
            return []

        names = z.namelist()
        if not names:
            print(f"[VISION] ❌ ZIP فارغ")
            return []

        rows = []
        with z.open(names[0]) as f:
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

        if verbose:
            print(f"[VISION] parsed {len(rows)} شمعة من {year}-{month:02d}")

        return rows

    except Exception as e:
        print(f"[VISION] ❌ {symbol} {year}-{month:02d}: {e}")
        return []


# ============================================================
# حفظ في Supabase — مع logging مفصّل
# ============================================================
def save_candles(rows, batch_size=500, verbose=True):
    """يُخزّن على دفعات — مع تسجيل كل خطأ"""
    if not rows:
        return 0

    sb = _sb_client()
    total = 0
    failed = 0

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        try:
            res = sb.table("candles").upsert(batch).execute()
            total += len(batch)
            if verbose and (i // batch_size) % 5 == 0:
                print(f"[SAVE] ✅ batch {i}-{i+len(batch)} ({total}/{len(rows)})")
        except Exception as e:
            failed += len(batch)
            err_str = str(e)[:200]
            print(f"[SAVE] ❌ batch {i}-{i+len(batch)}: {err_str}")

    if verbose:
        print(f"[SAVE] انتهى: نجح={total}, فشل={failed}")

    return total


# ============================================================
# الجلب والتخزين
# ============================================================
def fetch_and_store(symbol, days=None, progress_cb=None):
    """يجلب آخر N يوم ويخزّنها"""
    if days is None:
        days = CFG["DAYS"]

    def _log(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    # فحص cache أولاً
    try:
        if cache_has_data(symbol, days):
            _log(f"  ✅ {symbol}: موجود في cache")
            return 0
    except Exception as e:
        _log(f"  ⚠️ {symbol}: فحص cache فشل ({str(e)[:80]})")

    _log(f"  ⬇️  {symbol}: جلب من Binance Vision...")

    now = datetime.now(timezone.utc)
    months = []
    for i in range(8):
        d = now.replace(day=1) - timedelta(days=i * 30)
        months.append((d.year, d.month))
    months = list(dict.fromkeys(months))

    total = 0
    for (year, month) in months:
        try:
            rows = fetch_vision_month(symbol, year, month, verbose=False)
            if rows:
                saved = save_candles(rows, verbose=False)
                total += saved
                _log(f"  📥 {symbol} {year}-{month:02d}: {saved} شمعة")
            else:
                _log(f"  ⚪ {symbol} {year}-{month:02d}: لا بيانات")
        except Exception as e:
            _log(f"  ⚠️ {symbol} {year}-{month:02d}: {str(e)[:80]}")
        time.sleep(0.3)

    return total


def prepare_all_data(progress_cb=None):
    """يجلب كل العملات"""
    def _log(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    # تشخيص أولاً
    _log("🔍 تشخيص اتصال Supabase...")
    diag = diagnose_table()
    if not diag["ok"]:
        _log(f"❌ فشل التشخيص: {diag['issues']}")
        return 0
    _log("✅ الاتصال بـ Supabase سليم")

    total = 0
    for sym in CFG["SYMBOLS"]:
        total += fetch_and_store(sym, CFG["DAYS"], progress_cb)
    return total


# ============================================================
# Backtest runs
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
            print(f"[trades] batch {i}: {e}")
