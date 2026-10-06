"""
بوت إشعارات متكامل (Bybit / OKX / غيرها):
1) تقاطع EMA — 3 فريمات + كشف مبكر + مؤشرات داعمة + علامة Score
2) تنبيهات التغير المفاجئ
3) تقرير صباحي (ATR)
4) أمر /report

🆕 التحديثات:
- فلتر إلزامي للحجم / ADX / ATR / EMA50 / 15m
- منع الإشارات الضعيفة تلقائياً
- إحصائيات فلترة في اللوج و /status
"""
import os
import re
import asyncio
import logging
import threading
import time as _time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import ccxt
import pandas as pd
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import NetworkError, TimedOut

load_dotenv()

# ============================================================
# إعدادات عامة
# ============================================================
BOT_TOKEN = os.getenv("TELEGRAM_REPORT_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_REPORT_CHAT_ID")


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    m = re.search(r"-?\d+(\.\d+)?", str(raw))
    return float(m.group(0)) if m else default


def _get_int(name: str, default: int) -> int:
    return int(_get_float(name, default))


def _get_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("true", "1", "yes", "on")


EXCHANGE_NAME = os.getenv("EXCHANGE_NAME", "bybit").strip().lower()
MARKET_TYPE = os.getenv("MARKET_TYPE", "swap").strip().lower()
if MARKET_TYPE not in ("spot", "swap", "future"):
    MARKET_TYPE = "swap"

# ============================================================
# الرموز والفريمات
# ============================================================
_default_symbols = "BTC/USDT:USDT,XAG/USDT:USDT,XAU/USDT:USDT,XRP/USDT:USDT"
_raw = os.getenv("SYMBOLS", "").strip()
SYMBOLS = [s.strip().upper() for s in (_raw or _default_symbols).split(",") if s.strip()]

TIMEFRAMES = [t.strip() for t in os.getenv("TIMEFRAMES", "5m,15m,1h").split(",") if t.strip()]

# ============================================================
# إعدادات EMA
# ============================================================
EMA_FAST = _get_int("EMA_FAST", 7)
EMA_SLOW = _get_int("EMA_SLOW", 25)
JOB_INTERVAL_MIN = _get_int("JOB_INTERVAL_MIN", 2)
MIN_EMA_GAP = _get_float("MIN_EMA_GAP", 0.10)
STRONG_GAP_5M  = _get_float("STRONG_GAP_5M", 0.25)
STRONG_GAP_15M = _get_float("STRONG_GAP_15M", 0.20)
STRONG_GAP_1H  = _get_float("STRONG_GAP_1H", 0.30)

# ============================================================
# إعدادات الكشف المبكر
# ============================================================
ENABLE_PRE_CROSS  = _get_bool("ENABLE_PRE_CROSS", True)
ENABLE_LIVE_CROSS = _get_bool("ENABLE_LIVE_CROSS", True)
ENABLE_CONFIRMED  = _get_bool("ENABLE_CONFIRMED", True)

PRE_CROSS_GAP = _get_float("PRE_CROSS_GAP", 0.05)
PRE_CROSS_LOOKBACK = _get_int("PRE_CROSS_LOOKBACK", 3)
PRE_CROSS_COOLDOWN = _get_int("PRE_CROSS_COOLDOWN", 3)
OHLCV_CACHE_SECONDS = _get_int("OHLCV_CACHE_SECONDS", 30)

# ============================================================
# إعدادات نظام العلامة (Score)
# ============================================================
# الوضع: silent (يرسل الكل مع علامة) | strict (يحجب أقل من MIN_SCORE) | hybrid
SCORE_MODE = os.getenv("SCORE_MODE", "silent").strip().lower()

# العتبة الدنيا (تُطبَّق في strict و hybrid)
MIN_SCORE = _get_float("MIN_SCORE", 55)

# عتبة "الإشارة الذهبية" (في hybrid تُرسَل فوراً بإشعار قوي)
GOLD_SCORE = _get_float("GOLD_SCORE", 80)

# في hybrid: كل كم دقيقة يُرسَل ملخص الإشارات المحجوبة
HYBRID_DIGEST_MIN = _get_int("HYBRID_DIGEST_MIN", 60)

# ============================================================
# 🆕 فلتر الجودة الإلزامي (يطبق في كل الأوضاع)
# ============================================================
ENABLE_HARD_FILTER = _get_bool("ENABLE_HARD_FILTER", True)
MIN_VOL_RATIO      = _get_float("MIN_VOL_RATIO", 1.0)       # الحجم لا يقل عن المتوسط
MIN_ADX_HARD       = _get_float("MIN_ADX_HARD", 18.0)        # أدنى ADX مقبول
MAX_ATR_PCT_HARD   = _get_float("MAX_ATR_PCT_HARD", 0.70)    # تقلب مرتفع = ضوضاء
BLOCK_AGAINST_HTF  = _get_bool("BLOCK_AGAINST_HTF", True)    # منع ضد EMA50
BLOCK_TF_15M_LOW   = _get_bool("BLOCK_TF_15M_LOW", True)     # 15m يشترط علامة أعلى
MIN_SCORE_15M      = _get_float("MIN_SCORE_15M", 65)         # عتبة خاصة بـ 15m

# ============================================================
# إعدادات تنبيهات السعر
# ============================================================
PRICE_ALERT_INTERVAL_MIN = _get_int("PRICE_ALERT_INTERVAL_MIN", 1)
MAX_PRICE_ALERTS = _get_int("MAX_PRICE_ALERTS", 3)

PRICE_CHANGE_THRESHOLDS = {
    "BTC/USDT": _get_float("THRESHOLD_BTC", 1.0),
    "XAG/USDT": _get_float("THRESHOLD_XAG", 0.6),
    "XAU/USDT": _get_float("THRESHOLD_XAU", 0.4),
    "PAXG/USDT": _get_float("THRESHOLD_XAU", 0.4),
    "XRP/USDT": _get_float("THRESHOLD_XRP", 1.5),
    "BTC/USDT:USDT": _get_float("THRESHOLD_BTC", 1.0),
    "XAG/USDT:USDT": _get_float("THRESHOLD_XAG", 0.6),
    "XAU/USDT:USDT": _get_float("THRESHOLD_XAU", 0.4),
    "PAXG/USDT:USDT": _get_float("THRESHOLD_XAU", 0.4),
    "XRP/USDT:USDT": _get_float("THRESHOLD_XRP", 1.5),
}
DEFAULT_THRESHOLD = _get_float("DEFAULT_THRESHOLD", 1.0)

# ============================================================
# إعدادات التقرير الصباحي
# ============================================================
MORNING_REPORT_ENABLED = _get_bool("MORNING_REPORT_ENABLED", True)
MORNING_REPORT_HOUR = _get_int("MORNING_REPORT_HOUR", 9)
MORNING_REPORT_LOOKBACK_DAYS = _get_int("MORNING_REPORT_LOOKBACK_DAYS", 10)
MORNING_REPORT_MIN_GRIDS = _get_int("MORNING_REPORT_MIN_GRIDS", 15)
MORNING_REPORT_MAX_GRIDS = _get_int("MORNING_REPORT_MAX_GRIDS", 35)
MORNING_REPORT_ATR_MULTIPLIER = _get_float("MORNING_REPORT_ATR_MULTIPLIER", 3.0)
MORNING_REPORT_MAX_RANGE_PCT = _get_float("MORNING_REPORT_MAX_RANGE_PCT", 8.0)

_morning_raw = os.getenv("MORNING_REPORT_SYMBOLS", "").strip()
MORNING_SYMBOLS = [s.strip().upper() for s in (_morning_raw or ",".join(SYMBOLS)).split(",") if s.strip()]

SYRIA_TZ = ZoneInfo("Asia/Damascus")

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logging.getLogger("ccxt").setLevel(logging.WARNING)
log = logging.getLogger("cross")


# ============================================================
# ccxt
# ============================================================
def init_exchange():
    options = {
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": MARKET_TYPE},
    }
    if EXCHANGE_NAME == "bybit":
        options["options"]["unifiedMargin"] = False

    mapping = {
        "okx": ccxt.okx, "bybit": ccxt.bybit, "kucoin": ccxt.kucoin,
        "kraken": ccxt.kraken, "binance": ccxt.binance, "coinbase": ccxt.coinbase,
        "gate": ccxt.gate, "bitget": ccxt.bitget,
    }
    cls = mapping.get(EXCHANGE_NAME, ccxt.bybit)
    try:
        ex = cls(options)
        ex.load_markets()
        log.info(f"✅ تم تهيئة {ex.name} (السوق: {MARKET_TYPE}) | {len(ex.markets)} سوق متاح")
        return ex
    except Exception as e:
        log.error(f"❌ فشل التهيئة: {type(e).__name__}: {e}")
        import traceback
        log.error(traceback.format_exc())
        return None


_exchange = init_exchange()

# كاشات
_crossover_cache: dict = {}
_price_state: dict = {}
_ohlcv_cache: dict = {}
_hybrid_digest: list = []

# 🆕 عدّادات إحصائية للفلتر
_filter_stats = {
    "sent": 0,
    "hidden": 0,
    "filtered_vol": 0,
    "filtered_adx": 0,
    "filtered_atr": 0,
    "filtered_htf": 0,
    "filtered_15m_score": 0,
    "filtered_other": 0,
}


# ============================================================
# أدوات مساعدة
# ============================================================
def short(symbol: str) -> str:
    return symbol.split("/")[0].split(":")[0].upper()


def syria_now_str() -> str:
    now = datetime.now(SYRIA_TZ)
    days_ar = {
        "Monday": "الاثنين", "Tuesday": "الثلاثاء", "Wednesday": "الأربعاء",
        "Thursday": "الخميس", "Friday": "الجمعة", "Saturday": "السبت",
        "Sunday": "الأحد",
    }
    return f"{days_ar.get(now.strftime('%A'), now.strftime('%A'))} " \
           f"{now.strftime('%Y-%m-%d')} — {now.strftime('%H:%M:%S')}"


def syria_from_ts(ts_ms: int) -> str:
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=SYRIA_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs))


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["h"] - df["l"],
        (df["h"] - df["c"].shift()).abs(),
        (df["l"] - df["c"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["h"], df["l"], df["c"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    tr = pd.concat([
        high - low, (high - close.shift()).abs(), (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, 1e-9)
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def calc_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_f - ema_s
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 100:
        return f"{value:.3f}"
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.01:
        return f"{value:.5f}"
    return f"{value:.8f}"


# ============================================================
# جلب الشموع + كاش
# ============================================================
async def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 150):
    if _exchange is None:
        return None
    try:
        return await asyncio.to_thread(_exchange.fetch_ohlcv, symbol, timeframe, None, limit)
    except ccxt.BadSymbol:
        log.warning(f"⚠️ رمز غير مدعوم: {symbol}")
        return None
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        log.warning(f"⚠️ {symbol} {timeframe}: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        log.warning(f"❌ {symbol}: {type(e).__name__}: {e}")
        return None


async def fetch_ohlcv_cached(symbol: str, timeframe: str, limit: int = 150):
    key = (symbol, timeframe, limit)
    now = _time.time()
    cached = _ohlcv_cache.get(key)
    if cached and (now - cached["ts"]) < OHLCV_CACHE_SECONDS:
        return cached["data"]
    data = await fetch_ohlcv(symbol, timeframe, limit)
    if data:
        _ohlcv_cache[key] = {"data": data, "ts": now}
    return data


def clear_ohlcv_cache():
    _ohlcv_cache.clear()


# ============================================================
# تصنيف قوة التقاطع (وصفي)
# ============================================================
def classify_strength(timeframe: str, gap_pct: float) -> str:
    if timeframe == "1h":
        if gap_pct >= STRONG_GAP_1H:
            return "🔥 قوية جداً"
        if gap_pct >= MIN_EMA_GAP:
            return "🟢 قوية"
        return "🟡 متوسطة"
    if timeframe == "15m":
        if gap_pct >= STRONG_GAP_15M:
            return "🟢 قوية"
        if gap_pct >= MIN_EMA_GAP:
            return "🟡 متوسطة"
        return "⚪ ضعيفة"
    if timeframe == "5m":
        if gap_pct >= STRONG_GAP_5M:
            return "🟢 قوية"
        if gap_pct >= MIN_EMA_GAP:
            return "🟡 متوسطة"
        return "⚪ ضعيفة"
    if gap_pct >= STRONG_GAP_15M:
        return "🟢 قوية"
    if gap_pct >= MIN_EMA_GAP:
        return "🟡 متوسطة"
    return "⚪ ضعيفة"


# ============================================================
# المؤشرات الداعمة + العلامة (Score)
# ============================================================
def _score_volume(vol_ratio: float) -> tuple:
    """Volume Score (0-30 نقطة)"""
    if vol_ratio >= 4.0:   return 30, "قوي جداً 🔥"
    if vol_ratio >= 2.5:   return 25, "قوي"
    if vol_ratio >= 1.5:   return 18, "جيد"
    if vol_ratio >= 1.0:   return 10, "طبيعي"
    if vol_ratio >= 0.5:   return 5,  "ضعيف"
    return 0, "ضعيف جداً ⚠️"


def _score_adx(adx: float) -> tuple:
    """ADX Score (0-20 نقطة)"""
    if adx >= 50:  return 18, "اتجاه متطرف"
    if adx >= 35:  return 20, "اتجاه قوي جداً"
    if adx >= 25:  return 18, "اتجاه واضح"
    if adx >= 20:  return 12, "اتجاه ضعيف"
    if adx >= 15:  return 5,  "عرضي"
    return 0, "عرضي جداً"


def _score_gap(gap_pct: float) -> tuple:
    """فرق EMA Score (0-15 نقطة)"""
    if gap_pct >= 0.60:  return 12, "كبير جداً"
    if gap_pct >= 0.30:  return 15, "كبير"
    if gap_pct >= 0.10:  return 10, "متوسط"
    if gap_pct >= 0.05:  return 6,  "صغير"
    if gap_pct >= 0.02:  return 3,  "صغير جداً"
    return 0, "طفيلي"


def _score_macd(hist_now: float, hist_prev: float) -> tuple:
    """MACD Score (0-10 نقطة)"""
    rising = hist_now > hist_prev
    if hist_now > 0 and rising:    return 10, "يتسارع صعوداً ↗"
    if hist_now > 0 and not rising: return 5, "يتباطأ صعوداً ↘"
    if hist_now < 0 and rising:    return 7, "يتباطأ هبوطاً ↗"
    return 5, "يتسارع هبوطاً ↘"


def _score_rsi(rsi: float, direction: str) -> tuple:
    """RSI Score (0-10 نقطة)"""
    if direction == "bullish":
        if 55 <= rsi <= 70:   return 10, "زخم صاعد صحي ⭐"
        if rsi > 70:          return 5,  "تشبع شرائي (قد ينعكس)"
        if rsi >= 45:         return 7,  "محايد"
        if rsi >= 30:         return 3,  "زخم هابط (ضد الإشارة)"
        return 0, "تشبع بيعي"
    else:
        if 30 <= rsi <= 45:   return 10, "زخم هابط صحي ⭐"
        if rsi < 30:          return 5,  "تشبع بيعي (قد ينعكس)"
        if rsi <= 55:         return 7,  "محايد"
        if rsi <= 70:         return 3,  "زخم صاعد (ضد الإشارة)"
        return 0, "تشبع شرائي"


def _score_htf(price: float, ema50: float, direction: str) -> tuple:
    """توافق EMA50 Score (0-10 نقطة)"""
    above = price > ema50
    if direction == "bullish" and above:  return 10, "مع الاتجاه الأكبر ✅"
    if direction == "bearish" and not above: return 10, "مع الاتجاه الأكبر ✅"
    return 0, "ضد الاتجاه الأكبر ⚠️"


def _score_atr(atr_pct: float) -> tuple:
    """ATR Score (0-5 نقطة)"""
    if atr_pct >= 0.30:  return 5, "نشاط جيد"
    if atr_pct >= 0.15:  return 3, "طبيعي"
    return 1, "خمول"


def analyze_with_score(df: pd.DataFrame, direction: str, curr_idx: int = -2) -> dict:
    """
    حساب المؤشرات الداعمة + العلامة (0-100).
    الأوزان:
      Volume  : 30
      ADX     : 20
      Gap EMA : 15
      MACD    : 10
      RSI     : 10
      EMA50   : 10
      ATR     :  5
    """
    close = df["c"]
    vol = df["v"]
    current_price = float(close.iloc[curr_idx])

    # --- Volume ---
    try:
        start = max(0, len(df) + curr_idx - 20)
        end = len(df) + curr_idx
        vol_ma20 = float(vol.iloc[start:end].mean())
        vol_curr = float(vol.iloc[curr_idx])
        vol_ratio = vol_curr / vol_ma20 if vol_ma20 > 0 else 0
    except Exception:
        vol_ratio = 0
    vol_pts, vol_label = _score_volume(vol_ratio)

    # --- ADX ---
    try:
        adx = float(calc_adx(df).iloc[curr_idx])
    except Exception:
        adx = 0
    adx_pts, adx_label = _score_adx(adx)

    # --- Gap ---
    try:
        ef = float(calc_ema(close, EMA_FAST).iloc[curr_idx])
        es = float(calc_ema(close, EMA_SLOW).iloc[curr_idx])
        gap_pct = abs(ef - es) / es * 100 if es else 0
    except Exception:
        gap_pct = 0
    gap_pts, gap_label = _score_gap(gap_pct)

    # --- MACD ---
    try:
        _, _, hist = calc_macd(close)
        macd_pts, macd_label = _score_macd(float(hist.iloc[curr_idx]), float(hist.iloc[curr_idx - 1]))
    except Exception:
        macd_pts, macd_label = 0, "غير محدد"

    # --- RSI ---
    try:
        rsi = float(calc_rsi(close).iloc[curr_idx])
    except Exception:
        rsi = 50
    rsi_pts, rsi_label = _score_rsi(rsi, direction)

    # --- EMA50 ---
    try:
        ema50 = float(calc_ema(close, 50).iloc[curr_idx])
        htf_pts, htf_label = _score_htf(current_price, ema50, direction)
    except Exception:
        ema50 = current_price
        htf_pts, htf_label = 0, "?"

    # --- ATR ---
    try:
        atr = float(calc_atr(df).iloc[curr_idx])
        atr_pct = (atr / current_price) * 100 if current_price > 0 else 0
    except Exception:
        atr_pct = 0
    atr_pts, atr_label = _score_atr(atr_pct)

    # --- المجموع ---
    total = vol_pts + adx_pts + gap_pts + macd_pts + rsi_pts + htf_pts + atr_pts

    # --- التصنيف ---
    if total >= 80:
        grade = "🌟 ذهبية"
    elif total >= 65:
        grade = "⭐ قوية"
    elif total >= 55:
        grade = "✅ جيدة"
    elif total >= 45:
        grade = "🟡 متوسطة"
    else:
        grade = "⚪ ضعيفة"

    return {
        "score": total,
        "grade": grade,
        "adx": round(adx, 1), "adx_label": adx_label, "adx_pts": adx_pts,
        "rsi": round(rsi, 1), "rsi_label": rsi_label, "rsi_pts": rsi_pts,
        "macd_label": macd_label, "macd_pts": macd_pts,
        "vol_ratio": round(vol_ratio, 2), "vol_label": vol_label, "vol_pts": vol_pts,
        "atr_pct": round(atr_pct, 2), "atr_label": atr_label, "atr_pts": atr_pts,
        "htf_trend": "صاعد" if current_price > ema50 else "هابط",
        "htf_label": htf_label, "htf_pts": htf_pts,
        "gap_pct": round(gap_pct, 3), "gap_label": gap_label, "gap_pts": gap_pts,
    }


# ============================================================
# 🆕 فلتر الجودة الإلزامي
# ============================================================
def passes_hard_filter(cross: dict) -> tuple:
    """
    فلتر إلزامي يطبَّق على كل الإشارات.
    returns: (passed: bool, reason: str, category: str)
    """
    if not ENABLE_HARD_FILTER:
        return True, "", ""

    s = cross.get("support", {})
    if not s:
        return False, "لا توجد مؤشرات داعمة", "other"

    tf    = cross["timeframe"]
    vol   = s.get("vol_ratio", 0)
    adx   = s.get("adx", 0)
    atr   = s.get("atr_pct", 0)
    htf   = s.get("htf_pts", 0)
    score = s.get("score", 0)

    # 1) الحجم ضعيف جداً
    if vol < MIN_VOL_RATIO:
        return False, f"الحجم ضعيف ({vol}× < {MIN_VOL_RATIO})", "vol"

    # 2) ADX ضعيف جداً (سوق عرضي)
    if adx < MIN_ADX_HARD:
        return False, f"ADX منخفض ({adx} < {MIN_ADX_HARD})", "adx"

    # 3) تقلب مرتفع جداً (ضوضاء)
    if atr > MAX_ATR_PCT_HARD:
        return False, f"ATR مرتفع ({atr}% > {MAX_ATR_PCT_HARD}%)", "atr"

    # 4) ضد الاتجاه الأكبر (EMA50) — لا يطبق على "pre"
    if BLOCK_AGAINST_HTF and cross.get("alert_type") != "pre":
        if htf == 0:
            return False, "ضد الاتجاه الأكبر (EMA50)", "htf"

    # 5) إشارات 15m تشترط علامة أعلى
    if BLOCK_TF_15M_LOW and tf == "15m" and score < MIN_SCORE_15M:
        return False, f"15m بعلامة منخفضة ({score} < {MIN_SCORE_15M})", "15m_score"

    return True, "", ""


# ============================================================
# 1) التقاطع المؤكد (شمعة مغلقة)
# ============================================================
async def detect_crossover(symbol: str, timeframe: str) -> dict | None:
    if not ENABLE_CONFIRMED:
        return None

    ohlcv = await fetch_ohlcv_cached(symbol, timeframe, EMA_SLOW + 60)
    if not ohlcv or len(ohlcv) < EMA_SLOW + 5:
        return None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
    df["ef"] = calc_ema(df["c"], EMA_FAST)
    df["es"] = calc_ema(df["c"], EMA_SLOW)

    curr, prev = -2, -3
    cf = float(df["ef"].iloc[curr]); cs = float(df["es"].iloc[curr])
    pf = float(df["ef"].iloc[prev]); ps = float(df["es"].iloc[prev])

    bullish = (cf > cs) and (pf <= ps)
    bearish = (cf < cs) and (pf >= ps)
    if not (bullish or bearish):
        return None

    direction = "bullish" if bullish else "bearish"
    candle_ts = int(df["ts"].iloc[curr])

    cache_key = (symbol, timeframe)
    last = _crossover_cache.get(cache_key)
    if last and last.get("candle_ts") == candle_ts and last.get("direction") == direction:
        return None
    _crossover_cache[cache_key] = {"direction": direction, "candle_ts": candle_ts}

    support = analyze_with_score(df, direction, curr)

    return {
        "symbol": symbol, "timeframe": timeframe, "direction": direction,
        "ema_fast": round(cf, 8), "ema_slow": round(cs, 8),
        "price": round(float(df["c"].iloc[curr]), 8),
        "candle_ts": candle_ts, "gap_pct": round(abs(cf - cs) / cs * 100, 3),
        "strength": classify_strength(timeframe, abs(cf - cs) / cs * 100),
        "exchange": EXCHANGE_NAME.upper(), "market_type": MARKET_TYPE,
        "alert_type": "confirmed", "support": support,
    }


# ============================================================
# 2) التقاطع المبدئي (شمعة جارية)
# ============================================================
async def detect_live_crossover(symbol: str, timeframe: str) -> dict | None:
    if not ENABLE_LIVE_CROSS:
        return None

    ohlcv = await fetch_ohlcv_cached(symbol, timeframe, EMA_SLOW + 60)
    if not ohlcv or len(ohlcv) < EMA_SLOW + 5:
        return None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
    df["ef"] = calc_ema(df["c"], EMA_FAST)
    df["es"] = calc_ema(df["c"], EMA_SLOW)

    curr, prev = -1, -2
    cf = float(df["ef"].iloc[curr]); cs = float(df["es"].iloc[curr])
    pf = float(df["ef"].iloc[prev]); ps = float(df["es"].iloc[prev])

    bullish = (cf > cs) and (pf <= ps)
    bearish = (cf < cs) and (pf >= ps)
    if not (bullish or bearish):
        return None

    direction = "bullish" if bullish else "bearish"
    candle_ts = int(df["ts"].iloc[curr])

    cache_key = (symbol, timeframe, "live")
    last = _crossover_cache.get(cache_key)
    if last and last.get("candle_ts") == candle_ts:
        return None
    _crossover_cache[cache_key] = {"direction": direction, "candle_ts": candle_ts}

    support = analyze_with_score(df, direction, curr)

    return {
        "symbol": symbol, "timeframe": timeframe, "direction": direction,
        "ema_fast": round(cf, 8), "ema_slow": round(cs, 8),
        "price": round(float(df["c"].iloc[curr]), 8),
        "candle_ts": candle_ts, "gap_pct": round(abs(cf - cs) / cs * 100, 3),
        "strength": "⚡ مبدئي (قابل للتغير)",
        "exchange": EXCHANGE_NAME.upper(), "market_type": MARKET_TYPE,
        "alert_type": "live", "support": support,
    }


# ============================================================
# 3) التقارب المبكر
# ============================================================
_TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
    "4h": 14_400_000, "1d": 86_400_000,
}


async def detect_pre_crossover(symbol: str, timeframe: str) -> dict | None:
    if not ENABLE_PRE_CROSS:
        return None

    ohlcv = await fetch_ohlcv_cached(symbol, timeframe, EMA_SLOW + 60)
    if not ohlcv or len(ohlcv) < EMA_SLOW + 10:
        return None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
    df["ef"] = calc_ema(df["c"], EMA_FAST)
    df["es"] = calc_ema(df["c"], EMA_SLOW)

    curr = -2
    cf = float(df["ef"].iloc[curr]); cs = float(df["es"].iloc[curr])
    gap_pct = abs(cf - cs) / cs * 100
    if gap_pct >= PRE_CROSS_GAP:
        return None

    gaps = []
    for i in range(PRE_CROSS_LOOKBACK):
        idx = curr - i
        f = float(df["ef"].iloc[idx]); s = float(df["es"].iloc[idx])
        gaps.append(abs(f - s) / s * 100)
    gaps_chrono = list(reversed(gaps))
    if not all(gaps_chrono[i] >= gaps_chrono[i + 1] for i in range(len(gaps_chrono) - 1)):
        return None

    if abs(cf - cs) < 1e-9:
        return None

    direction = "bullish" if cf < cs else "bearish"
    candle_ts = int(df["ts"].iloc[curr])

    cache_key = (symbol, timeframe, "pre")
    last = _crossover_cache.get(cache_key)
    if last:
        tf_ms = _TF_MS.get(timeframe, 900_000)
        if candle_ts - last.get("candle_ts", 0) < tf_ms * PRE_CROSS_COOLDOWN:
            return None
    _crossover_cache[cache_key] = {"direction": direction, "candle_ts": candle_ts}

    support = analyze_with_score(df, direction, curr)

    return {
        "symbol": symbol, "timeframe": timeframe, "direction": direction,
        "ema_fast": round(cf, 8), "ema_slow": round(cs, 8),
        "price": round(float(df["c"].iloc[curr]), 8),
        "candle_ts": candle_ts, "gap_pct": round(gap_pct, 3),
        "strength": "🔔 تقارب وشيك",
        "exchange": EXCHANGE_NAME.upper(), "market_type": MARKET_TYPE,
        "alert_type": "pre", "support": support,
    }


# ============================================================
# تنبيهات التغير المفاجئ
# ============================================================
async def detect_sudden_change(symbol: str) -> dict | None:
    ohlcv = await fetch_ohlcv(symbol, "1m", 3)
    if not ohlcv or len(ohlcv) < 2:
        return None

    prev_close = float(ohlcv[-2][4])
    current_price = float(ohlcv[-1][4])
    if prev_close == 0:
        return None

    change_pct = ((current_price - prev_close) / prev_close) * 100
    threshold = PRICE_CHANGE_THRESHOLDS.get(symbol, DEFAULT_THRESHOLD)

    state = _price_state.setdefault(symbol, {"direction": None, "alert_count": 0, "alerting": False})

    if abs(change_pct) >= threshold:
        direction = "up" if change_pct > 0 else "down"
        if state["direction"] != direction:
            state["direction"] = direction
            state["alert_count"] = 0
            state["alerting"] = False
        if not state["alerting"]:
            state["alerting"] = True
            state["alert_count"] = 1
        elif state["alert_count"] < MAX_PRICE_ALERTS:
            state["alert_count"] += 1
        else:
            return None
        return {
            "symbol": symbol, "direction": direction,
            "change_pct": round(change_pct, 2),
            "current_price": round(current_price, 8),
            "prev_price": round(prev_close, 8),
            "threshold": threshold,
            "alert_count": state["alert_count"],
            "max_alerts": MAX_PRICE_ALERTS,
        }
    else:
        state["direction"] = None
        state["alert_count"] = 0
        state["alerting"] = False
    return None


# ============================================================
# تحليل النطاق (التقرير الصباحي)
# ============================================================
async def analyze_range(symbol: str) -> dict | None:
    ohlcv_1d = await fetch_ohlcv(symbol, "1d", MORNING_REPORT_LOOKBACK_DAYS + 20)
    if not ohlcv_1d or len(ohlcv_1d) < 10:
        return None

    df = pd.DataFrame(ohlcv_1d, columns=["ts", "o", "h", "l", "c", "v"])
    current = float(df["c"].iloc[-1])
    if current == 0:
        return None

    df["tr"] = pd.concat([
        df["h"] - df["l"], (df["h"] - df["c"].shift()).abs(), (df["l"] - df["c"].shift()).abs(),
    ], axis=1).max(axis=1)

    atr_daily = float(df["tr"].tail(14).mean())
    atr_daily_pct = (atr_daily / current) * 100

    df_recent = df.tail(MORNING_REPORT_LOOKBACK_DAYS)
    highest = float(df_recent["h"].max())
    lowest = float(df_recent["l"].min())
    avg_volume = float(df_recent["v"].mean())

    span = atr_daily * MORNING_REPORT_ATR_MULTIPLIER
    if span <= 0:
        return None

    lower = current - (span / 2)
    upper = current + (span / 2)
    range_pct = ((upper - lower) / current) * 100

    if range_pct > MORNING_REPORT_MAX_RANGE_PCT:
        span = current * (MORNING_REPORT_MAX_RANGE_PCT / 100)
        lower = current - (span / 2)
        upper = current + (span / 2)
        range_pct = MORNING_REPORT_MAX_RANGE_PCT

    if range_pct <= 0:
        return None

    ideal_grids = int(range_pct / 0.10)
    grids = max(MORNING_REPORT_MIN_GRIDS, min(MORNING_REPORT_MAX_GRIDS, ideal_grids))

    grid_step = (upper - lower) / grids
    grid_step_pct = (grid_step / current) * 100 if current else 0

    return {
        "symbol": symbol, "current": current, "highest": highest, "lowest": lowest,
        "suggested_lower": lower, "suggested_upper": upper,
        "range_pct": round(range_pct, 2), "atr_pct": round(atr_daily_pct, 2),
        "atr_multiplier": MORNING_REPORT_ATR_MULTIPLIER,
        "max_range_pct": MORNING_REPORT_MAX_RANGE_PCT,
        "grids": grids, "grid_step": grid_step,
        "grid_step_pct": round(grid_step_pct, 3),
        "avg_volume": round(avg_volume, 2), "lookback": MORNING_REPORT_LOOKBACK_DAYS,
    }


# ============================================================
# بناء الرسائل
# ============================================================
def _stars(points: int, max_points: int) -> str:
    if max_points == 0:
        return ""
    ratio = points / max_points
    if ratio >= 0.9:  return "⭐⭐⭐"
    if ratio >= 0.6:  return "⭐⭐"
    if ratio >= 0.3:  return "⭐"
    return "▫️"


def build_message(cross: dict) -> str:
    symbol = cross["symbol"]
    tf = cross["timeframe"]
    is_bull = (cross["direction"] == "bullish")
    alert_type = cross.get("alert_type", "confirmed")
    s = cross.get("support", {})
    score = s.get("score", 0)
    grade = s.get("grade", "?")

    if alert_type == "pre":
        emoji = "🔔"
        title = "تقارب وشيك — تحذير مبكر"
        type_label = "🔔 <b>تحذير مبكر</b> — لم يحدث التقاطع بعد"
        dir_label = "🟢 اتجاه محتمل: صاعد" if is_bull else "🔴 اتجاه محتمل: هابط"
    elif alert_type == "live":
        emoji = "⚡"
        title = "تقاطع مبدئي"
        type_label = "⚡ <b>تقاطع مبدئي</b> — على الشمعة الجارية"
        dir_label = "🟢 صاعد" if is_bull else "🔴 هابط"
    else:
        emoji = "🚀" if is_bull else "🔻"
        title = "تقاطع صاعد 🟢" if is_bull else "تقاطع هابط 🔴"
        type_label = "✅ <b>تقاطع مؤكد</b> — على شمعة مغلقة"
        dir_label = "🟢 صاعد" if is_bull else "🔴 هابط"

    candle_time = syria_from_ts(cross["candle_ts"])

    # 🆕 ملاحظات الفلتر (في وضع silent فقط، للشفافية)
    filter_note = ""
    if s and SCORE_MODE == "silent" and ENABLE_HARD_FILTER:
        notes = []
        if s.get("vol_ratio", 0) < MIN_VOL_RATIO:
            notes.append(f"الحجم ضعيف ({s.get('vol_ratio')}×)")
        if s.get("adx", 0) < MIN_ADX_HARD:
            notes.append(f"ADX منخفض ({s.get('adx')})")
        if s.get("atr_pct", 0) > MAX_ATR_PCT_HARD:
            notes.append(f"تقلب مرتفع ({s.get('atr_pct')}%)")
        if BLOCK_AGAINST_HTF and s.get("htf_pts", 0) == 0 and alert_type != "pre":
            notes.append("ضد الاتجاه الأكبر (EMA50)")
        if notes:
            filter_note = "\n⚠️ <i>" + " | ".join(notes) + "</i>\n"

    support_block = ""
    if s:
        support_block = (
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>📌 مؤشرات داعمة (النقاط):</b>\n"
            f"• الحجم: <b>{s.get('vol_ratio','?')}×</b> "
            f"{_stars(s.get('vol_pts',0),30)} {s.get('vol_label','?')} "
            f"<i>({s.get('vol_pts',0)}/30)</i>\n"
            f"• ADX: <b>{s.get('adx','?')}</b> "
            f"{_stars(s.get('adx_pts',0),20)} {s.get('adx_label','?')} "
            f"<i>({s.get('adx_pts',0)}/20)</i>\n"
            f"• فرق EMA: <b>{s.get('gap_pct','?')}%</b> "
            f"{_stars(s.get('gap_pts',0),15)} "
            f"<i>({s.get('gap_pts',0)}/15)</i>\n"
            f"• MACD: {s.get('macd_label','?')} "
            f"{_stars(s.get('macd_pts',0),10)} <i>({s.get('macd_pts',0)}/10)</i>\n"
            f"• RSI: <b>{s.get('rsi','?')}</b> — {s.get('rsi_label','?')} "
            f"{_stars(s.get('rsi_pts',0),10)} <i>({s.get('rsi_pts',0)}/10)</i>\n"
            f"• EMA50: {s.get('htf_label','?')} "
            f"{_stars(s.get('htf_pts',0),10)} <i>({s.get('htf_pts',0)}/10)</i>\n"
            f"• ATR: {s.get('atr_pct','?')}% — {s.get('atr_label','?')} "
            f"{_stars(s.get('atr_pts',0),5)} <i>({s.get('atr_pts',0)}/5)</i>\n"
        )

    score_line = f"\n🎯 <b>العلامة النهائية: {score}/100 {grade}</b>\n" if s else ""

    return (
        f"{emoji} <b>{title} — {short(symbol)} [{tf}]</b>\n"
        f"🇸🇾 <b>{syria_now_str()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{type_label}\n"
        f"📊 <b>{dir_label}</b>\n"
        f"⚡ القوة الوصفية: <b>{cross['strength']}</b>\n"
        f"📏 فرق EMA: <b>{cross['gap_pct']:.3f}%</b>"
        f"{score_line}"
        f"{filter_note}"
        f"{support_block}"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"• EMA{EMA_FAST}: {fmt_price(cross['ema_fast'])}\n"
        f"• EMA{EMA_SLOW}: {fmt_price(cross['ema_slow'])}\n"
        f"• السعر: {fmt_price(cross['price'])}\n"
        f"• الفريم: <b>{tf}</b>\n"
        f"• وقت الشمعة: {candle_time}\n"
        f"• المصدر: {cross['exchange']} ({cross.get('market_type', MARKET_TYPE)})"
    )


def build_price_alert_message(alert: dict) -> str:
    symbol = alert["symbol"]
    is_up = alert["direction"] == "up"
    emoji = "🚀" if is_up else "🔻"
    title = "ارتفاع مفاجئ 🟢" if is_up else "انخفاض مفاجئ 🔴"

    return (
        f"{emoji} <b>تنبيه تغير مفاجئ — {short(symbol)}</b>\n"
        f"🇸🇾 <b>{syria_now_str()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{title}</b>\n"
        f"📈 نسبة التغير: <b>{alert['change_pct']}%</b>\n"
        f"🎯 العتبة: {alert['threshold']}%\n"
        f"🔔 التنبيه: <b>{alert['alert_count']} من {alert['max_alerts']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"• السعر الحالي: {fmt_price(alert['current_price'])}\n"
        f"• السعر السابق: {fmt_price(alert['prev_price'])}\n"
        f"• المصدر: {EXCHANGE_NAME.upper()} ({MARKET_TYPE})"
    )


def build_morning_report(analyses: list[dict], title: str = "🌅 التقرير الصباحي") -> str:
    if not analyses:
        return f"{title}\n📭 لا توجد بيانات كافية."

    header = (
        f"{title} — <b>النطاقات المقترحة</b>\n"
        f"🇸🇾 {syria_now_str()}\n"
        f"📅 تحليل آخر <b>{MORNING_REPORT_LOOKBACK_DAYS}</b> أيام\n"
        f"📐 المعادلة: ATR يومي (1d) × <b>{MORNING_REPORT_ATR_MULTIPLIER}</b> "
        f"| حد أقصى: {MORNING_REPORT_MAX_RANGE_PCT}%\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )
    body = ""
    for a in analyses:
        body += (
            f"\n💠 <b>{short(a['symbol'])}</b> — السعر الحالي: "
            f"<b>{fmt_price(a['current'])}</b>\n"
            f"  📉 أدنى {a['lookback']} أيام: {fmt_price(a['lowest'])}\n"
            f"  📈 أعلى {a['lookback']} أيام: {fmt_price(a['highest'])}\n"
            f"  🎯 النطاق المقترح: <b>{fmt_price(a['suggested_lower'])} – "
            f"{fmt_price(a['suggested_upper'])}</b>\n"
            f"  📊 عرض النطاق: {a['range_pct']}%\n"
            f"  🌊 تقلب يومي (ATR): {a['atr_pct']}%\n"
            f"  🔢 عدد الشبكات: <b>{a['grids']}</b>\n"
            f"  📏 الفارق بين الشبكات: {fmt_price(a['grid_step'])} "
            f"({a['grid_step_pct']}%)\n"
            f"  ──────────────────\n"
        )
    footer = "\n💡 <i>ملاحظة: الفارق بين الشبكات يجب أن يكون ≥ 0.10% لتغطية رسوم التداول.</i>"
    return header + body + footer


def build_hybrid_digest() -> str:
    global _hybrid_digest
    if not _hybrid_digest:
        return ""

    msg = (
        f"📊 <b>ملخص الإشارات المتوسطة</b>\n"
        f"🇸🇾 {syria_now_str()}\n"
        f"<i>إشارات بعلامة أقل من {MIN_SCORE} — للعلم فقط</i>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )
    for item in _hybrid_digest[-15:]:
        emoji = "🚀" if item["direction"] == "bullish" else "🔻"
        msg += (
            f"{emoji} <b>{short(item['symbol'])}</b> [{item['timeframe']}] — "
            f"{item['score']}/100 {item['grade']} "
            f"<i>({item['gap_pct']}%)</i>\n"
        )
    msg += f"\n━━━━━━━━━━━━━━━━━━━\n📈 إجمالي: {len(_hybrid_digest)} إشارة"
    _hybrid_digest = []
    return msg


# ============================================================
# الأوامر
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = _exchange.name if _exchange else "❌ فشل"
    thresholds_str = "\n".join([
        f"  • {short(k)}: {v}%" for k, v in PRICE_CHANGE_THRESHOLDS.items() if ":" not in k
    ])
    mode_desc = {
        "silent": "🔇 صامت — يرسل الكل مع علامة",
        "strict": "🎯 صارم — يحجب أقل من العتبة",
        "hybrid": "⚖️ مزدوج — ذهبي فوري + متبقي في ملخص",
    }.get(SCORE_MODE, SCORE_MODE)

    await update.message.reply_text(
        f"🔀 <b>بوت متكامل — EMA + Score + تنبيهات</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>الإعدادات:</b>\n"
        f"• المصدر: <b>{source}</b> ({MARKET_TYPE})\n"
        f"• الفريمات: <b>{', '.join(TIMEFRAMES)}</b>\n"
        f"• EMA: {EMA_FAST}/{EMA_SLOW}\n"
        f"• الرموز: {len(SYMBOLS)}\n"
        f"• فحص التقاطعات: كل {JOB_INTERVAL_MIN} دقائق\n\n"
        f"<b>🎯 نظام العلامة (Score 0-100):</b>\n"
        f"• الوضع: <b>{mode_desc}</b>\n"
        f"• العتبة الدنيا: <b>{MIN_SCORE}</b>\n"
        f"• عتبة ذهبية: <b>{GOLD_SCORE}</b>\n"
        f"• الأوزان: Volume 30 | ADX 20 | Gap 15 | MACD 10 | RSI 10 | EMA50 10 | ATR 5\n\n"
        f"<b>🔒 الفلتر الإلزامي:</b>\n"
        f"• {'✅ مفعل' if ENABLE_HARD_FILTER else '❌ معطل'}\n"
        f"• حجم أدنى: {MIN_VOL_RATIO}×\n"
        f"• ADX أدنى: {MIN_ADX_HARD}\n"
        f"• ATR أقصى: {MAX_ATR_PCT_HARD}%\n"
        f"• منع ضد EMA50: {'✅' if BLOCK_AGAINST_HTF else '❌'}\n"
        f"• عتبة 15m خاصة: {MIN_SCORE_15M}\n\n"
        f"<b>🔔 تنبيهات التغير:</b>\n"
        f"• الفحص: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة\n"
        f"• الحد الأقصى: {MAX_PRICE_ALERTS}\n{thresholds_str}\n\n"
        f"<b>🌅 التقرير الصباحي:</b>\n"
        f"• {'✅ مفعل' if MORNING_REPORT_ENABLED else '❌ معطل'}\n"
        f"• الساعة {MORNING_REPORT_HOUR}:00 (سوريا)\n\n"
        f"<b>الأوامر:</b>\n"
        f"/cross — فحص شامل\n"
        f"/cross5 /cross15 /cross1h — فريم واحد\n"
        f"/checkprice — فحص التغيرات\n"
        f"/report — التقرير الصباحي\n"
        f"/symbols /status",
        parse_mode="HTML",
    )


async def _run_cross(update, timeframes_filter: list[str] | None = None):
    if _exchange is None:
        await update.message.reply_text("❌ المنصة غير مهيأة.")
        return

    tfs = timeframes_filter or TIMEFRAMES
    await update.message.reply_text(f"🔍 جاري الفحص الشامل على: {', '.join(tfs)}...")
    clear_ohlcv_cache()

    detectors = []
    if ENABLE_PRE_CROSS:  detectors.append(detect_pre_crossover)
    if ENABLE_LIVE_CROSS: detectors.append(detect_live_crossover)
    if ENABLE_CONFIRMED:  detectors.append(detect_crossover)

    found = 0
    filtered = 0
    for symbol in SYMBOLS:
        for tf in tfs:
            for key in [(symbol, tf), (symbol, tf, "pre"), (symbol, tf, "live")]:
                _crossover_cache.pop(key, None)
            for detector in detectors:
                cross = await detector(symbol, tf)
                if not cross:
                    continue

                # 🆕 تطبيق الفلتر الإلزامي
                passed, reason, category = passes_hard_filter(cross)
                if not passed:
                    filtered += 1
                    continue

                try:
                    await update.message.reply_text(build_message(cross), parse_mode="HTML")
                    found += 1
                except Exception as e:
                    log.error(f"send {symbol} {tf}: {e}")

    msg = f"✅ تم إرسال {found} إشارة."
    if filtered:
        msg += f"\n🚫 تم حجب {filtered} إشارة ضعيفة (فلتر إلزامي)."
    if found == 0 and filtered == 0:
        msg = f"⚪ لا إشارات على {', '.join(tfs)}"
    await update.message.reply_text(msg)


async def cmd_cross(update, context): await _run_cross(update)
async def cmd_cross5(update, context): await _run_cross(update, ["5m"])
async def cmd_cross15(update, context): await _run_cross(update, ["15m"])
async def cmd_cross1h(update, context): await _run_cross(update, ["1h"])


async def cmd_checkprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _exchange is None:
        await update.message.reply_text("❌ المنصة غير مهيأة.")
        return
    await update.message.reply_text("🔍 جاري فحص التغيرات المفاجئة...")
    found = 0
    for symbol in SYMBOLS:
        alert = await detect_sudden_change(symbol)
        if alert:
            try:
                await update.message.reply_text(build_price_alert_message(alert), parse_mode="HTML")
                found += 1
            except Exception as e:
                log.error(f"send {symbol}: {e}")
    if found == 0:
        await update.message.reply_text("⚪ لا توجد تغيرات مفاجئة تتجاوز العتبات حالياً.")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _exchange is None:
        await update.message.reply_text("❌ المنصة غير مهيأة.")
        return
    await update.message.reply_text(
        f"🔍 جاري تحليل آخر {MORNING_REPORT_LOOKBACK_DAYS} أيام لـ {len(MORNING_SYMBOLS)} رمز..."
    )
    analyses = []
    for symbol in MORNING_SYMBOLS:
        try:
            a = await analyze_range(symbol)
            if a:
                analyses.append(a)
        except Exception as e:
            log.exception(f"report {symbol}: {e}")
    if not analyses:
        await update.message.reply_text("⚪ لا توجد بيانات كافية.")
        return
    try:
        await update.message.reply_text(
            build_morning_report(analyses, title="📊 تقرير فوري"),
            parse_mode="HTML",
        )
    except Exception as e:
        log.error(f"send report: {e}")


async def cmd_symbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📋 <b>الرموز ({len(SYMBOLS)})</b>\n\n"
        + "\n".join(f"• {s}" for s in SYMBOLS)
        + f"\n\n🌅 <b>رموز التقرير ({len(MORNING_SYMBOLS)}):</b>\n"
        + "\n".join(f"• {s}" for s in MORNING_SYMBOLS),
        parse_mode="HTML",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = _exchange.name if _exchange else "❌ فشل"
    states = []
    for symbol in SYMBOLS:
        st = _price_state.get(symbol, {"direction": None, "alert_count": 0, "alerting": False})
        status = f"🔴 تنبيه ({st['alert_count']}/{MAX_PRICE_ALERTS})" if st["alerting"] else "⚪ طبيعي"
        states.append(f"• {short(symbol)}: {status}")

    total_filtered = (
        _filter_stats["filtered_vol"]
        + _filter_stats["filtered_adx"]
        + _filter_stats["filtered_atr"]
        + _filter_stats["filtered_htf"]
        + _filter_stats["filtered_15m_score"]
        + _filter_stats["filtered_other"]
    )

    await update.message.reply_text(
        f"🤖 <b>حالة البوت</b>\n\n"
        f"المصدر: <b>{source}</b> ({MARKET_TYPE})\n"
        f"الفريمات: {', '.join(TIMEFRAMES)}\n"
        f"EMA: {EMA_FAST}/{EMA_SLOW}\n"
        f"الرموز: {len(SYMBOLS)}\n\n"
        f"<b>🎯 نظام العلامة:</b>\n"
        f"• الوضع: <b>{SCORE_MODE}</b>\n"
        f"• العتبة: {MIN_SCORE} | ذهبية: {GOLD_SCORE}\n"
        f"• في الملخص: {len(_hybrid_digest)} إشارة\n\n"
        f"<b>🔒 الفلتر الإلزامي:</b>\n"
        f"• {'✅ مفعل' if ENABLE_HARD_FILTER else '❌ معطل'}\n"
        f"• حجم ≥ {MIN_VOL_RATIO}× | ADX ≥ {MIN_ADX_HARD} | "
        f"ATR ≤ {MAX_ATR_PCT_HARD}%\n"
        f"• ضد EMA50: {'محجوب' if BLOCK_AGAINST_HTF else 'مسموح'}\n"
        f"• 15m: عتبة {MIN_SCORE_15M}\n\n"
        f"<b>📊 إحصائيات الفلترة:</b>\n"
        f"• مُرسَلة: {_filter_stats['sent']}\n"
        f"• محجوبة كلياً: {total_filtered}\n"
        f"   - حجم: {_filter_stats['filtered_vol']}\n"
        f"   - ADX: {_filter_stats['filtered_adx']}\n"
        f"   - ATR: {_filter_stats['filtered_atr']}\n"
        f"   - ضد EMA50: {_filter_stats['filtered_htf']}\n"
        f"   - 15m ضعيفة: {_filter_stats['filtered_15m_score']}\n"
        f"• في الملخص: {_filter_stats['hidden']}\n\n"
        f"<b>🔍 الكشف المبكر:</b>\n"
        f"• pre: {'✅' if ENABLE_PRE_CROSS else '❌'} | "
        f"live: {'✅' if ENABLE_LIVE_CROSS else '❌'} | "
        f"confirmed: {'✅' if ENABLE_CONFIRMED else '❌'}\n"
        f"• عتبة التقارب: {PRE_CROSS_GAP}%\n\n"
        f"<b>💾 كاش:</b> {len(_ohlcv_cache)} OHLCV + {len(_crossover_cache)} إشارة\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>حالة تنبيهات السعر:</b>\n" + "\n".join(states),
        parse_mode="HTML",
    )


async def error_handler(update, context):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning(f"⚠️ network: {err}")
        return
    log.error(f"❌ error: {err}", exc_info=err)


# ============================================================
# Jobs
# ============================================================
def _should_send(cross: dict) -> tuple:
    """
    تحديد ما إذا كانت الإشارة تُرسَل فوراً + نوع الإرسال.
    returns: (send_now: bool, send_to_digest: bool, reason: str)
    """
    # 🔒 الفلتر الإلزامي أولاً
    passed, reason, _category = passes_hard_filter(cross)
    if not passed:
        return False, False, reason

    score = cross.get("support", {}).get("score", 0)
    mode = SCORE_MODE

    if mode == "silent":
        return True, False, ""

    if mode == "strict":
        if score >= MIN_SCORE:
            return True, False, ""
        return False, False, f"أقل من العتبة ({score} < {MIN_SCORE})"

    if mode == "hybrid":
        if score >= GOLD_SCORE:
            return True, False, ""
        if score >= MIN_SCORE:
            return True, False, ""
        return False, True, "علامة منخفضة"

    return True, False, ""


async def crossover_job(context: ContextTypes.DEFAULT_TYPE):
    if _exchange is None:
        return

    detectors = []
    if ENABLE_PRE_CROSS:  detectors.append(detect_pre_crossover)
    if ENABLE_LIVE_CROSS: detectors.append(detect_live_crossover)
    if ENABLE_CONFIRMED:  detectors.append(detect_crossover)

    total = 0
    counts = {"pre": 0, "live": 0, "confirmed": 0}
    hidden = 0
    filtered = 0

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            for detector in detectors:
                try:
                    cross = await detector(symbol, tf)
                    if not cross:
                        continue

                    send_now, to_digest, reason = _should_send(cross)
                    score = cross.get("support", {}).get("score", 0)

                    if send_now and CHAT_ID:
                        try:
                            await context.bot.send_message(
                                chat_id=CHAT_ID,
                                text=build_message(cross),
                                parse_mode="HTML",
                            )
                            log.info(
                                f"[{cross['alert_type']}] {symbol} [{tf}] "
                                f"{cross['direction']} | Score={score} | "
                                f"vol={cross['support'].get('vol_ratio')}× | "
                                f"adx={cross['support'].get('adx')} | "
                                f"htf={cross['support'].get('htf_trend')}"
                            )
                            counts[cross["alert_type"]] = counts.get(cross["alert_type"], 0) + 1
                            total += 1
                            _filter_stats["sent"] += 1
                        except Exception as e:
                            log.error(f"send {symbol} {tf}: {e}")

                    elif to_digest:
                        _hybrid_digest.append({
                            "symbol": symbol, "timeframe": tf,
                            "direction": cross["direction"],
                            "score": score,
                            "grade": cross["support"].get("grade", "?"),
                            "gap_pct": cross["gap_pct"],
                        })
                        hidden += 1
                        _filter_stats["hidden"] += 1
                        log.debug(f"💤 محجوب: {symbol} [{tf}] Score={score}")

                    else:
                        # محجوب بواسطة الفلتر الإلزامي
                        filtered += 1
                        # تحديث الإحصائيات حسب السبب
                        if "الحجم" in reason:
                            _filter_stats["filtered_vol"] += 1
                        elif "ADX" in reason:
                            _filter_stats["filtered_adx"] += 1
                        elif "ATR" in reason:
                            _filter_stats["filtered_atr"] += 1
                        elif "EMA50" in reason:
                            _filter_stats["filtered_htf"] += 1
                        elif "15m" in reason:
                            _filter_stats["filtered_15m_score"] += 1
                        else:
                            _filter_stats["filtered_other"] += 1
                        log.debug(f"🚫 محجوب: {symbol} [{tf}] — {reason}")

                except Exception as e:
                    log.exception(f"job {symbol} {tf}: {e}")

    if total or hidden or filtered:
        log.info(
            f"📤 {total} مُرسَلة "
            f"(pre={counts.get('pre',0)}, live={counts.get('live',0)}, "
            f"confirmed={counts.get('confirmed',0)}) | "
            f"💤 {hidden} في الملخص | 🚫 {filtered} محجوبة"
        )


async def hybrid_digest_job(context: ContextTypes.DEFAULT_TYPE):
    if SCORE_MODE != "hybrid" or not _hybrid_digest or not CHAT_ID:
        return
    try:
        msg = build_hybrid_digest()
        if msg:
            await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")
            log.info(f"📊 تم إرسال ملخص الإشارات المحجوبة")
    except Exception as e:
        log.error(f"digest: {e}")


async def price_alert_job(context: ContextTypes.DEFAULT_TYPE):
    if _exchange is None:
        return
    total = 0
    for symbol in SYMBOLS:
        try:
            alert = await detect_sudden_change(symbol)
            if not alert:
                continue
            if CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=CHAT_ID, text=build_price_alert_message(alert), parse_mode="HTML"
                    )
                    total += 1
                except Exception as e:
                    log.error(f"send {symbol}: {e}")
        except Exception as e:
            log.exception(f"job price_alert {symbol}: {e}")
    if total:
        log.info(f"📤 {total} إشعار تغير مفاجئ")


async def morning_report_job(context: ContextTypes.DEFAULT_TYPE):
    if not MORNING_REPORT_ENABLED or _exchange is None:
        return
    analyses = []
    for symbol in MORNING_SYMBOLS:
        try:
            a = await analyze_range(symbol)
            if a:
                analyses.append(a)
        except Exception as e:
            log.exception(f"morning report {symbol}: {e}")
    if not analyses:
        return
    if CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=build_morning_report(analyses, title="🌅 التقرير الصباحي"),
                parse_mode="HTML",
            )
        except Exception as e:
            log.error(f"send morning: {e}")


# ============================================================
# Health
# ============================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Smart Bot")
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass


def run_health():
    port = int(os.getenv("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()


# ============================================================
# Main
# ============================================================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_REPORT_BOT_TOKEN و TELEGRAM_REPORT_CHAT_ID مطلوبان")
        return

    threading.Thread(target=run_health, daemon=True).start()

    print(f"🔀 Smart Bot — المصدر: {EXCHANGE_NAME.upper()} ({MARKET_TYPE})")
    print(f"📊 الرموز: {len(SYMBOLS)} | الفريمات: {', '.join(TIMEFRAMES)}")
    print(f"📏 EMA {EMA_FAST}/{EMA_SLOW}")
    print(f"🎯 SCORE_MODE={SCORE_MODE} | MIN_SCORE={MIN_SCORE} | GOLD_SCORE={GOLD_SCORE}")
    print(f"🔒 فلتر إلزامي: {'✅' if ENABLE_HARD_FILTER else '❌'} | "
          f"vol≥{MIN_VOL_RATIO}× adx≥{MIN_ADX_HARD} atr≤{MAX_ATR_PCT_HARD}%")
    print(f"🔔 الكشف المبكر: pre={'✅' if ENABLE_PRE_CROSS else '❌'} "
          f"live={'✅' if ENABLE_LIVE_CROSS else '❌'} "
          f"confirmed={'✅' if ENABLE_CONFIRMED else '❌'}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cross", cmd_cross))
    app.add_handler(CommandHandler("cross5", cmd_cross5))
    app.add_handler(CommandHandler("cross15", cmd_cross15))
    app.add_handler(CommandHandler("cross1h", cmd_cross1h))
    app.add_handler(CommandHandler("checkprice", cmd_checkprice))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("symbols", cmd_symbols))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(
            crossover_job, interval=JOB_INTERVAL_MIN * 60, first=15, name="crossover",
        )
        app.job_queue.run_repeating(
            price_alert_job, interval=PRICE_ALERT_INTERVAL_MIN * 60, first=20, name="price_alert",
        )
        if SCORE_MODE == "hybrid":
            app.job_queue.run_repeating(
                hybrid_digest_job, interval=HYBRID_DIGEST_MIN * 60, first=HYBRID_DIGEST_MIN * 60,
                name="hybrid_digest",
            )
            print(f"📊 ملخص الإشارات المحجوبة: كل {HYBRID_DIGEST_MIN} دقيقة")

        if MORNING_REPORT_ENABLED:
            from datetime import time as dt_time
            now_syria = datetime.now(SYRIA_TZ)
            target_syria = now_syria.replace(
                hour=MORNING_REPORT_HOUR, minute=0, second=0, microsecond=0
            )
            target_utc = target_syria.astimezone(timezone.utc)
            app.job_queue.run_daily(
                morning_report_job,
                time=dt_time(hour=target_utc.hour, minute=target_utc.minute, tzinfo=timezone.utc),
                name="morning_report",
            )

    print("✅ جاهز")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=5,
        read_timeout=30, write_timeout=30,
        connect_timeout=30, pool_timeout=30,
    )


if __name__ == "__main__":
    main()
