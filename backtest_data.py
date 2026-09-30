"""
backtest_data.py
================
جلب وتخزين الشموع التاريخية في Supabase

التحسينات:
- timeout أطول لـ Supabase (120 ثانية)
- batch_size صغير (200) لتجنب الفشل
- Retry تلقائي عند فشل أي batch
- تنظيف القيم (NaN, inf)
- إعادة إنشاء Supabase client دورياً
- Logging مفصّل
"""
import io
import time
import math
import zipfile
import requests
from datetime import datetime, timezone, timedelta
from supabase import create_client
from backtest_config import CFG

VISION_BASE = "https://data.binance.vision/data/spot/monthly/klines"

# إعدادات الحفظ
SAVE_BATCH_SIZE = 200           # عدد الصفوف لكل دفعة
SAVE_RETRIES = 3                # عدد محاولات إعادة الإرسال
CLIENT_RESET_EVERY = 30         # إعادة إنشاء client كل N دفعة
VISION_TIMEOUT = 120            # ثوانٍ لجلب ملف ZIP
SUPABASE_TIMEOUT = 120          # ثوانٍ لعمليات Supabase

_sb = None
_sb_batches_since_reset = 0


# ============================================================
# Supabase Client
# ============================================================
def _sb_client():
    """
    يبني Supabase client مع timeout أطول.
    يعيد الاستخدام ما لم يُطلب reset.
    """
    global _sb, _sb_batches_since_reset

    if _sb is not None:
        return _sb

    url = CFG["SUPABASE_URL"]
    key = CFG["SUPABASE_KEY"]

    if not url or not key:
        raise RuntimeError("SUPABASE_URL أو SUPABASE_KEY مفقود")

    print(f"[SB] init: {url[:50]}... key_len={len(key)}")

    # محاولة 1: استخدام ClientOptions مع timeout مخصص
    try:
        from supabase import ClientOptions
        options = ClientOptions(
            postgrest_client_timeout=SUPABASE_TIMEOUT,
        )
        _sb = create_client(url, key, options=options)
        print(f"[SB] ✅ ClientOptions (timeout={SUPABASE_TIMEOUT}s)")
    except (ImportError, TypeError):
        # محاولة 2: بدون options
        _sb = create_client(url, key)
        print("[SB] ⚠️ بدون ClientOptions (timeout افتراضي)")

    _sb_batches_since_reset = 0
    return _sb


def _reset_sb_client():
    """يعيد إنشاء client لتجنب connection pool issues"""
    global _sb, _sb_batches_since_reset
    _sb = None
    _sb_batches_since_reset = 0
    print("[SB] 🔄 reset client")


# ============================================================
# أدوات
# ============================================================
def _normalize_ts(ts):
    """يوحّد أطوال أرقام Unix timestamps"""
    ts = int(ts)
    if ts > 10**17: return ts // 1_000_000
    if ts > 10**14: return ts // 1_000_000_000
    if ts > 10**12: return ts // 1000
    return ts


def _is_finite_number(v):
    """يتحقق أن القيمة رقم صالح (ليس NaN/Inf/None)"""
    if v is None:
        return False
    try:
        f = float(v)
        return math.isfinite(f)
    except (TypeError, ValueError):
        return False


# ============================================================
# تشخيص
# ============================================================
def diagnose_table():
    """يتحقق من الجدول + الكتابة"""
    result = {"ok": False, "issues": [], "info": {}}

    try:
        sb = _sb_client()
        sb.table("candles").select("symbol").limit(1).execute()
        result["info"]["table_exists"] = True
        print("[DIAG] ✅ جدول candles موجود")
    except Exception as e:
        result["issues"].append(f"جدول: {e}")
        print(f"[DIAG] ❌ جدول: {e}")
        return result

    try:
        sb.table("candles").upsert({
            "symbol": "__DIAG__",
            "open_time": 1,
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
            "volume": 1.0, "quote_volume": 1.0, "taker_buy_base": 0.0,
            "interval": "15m",
        }).execute()
        sb.table("candles").delete().eq("symbol", "__DIAG__").execute()
        result["ok"] = True
        result["info"]["write_test"] = "ok"
        print("[DIAG] ✅ الكتابة تعمل")
    except Exception as e:
        result["issues"].append(f"كتابة: {e}")
        print(f"[DIAG] ❌ كتابة: {e}")

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
        print(f"[count] {symbol}: {e}")
        return 0


def cache_has_data(symbol, days):
    """يتحقق أن البيانات كافية في cache"""
    n = _cache_count(symbol, days)
    need = days * 90
    print(f"[cache] {symbol}: {n} شمعة (يحتاج ≥ {need})")
    return n >= need


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
            print(f"[load] {symbol} offset={offset}: {e}")
            break

        rows = res.data or []
        if not rows:
            break

        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size

    print(f"[load] {symbol}: {len(all_rows)} شمعة")
    return all_rows


# ============================================================
# جلب من Binance Vision
# ============================================================
def fetch_vision_month(symbol, year, month, verbose=True):
    """
    يجلب ملف شهر واحد من Binance Vision.
    - timeout: 120 ثانية
    - retry: 3 محاولات عند الفشل
    - يعيد list of dicts (بدون كتابة)
    """
    fname = f"{symbol}-15m-{year}-{month:02d}.zip"
    url = f"{VISION_BASE}/{symbol}/15m/{fname}"

    if verbose:
        print(f"[VISION] {symbol} {year}-{month:02d} ...")

    content = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=VISION_TIMEOUT)
            if r.status_code == 404:
                if verbose:
                    print(f"[VISION] {symbol} {year}-{month:02d}: 404")
                return []
            if r.status_code != 200:
                if verbose:
                    print(f"[VISION] {symbol} {year}-{month:02d}: HTTP {r.status_code}")
                return []
            content = r.content
            break
        except requests.exceptions.Timeout:
            if verbose:
                print(f"[VISION] {symbol} {year}-{month:02d}: timeout, retry {attempt+1}/3")
            if attempt == 2:
                return []
            time.sleep(3)
        except Exception as e:
            if verbose:
                print(f"[VISION] {symbol} {year}-{month:02d}: {str(e)[:100]}")
            return []

    if content is None:
        return []

    # فك ZIP
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as e:
        print(f"[VISION] ZIP corrupt: {e}")
        return []

    names = z.namelist()
    if not names:
        return []

    # قراءة الأسطر
    rows = []
    try:
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
                    row = {
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
                    }
                    rows.append(row)
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        print(f"[VISION] parse error: {e}")
        return []

    if verbose:
        print(f"[VISION] {symbol} {year}-{month:02d}: {len(rows)} شمعة")

    return rows


# ============================================================
# تنظيف الصفوف
# ============================================================
def _clean_rows(rows):
    """
    ينظّف الصفوف من:
    - NaN / Inf
    - open_time غير صالح
    - symbol فارغ
    """
    cleaned = []
    skipped = 0

    for r in rows:
        try:
            sym = str(r.get("symbol", "")).strip()
            if not sym:
                skipped += 1
                continue

            ot = int(r.get("open_time", 0))
            if ot <= 0 or ot > 9_999_999_999_999:
                skipped += 1
                continue

            # تحقق من كل قيم float
            numeric_keys = ["open", "high", "low", "close",
                            "volume", "quote_volume", "taker_buy_base"]
            clean_row = {"symbol": sym, "open_time": ot, "interval": "15m"}
            valid = True
            for k in numeric_keys:
                v = r.get(k, 0.0)
                if not _is_finite_number(v):
                    skipped += 1
                    valid = False
                    break
                clean_row[k] = float(v)

            if valid:
                cleaned.append(clean_row)
        except Exception:
            skipped += 1
            continue

    return cleaned, skipped


# ============================================================
# حفظ في Supabase
# ============================================================
def save_candles(rows, batch_size=SAVE_BATCH_SIZE, verbose=True):
    """
    حفظ آمن في Supabase:
    - تنظيف القيم
    - batch_size صغير
    - retry تلقائي
    - تقسيم إلى أنصاف عند الفشل
    - إعادة إنشاء client دورياً
    """
    global _sb_batches_since_reset

    if not rows:
        return 0

    # 1. تنظيف
    cleaned, skipped = _clean_rows(rows)
    if verbose and skipped:
        print(f"[SAVE] cleaned: {len(rows)} → {len(cleaned)} (تخطي {skipped})")
    if not cleaned:
        return 0

    # 2. الحفظ على دفعات
    total_saved = 0
    total_failed = 0

    for i in range(0, len(cleaned), batch_size):
        batch = cleaned[i:i + batch_size]
        saved = _save_batch(batch, verbose)

        if saved:
            total_saved += saved
        else:
            # محاولة تقسيم
            if len(batch) > 20:
                half = len(batch) // 2
                for sub in [batch[:half], batch[half:]]:
                    saved_sub = _save_batch(sub, verbose=False)
                    total_saved += saved_sub
            else:
                total_failed += len(batch)

        # إعادة إنشاء client دورياً
        _sb_batches_since_reset += 1
        if _sb_batches_since_reset >= CLIENT_RESET_EVERY:
            _reset_sb_client()

    if verbose:
        print(f"[SAVE] ✅ {total_saved} / {len(cleaned)} (فشل: {total_failed})")

    return total_saved


def _save_batch(batch, verbose=True):
    """يحفظ دفعة واحدة مع retry"""
    sb = _sb_client()

    for attempt in range(SAVE_RETRIES):
        try:
            sb.table("candles").upsert(batch).execute()
            if verbose and attempt > 0:
                print(f"[SAVE] ✅ retry {attempt+1} نجح")
            return len(batch)
        except Exception as e:
            err = str(e)[:200]
            if attempt < SAVE_RETRIES - 1:
                if verbose:
                    print(f"[SAVE] ⚠️ محاولة {attempt+1} فشلت: {err[:100]}")
                time.sleep(1.5 * (attempt + 1))
            else:
                if verbose:
                    print(f"[SAVE] ❌ فشل نهائي ({len(batch)} صف): {err[:150]}")
                # محاولة إعادة إنشاء client
                _reset_sb_client()
                return 0

    return 0


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

    # 1. هل موجود في cache؟
    try:
        if cache_has_data(symbol, days):
            _log(f"  ✅ {symbol}: موجود في cache")
            return 0
    except Exception as e:
        _log(f"  ⚠️ {symbol}: cache check: {str(e)[:80]}")

    # 2. جلب الأشهر
    _log(f"  ⬇️  {symbol}: جلب من Binance Vision...")

    now = datetime.now(timezone.utc)
    months = []
    for i in range(8):
        d = now.replace(day=1) - timedelta(days=i * 30)
        months.append((d.year, d.month))
    months = list(dict.fromkeys(months))

    total_saved = 0

    for (year, month) in months:
        try:
            rows = fetch_vision_month(symbol, year, month, verbose=False)
            if not rows:
                _log(f"  ⚪ {symbol} {year}-{month:02d}: لا بيانات")
                continue

            # حفظ على دفعات صغيرة
            saved = save_candles(rows, verbose=False)
            total_saved += saved

            _log(f"  📥 {symbol} {year}-{month:02d}: {saved}/{len(rows)} شمعة")
        except Exception as e:
            _log(f"  ❌ {symbol} {year}-{month:02d}: {str(e)[:100]}")

        time.sleep(0.5)  # Rate limit protection

    return total_saved


def prepare_all_data(progress_cb=None):
    """يجلب كل العملات"""
    def _log(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    # تشخيص
    _log("🔍 تشخيص اتصال Supabase...")
    diag = diagnose_table()
    if not diag["ok"]:
        _log(f"❌ فشل التشخيص: {diag['issues']}")
        return 0
    _log("✅ Supabase سليم")

    total = 0
    for sym in CFG["SYMBOLS"]:
        _log(f"\n🎯 {sym}")
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
    """يحفظ الصفقات على دفعات صغيرة"""
    if not trades:
        return

    sb = _sb_client()
    batch_size = 200

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
            print(f"[trades] batch {i}: {str(e)[:150]}")
            time.sleep(1)
