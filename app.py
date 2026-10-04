"""
بوت إشعارات تقاطع EMA + إشعارات التغير المفاجئ في السعر
يدعم 3 فريمات متوازية (5m + 15m + 1h) + تصنيف قوة
يرسل كل التقاطعات مع ذكر قوتها
+ يرسل 3 تنبيهات متتالية عند اكتشاف تغير مفاجئ، ثم يتوقف حتى يعود التغير طبيعياً
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
# إعدادات
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


EXCHANGE_NAME = os.getenv("EXCHANGE_NAME", "okx").strip().lower()

# ✅ الرموز المحدّثة (BTC كمؤشر عام، الذهب، الفضة، XRP)
_default_symbols = "BTC/USDT,PAXG/USDT,XAG/USDT,XRP/USDT"
_raw = os.getenv("SYMBOLS", "").strip()
SYMBOLS = [
    s.strip().upper()
    for s in (_raw or _default_symbols).split(",")
    if s.strip()
]

# ✅ الفريمات المدعومة
TIMEFRAMES = [
    t.strip()
    for t in os.getenv("TIMEFRAMES", "5m,15m,1h").split(",")
    if t.strip()
]

EMA_FAST = _get_int("EMA_FAST", 7)
EMA_SLOW = _get_int("EMA_SLOW", 25)

JOB_INTERVAL_MIN = _get_int("JOB_INTERVAL_MIN", 2)

# عتبة "الضعيفة" — للتصنيف فقط، لا تحجب الإشارات
MIN_EMA_GAP = _get_float("MIN_EMA_GAP", 0.10)

# ✅ عتبات القوة لكل فريم
STRONG_GAP_5M  = _get_float("STRONG_GAP_5M", 0.25)
STRONG_GAP_15M = _get_float("STRONG_GAP_15M", 0.20)
STRONG_GAP_1H  = _get_float("STRONG_GAP_1H", 0.30)

# ✅ إعدادات إشعار التغير المفاجئ في السعر
PRICE_ALERT_INTERVAL_MIN = _get_int("PRICE_ALERT_INTERVAL_MIN", 1)  # كل دقيقة
MAX_PRICE_ALERTS = _get_int("MAX_PRICE_ALERTS", 3)  # إجمالي 3 تنبيهات

# ✅ عتبات التغير المفاجئ لكل عملة (نسبة مئوية)
PRICE_CHANGE_THRESHOLDS = {
    "BTC/USDT": _get_float("THRESHOLD_BTC", 1.0),
    "XAG/USDT": _get_float("THRESHOLD_XAG", 0.6),
    "XAU/USDT": _get_float("THRESHOLD_XAU", 0.4),
    "PAXG/USDT": _get_float("THRESHOLD_XAU", 0.4), # بديل الذهب
    "XRP/USDT": _get_float("THRESHOLD_XRP", 1.5),
}
DEFAULT_THRESHOLD = _get_float("DEFAULT_THRESHOLD", 1.0)

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
        # ملاحظة: إذا كنت تتداول XAU/XAG كعقود دائمة، غيّر القيمة إلى 'swap'
        "options": {"defaultType": "spot"},
    }
    mapping = {
        "okx": ccxt.okx,
        "bybit": ccxt.bybit,
        "kucoin": ccxt.kucoin,
        "kraken": ccxt.kraken,
        "binance": ccxt.binance,
        "coinbase": ccxt.coinbase,
    }
    cls = mapping.get(EXCHANGE_NAME, ccxt.okx)
    try:
        ex = cls(options)
        log.info(f"✅ تم تهيئة {ex.name}")
        return ex
    except Exception as e:
        log.error(f"❌ فشل التهيئة: {e}")
        return None


_exchange = init_exchange()

# كاش: {(symbol, timeframe): {"direction":..., "candle_ts":...}}
_crossover_cache: dict = {}

# كاش إضافي: {symbol: {"direction":..., "alert_count":..., "alerting":...}} لإشعارات السعر
_price_state: dict = {}


# ============================================================
# أدوات مساعدة
# ============================================================
def short(symbol: str) -> str:
    return symbol.split("/")[0].upper()


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
# تصنيف قوة الإشارة
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
# اكتشاف التقاطع — يرسل كل التقاطعات (بدون حجب)
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
        "ema_fast": round(cf, 4),
        "ema_slow": round(cs, 4),
        "price": round(float(df["c"].iloc[curr]), 4),
        "candle_ts": candle_ts,
        "gap_pct": round(gap_pct, 3),
        "strength": classify_strength(timeframe, gap_pct),
        "exchange": EXCHANGE_NAME.upper(),
    }


# ============================================================
# ✅ منطق إشعار التغير المفاجئ في السعر (محدث)
# ============================================================
async def detect_sudden_change(symbol: str) -> dict | None:
    # نستخدم شموع 1m للكشف السريع
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
        "alerting": False
    })

    if abs(change_pct) >= threshold:
        direction = "up" if change_pct > 0 else "down"

        # إذا تغير الاتجاه، نبدأ حالة جديدة
        if state["direction"] != direction:
            state["direction"] = direction
            state["alert_count"] = 0
            state["alerting"] = False

        # إدارة عدد التنبيهات
        if not state["alerting"]:
            state["alerting"] = True
            state["alert_count"] = 1
        elif state["alert_count"] < MAX_PRICE_ALERTS:
            state["alert_count"] += 1
        else:
            # وصلنا للحد الأقصى (3 تنبيهات)، لا نرسل حتى يعود التغير طبيعياً
            return None

        return {
            "symbol": symbol,
            "direction": direction,
            "change_pct": round(change_pct, 2),
            "current_price": round(current_price, 4),
            "prev_price": round(prev_close, 4),
            "threshold": threshold,
            "alert_count": state["alert_count"],
            "max_alerts": MAX_PRICE_ALERTS,
        }
    else:
        # عاد التغير إلى طبيعته: نصفّر الحالة
        state["direction"] = None
        state["alert_count"] = 0
        state["alerting"] = False

    return None


# ============================================================
# بناء الرسالة
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
        f"• المصدر: {cross['exchange']}"
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
        f"• المصدر: {EXCHANGE_NAME.upper()}"
    )


# ============================================================
# الأوامر
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = _exchange.name if _exchange else "❌ فشل"
    thresholds_str = "\n".join([f"  • {short(k)}: {v}%" for k, v in PRICE_CHANGE_THRESHOLDS.items()])
    
    await update.message.reply_text(
        f"🔀 <b>بوت تقاطع EMA + تنبيهات التغير المفاجئ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>الإعدادات:</b>\n"
        f"• المصدر: <b>{source}</b>\n"
        f"• الفريمات: <b>{', '.join(TIMEFRAMES)}</b>\n"
        f"• EMA: {EMA_FAST}/{EMA_SLOW}\n"
        f"• الرموز: {len(SYMBOLS)}\n"
        f"• الفحص الدوري: كل {JOB_INTERVAL_MIN} دقائق\n"
        f"• عتبات القوة — 5m: {STRONG_GAP_5M}% | "
        f"15m: {STRONG_GAP_15M}% | 1h: {STRONG_GAP_1H}%\n"
        f"• عتبة الضعيفة: {MIN_EMA_GAP}%\n\n"
        f"<b>🆕 تنبيهات التغير المفاجئ:</b>\n"
        f"• الفحص: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة\n"
        f"• إجمالي التنبيهات: {MAX_PRICE_ALERTS} تنبيهات\n"
        f"• العتبات المخصصة:\n{thresholds_str}\n\n"
        f"<b>الأوامر:</b>\n"
        f"/cross — فحص فوري (كل الفريمات)\n"
        f"/cross5 — فحص 5m فقط\n"
        f"/cross15 — فحص 15m فقط\n"
        f"/cross1h — فحص 1h فقط\n"
        f"/checkprice — فحص فوري للتغير المفاجئ\n"
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
        f"🔍 جاري الفحص على: {', '.join(tfs)}..."
    )

    found = 0
    for symbol in SYMBOLS:
        for tf in tfs:
            _crossover_cache.pop((symbol, tf), None)
            cross = await detect_crossover(symbol, tf)
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
            f"⚪ لا تقاطعات على {', '.join(tfs)}"
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
        # ملاحظة: لا نقوم بتصفير الحالة هنا، بل نستخدم الحالة الحالية
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


async def cmd_symbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📋 <b>الرموز ({len(SYMBOLS)})</b>\n\n"
        + "\n".join(f"• {s}" for s in SYMBOLS),
        parse_mode="HTML",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source = _exchange.name if _exchange else "❌ فشل"
    cached_pairs = len(_crossover_cache)
    
    # عرض حالة إشعارات السعر
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
        f"الفريمات: {', '.join(TIMEFRAMES)}\n"
        f"EMA: {EMA_FAST}/{EMA_SLOW}\n"
        f"الرموز: {len(SYMBOLS)}\n"
        f"عتبات القوة — 5m: {STRONG_GAP_5M}% | "
        f"15m: {STRONG_GAP_15M}% | 1h: {STRONG_GAP_1H}%\n"
        f"الفحص: كل {JOB_INTERVAL_MIN} دقيقة\n"
        f"في الكاش: {cached_pairs} (رمز، فريم)\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>🆕 حالة تنبيهات السعر:</b>\n{states_str}",
        parse_mode="HTML",
    )


async def error_handler(update, context):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning(f"⚠️ network: {err}")
        return
    log.error(f"❌ error: {err}", exc_info=err)


# ============================================================
# Job الدوري
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


# ✅ مهمة دورية جديدة لإشعارات التغير المفاجئ
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


# ============================================================
# Health
# ============================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Cross + Price Alert Bot")

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

    print(f"🔀 Cross + Price Alert Bot — المصدر: {EXCHANGE_NAME.upper()}")
    print(f"📊 الرموز: {len(SYMBOLS)} | الفريمات: {', '.join(TIMEFRAMES)}")
    print(f"📏 EMA {EMA_FAST}/{EMA_SLOW}")
    print(f"📐 عتبات القوة — 5m: {STRONG_GAP_5M}% | "
          f"15m: {STRONG_GAP_15M}% | 1h: {STRONG_GAP_1H}%")
    print(f"🆕 تنبيهات التغير: كل {PRICE_ALERT_INTERVAL_MIN} دقيقة | "
          f"الحد الأقصى: {MAX_PRICE_ALERTS} | العتبات: {PRICE_CHANGE_THRESHOLDS}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cross", cmd_cross))
    app.add_handler(CommandHandler("cross5", cmd_cross5))
    app.add_handler(CommandHandler("cross15", cmd_cross15))
    app.add_handler(CommandHandler("cross1h", cmd_cross1h))
    app.add_handler(CommandHandler("checkprice", cmd_checkprice))
    app.add_handler(CommandHandler("symbols", cmd_symbols))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_error_handler(error_handler)

    if app.job_queue:
        # مهمة تقاطع EMA (الاصلية)
        app.job_queue.run_repeating(
            crossover_job,
            interval=JOB_INTERVAL_MIN * 60,
            first=15,
            name="crossover",
        )
        # مهمة تنبيهات السعر (الجديدة) - كل دقيقة
        app.job_queue.run_repeating(
            price_alert_job,
            interval=PRICE_ALERT_INTERVAL_MIN * 60,
            first=20,
            name="price_alert",
        )
        print(f"⏰ فحص التقاطعات كل {JOB_INTERVAL_MIN} دقائق")
        print(f"⏰ فحص التغيرات المفاجئة كل {PRICE_ALERT_INTERVAL_MIN} دقيقة")

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
