"""
Smart Analyst — رأي المحلل + المصفوفة الإحصائية.
يدعم:
- EARLY BUY / EARLY SELL
- تفسير اتجاه المصفوفة (احتمال صعود/هبوط)
- إشعارات التغيرات المفاجئة
"""
import os
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import NetworkError, TimedOut
from supabase import create_client

from matrix import agreement_score, final_confidence, debug_matrix

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_REPORT_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_REPORT_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
           "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOGE/USDT"]

HOURLY_MIN = 60
ALERT_MIN = 5


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
logger = logging.getLogger(__name__)


async def error_handler(update, context):
    """معالج أخطاء — يتجاهل أخطاء الشبكة"""
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"⚠️ network: {err}")
        return
    logger.error(f"❌ error: {err}", exc_info=err)


# ============================================================
# Helpers
# ============================================================
def _short(symbol: str) -> str:
    """BTC/USDT → BTC"""
    return symbol.split("/")[0].upper()


def _confidence(snap: dict) -> int:
    """ثقة الإشارة (0-100)"""
    score = abs(snap.get("total_score", 0))
    details = snap.get("details") or {}
    regime = (details.get("regime") or {}).get("regime", "")

    breakdown = [
        snap.get("trend_score", 0),
        snap.get("momentum_score", 0),
        snap.get("volume_score", 0),
        snap.get("orderflow_score", 0),
        snap.get("structure_score", 0),
        snap.get("context_score", 0),
    ]

    base = min(score * 3, 60)

    regime_bonus = 0
    if regime == "trending": regime_bonus = 20
    elif regime == "ranging": regime_bonus = 5
    elif regime == "high_vol": regime_bonus = -10

    positive = sum(1 for b in breakdown if b > 2)
    negative = sum(1 for b in breakdown if b < -2)
    harmony = (positive - negative) * 5

    return max(0, min(100, int(base + regime_bonus + harmony)))


def _action_text(state: str, symbol: str) -> str | None:
    """نص الإجراء المختصر — مع اتجاه واضح"""
    s = _short(symbol)

    # BUY / EARLY BUY
    if "STRONG BUY" in state:
        return f"🟢🔥 اشتر بقوة {s}"
    if "EARLY BUY" in state:
        return f"🔵 فرصة شراء مبكرة — {s}"
    if "BUY" in state:
        return f"🟢 اشتر {s}"

    # SELL / EARLY SELL
    if "STRONG SELL" in state:
        return f"🔴🔥 بع بقوة {s}"
    if "EARLY SELL" in state:
        return f"🔵 فرصة بيع مبكرة — {s}"
    if "SELL" in state:
        return f"🔴 بع {s}"

    return None


def _is_buy_state(state: str) -> bool:
    """هل الحالة اتجاهها شراء؟"""
    return "BUY" in state


# ============================================================
# بناء الرسالة المختصرة
# ============================================================
def build_short_signal(symbol: str, state: str, signal_conf: int,
                       direction: str = "LONG",
                       full_details: bool = False) -> str | None:
    """
    رسالة مختصرة:
    - الإجراء
    - ثقة الإشارة
    - توافق المصفوفة (مع تفسير الاتجاه)
    - القرار النهائي
    """
    action = _action_text(state, symbol)
    if not action:
        return None

    m = agreement_score(symbol, direction)

    # القرار النهائي
    if m["available"]:
        final = final_confidence(signal_conf, m["agreement"], direction)
    else:
        final = signal_conf

    lines = [action]
    lines.append(f"🎯 ثقة الإشارة: <b>{signal_conf}%</b>")

    if m["available"]:
        # ⚠️ تفسير الاتجاه
        is_buy = _is_buy_state(state)
        direction_label = "احتمال صعود" if is_buy else "احتمال هبوط"

        # ⚠️ تحذير إذا المصدر __combined__
        source = m.get("source", "?")
        if source == "__combined__":
            lines.append(f"⚠️ <i>لا توجد بيانات {_short(symbol)} — متوسط السوق</i>")

        lines.append(f"📊 توافق المصفوفة: <b>{m['agreement']}%</b> ({direction_label})")
        lines.append(f"⚡ <b>القرار النهائي: {final}%</b>")

        # ⚠️ تحذير إذا التوافق منخفض
        if m["agreement"] < 45:
            lines.append(f"🚨 <b>تحذير: المصفوفة لا تدعم الإشارة!</b>")

        if full_details:
            lines.append("")
            lines.append(f"<i>عينة: {m['n']} | مصدر: {m.get('source', '?')} | "
                         f"t={m['t_stat']} | WR={m['win_rate']}%</i>")
    else:
        reason = m.get("reason", "لا توجد بيانات")
        lines.append(f"📊 المصفوفة: <i>{reason}</i>")
        lines.append(f"⚡ <b>القرار النهائي: {signal_conf}%</b>")

    return "\n".join(lines)


# ============================================================
# الفحص الأساسي
# ============================================================
def get_latest_snapshot(symbol: str) -> dict | None:
    res = (
        supabase.table("snapshots").select("*")
        .eq("symbol", symbol)
        .order("timestamp", desc=True)
        .limit(1).execute()
    )
    return res.data[0] if res.data else None


# ============================================================
# Handlers
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    text = (
        "🧠 <b>رأي المحلل + المصفوفة</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "رسائل مختصرة:\n"
        "• الإجراء (اشتر/بع) — مع اتجاه واضح\n"
        "• ثقة الإشارة\n"
        "• توافق المصفوفة (احتمال صعود/هبوط)\n"
        "• القرار النهائي\n\n"
        f"📌 <b>Chat ID:</b> <code>{cid}</code>\n\n"
        "<b>الأوامر:</b>\n"
        "/now — فحص فوري (كل العملات)\n"
        "/sym BTC/USDT — رمز محدد\n"
        "/matrix BTC — إحصاء المصفوفة\n"
        "/raw — تشخيص المصفوفة"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري الفحص...")
    sent = 0
    for symbol in SYMBOLS:
        snap = get_latest_snapshot(symbol)
        if not snap:
            continue

        state = snap.get("state", "NO TRADE")
        if state in ("NO TRADE", "WATCH"):
            continue

        conf = _confidence(snap)
        direction = "LONG" if _is_buy_state(state) else "SHORT"
        msg = build_short_signal(symbol, state, conf, direction, full_details=True)

        if msg:
            await update.message.reply_text(msg, parse_mode="HTML")
            sent += 1

    if sent == 0:
        await update.message.reply_text("لا توجد إشارات نشطة حالياً.")


async def cmd_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم: /sym BTC/USDT")
        return

    sym = context.args[0].upper()
    if "/" not in sym:
        sym = sym + "/USDT"

    snap = get_latest_snapshot(sym)
    if not snap:
        await update.message.reply_text(f"❌ لا توجد بيانات لـ {sym}")
        return

    state = snap.get("state", "NO TRADE")
    conf = _confidence(snap)
    direction = "LONG" if _is_buy_state(state) else "SHORT"
    msg = build_short_signal(sym, state, conf, direction, full_details=True)

    if msg:
        await update.message.reply_text(msg, parse_mode="HTML")
    else:
        await update.message.reply_text(f"⚪ {sym}: {state} — لا إشارة")


async def cmd_matrix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعرض إحصائيات المصفوفة"""
    sym = "BTC/USDT"
    if context.args:
        s = context.args[0].upper()
        sym = s if "/" in s else s + "/USDT"

    now = datetime.now(timezone.utc)
    m = agreement_score(sym, "LONG", now)

    if not m["available"]:
        debug = debug_matrix(sym, now)
        text = (
            f"📊 <b>{sym}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"❌ {m.get('reason', 'غير معروف')}\n\n"
            f"<b>تشخيص:</b>\n"
            f"<code>{debug}</code>"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        return

    text = (
        f"📊 <b>{sym}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"• Win Rate: <b>{m['win_rate']}%</b>\n"
        f"• متوسط العائد: {m['avg_return']}%\n"
        f"• العينة: {m['n']} صفقة\n"
        f"• t-stat: {m['t_stat']}\n"
        f"• المصدر: {m['source']}\n\n"
        f"<i>تفسير:</i>\n"
        f"• LONG → {m['win_rate']}% (احتمال صعود)\n"
        f"• SHORT → {round(100 - m['win_rate'], 1)}% (احتمال هبوط)"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تشخيص المصفوفة"""
    debug = debug_matrix("BTC/USDT")
    await update.message.reply_text(f"<code>{debug}</code>", parse_mode="HTML")


# ============================================================
# Scheduled Jobs
# ============================================================
async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    """كل ساعة — رسائل مختصرة لكل إشارة نشطة"""
    print(f"⏰ [HOURLY] {datetime.now(timezone.utc).strftime('%H:%M')}")
    sent = 0

    for symbol in SYMBOLS:
        try:
            snap = get_latest_snapshot(symbol)
            if not snap:
                continue

            state = snap.get("state", "NO TRADE")
            if state in ("NO TRADE", "WATCH"):
                continue

            conf = _confidence(snap)
            direction = "LONG" if _is_buy_state(state) else "SHORT"
            msg = build_short_signal(symbol, state, conf, direction)

            if msg and CHAT_ID:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=msg,
                    parse_mode="HTML",
                )
                sent += 1
                print(f"✅ [{symbol}] أُرسل")
        except Exception as e:
            print(f"❌ [{symbol}] {e}")

    if sent == 0:
        print("⚠️ لا توجد إشارات في هذه الساعة")


_state_cache = {}


async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    """فحص التغيرات كل 5 دقائق"""
    try:
        res = (
            supabase.table("snapshots").select("*")
            .order("timestamp", desc=True)
            .limit(40).execute()
        )
        rows = res.data or []

        latest = {}
        for r in rows:
            s = r["symbol"]
            if s not in latest:
                latest[s] = r

        for symbol, snap in latest.items():
            state = snap.get("state")
            score = snap.get("total_score", 0)
            last = _state_cache.get(symbol)

            if last and last[0] != state:
                # إشعار عند التغير إلى إشارة
                if state in ("STRONG BUY SETUP", "BUY SETUP",
                             "STRONG SELL SETUP", "SELL SETUP",
                             "EARLY BUY", "EARLY SELL"):
                    conf = _confidence(snap)
                    direction = "LONG" if _is_buy_state(state) else "SHORT"
                    msg = build_short_signal(symbol, state, conf, direction)

                    if msg and CHAT_ID:
                        header = f"⚡ <b>تغير مفاجئ — {_short(symbol)}</b>\n"
                        header += f"<i>{last[0]} → {state}</i>\n\n"
                        await context.bot.send_message(
                            chat_id=CHAT_ID,
                            text=header + msg,
                            parse_mode="HTML",
                        )
                        print(f"⚡ [{symbol}] {last[0]} → {state}")

            _state_cache[symbol] = (state, score)
    except Exception as e:
        print(f"[alert_job] {e}")


# ============================================================
# Health Server
# ============================================================
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def _run_health():
    port = int(os.getenv("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), _H).serve_forever()


# ============================================================
# Main
# ============================================================
def main():
    threading.Thread(target=_run_health, daemon=True).start()
    print("🧠 Analyst Bot + Matrix يبدأ...")

    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("sym", cmd_symbol))
    app.add_handler(CommandHandler("matrix", cmd_matrix))
    app.add_handler(CommandHandler("raw", cmd_raw))
    app.add_error_handler(error_handler)

    # Jobs
    if app.job_queue:
        app.job_queue.run_repeating(
            hourly_job, interval=HOURLY_MIN * 60, first=10, name="hourly"
        )
        app.job_queue.run_repeating(
            alert_job, interval=ALERT_MIN * 60, first=30, name="alerts"
        )
        print(f"⏰ كل {HOURLY_MIN} دقيقة: رسائل مختصرة")
        print(f"⚡ كل {ALERT_MIN} دقائق: تغيرات مفاجئة")

    print("✅ Bot جاهز")

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
