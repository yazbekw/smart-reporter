"""
backtest_data.py
================
جلب الشموع التاريخية من Binance Vision وتخزينها في Supabase.

المزايا:
- Supabase client واحد (singleton) — بدون إعادة إنشاء
- إعادة محاولة عند فشل حفظ أي دفعة
- تقسيم الدفعات تلقائياً عند الفشل
- تنظيف القيم (NaN/Inf/None)
- Logging مفصّل لكل خطوة
- سرعة عالية (لا reset للـ client)
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

# ============================================================
# الإعدادات
# ============================================================
SAVE_BATCH_SIZE = 200       # حجم الدفعة
SAVE_RETRIES = 3            # عدد محاولات الإرسال
VISION_TIMEOUT = 120        # ثوانٍ لجلب ZIP
SUPABASE_TIMEOUT = 120      # ثوانٍ لعمليات Supabase


# ============================================================
# Supabase Client (Singleton)
# ============================================================
_sb = None


def _sb_client():
    """يعيد client واحد — لا يُعاد إنشاؤه أبداً"""
    global _sb
    if _sb is not None:
        return _sb

    url = CFG["SUPABASE_URL"]
    key = CFG["SUPABASE_KEY"]

    if not url or not key:
        raise RuntimeError("SUPABASE_URL أو SUPABASE_KEY مفقود")

    print(f"[SB] init: {url[:60]}... key_len={len(key)}")

    try:
        from supabase import ClientOptions
        options = ClientOptions(postgrest_client_timeout=SUPABASE_TIMEOUT)
        _sb = create_client(url, key, options=options)
        print(f"[SB] ✅ Ready (timeout={SUPABASE_TIMEOUT}s)")
    except (ImportError, TypeError):
        _sb = create_client(url, key)
        print("[SB] ✅ Ready (default options)")

    return _sb


# ============================================================
# أدوات مساعدة
# ============================================================
def _normalize_ts(ts):
    """يوحّد أطوال Unix timestamps"""
    ts = int(ts)
    if ts > 10**17: return ts // 1_000_000
    if ts > 10**14: return ts // 1_000_000_000
    if ts > 10**12: return ts // 1000
    return ts


def _is_finite(v):
    """يتحقق أن القيمة رقم صالح"""
    if v is None:
        return False
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


# ============================================================
# تشخيص
# ============================================================
def diagnose_table():
    """يتحقق من الجدول + الكتابة"""
    result = {"ok": False, "issues": []}

    try:
        sb = _sb_client()
        sb.table("candles").select("symbol").limit(1).execute()
        print("[DIAG] ✅ جدول candles موجود")
    except Exception as e:
        result["issues"].append(f"جدول: {str(e)[:150]}")
        print(f"[DIAG] ❌ {str(e)[:150]}")
        return result

    try:
        sb = _sb_client()
        sb.table("candles").upsert({
            "symbol": "__DIAG__",
            "open_time": 1,
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
            "volume": 1.0, "quote_volume": 1.0, "taker_buy_base": 0.0,
            "interval": "15m",
        }).execute()
        sb.table("candles").delete().eq("symbol", "__DIAG__").execute()
        result["ok"] = True
        print("[DIAG] ✅ الكتابة تعمل")
    except Exception as e:
        result["issues"].append(f"كتابة: {str(e)[:150]}")
        print(f"[DIAG] ❌ {str(e)[:150]}")

    return result


# ============================================================
# قراءة من Supabase
# ============================================================
def _cache_count(symbol, days):
    """يعد الصفوف الموجودة في cache"""
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
        print(f"[count] {symbol}: {str(e)[:120]}")
        return 0


def cache_has_data(symbol, days):
    """يتحقق أن البيانات كافية"""
    n = _cache_count(symbol, days)
    need = days * 90
    print(f"[cache] {symbol}: {n} / {need}")
    return n >= need


def load_candles(symbol, days=None):
    """يقرأ الشموع من Supabase"""
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
            print(f"[load] {symbol} @{offset}: {str(e)[:120]}")
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
def fetch_vision_month(symbol, year, month, verbose=False):
    """يجلب ملف شهر من Binance Vision"""
    fname = f"{symbol}-15m-{year}-{month:02d}.zip"
    url = f"{VISION_BASE}/{symbol}/15m/{fname}"

    if verbose:
        print(f"[VISION] {symbol} {year}-{month:02d}")

    content = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=VISION_TIMEOUT)
            if r.status_code == 404:
                return []  # الشهر غير متاح (طبيعي)
            if r.status_code != 200:
                return []
            content = r.content
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                return []
            time.sleep(3)
        except Exception:
            return []

    if content is None:
        return []

    try:
        z = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return []

    names = z.namelist()
    if not names:
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
            except (ValueError, IndexError):
                continue

    if verbose:
        print(f"[VISION] {symbol} {year}-{month:02d}: {len(rows)} شمعة")

    return rows


# ============================================================
# تنظيف الصفوف
# ============================================================
def _clean_rows(rows):
    """ينظّف + يُزيل التكرار"""
    cleaned = []
    seen = set()

    for r in rows:
        try:
            sym = str(r.get("symbol", "")).strip()
            if not sym:
                continue
            ot = int(r.get("open_time", 0))
            if ot <= 0 or ot > 9_999_999_999_999:
                continue

            # ✅ إزالة التكرار
            key = (sym, ot)
            if key in seen:
                continue
            seen.add(key)

            vals = {
                "open": r.get("open", 0),
                "high": r.get("high", 0),
                "low": r.get("low", 0),
                "close": r.get("close", 0),
                "volume": r.get("volume", 0),
                "quote_volume": r.get("quote_volume", 0),
                "taker_buy_base": r.get("taker_buy_base", 0),
            }
            if not all(_is_finite(v) for v in vals.values()):
                continue

            cleaned.append({
                "symbol": sym,
                "open_time": ot,
                "interval": "15m",
                **{k: float(v) for k, v in vals.items()},
            })
        except Exception:
            continue

    return cleaned


# ============================================================
# حفظ في Supabase
# ============================================================
def save_candles(rows, batch_size=SAVE_BATCH_SIZE, verbose=True):
    """
    يحفظ الصفوف في Supabase.
    - batch صغير (200)
    - retry تلقائي
    - تقسيم عند الفشل
    """
    if not rows:
        return 0

    cleaned = _clean_rows(rows)
    if not cleaned:
        return 0

    sb = _sb_client()
    total_saved = 0

    for i in range(0, len(cleaned), batch_size):
        batch = cleaned[i:i + batch_size]
        saved = _try_save(sb, batch)
        total_saved += saved

        if saved == 0 and len(batch) > 50:
            # قسم النصف
            half = len(batch) // 2
            s1 = _try_save(sb, batch[:half])
            s2 = _try_save(sb, batch[half:])
            total_saved += s1 + s2

    if verbose:
        print(f"[SAVE] ✅ {total_saved} / {len(cleaned)}")
    return total_saved


def _try_save(sb, batch):
    """يحاول حفظ دفعة (بـ 3 محاولات)"""
    for attempt in range(SAVE_RETRIES):
        try:
            sb.table("candles").upsert(batch).execute()
            return len(batch)
        except Exception as e:
            if attempt < SAVE_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
            else:
                print(f"[SAVE] ❌ {len(batch)} صف: {str(e)[:120]}")
    return 0


# ============================================================
# جلب + تخزين
# ============================================================
def fetch_and_store(symbol, days=None, progress_cb=None):
    """يجلب آخر N يوم ويخزّنها"""
    if days is None:
        days = CFG["DAYS"]

    def log(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    # 1. فحص cache
    if cache_has_data(symbol, days):
        log(f"  ✅ {symbol}: موجود في cache")
        return 0

    log(f"  ⬇️  {symbol}: جلب من Binance Vision...")

    # 2. قائمة الأشهر (آخر 8 أشهر)
    now = datetime.now(timezone.utc)
    months = []
    for i in range(8):
        d = now.replace(day=1) - timedelta(days=i * 30)
        months.append((d.year, d.month))
    months = list(dict.fromkeys(months))

    # 3. جلب + حفظ
    total = 0
    for (year, month) in months:
        try:
            rows = fetch_vision_month(symbol, year, month, verbose=False)
            if not rows:
                log(f"  ⚪ {symbol} {year}-{month:02d}: لا بيانات")
                continue

            saved = save_candles(rows, verbose=False)
            total += saved
            log(f"  📥 {symbol} {year}-{month:02d}: {saved}/{len(rows)}")

            # إشارة سريعة للنجاح
            if saved == 0:
                log(f"  ⚠️ {symbol} {year}-{month:02d}: فشل الحفظ!")

        except Exception as e:
            log(f"  ❌ {symbol} {year}-{month:02d}: {str(e)[:100]}")

        time.sleep(0.3)

    return total


def prepare_all_data(progress_cb=None):
    """يجلب كل العملات"""
    def log(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    log("🔍 تشخيص Supabase...")
    diag = diagnose_table()
    if not diag["ok"]:
        log(f"❌ فشل: {diag['issues']}")
        return 0
    log("✅ Supabase سليم")

    total = 0
    for sym in CFG["SYMBOLS"]:
        log(f"\n🎯 {sym}")
        total += fetch_and_store(sym, CFG["DAYS"], progress_cb)

    log(f"\n✅ إجمالي: {total} شمعة")
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
    """يحفظ الصفقات على دفعات"""
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
            print(f"[trades] batch {i}: {str(e)[:120]}")
            time.sleep(1)
