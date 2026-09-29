"""
Binance Monitor Bot — مراقبة صفقات Binance Futures + أوامر Telegram تفاعلية.
يعمل على Web Service (يفتح منفذ PORT) + WebSocket للإشعارات الفورية.
"""
import os
import sys
import time
import re
import json
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import BinanceAPIException
from telegram import Update
from telegram.constants import ParseMode, ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import NetworkError, TimedOut
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# الإعدادات
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")

REQUIRED = {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "TELEGRAM_CHAT_ID": CHAT_ID_RAW,
    "BINANCE_API_KEY": API_KEY,
    "BINANCE_API_SECRET": API_SECRET,
}
missing = [k for k, v in REQUIRED.items() if not v]
if missing:
    print(f"❌ متغيرات ناقصة: {', '.join(missing)}", file=sys.stderr)
    sys.exit(1)

CHAT_ID = int(CHAT_ID_RAW)

HOURLY_MIN = 60           # دورة التقرير كل ساعة
WS_RECONNECT_DELAY = 10   # ثوانٍ قبل إعادة الاتصال بـ WebSocket

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
logging.getLogger("binance").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# ============================================================
# حالة عامة
# ============================================================
binance_client: AsyncClient | None = None
notifications_enabled = True

# كاش HTTP + كشف الحظر
_http_cache: dict = {}
_banned_until_ms: int = 0


# ============================================================
# Health Server (ليعمل على Web Service)
# ============================================================
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Binance Monitor Bot")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def _run_health():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info(f"🩺 Health server على المنفذ {port}")
    server.serve_forever()


# ============================================================
# أدوات مساعدة
# ============================================================
def is_banned() -> bool:
    return _banned_until_ms > int(time.time() * 1000)


def ban_remaining_sec() -> int:
    if not is_banned():
        return 0
    return max(0, (_banned_until_ms - int(time.time() * 1000)) // 1000)


def _register_ban(exc: Exception):
    global _banned_until_ms
    m = re.search(r"banned until (\d+)", str(exc))
    if m:
        _banned_until_ms = int(m.group(1))
        log.warning(
            f"⛔ Binance IP banned until "
            f"{datetime.fromtimestamp(_banned_until_ms/1000, timezone.utc)}"
        )


async def cached_http(key: str, coro_factory, ttl: int = 20):
    """HTTP مع كاش + كشف الحظر + رسالة واضحة."""
    if is_banned():
        raise RuntimeError(
            f"⛔ Binance حظر IP مؤقتاً. المتبقي: ~{ban_remaining_sec()//60} دقيقة."
        )

    now = time.time()
    hit = _http_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]

    try:
        val = await coro_factory()
        _http_cache[key] = (now, val)
        return val
    except BinanceAPIException as e:
        if e.code == -1003:
            _register_ban(e)
            raise RuntimeError(
                f"⛔ Binance حظر IP. المتبقي: ~{ban_remaining_sec()//60} دقيقة."
            )
        raise


async def send(text: str, context: ContextTypes.DEFAULT_TYPE | None = None):
    """إرسال للقناة الأساسية."""
    if not notifications_enabled:
        return
    try:
        if context is not None:
            await context.bot.send_message(
                chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML
            )
        elif binance_client is not None and _app is not None:
            await _app.bot.send_message(
                chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML
            )
    except Exception as e:
        log.error(f"send error: {e}")


# ============================================================
# جلب البيانات
# ============================================================
async def fetch_positions() -> list:
    data = await cached_http(
        "positions",
        lambda: binance_client.futures_position_information(),
        ttl=20,
    )
    return [p for p in data if float(p["positionAmt"]) != 0]


async def fetch_account() -> dict:
    return await cached_http(
        "account",
        lambda: binance_client.futures_account(),
        ttl=20,
    )


async def fetch_open_orders() -> list:
    return await cached_http(
        "orders",
        lambda: binance_client.futures_get_open_orders(),
        ttl=20,
    )


# ============================================================
# التنسيق
# ============================================================
def fmt_position(p: dict) -> str | None:
    try:
        amt = float(p["positionAmt"])
    except (KeyError, ValueError):
        return None
    if amt == 0:
        return None

    side = "🟢 LONG" if amt > 0 else "🔴 SHORT"
    entry = float(p.get("entryPrice", 0) or 0)
    mark = float(p.get("markPrice", 0) or 0)
    pnl = float(p.get("unRealizedProfit", 0) or 0)
    lev = p.get("leverage", "?")
    icon = "📈" if pnl >= 0 else "📉"

    lines = [
        f"{side} | <b>{p.get('symbol', '?')}</b>",
        f"  الحجم: {abs(amt)}",
        f"  الدخول: {entry}",
    ]
    if mark:
        lines.append(f"  الحالي: {mark}")
    lines.append(f"  الرافعة: x{lev}")
    lines.append(f"  {icon} PnL: <b>{pnl:+.4f} USDT</b>")
    return "\n".join(lines)


HELP_TEXT = (
    "🤖 <b>بوت مراقبة Binance</b>\n"
    "━━━━━━━━━━━━━━━━━━━\n\n"
    "<b>الأوامر:</b>\n"
    "/positions — الصفقات المفتوحة\n"
    "/balance — الرصيد والهامش\n"
    "/pnl — الربح/الخسارة\n"
    "/orders — الأوامر المعلقة\n"
    "/status — حالة البوت\n"
    "/mute — إيقاف الإشعارات\n"
    "/unmute — تشغيل الإشعارات\n"
    "/help — المساعدة"
)


# ============================================================
# أوامر Telegram
# ============================================================
def authorized(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == CHAT_ID


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        positions = await fetch_positions()
        if not positions:
            await update.message.reply_text("لا توجد صفقات مفتوحة حالياً. ✨")
            return

        body = "\n\n".join(filter(None, (fmt_position(p) for p in positions)))
        total_pnl = sum(float(p.get("unRealizedProfit", 0) or 0) for p in positions)

        await update.message.reply_text(
            f"📊 <b>الصفقات المفتوحة ({len(positions)})</b>\n\n"
            f"{body}\n\n──────────────\n"
            f"📈 إجمالي PnL: <b>{total_pnl:+.4f} USDT</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception("cmd_positions")
        await update.message.reply_text(f"❌ {e}")


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        account = await fetch_account()
        wallet = float(account.get("totalWalletBalance", 0) or 0)
        unrealized = float(account.get("totalUnrealizedProfit", 0) or 0)
        margin_bal = float(account.get("totalMarginBalance", 0) or 0)
        available = float(account.get("availableBalance", 0) or 0)
        used = float(account.get("totalPositionInitialMargin", 0) or 0)

        await update.message.reply_text(
            f"💰 <b>الرصيد</b>\n\n"
            f"المحفظة: <b>{wallet:.2f}</b> USDT\n"
            f"PnL عائم: <b>{unrealized:+.4f}</b> USDT\n"
            f"رصيد الهامش: <b>{margin_bal:.2f}</b> USDT\n"
            f"هامش مستخدم: <b>{used:.2f}</b> USDT\n"
            f"متاح للتداول: <b>{available:.2f}</b> USDT",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception("cmd_balance")
        await update.message.reply_text(f"❌ {e}")


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        positions = await fetch_positions()
        unrealized = sum(float(p.get("unRealizedProfit", 0) or 0) for p in positions)

        realized_24h = 0.0
        try:
            start_ms = int((time.time() - 86400) * 1000)
            income = await cached_http(
                "income_24h",
                lambda: binance_client.futures_income_history(
                    startTime=start_ms, incomeType="REALIZED_PNL", limit=1000
                ),
                ttl=60,
            )
            realized_24h = sum(float(i["income"]) for i in income)
        except Exception as e:
            log.warning(f"income_history: {e}")

        lines = []
        for p in positions:
            pnl = float(p.get("unRealizedProfit", 0) or 0)
            icon = "📈" if pnl >= 0 else "📉"
            lines.append(f"{icon} <b>{p.get('symbol', '?')}</b>: {pnl:+.4f} USDT")
        breakdown = "\n".join(lines) if lines else "—"

        await update.message.reply_text(
            f"📊 <b>PnL</b>\n\n"
            f"عائم الآن: <b>{unrealized:+.4f}</b> USDT\n"
            f"محقق (24س): <b>{realized_24h:+.4f}</b> USDT\n\n"
            f"<b>تفصيل:</b>\n{breakdown}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception("cmd_pnl")
        await update.message.reply_text(f"❌ {e}")


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        orders = await fetch_open_orders()
        if not orders:
            await update.message.reply_text("لا توجد أوامر معلقة.")
            return
        lines = []
        for o in orders:
            price = o.get("price") or o.get("stopPrice") or "Market"
            lines.append(
                f"• <b>{o['symbol']}</b> {o['side']} {o['type']}\n"
                f"  الكمية: {o['origQty']} @ {price}"
            )
        await update.message.reply_text(
            f"📋 <b>الأوامر المعلقة ({len(orders)})</b>\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.exception("cmd_orders")
        await update.message.reply_text(f"❌ {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    status = "🟢 متصل" if binance_client else "🔴 غير متصل"
    notif = "🔔 مفعلة" if notifications_enabled else "🔕 مكتومة"
    ban = (f"\n⛔ محظور — متبقي ~{ban_remaining_sec()//60} دقيقة"
           if is_banned() else "")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    await update.message.reply_text(
        f"🤖 <b>حالة البوت</b>\n\n"
        f"Binance: {status}\n"
        f"الإشعارات: {notif}\n"
        f"الوقت: {ts}{ban}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global notifications_enabled
    if not authorized(update):
        return
    notifications_enabled = False
    await update.message.reply_text("🔕 تم إيقاف الإشعارات.")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global notifications_enabled
    if not authorized(update):
        return
    notifications_enabled = True
    await update.message.reply_text("🔔 تم تشغيل الإشعارات.")


# ============================================================
# Jobs (تعمل عبر JobQueue)
# ============================================================
async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    """تقرير كل ساعة."""
    try:
        if is_banned():
            log.warning("hourly: متخطى (حظر)")
            return

        positions = await fetch_positions()
        try:
            account = await fetch_account()
            balance = float(account.get("totalWalletBalance", 0) or 0)
        except Exception:
            balance = 0.0

        unrealized = sum(float(p.get("unRealizedProfit", 0) or 0) for p in positions)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        if not positions:
            msg = (
                f"⏰ <b>تقرير كل ساعة</b> | {ts}\n\n"
                f"لا توجد صفقات مفتوحة.\n"
                f"💰 الرصيد: {balance:.2f} USDT"
            )
        else:
            body = "\n\n".join(filter(None, (fmt_position(p) for p in positions)))
            msg = (
                f"⏰ <b>تقرير كل ساعة</b> | {ts}\n\n{body}\n\n"
                f"──────────────\n"
                f"💰 الرصيد: {balance:.2f} USDT\n"
                f"📊 PnL: {unrealized:+.4f} USDT\n"
                f"🔢 العدد: {len(positions)}"
            )

        await context.bot.send_message(
            chat_id=CHAT_ID, text=msg, parse_mode=ParseMode.HTML
        )
        log.info("✅ Hourly report sent")
    except Exception as e:
        log.exception(f"hourly_job: {e}")


# ============================================================
# WebSocket (يعمل في task منفصل)
# ============================================================
async def user_stream_task(app: Application):
    """يستمع لتحديثات Binance ويرسل إشعارات."""
    bsm = BinanceSocketManager(binance_client)
    while True:
        try:
            async with bsm.futures_user_socket() as stream:
                log.info("🔌 WebSocket متصل")
                try:
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text="🔌 تم الاتصال بـ Binance WebSocket",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

                while True:
                    msg = await stream.recv()

                    # فتح/تعديل صفقة
                    if msg.get("e") == "ACCOUNT_UPDATE":
                        for pos in msg["a"]["P"]:
                            amt = float(pos["pa"])
                            if amt != 0 and pos.get("bc", "0") == "0":
                                side = "🟢 LONG" if amt > 0 else "🔴 SHORT"
                                text = (
                                    f"🚀 <b>صفقة مفتوحة</b>\n\n"
                                    f"الاتجاه: {side}\n"
                                    f"الرمز: <b>{pos['s']}</b>\n"
                                    f"الحجم: {abs(amt)}\n"
                                    f"الدخول: {pos['ep']}"
                                )
                                if notifications_enabled:
                                    try:
                                        await app.bot.send_message(
                                            chat_id=CHAT_ID, text=text,
                                            parse_mode=ParseMode.HTML,
                                        )
                                    except Exception as e:
                                        log.error(f"WS send: {e}")

                    # تنفيذ أمر
                    elif msg.get("e") == "ORDER_TRADE_UPDATE":
                        o = msg["o"]
                        if o["X"] == "FILLED":
                            rp_val = float(o.get("rp", "0") or 0)
                            pnl_txt = (
                                f"\n💰 PnL محقق: <b>{rp_val:+.4f} USDT</b>"
                                if rp_val != 0 else ""
                            )
                            text = (
                                f"⚡ <b>تنفيذ أمر</b>\n"
                                f"الرمز: <b>{o['s']}</b>\n"
                                f"الاتجاه: {o['S']}\n"
                                f"الكمية: {o['q']}\n"
                                f"متوسط السعر: {o.get('ap') or '0'}{pnl_txt}"
                            )
                            if notifications_enabled:
                                try:
                                    await app.bot.send_message(
                                        chat_id=CHAT_ID, text=text,
                                        parse_mode=ParseMode.HTML,
                                    )
                                except Exception as e:
                                    log.error(f"WS send: {e}")
        except Exception as e:
            log.exception(f"user_stream: {e}")
            await asyncio.sleep(WS_RECONNECT_DELAY)


async def post_init(app: Application):
    """يشغّل مهام Binance بعد جاهزية التطبيق."""
    global binance_client, _app
    _app = app

    binance_client = await AsyncClient.create(API_KEY, API_SECRET)
    log.info("Binance client جاهز")

    # WebSocket في task خلفي
    app.create_task(user_stream_task(app))

    # رسالة بدء
    try:
        await app.bot.send_message(
            chat_id=CHAT_ID,
            text="🤖 <b>بدأ بوت مراقبة Binance</b>\nأرسل /help للأوامر.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error(f"startup send: {e}")


async def post_shutdown(app: Application):
    if binance_client:
        await binance_client.close_connection()
        log.info("Binance client مُغلق")


async def error_handler(update, context):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning(f"⚠️ network: {err}")
        return
    log.error(f"❌ error: {err}", exc_info=err)


# ============================================================
# Main
# ============================================================
_app: Application | None = None


def main():
    # 1) Health server (ليعمل على Web Service)
    threading.Thread(target=_run_health, daemon=True).start()

    print("🚀 Binance Monitor Bot يبدأ...")

    # 2) تطبيق Telegram
    app = Application.builder() \
        .token(TELEGRAM_TOKEN) \
        .post_init(post_init) \
        .post_shutdown(post_shutdown) \
        .build()

    # 3) Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_error_handler(error_handler)

    # 4) Jobs
    if app.job_queue:
        app.job_queue.run_repeating(
            hourly_job,
            interval=HOURLY_MIN * 60,
            first=300,     # أول تقرير بعد 5 دقائق
            name="hourly",
        )
        print(f"⏰ تقرير كل {HOURLY_MIN} دقيقة")

    print("✅ Bot جاهز")

    # 5) التشغيل (نفس طريقة bot.py)
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
