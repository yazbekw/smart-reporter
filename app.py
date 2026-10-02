"""
بوت إشعارات تقاطع EMA — مصادر متعددة (OKX / Bybit / KuCoin / Kraken)
بدون الحاجة إلى API Key — بيانات عامة فقط
"""
import os
import asyncio
import logging
import threading
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
# الإعدادات الأساسية
# ============================================================
BOT_TOKEN = os.getenv("TELEGRAM_REPORT_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_REPORT_CHAT_ID")

# المصدر: okx / bybit / kucoin / kraken / binance
EXCHANGE_NAME = os.getenv("EXCHANGE_NAME", "okx").lower()

# الرموز (صيغة ccxt الموحدة: BASE/QUOTE)
SYMBOLS = [
    s.strip().upper()
    for s in os.getenv(
        "SYMBOLS",
        "BTC/USDT,ETH/USDT,BNB/USDT,SOL/USDT,XRP/USDT,ADA/USDT,AVAX/USDT,DOGE/USDT"
    ).split(",")
    if s.strip()
]

# إعدادات التقاطع
TIMEFRAME       = os.getenv("TIMEFRAME", "15m")
EMA_FAST        = int(os.getenv("EMA_FAST", "7"))
EMA_SLOW        = int(os.getenv("EMA_SLOW", "25"))
JOB_INTERVAL_MIN = int(os.getenv("JOB_INTERVAL_MIN", "5"))

# فلتر الاتجاه العام (اختياري)
TREND_FILTER_ENABLED = os.getenv("TREND_FILTER_ENABLED", "false").lower() == "true"
TREND_TIMEFRAME      = os.getenv("TREND_TIMEFRAME", "1h")
TREND_EMA_PERIOD     = int(os.getenv("TREND_EMA_PERIOD", "200"))

# توقيت سوريا
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
# التهيئة — ccxt exchange
# ============================================================
def init_exchange():
    """
    ينشئ نسخة ccxt من المنصة المحددة.
    مع تفعيل rate limit + timeout للحماية.
    """
    options = {
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "spot"},
    }
    try:
        if EXCHANGE_NAME == "okx":
            ex = ccxt.okx(options)
        elif EXCHANGE_NAME == "bybit":
            ex = ccxt.bybit(options)
        elif EXCHANGE_NAME == "kucoin":
            ex = ccxt.kucoin(options)
        elif EXCHANGE_NAME == "kraken":
            ex = ccxt.kraken(options)
        elif EXCHANGE_NAME == "binance":
            ex = ccxt.binance(options)
        elif EXCHANGE_NAME == "coinbase":
            ex = ccxt.coinbase(options)
        else:
            log.warning(f"منصة غير معروفة: {EXCHANGE_NAME} → OKX افتراضياً")
            ex = ccxt.okx(options)
        log.info(f"✅ تم تهيئة {ex.name}")
        return ex
    except Exception as e:
        log.error(f"❌ فشل تهيئة {EXCHANGE_NAME}: {e}")
        return None


_exchange = init_exchange()

# كاش للتقاطعات لتجنب التكرار
_crossover_cache: dict = {}
# كاش لاتجاه EMA 200
_trend_cache: dict = {}


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
    day_ar = days_ar.get(now.strftime("%A"), now.strftime("%A"))
    return f"{day_ar} {now.strftime('%Y-%m-%d')} — {now.strftime('%H:%M:%S')}"


def syria_from_ts(ts_ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=SYRIA_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


# ============================================================
# جلب البيانات من المنصة
# ============================================================
async def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 150) -> list | None:
    """يجلب الشموع من المنصة المختارة (async عبر to_thread)."""
    if _exchange is None:
        return None
    try:
        return await asyncio.to_thread(
            _exchange.fetch_ohlcv, symbol, timeframe, None, limit
        )
    except ccxt.BadSymbol:
        log.warning(f"⚠️ رمز غير مدعوم على {EXCHANGE_NAME}: {symbol}")
        return None
    except ccxt.NetworkError as e:
        log.warning(f"🌐 شبكة {symbol}: {e}")
        return None
    except ccxt.ExchangeError as e:
        log.warning(f"⚠️ منصة {symbol}: {e}")
        return None
    except Exception as e:
        log.warning(f"❌ {symbol}: {e}")
        return None


# ============================================================
# فلتر الاتجاه العام
# ============================================================
async def get_trend(symbol: str) -> str | None:
    """
    يحسب اتجاه EMA 200 على إطار أعلى.
    يُعيد 'UP' / 'DOWN' / None.
    """
    if not TREND_FILTER_ENABLED:
        return None

    # كاش 10 دقائق
    import time as _t
    cached = _trend_cache.get(symbol)
    now = _t.time()
    if cached and now - cached[0] < 600:
        return cached[1]

    ohlcv = await fetch_ohlcv(symbol, TREND_TIMEFRAME, TREND_EMA_PERIOD + 30)
    if not ohlcv or len(ohlcv) < TREND_EMA_PERIOD + 5:
        return None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
    ema = calc_ema(df["c"], TREND_EMA_PERIOD)
    last_price = float(df["c"].iloc[-1])
    trend = "UP" if last_price > float(ema.iloc[-1]) else "DOWN"
    _trend_cache[symbol] = (now, trend)
    return trend


# ============================================================
# اكتشاف التقاطع
# ============================================================
async def detect_crossover(symbol: str) -> dict | None:
    """
    يفحص تقاطع EMA على الشمعة المغلقة الأخيرة.
    يُعيد dict أو None.
    """
    ohlcv = await fetch_ohlcv(symbol, TIMEFRAME, EMA_SLOW + 50)
    if not ohlcv or len(ohlcv) < EMA_SLOW + 5:
        return None

    df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
    df["ema_fast"] = calc_ema(df["c"], EMA_FAST)
    df["ema_slow"] = calc_ema(df["c"], EMA_SLOW)

    # الشمعة المغلقة الأخيرة = -2 (الأخيرة قيد التكوين)
    curr, prev = -2, -3

    cf, cs = float(df["ema_fast"].iloc[curr]), float(df["ema_slow"].iloc[curr])
    pf, ps = float(df["ema_fast"].iloc[prev]), float(df["ema_slow"].iloc[prev])

    bullish = (cf > cs) and (pf <= ps)
    bearish = (cf < cs) and (pf >= ps)

    if not (bullish or bearish):
        return None

    direction = "bullish" if bullish else "bearish"
    candle_ts = int(df["ts"].iloc[curr])

    # منع التكرار
    last = _crossover_cache.get(symbol)
    if last and last.get("candle_ts") == candle_ts and last.get("direction") == direction:
        return None
    _crossover_cache[symbol] = {"direction": direction, "candle_ts": candle_ts}

    return {
        "symbol": symbol,
        "direction": direction,
        "ema_fast": round(cf, 4),
        "ema_slow": round(cs, 4),
        "price": round(float(df["c"].iloc[curr]), 4),
        "candle_ts": candle_ts,
        "timeframe": TIMEFRAME,
        "exchange": EXCHANGE_NAME.upper(),
    }


# ============================================================
# بناء الرسالة
# ============================================================
def build_message(cross: dict, trend: str | None = None) -> str:
    symbol = cross["symbol"]
    is_bull = (cross["direction"] == "bullish")
    emoji = "🚀" if is_bull else "🔻"
    title = "تقاطع صاعد 🟢" if is_bull else "تقاطع هابط 🔴"
    candle_time = syria_from_ts(cross["candle_ts"])

    lines = [
        f"{emoji} <b>إشارة تقاطع EMA — {short(symbol)}</b>",
        f"🇸🇾 <b>{syria_now_str()}</b>",
        "━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>{title}</b>",
        f"• EMA{EMA_FAST}: {cross['ema_fast']}",
        f"• EMA{EMA_SLOW}: {cross['ema_slow']}",
        f"• السعر: {cross['price']}",
        f"• الفريم: {cross['timeframe']}",
        f"• وقت الشمعة: {candle_time}",
        f"• المصدر: {cross['exchange']}",
    ]

    # فلتر الاتجاه إن مفعّل
    if TREND_FILTER_ENABLED and trend:
        trend_emoji = "📈" if trend == "UP" else "📉"
        compatible = (
            (is_bull and trend == "UP") or
            (not is_bull and trend == "DOWN")
        )
        compat_txt = "✅ متوافق" if compatible else "⚠️ ضد الاتجاه"
        lines.append(
            f"• الاتجاه العام ({TREND_TIMEFRAME} EMA{TREND_EMA_PERIOD}): "
            f"{trend_emoji} {trend} | {compat_txt}"
        )

    return "\n".join(lines)


# ============================================================
# الأوامر
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source_status = _exchange.name if _exchange else "❌ فشل"
    trend_status = (
        f"🟢 مفعل ({TREND_TIMEFRAME} EMA{TREND_EMA_PERIOD})"
        if TREND_FILTER_ENABLED else "🔴 معطل"
    )
    await update.message.reply_text(
        f"🔀 <b>بوت تقاطع EMA</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>الإعدادات:</b>\n"
        f"• المصدر: <b>{source_status}</b>\n"
        f"• الفريم: {TIMEFRAME}\n"
        f"• EMA: {EMA_FAST}/{EMA_SLOW}\n"
        f"• الرموز: {len(SYMBOLS)}\n"
        f"• الفحص: كل {JOB_INTERVAL_MIN} دقائق\n"
        f"• فلتر الاتجاه: {trend_status}\n\n"
        f"<b>الأوامر:</b>\n"
        f"/cross — فحص فوري\n"
        f"/symbols — عرض الرموز\n"
        f"/status — حالة البوت",
        parse_mode="HTML",
    )


async def cmd_cross(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _exchange is None:
        await update.message.reply_text("❌ المنصة غير مهيأة.")
        return

    await update.message.reply_text("🔍 جاري الفحص...")
    found = 0

    for symbol in SYMBOLS:
        # مسح الكاش لفحص فوري
        _crossover_cache.pop(symbol, None)
        cross = await detect_crossover(symbol)
        if not cross:
            continue

        trend = await get_trend(symbol) if TREND_FILTER_ENABLED else None
        msg = build_message(cross, trend)
        try:
            await update.message.reply_text(msg, parse_mode="HTML")
            found += 1
        except Exception as e:
            log.error(f"send {symbol}: {e}")

    if found == 0:
        await update.message.reply_text(
            f"⚪ لا توجد تقاطعات جديدة على {TIMEFRAME}"
        )


async def cmd_symbols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📋 <b>الرموز ({len(SYMBOLS)})</b>\n\n"
        + "\n".join(f"• {s}" for s in SYMBOLS),
        parse_mode="HTML",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    source_status = _exchange.name if _exchange else "❌ فشل"
    await update.message.reply_text(
        f"🤖 <b>حالة البوت</b>\n\n"
        f"المصدر: <b>{source_status}</b>\n"
        f"الفريم: {TIMEFRAME}\n"
        f"EMA: {EMA_FAST}/{EMA_SLOW}\n"
        f"الرموز: {len(SYMBOLS)}\n"
        f"الفحص: كل {JOB_INTERVAL_MIN} دقيقة\n"
        f"في الكاش: {len(_crossover_cache)} رمز",
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

    found = 0
    for symbol in SYMBOLS:
        try:
            cross = await detect_crossover(symbol)
            if not cross:
                continue

            trend = await get_trend(symbol) if TREND_FILTER_ENABLED else None
            msg = build_message(cross, trend)

            if CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=CHAT_ID, text=msg, parse_mode="HTML"
                    )
                    emoji = "🚀" if cross["direction"] == "bullish" else "🔻"
                    log.info(f"{emoji} {symbol} → {cross['direction']}")
                    found += 1
                except Exception as e:
                    log.error(f"إرسال {symbol}: {e}")
        except Exception as e:
            log.exception(f"crossover_job {symbol}: {e}")

    if found:
        log.info(f"📤 {found} تقاطع مرسل")


# ============================================================
# Health Server
# ============================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Cross Bot")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def run_health():
    port = int(os.getenv("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()
    log.info(f"🩺 Health server على المنفذ {port}")


# ============================================================
# Main
# ============================================================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM_REPORT_BOT_TOKEN و TELEGRAM_REPORT_CHAT_ID مطلوبان")
        return

    threading.Thread(target=run_health, daemon=True).start()

    print(f"🔀 Cross Bot يبدأ — المصدر: {EXCHANGE_NAME.upper()}")
    print(f"📊 الرموز: {len(SYMBOLS)} | فريم: {TIMEFRAME} | EMA {EMA_FAST}/{EMA_SLOW}")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cross", cmd_cross))
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
        print(f"⏰ فحص التقاطع كل {JOB_INTERVAL_MIN} دقائق")

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
