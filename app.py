"""
بوت إشعارات متكامل (يدعم Spot و Futures):
1) تقاطع EMA — 3 فريمات + تصنيف قوة
2) تنبيهات التغير المفاجئ في السعر — فحص كل دقيقة، 3 تنبيهات كحد أقصى
3) تقرير صباحي — تحليل آخر 10 أيام واقتراح نطاق وعدد شبكات لبوت الشبكة
4) أمر /report لطلب التقرير يدوياً في أي وقت
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


EXCHANGE_NAME = os.getenv("EXCHANGE_NAME", "binance").strip().lower()

# ✅ نوع السوق: spot أو swap (عقود دائمة)
MARKET_TYPE = os.getenv("MARKET_TYPE", "swap").strip().lower()  # swap افتراضياً
if MARKET_TYPE not in ("spot", "swap", "future"):
    MARKET_TYPE = "swap"

# ============================================================
# الرموز والفريمات
# ============================================================
# عند استخدام swap، تكون الرموز بالشكل: BTC/USDT:USDT
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
# إعدادات تنبيهات التغير المفاجئ
# ============================================================
PRICE_ALERT_INTERVAL_MIN = _get_int("PRICE_ALERT_INTERVAL_MIN", 1)
MAX_PRICE_ALERTS = _get_int("MAX_PRICE_ALERTS", 3)

# ✅ العتبات — تشمل صيغتي Spot و Swap
PRICE_CHANGE_THRESHOLDS = {
    # Spot
    "BTC/USDT": _get_float("THRESHOLD_BTC", 1.0),
    "XAG/USDT": _get_float("THRESHOLD_XAG", 0.6),
    "XAU/USDT": _get_float("THRESHOLD_XAU", 0.4),
    "PAXG/USDT": _get_float("THRESHOLD_XAU", 0.4),
    "XRP/USDT": _get_float("THRESHOLD_XRP", 1.5),
    # Swap (Perpetual)
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
        # ✅ نوع السوق: swap لدعم العقود الدائمة
        "options": {"defaultType": MARKET_TYPE},
    }
    mapping = {
        "okx": ccxt.okx,
        "bybit": ccxt.bybit,
        "kucoin": ccxt.kucoin,
        "kraken": ccxt.kraken,
        "binance": ccxt.binance,
        "coinbase": ccxt.coinbase,
    }
    cls = mapping.get(EXCHANGE_NAME, ccxt.binance)
    try:
        ex = cls(options)
        # تحميل الأسواق للتحقق من صحة الرموز
        ex.load_markets()
        log.info(f"✅ تم تهيئة {ex.name} (السوق: {MARKET_TYPE})")
        return ex
    except Exception as e:
        log.error(f"❌ فشل التهيئة: {e}")
        return None


_exchange = init_exchange()

# كاشات
_crossover_cache: dict = {}
_price_state: dict = {}


# ============================================================
# أدوات مساعدة
# ============================================================
def short(symbol: str) -> str:
    """يستخرج العملة الأساسية من رمز مثل BTC/USDT أو BTC/USDT:USDT."""
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


# ============================================================
# جلب الشموع
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
        log.warning(f"⚠️ {symbol} {timeframe}: {e}")
        return None
    except Exception as e:
        log.warning(f"❌ {symbol}: {e}")
        return None


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
# اكتشاف تقاطع EMA
# ============================================================
async def detect_crossover(symbol: str, timeframe: str) -> dict | None:
    ohlcv = await fetch_ohlcv(symbol, timeframe, EMA_SLOW + 50)
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
        "ema_fast": round(cf, 6),
        "ema_slow": round(cs, 6),
        "price": round(float(df["c"].iloc[curr]), 6),
        "candle_ts": candle_ts,
        "gap_pct": round(gap_pct, 3),
        "strength": classify_strength(timeframe, gap_pct),
        "exchange": EXCHANGE_NAME.upper(),
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
            "current_price": round(current_price, 6),
            "prev_price": round(prev_close, 6),
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
# تحليل النطاق للتقرير الصباحي
# ============================================================
async def analyze_range(symbol: str) -> dict | None:
    limit = MORNING_REPORT_LOOKBACK_DAYS * 6 + 10
    ohlcv = await fetch_ohlcv(symbol, "4h", limit)
    if not ohlcv or len(ohlcv) < 30:
        return None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
    df = df.tail(MORNING_REPORT_LOOKBACK_DAYS * 6)

    highest = float(df["h"].max())
    lowest = float(df["l"].min())
    current = float(df["c"].iloc[-1])
    avg_volume = float(df["v"].mean())

    df["tr"] = pd.concat([
        df["h"] - df["l"],
        (df["h"] - df["c"].shift()).abs(),
        (df["l"] - df["c"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(df["tr"].tail(30).mean())
    atr_pct = (atr / current) * 100 if current else 0

    span = highest - lowest
    if span == 0:
        return None

    lower = round(lowest - span * 0.05, 8)
    upper = round(highest + span * 0.05, 8)
    range_pct = ((upper - lower) / current) * 100 if current else 0

    if range_pct <= 0:
        return None

    # كل شبكة ≥ 0.10%
    ideal_grids = int(range_pct / 0.10)
    grids = max(MORNING_REPORT_MIN_GRIDS, min(MORNING_REPORT_MAX_GRIDS, ideal_grids))

    grid_step = (upper - lower) / grids
    grid_step_pct = (grid_step / current) * 100 if current else 0

    return {
        "symbol": symbol,
        "current": round(current, 8),
        "highest": round(highest, 8),
        "lowest": round(lowest, 8),
        "suggested_lower": lower,
        "suggested_upper": upper,
        "range_pct": round(range_pct, 2),
        "atr_pct": round(atr_pct, 2),
        "grids": grids,
        "grid_step": round(grid_step, 10),
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
    emoji = "🚀" if is_bull else "🔻"
    title = "تقاطع صاعد 🟢" if is_bull else "تقاطع هابط 🔴"
    candle_time = syria_from_ts(cross["candle_ts"])

    return (
        f"{emoji} <b>تقاطع EMA — {short(symbol)} [{tf}]</b>\n"
        f"🇸🇾 <b>{syria_now_str()}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>{title}</b>\n"
        f"⚡ القوة: <b>{cross['strength']}</b>\n"
        f"📏 فرق EMA: <b>{cross['gap_pct']:.3f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"• EMA{EMA_FAST}: {cross['ema_fast']}\n"
        f"• EMA{EMA_SLOW}: {cross['ema_slow']}\n"
        f"• السعر: {cross['price']}\n"
        f"• الفريم: <b>{tf}</b>\n"
        f"• وقت الشمعة: {candle_time}\n"
        f"• المصدر: {cross['exchange']} ({MARKET_TYPE})"
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
        f"• السعر الحالي: {alert['current_price']}\n"
        f"• السعر السابق: {alert['prev_price']}\n"
        f"• المصدر: {EXCHANGE_NAME.upper()} ({MARKET_TYPE})"
    )


def build_morning_report(analyses: list[dict], title: str = "🌅 التقرير الصباحي") -> str:
    if not analyses:
        return f"{title}\n📭 لا توجد بيانات كافية."

    header = (
        f"{title} — <b>النطاقات المقترحة</b>\n"
        f"🇸🇾 {syria_now_str()}\n"
        f"📅 تحليل آخر <b>{MORNING_REPORT_LOOKBACK_DAYS}</b> أيام (فريم 4 ساعات)\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    body = ""
    for a in analyses:
        body += (
            f"\n💠 <b>{short(a['symbol'])}</b> — السعر الحالي: <b>{a['current']}</b>\n"
            f"  📉 أدنى سعر: {a['lowest']}  |  📈 أعلى سعر: {a['highest']}\n"
            f"  🎯 النطاق المقترح: <b>{a['suggested_lower']} – {a['suggested_upper']}</b>\n"
            f"  📊 عرض النطاق: {a['range_pct']}%\n"
            f"  🌊 متوسط التقلب: {a['atr_pct']}%\n"
            f"  🔢 عدد الشبكات: <b>{a['grids']}</b>\n"
            f"  📏 الفارق بين الشبكات: {a['grid_step']} ({a['grid_step_pct']}%)\n"
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
        if ":" not in k  # نعرض Spot فقط لتجنب التكرار
    ])

    await update.message.reply_text(
        f"🔀 <b>بوت متكامل — تقاطع EMA + تنبيهات + تقرير صباحي</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>الإعدادات:</b>\n"
        f"• المصدر: <b>{source}</b>\n"
        f"• نوع السوق: <b>{MARKET_TYPE}</b>\n"
        f"• الفريمات: <b>{', '.join(TIMEFRAMES)}</b>\n"
        f"• EMA: {EMA_FAST}/{EMA_SLOW}\n"
        f"• الرموز: {len(SYMBOLS)}\n"
        f"• فحص التقاطعات: كل {JOB_INTERVAL_MIN} دقائق\n"
        f"• عتبات القوة — 5m: {STRONG_GAP_5M}% | "
        f"15m: {STRONG_GAP_15M}% | 1h: {STRONG_GAP_1H}%\n"
        f"• عتبة الضعيفة: {MIN_EMA_GAP}%\n\n"
        f"<b>🔔 تنبيهات التغير المفاجئ:</b>\n"
        f"• الفحص: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة\n"
        f"• إجمالي التنبيهات: {MAX_PRICE_ALERTS}\n"
        f"• العتبات المخصصة:\n{thresholds_str}\n\n"
        f"<b>🌅 التقرير الصباحي:</b>\n"
        f"• الحالة: {'✅ مفعل' if MORNING_REPORT_ENABLED else '❌ معطل'}\n"
        f"• الساعة: {MORNING_REPORT_HOUR}:00 (توقيت سوريا)\n"
        f"• الأيام: {MORNING_REPORT_LOOKBACK_DAYS}\n"
        f"• الرموز: {', '.join(short(s) for s in MORNING_SYMBOLS)}\n\n"
        f"<b>الأوامر:</b>\n"
        f"/cross — فحص تقاطعات (كل الفريمات)\n"
        f"/cross5 /cross15 /cross1h — فحص فريم واحد\n"
        f"/checkprice — فحص فوري للتغيرات المفاجئة\n"
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
    await update.message.reply_text(f"🔍 جاري الفحص على: {', '.join(tfs)}...")

    found = 0
    for symbol in SYMBOLS:
        for tf in tfs:
            _crossover_cache.pop((symbol, tf), None)
            cross = await detect_crossover(symbol, tf)
            if not cross:
                continue
            try:
                await update.message.reply_text(build_message(cross), parse_mode="HTML")
                found += 1
            except Exception as e:
                log.error(f"send {symbol} {tf}: {e}")

    if found == 0:
        await update.message.reply_text(f"⚪ لا تقاطعات على {', '.join(tfs)}")


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
    """طلب التقرير الصباحي في أي وقت."""
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
        f"عتبات القوة — 5m: {STRONG_GAP_5M}% | "
        f"15m: {STRONG_GAP_15M}% | 1h: {STRONG_GAP_1H}%\n"
        f"فحص التقاطعات: كل {JOB_INTERVAL_MIN} دقيقة\n"
        f"فحص التغيرات: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة\n"
        f"في الكاش: {cached_pairs} (رمز، فريم)\n"
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

    total = 0
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                cross = await detect_crossover(symbol, tf)
                if not cross:
                    continue

                msg = build_message(cross)
                if CHAT_ID:
                    try:
                        await context.bot.send_message(
                            chat_id=CHAT_ID, text=msg, parse_mode="HTML"
                        )
                        emoji = "🚀" if cross["direction"] == "bullish" else "🔻"
                        log.info(
                            f"{emoji} {symbol} [{tf}] {cross['direction']} "
                            f"| gap={cross['gap_pct']}% | {cross['strength']}"
                        )
                        total += 1
                    except Exception as e:
                        log.error(f"send {symbol} {tf}: {e}")
            except Exception as e:
                log.exception(f"job {symbol} {tf}: {e}")

    if total:
        log.info(f"📤 {total} إشارة مُرسَلة")


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
                        f"| change={alert['change_pct']}% | alert={alert['alert_count']}/{MAX_PRICE_ALERTS}"
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
    print(f"🔔 تنبيهات التغير: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة | "
          f"الحد الأقصى: {MAX_PRICE_ALERTS}")
    print(f"🌅 التقرير الصباحي: {'✅ مفعل' if MORNING_REPORT_ENABLED else '❌ معطل'} "
          f"| الساعة {MORNING_REPORT_HOUR}:00 (سوريا) | آخر {MORNING_REPORT_LOOKBACK_DAYS} أيام")

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
        # 1) تقاطع EMA
        app.job_queue.run_repeating(
            crossover_job,
            interval=JOB_INTERVAL_MIN * 60,
            first=15,
            name="crossover",
        )
        # 2) تنبيهات التغير المفاجئ
        app.job_queue.run_repeating(
            price_alert_job,
            interval=PRICE_ALERT_INTERVAL_MIN * 60,
            first=20,
            name="price_alert",
        )
        # 3) التقرير الصباحي
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
