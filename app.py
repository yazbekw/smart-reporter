"""
بوت إشعارات متكامل (Bybit / OKX / غيرها):
1) تقاطع EMA — 3 فريمات + كشف مبكر + مؤشرات داعمة
2) تنبيهات التغير المفاجئ — فحص كل دقيقة، 3 تنبيهات كحد أقصى
3) تقرير صباحي — نطاق مبني على ATR اليومي (1d) + حد أقصى
4) أمر /report لطلب التقرير في أي وقت
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
SYMBOLS = [
    s.strip().upper()
    for s in (_raw or _default_symbols).split(",")
    if s.strip()
]

TIMEFRAMES = [
    t.strip()
    for t in os.getenv("TIMEFRAMES", "5m,15m,1h").split(",")
    if t.strip()
]

# ============================================================
# إعدادات تقاطع EMA
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
ENABLE_PRE_CROSS  = _get_bool("ENABLE_PRE_CROSS", True)   # 🔔 تحذير مبكر
ENABLE_LIVE_CROSS = _get_bool("ENABLE_LIVE_CROSS", True)  # ⚡ تقاطع مبدئي
ENABLE_CONFIRMED  = _get_bool("ENABLE_CONFIRMED", True)   # ✅ تقاطع مؤكد

# عتبة "التقارب" — إذا كان الفرق أقل من هذه النسبة، يعتبر وشيكاً
PRE_CROSS_GAP = _get_float("PRE_CROSS_GAP", 0.05)  # %

# عدد الشموع المطلوبة للتحقق من أن التقارب "يتسارع"
PRE_CROSS_LOOKBACK = _get_int("PRE_CROSS_LOOKBACK", 3)

# فترة التهدئة: لا تكرر التحذير المبكر قبل N شمعة
PRE_CROSS_COOLDOWN = _get_int("PRE_CROSS_COOLDOWN", 3)

# كاش OHLCV — كم ثانية يُعتبر الكاش صالحاً
OHLCV_CACHE_SECONDS = _get_int("OHLCV_CACHE_SECONDS", 30)

# ============================================================
# إعدادات تنبيهات التغير المفاجئ
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
MORNING_SYMBOLS = [
    s.strip().upper()
    for s in (_morning_raw or ",".join(SYMBOLS)).split(",")
    if s.strip()
]

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
        "okx": ccxt.okx,
        "bybit": ccxt.bybit,
        "kucoin": ccxt.kucoin,
        "kraken": ccxt.kraken,
        "binance": ccxt.binance,
        "coinbase": ccxt.coinbase,
        "gate": ccxt.gate,
        "bitget": ccxt.bitget,
    }
    cls = mapping.get(EXCHANGE_NAME, ccxt.bybit)

    try:
        ex = cls(options)
        ex.load_markets()
        log.info(f"✅ تم تهيئة {ex.name} (السوق: {MARKET_TYPE}) | "
                 f"{len(ex.markets)} سوق متاح")
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
    high = df["h"]
    low = df["l"]
    close = df["c"]

    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
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
        return await asyncio.to_thread(
            _exchange.fetch_ohlcv, symbol, timeframe, None, limit
        )
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
    """كاش قصير الأمد لتقليل استهلاك rate limit."""
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
# تصنيف قوة تقاطع EMA
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
# المؤشرات الداعمة (تُضاف لكل إشارة)
# ============================================================
def analyze_support(df: pd.DataFrame, curr_idx: int = -2) -> dict:
    """
    حساب المؤشرات الداعمة للإشارة — لا تُستخدم كفلتر، بل كمعلومات إضافية.
    """
    close = df["c"]
    vol = df["v"]
    current_price = float(close.iloc[curr_idx])

    # ADX
    try:
        adx = float(calc_adx(df).iloc[curr_idx])
    except Exception:
        adx = 0
    if adx >= 40:
        adx_label = "اتجاه قوي جداً"
    elif adx >= 25:
        adx_label = "اتجاه واضح"
    elif adx >= 20:
        adx_label = "اتجاه ضعيف"
    else:
        adx_label = "سوق عرضي"

    # Volume ratio
    try:
        start = max(0, len(df) + curr_idx - 20)
        end = len(df) + curr_idx
        vol_ma20 = float(vol.iloc[start:end].mean())
        vol_curr = float(vol.iloc[curr_idx])
        vol_ratio = vol_curr / vol_ma20 if vol_ma20 > 0 else 0
    except Exception:
        vol_ratio = 0
    if vol_ratio >= 2.0:
        vol_label = "قوي جداً"
    elif vol_ratio >= 1.2:
        vol_label = "جيد"
    elif vol_ratio >= 0.7:
        vol_label = "طبيعي"
    else:
        vol_label = "ضعيف"

    # RSI
    try:
        rsi = float(calc_rsi(close).iloc[curr_idx])
    except Exception:
        rsi = 50
    if rsi >= 70:
        rsi_label = "تشبع شرائي"
    elif rsi >= 55:
        rsi_label = "زخم صاعد"
    elif rsi >= 45:
        rsi_label = "محايد"
    elif rsi >= 30:
        rsi_label = "زخم هابط"
    else:
        rsi_label = "تشبع بيعي"

    # MACD histogram
    try:
        _, _, hist = calc_macd(close)
        h_curr = float(hist.iloc[curr_idx])
        h_prev = float(hist.iloc[curr_idx - 1])
        rising = h_curr > h_prev
        if h_curr > 0 and rising:
            macd_label = "يتسارع صعوداً ↗"
        elif h_curr > 0 and not rising:
            macd_label = "يتباطأ صعوداً ↘"
        elif h_curr < 0 and rising:
            macd_label = "يتباطأ هبوطاً ↗"
        else:
            macd_label = "يتسارع هبوطاً ↘"
    except Exception:
        macd_label = "غير محدد"

    # ATR %
    try:
        atr = float(calc_atr(df).iloc[curr_idx])
        atr_pct = (atr / current_price) * 100 if current_price > 0 else 0
    except Exception:
        atr_pct = 0

    # HTF trend (EMA50 كنائب للاتجاه الأكبر)
    try:
        ema50 = float(calc_ema(close, 50).iloc[curr_idx])
        htf_trend = "صاعد" if current_price > ema50 else "هابط"
    except Exception:
        htf_trend = "?"

    return {
        "adx": round(adx, 1),
        "adx_label": adx_label,
        "vol_ratio": round(vol_ratio, 2),
        "vol_label": vol_label,
        "rsi": round(rsi, 1),
        "rsi_label": rsi_label,
        "macd_label": macd_label,
        "atr_pct": round(atr_pct, 2),
        "htf_trend": htf_trend,
    }


# ============================================================
# 1) اكتشاف التقاطع المؤكد (شمعة مغلقة -2)
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

    cf = float(df["ef"].iloc[curr])
    cs = float(df["es"].iloc[curr])
    pf = float(df["ef"].iloc[prev])
    ps = float(df["es"].iloc[prev])

    bullish = (cf > cs) and (pf <= ps)
    bearish = (cf < cs) and (pf >= ps)
    if not (bullish or bearish):
        return None

    direction = "bullish" if bullish else "bearish"
    gap_pct = abs(cf - cs) / cs * 100
    candle_ts = int(df["ts"].iloc[curr])

    cache_key = (symbol, timeframe)
    last = _crossover_cache.get(cache_key)
    if last and last.get("candle_ts") == candle_ts and last.get("direction") == direction:
        return None
    _crossover_cache[cache_key] = {"direction": direction, "candle_ts": candle_ts}

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "ema_fast": round(cf, 8),
        "ema_slow": round(cs, 8),
        "price": round(float(df["c"].iloc[curr]), 8),
        "candle_ts": candle_ts,
        "gap_pct": round(gap_pct, 3),
        "strength": classify_strength(timeframe, gap_pct),
        "exchange": EXCHANGE_NAME.upper(),
        "market_type": MARKET_TYPE,
        "alert_type": "confirmed",
        "support": analyze_support(df, curr),
    }


# ============================================================
# 2) اكتشاف التقاطع المبدئي (شمعة جارية -1)
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

    cf = float(df["ef"].iloc[curr])
    cs = float(df["es"].iloc[curr])
    pf = float(df["ef"].iloc[prev])
    ps = float(df["es"].iloc[prev])

    bullish = (cf > cs) and (pf <= ps)
    bearish = (cf < cs) and (pf >= ps)
    if not (bullish or bearish):
        return None

    direction = "bullish" if bullish else "bearish"
    gap_pct = abs(cf - cs) / cs * 100
    candle_ts = int(df["ts"].iloc[curr])

    cache_key = (symbol, timeframe, "live")
    last = _crossover_cache.get(cache_key)
    if last and last.get("candle_ts") == candle_ts:
        return None
    _crossover_cache[cache_key] = {"direction": direction, "candle_ts": candle_ts}

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "ema_fast": round(cf, 8),
        "ema_slow": round(cs, 8),
        "price": round(float(df["c"].iloc[curr]), 8),
        "candle_ts": candle_ts,
        "gap_pct": round(gap_pct, 3),
        "strength": "⚡ مبدئي (قابل للتغير)",
        "exchange": EXCHANGE_NAME.upper(),
        "market_type": MARKET_TYPE,
        "alert_type": "live",
        "support": analyze_support(df, curr),
    }


# ============================================================
# 3) اكتشاف التقارب المبكر (قبل التقاطع)
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

    cf = float(df["ef"].iloc[curr])
    cs = float(df["es"].iloc[curr])
    gap_pct = abs(cf - cs) / cs * 100

    # الشرط 1: الفرق صغير
    if gap_pct >= PRE_CROSS_GAP:
        return None

    # الشرط 2: الفرق يتقلص
    gaps = []
    for i in range(PRE_CROSS_LOOKBACK):
        idx = curr - i
        f = float(df["ef"].iloc[idx])
        s = float(df["es"].iloc[idx])
        gaps.append(abs(f - s) / s * 100)

    gaps_chrono = list(reversed(gaps))
    is_converging = all(
        gaps_chrono[i] >= gaps_chrono[i + 1]
        for i in range(len(gaps_chrono) - 1)
    )
    if not is_converging:
        return None

    # الشرط 3: لم يعبر بعد
    if abs(cf - cs) < 1e-9:
        return None

    direction = "bullish" if cf < cs else "bearish"
    candle_ts = int(df["ts"].iloc[curr])

    # cooldown للتحذير المبكر
    cache_key = (symbol, timeframe, "pre")
    last = _crossover_cache.get(cache_key)
    if last:
        last_ts = last.get("candle_ts", 0)
        tf_ms = _TF_MS.get(timeframe, 900_000)
        if candle_ts - last_ts < tf_ms * PRE_CROSS_COOLDOWN:
            return None
    _crossover_cache[cache_key] = {"direction": direction, "candle_ts": candle_ts}

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "ema_fast": round(cf, 8),
        "ema_slow": round(cs, 8),
        "price": round(float(df["c"].iloc[curr]), 8),
        "candle_ts": candle_ts,
        "gap_pct": round(gap_pct, 3),
        "strength": "🔔 تقارب وشيك",
        "exchange": EXCHANGE_NAME.upper(),
        "market_type": MARKET_TYPE,
        "alert_type": "pre",
        "support": analyze_support(df, curr),
    }


# ============================================================
# منطق إشعار التغير المفاجئ (1m)
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

    state = _price_state.setdefault(symbol, {
        "direction": None,
        "alert_count": 0,
        "alerting": False,
    })

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
            "symbol": symbol,
            "direction": direction,
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
# تحليل النطاق للتقرير الصباحي (ATR على 1d)
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
        df["h"] - df["l"],
        (df["h"] - df["c"].shift()).abs(),
        (df["l"] - df["c"].shift()).abs(),
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
        "symbol": symbol,
        "current": current,
        "highest": highest,
        "lowest": lowest,
        "suggested_lower": lower,
        "suggested_upper": upper,
        "range_pct": round(range_pct, 2),
        "atr_pct": round(atr_daily_pct, 2),
        "atr_multiplier": MORNING_REPORT_ATR_MULTIPLIER,
        "max_range_pct": MORNING_REPORT_MAX_RANGE_PCT,
        "grids": grids,
        "grid_step": grid_step,
        "grid_step_pct": round(grid_step_pct, 3),
        "avg_volume": round(avg_volume, 2),
        "lookback": MORNING_REPORT_LOOKBACK_DAYS,
    }


# ============================================================
# بناء الرسائل
# ============================================================
def build_message(cross: dict) -> str:
    symbol = cross["symbol"]
    tf = cross["timeframe"]
    is_bull = (cross["direction"] == "bullish")
    alert_type = cross.get("alert_type", "confirmed")

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
    s = cross.get("support", {})

    support_block = ""
    if s:
        support_block = (
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>📌 مؤشرات داعمة:</b>\n"
            f"• ADX: <b>{s.get('adx', '?')}</b> — {s.get('adx_label', '?')}\n"
            f"• RSI: <b>{s.get('rsi', '?')}</b> — {s.get('rsi_label', '?')}\n"
            f"• MACD: {s.get('macd_label', '?')}\n"
            f"• الحجم: <b>{s.get('vol_ratio', '?')}×</b> — {s.get('vol_label', '?')}\n"
            f"• ATR: {s.get('atr_pct', '?')}%\n"
            f"• اتجاه EMA50: {s.get('htf_trend', '?')}\n"
        )

    return (
        f"{emoji} <b>{title} — {short(symbol)} [{tf}]</b>\n"
        f"🇸🇾 <b>{syria_now_str()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{type_label}\n"
        f"📊 <b>{dir_label}</b>\n"
        f"⚡ القوة: <b>{cross['strength']}</b>\n"
        f"📏 فرق EMA: <b>{cross['gap_pct']:.3f}%</b>\n"
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

    footer = (
        f"\n💡 <i>ملاحظة: الفارق بين الشبكات يجب أن يكون ≥ 0.10% "
        f"لتغطية رسوم التداول.</i>"
    )
    return header + body + footer


# ============================================================
# الأوامر
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = _exchange.name if _exchange else "❌ فشل"
    thresholds_str = "\n".join([
        f"  • {short(k)}: {v}%" for k, v in PRICE_CHANGE_THRESHOLDS.items()
        if ":" not in k
    ])

    await update.message.reply_text(
        f"🔀 <b>بوت متكامل — تقاطع EMA + كشف مبكر + تنبيهات</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>الإعدادات:</b>\n"
        f"• المصدر: <b>{source}</b>\n"
        f"• نوع السوق: <b>{MARKET_TYPE}</b>\n"
        f"• الفريمات: <b>{', '.join(TIMEFRAMES)}</b>\n"
        f"• EMA: {EMA_FAST}/{EMA_SLOW}\n"
        f"• الرموز: {len(SYMBOLS)}\n"
        f"• فحص التقاطعات: كل {JOB_INTERVAL_MIN} دقائق\n"
        f"• عتبات القوة — 5m: {STRONG_GAP_5M}% | "
        f"15m: {STRONG_GAP_15M}% | 1h: {STRONG_GAP_1H}%\n\n"
        f"<b>🔔 الكشف المبكر:</b>\n"
        f"• تحذير مبكر: {'✅' if ENABLE_PRE_CROSS else '❌'} "
        f"(عتبة التقارب: {PRE_CROSS_GAP}%)\n"
        f"• تقاطع مبدئي: {'✅' if ENABLE_LIVE_CROSS else '❌'}\n"
        f"• تقاطع مؤكد: {'✅' if ENABLE_CONFIRMED else '❌'}\n\n"
        f"<b>🔔 تنبيهات التغير المفاجئ:</b>\n"
        f"• الفحص: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة\n"
        f"• إجمالي التنبيهات: {MAX_PRICE_ALERTS}\n"
        f"• العتبات المخصصة:\n{thresholds_str}\n\n"
        f"<b>🌅 التقرير الصباحي:</b>\n"
        f"• الحالة: {'✅ مفعل' if MORNING_REPORT_ENABLED else '❌ معطل'}\n"
        f"• الساعة: {MORNING_REPORT_HOUR}:00 (توقيت سوريا)\n"
        f"• الأيام: {MORNING_REPORT_LOOKBACK_DAYS}\n"
        f"• معامل ATR: {MORNING_REPORT_ATR_MULTIPLIER}\n"
        f"• حد أقصى للنطاق: {MORNING_REPORT_MAX_RANGE_PCT}%\n\n"
        f"<b>الأوامر:</b>\n"
        f"/cross — فحص شامل (مبكر + مبدئي + مؤكد)\n"
        f"/cross5 /cross15 /cross1h — فريم واحد\n"
        f"/checkprice — فحص التغيرات المفاجئة\n"
        f"/report — التقرير الصباحي فوراً\n"
        f"/symbols — عرض الرموز\n"
        f"/status — حالة البوت",
        parse_mode="HTML",
    )


async def _run_cross(update, timeframes_filter: list[str] | None = None):
    if _exchange is None:
        await update.message.reply_text("❌ المنصة غير مهيأة.")
        return

    tfs = timeframes_filter or TIMEFRAMES
    await update.message.reply_text(
        f"🔍 جاري الفحص الشامل على: {', '.join(tfs)}..."
    )

    # مسح الكاش لضمان بيانات حديثة
    clear_ohlcv_cache()

    detectors = []
    if ENABLE_PRE_CROSS:
        detectors.append(detect_pre_crossover)
    if ENABLE_LIVE_CROSS:
        detectors.append(detect_live_crossover)
    if ENABLE_CONFIRMED:
        detectors.append(detect_crossover)

    found = 0
    for symbol in SYMBOLS:
        for tf in tfs:
            # مسح كاش الإشارات لهذا الزوج
            _crossover_cache.pop((symbol, tf), None)
            _crossover_cache.pop((symbol, tf, "pre"), None)
            _crossover_cache.pop((symbol, tf, "live"), None)

            for detector in detectors:
                cross = await detector(symbol, tf)
                if not cross:
                    continue
                try:
                    await update.message.reply_text(
                        build_message(cross), parse_mode="HTML"
                    )
                    found += 1
                except Exception as e:
                    log.error(f"send {symbol} {tf}: {e}")

    if found == 0:
        await update.message.reply_text(
            f"⚪ لا إشارات على {', '.join(tfs)}"
        )


async def cmd_cross(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_cross(update)


async def cmd_cross5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_cross(update, ["5m"])


async def cmd_cross15(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_cross(update, ["15m"])


async def cmd_cross1h(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _run_cross(update, ["1h"])


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
                await update.message.reply_text(
                    build_price_alert_message(alert), parse_mode="HTML"
                )
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
        f"🔍 جاري تحليل آخر {MORNING_REPORT_LOOKBACK_DAYS} أيام لـ "
        f"{len(MORNING_SYMBOLS)} رمز..."
    )

    analyses = []
    for symbol in MORNING_SYMBOLS:
        try:
            a = await analyze_range(symbol)
            if a:
                analyses.append(a)
            else:
                log.warning(f"⚠️ لا توجد بيانات كافية لـ {symbol}")
        except Exception as e:
            log.exception(f"report {symbol}: {e}")

    if not analyses:
        await update.message.reply_text("⚪ لا توجد بيانات كافية لإنشاء التقرير.")
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
    cached_pairs = len(_crossover_cache)
    cached_ohlcv = len(_ohlcv_cache)

    states = []
    for symbol in SYMBOLS:
        st = _price_state.get(symbol, {"direction": None, "alert_count": 0, "alerting": False})
        if st["alerting"]:
            status = f"🔴 تنبيه ({st['alert_count']}/{MAX_PRICE_ALERTS})"
        else:
            status = "⚪ طبيعي"
        states.append(f"• {short(symbol)}: {status}")
    states_str = "\n".join(states)

    await update.message.reply_text(
        f"🤖 <b>حالة البوت</b>\n\n"
        f"المصدر: <b>{source}</b>\n"
        f"نوع السوق: <b>{MARKET_TYPE}</b>\n"
        f"الفريمات: {', '.join(TIMEFRAMES)}\n"
        f"EMA: {EMA_FAST}/{EMA_SLOW}\n"
        f"الرموز: {len(SYMBOLS)}\n"
        f"فحص التقاطعات: كل {JOB_INTERVAL_MIN} دقيقة\n"
        f"فحص التغيرات: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة\n"
        f"<b>الكشف المبكر:</b>\n"
        f"• تحذير مبكر: {'✅' if ENABLE_PRE_CROSS else '❌'} "
        f"(عتبة {PRE_CROSS_GAP}% | cooldown {PRE_CROSS_COOLDOWN} شموع)\n"
        f"• تقاطع مبدئي: {'✅' if ENABLE_LIVE_CROSS else '❌'}\n"
        f"• تقاطع مؤكد: {'✅' if ENABLE_CONFIRMED else '❌'}\n"
        f"في كاش الإشارات: {cached_pairs}\n"
        f"في كاش OHLCV: {cached_ohlcv}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>حالة تنبيهات السعر:</b>\n{states_str}",
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
async def crossover_job(context: ContextTypes.DEFAULT_TYPE):
    if _exchange is None:
        return

    detectors = []
    if ENABLE_PRE_CROSS:
        detectors.append(detect_pre_crossover)
    if ENABLE_LIVE_CROSS:
        detectors.append(detect_live_crossover)
    if ENABLE_CONFIRMED:
        detectors.append(detect_crossover)

    total = 0
    counts = {"pre": 0, "live": 0, "confirmed": 0}

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            for detector in detectors:
                try:
                    cross = await detector(symbol, tf)
                    if not cross:
                        continue

                    msg = build_message(cross)
                    if CHAT_ID:
                        try:
                            await context.bot.send_message(
                                chat_id=CHAT_ID, text=msg, parse_mode="HTML"
                            )
                            log.info(
                                f"[{cross['alert_type']}] {symbol} [{tf}] "
                                f"{cross['direction']} | gap={cross['gap_pct']}% "
                                f"| {cross['strength']}"
                            )
                            counts[cross["alert_type"]] = counts.get(cross["alert_type"], 0) + 1
                            total += 1
                        except Exception as e:
                            log.error(f"send {symbol} {tf}: {e}")
                except Exception as e:
                    log.exception(f"job {symbol} {tf}: {e}")

    if total:
        log.info(
            f"📤 {total} إشارة مُرسَلة "
            f"(pre={counts.get('pre', 0)}, "
            f"live={counts.get('live', 0)}, "
            f"confirmed={counts.get('confirmed', 0)})"
        )


async def price_alert_job(context: ContextTypes.DEFAULT_TYPE):
    if _exchange is None:
        return

    total = 0
    for symbol in SYMBOLS:
        try:
            alert = await detect_sudden_change(symbol)
            if not alert:
                continue

            msg = build_price_alert_message(alert)
            if CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=CHAT_ID, text=msg, parse_mode="HTML"
                    )
                    emoji = "🚀" if alert["direction"] == "up" else "🔻"
                    log.info(
                        f"{emoji} {symbol} {alert['direction']} "
                        f"| change={alert['change_pct']}% | "
                        f"alert={alert['alert_count']}/{MAX_PRICE_ALERTS}"
                    )
                    total += 1
                except Exception as e:
                    log.error(f"send {symbol}: {e}")
        except Exception as e:
            log.exception(f"job price_alert {symbol}: {e}")

    if total:
        log.info(f"📤 {total} إشعار تغير مفاجئ مُرسَل")


async def morning_report_job(context: ContextTypes.DEFAULT_TYPE):
    if not MORNING_REPORT_ENABLED or _exchange is None:
        return

    log.info("🌅 بدء إعداد التقرير الصباحي...")
    analyses = []
    for symbol in MORNING_SYMBOLS:
        try:
            a = await analyze_range(symbol)
            if a:
                analyses.append(a)
        except Exception as e:
            log.exception(f"morning report {symbol}: {e}")

    if not analyses:
        log.warning("⚠️ لا توجد تحليلات للتقرير الصباحي")
        return

    msg = build_morning_report(analyses, title="🌅 التقرير الصباحي")
    if CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=CHAT_ID, text=msg, parse_mode="HTML"
            )
            log.info(f"📤 تم إرسال التقرير الصباحي ({len(analyses)} رمز)")
        except Exception as e:
            log.error(f"send morning report: {e}")


# ============================================================
# Health
# ============================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Full Bot")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


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

    print(f"🔀 Full Bot — المصدر: {EXCHANGE_NAME.upper()} ({MARKET_TYPE})")
    print(f"📊 الرموز: {len(SYMBOLS)} | الفريمات: {', '.join(TIMEFRAMES)}")
    print(f"📏 EMA {EMA_FAST}/{EMA_SLOW}")
    print(f"📐 عتبات القوة — 5m: {STRONG_GAP_5M}% | "
          f"15m: {STRONG_GAP_15M}% | 1h: {STRONG_GAP_1H}%")
    print(f"🔔 الكشف المبكر: "
          f"pre={'✅' if ENABLE_PRE_CROSS else '❌'} "
          f"live={'✅' if ENABLE_LIVE_CROSS else '❌'} "
          f"confirmed={'✅' if ENABLE_CONFIRMED else '❌'} "
          f"| عتبة التقارب: {PRE_CROSS_GAP}%")
    print(f"💾 كاش OHLCV: {OHLCV_CACHE_SECONDS} ثانية")
    print(f"🔔 تنبيهات التغير: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة | "
          f"الحد الأقصى: {MAX_PRICE_ALERTS}")
    print(f"🌅 التقرير الصباحي: "
          f"{'✅ مفعل' if MORNING_REPORT_ENABLED else '❌ معطل'} | "
          f"الساعة {MORNING_REPORT_HOUR}:00 (سوريا) | "
          f"آخر {MORNING_REPORT_LOOKBACK_DAYS} أيام | "
          f"معامل ATR: {MORNING_REPORT_ATR_MULTIPLIER} | "
          f"حد أقصى: {MORNING_REPORT_MAX_RANGE_PCT}%")

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
            crossover_job,
            interval=JOB_INTERVAL_MIN * 60,
            first=15,
            name="crossover",
        )
        app.job_queue.run_repeating(
            price_alert_job,
            interval=PRICE_ALERT_INTERVAL_MIN * 60,
            first=20,
            name="price_alert",
        )
        if MORNING_REPORT_ENABLED:
            from datetime import time as dt_time
            now_syria = datetime.now(SYRIA_TZ)
            target_syria = now_syria.replace(
                hour=MORNING_REPORT_HOUR, minute=0, second=0, microsecond=0
            )
            target_utc = target_syria.astimezone(timezone.utc)

            app.job_queue.run_daily(
                morning_report_job,
                time=dt_time(
                    hour=target_utc.hour,
                    minute=target_utc.minute,
                    tzinfo=timezone.utc,
                ),
                name="morning_report",
            )
            print(f"⏰ التقرير الصباحي: كل يوم {MORNING_REPORT_HOUR}:00 بتوقيت سوريا")

        print(f"⏰ فحص التقاطعات: كل {JOB_INTERVAL_MIN} دقائق")
        print(f"⏰ فحص التغيرات: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة")

    print("✅ جاهز")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=5,
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
    )


if __name__ == "__main__":
    main()
