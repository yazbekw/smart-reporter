"""
Smart Analyst — رأي المحلل فقط
- كل ساعة: رأي المحلل لكل عملة
- عند تغير مفاجئ: رأي فوري
"""
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from supabase import create_client

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_REPORT_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_REPORT_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
           "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOGE/USDT"]

HOURLY_MIN = 60
ANOMALY_MIN = 10


# ============================================================
# Helpers
# ============================================================
def _num(n):
    try:
        n = float(n)
        if n >= 1000: return f"{n:,.2f}"
        if n >= 1: return f"{n:.4f}"
        return f"{n:.6f}"
    except Exception:
        return str(n)


def _emoji(state):
    if "STRONG BUY" in state: return "🟢🔥"
    if "BUY" in state: return "🟢"
    if state == "WAIT FOR CONFIRMATION": return "🔵"
    if "WATCH" in state: return "🟡"
    if "STRONG SELL" in state: return "🔴🔥"
    if "SELL" in state: return "🔴"
    return "⚪"


# ============================================================
# رأي المحلل
# ============================================================
def build_opinion(symbol: str) -> str | None:
    try:
        snap_res = (
            supabase.table("snapshots").select("*")
            .eq("symbol", symbol)
            .order("timestamp", desc=True)
            .limit(1).execute()
        )
        if not snap_res.data:
            return None
        snap = snap_res.data[0]

        state = snap.get("state", "NO TRADE")
        score = snap.get("total_score", 0)
        price = snap.get("price", 0)
        details = snap.get("details") or {}
        regime = details.get("regime") or {}
        levels = details.get("levels") or {}
        warnings = details.get("warnings") or []
        breakdown = {
            "trend": snap.get("trend_score", 0),
            "momentum": snap.get("momentum_score", 0),
            "volume": snap.get("volume_score", 0),
            "orderflow": snap.get("orderflow_score", 0),
            "structure": snap.get("structure_score", 0),
            "context": snap.get("context_score", 0),
            "risk": snap.get("risk_score", 0),
        }

        lines = []
        lines.append(f"🧠 <b>رأي المحلل — {symbol}</b>")
        lines.append(f"{_emoji(state)} <b>{state}</b>")
        lines.append(f"💰 السعر: <b>{_num(price)}</b> | 📊 النقاط: <b>{score}</b>")
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("💬 <b>رأي المحلل</b>")
        lines.append("")

        # ===== التقييم =====
        if "STRONG BUY" in state:
            lines.append("الوضع إيجابي بقوة. الأدلة مجتمعة تشير إلى فرصة شراء عالية الجودة.")
        elif state == "BUY SETUP":
            lines.append("الوضع إيجابي. توجد أدلة كافية لاعتبار هذه فرصة شراء محتملة.")
        elif state == "WAIT FOR CONFIRMATION":
            lines.append("الوضع مائل للإيجابية، لكن يحتاج تأكيداً. الإشارة قريبة لكن غير مكتملة.")
        elif state == "WATCH":
            lines.append("الوضع غير حاسم. توجد أدلة إيجابية جزئية، لكنها غير كافية للدخول.")
        elif "STRONG SELL" in state:
            lines.append("الوضع سلبي بقوة. الأدلة تشير إلى ضغط بيعي حاد.")
        elif state == "SELL SETUP":
            lines.append("الوضع سلبي. توجد أدلة كافية لاعتبار هذه فرصة بيع محتملة.")
        else:
            lines.append("الوضع محايد. لا توجد أدلة كافية لاتخاذ قرار.")
        lines.append("")

        # ===== حالة السوق =====
        rk = regime.get("regime")
        if rk == "trending":
            lines.append("📈 السوق في اتجاه واضح — الاتجاه صديقك.")
        elif rk == "ranging":
            lines.append("↔️ السوق جانبي — ركّز على الدعم والمقاومة.")
        elif rk == "high_vol":
            lines.append("🔥 التقلب مرتفع — قلل حجم الصفقة.")
        elif rk == "low_vol":
            lines.append("😴 التقلب منخفض — قد يجهّز السوق نفسه لاختراق.")
        if rk:
            lines.append("")

        # ===== أقوى/أضعف عامل =====
        labels = {
            "trend": "الاتجاه", "momentum": "الزخم", "volume": "الحجم",
            "orderflow": "تدفق الأوامر", "structure": "البنية",
            "context": "السياق", "risk": "المخاطرة",
        }
        strongest = max(breakdown.items(), key=lambda x: x[1])
        weakest = min(breakdown.items(), key=lambda x: x[1])
        if strongest[1] > 3:
            lines.append(f"💪 أقوى عامل: {labels[strongest[0]]} (+{strongest[1]})")
        if weakest[1] < -2:
            lines.append(f"⚠️ أضعف عامل: {labels[weakest[0]]} ({weakest[1]})")
        lines.append("")

        # ===== النصيحة =====
        if "STRONG BUY" in state:
            lines.append("🎯 <b>النصيحة:</b> فرصة جيدة. ادخل داخل منطقة الدخول مع وقف دقيق.")
        elif state == "BUY SETUP":
            lines.append("🎯 <b>النصيحة:</b> ادخل جزئياً (50%) حتى تتأكد الإشارة.")
        elif state == "WAIT FOR CONFIRMATION":
            lines.append("🎯 <b>النصيحة:</b> انتظر شمعة تأكيد قبل التنفيذ.")
        elif state == "WATCH":
            lines.append("🎯 <b>النصيحة:</b> لا تدخل الآن. راقب وانتظر.")
        elif "STRONG SELL" in state:
            lines.append("🎯 <b>النصيحة:</b> اخرج من أي شراء. لا تشتر الآن.")
        elif state == "SELL SETUP":
            lines.append("🎯 <b>النصيحة:</b> البيع يحتاج تأكيداً إضافياً.")
        else:
            lines.append("🎯 <b>النصيحة:</b> لا تداول. الأفضل الانتظار.")
        lines.append("")

        # ===== تحذير =====
        critical = [w for w in warnings if "R:R" in w or "مقاومة" in w or "Order Flow" in w]
        if critical:
            lines.append("🚨 <b>تحذير:</b>")
            for w in critical[:3]:
                lines.append(f"• {w}")
            lines.append("")

        # ===== خطة التداول =====
        if levels:
            lines.append("━━━━━━━━━━━━━━━━━━━")
            lines.append("🎯 <b>خطة التداول</b>")
            lines.append("")
            lines.append(f"📍 الدخول: {_num(levels.get('entry_low'))} – {_num(levels.get('entry_high'))}")
            lines.append(f"🛑 الوقف: {_num(levels.get('stop_loss'))}")
            lines.append(f"🎯 TP1: {_num(levels.get('tp1'))}")
            lines.append(f"🎯 TP2: {_num(levels.get('tp2'))}")
            lines.append(f"⚖️ R:R: 1 : {levels.get('rr', '—')}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append("🧠 <i>Smart Analyst</i>")

        return "\n".join(lines)

    except Exception as e:
        print(f"[{symbol}] {e}")
        return None


# ============================================================
# إرسال لكل العملات
# ============================================================
async def send_all(bot, tag: str = ""):
    sent = 0
    for s in SYMBOLS:
        try:
            text = build_opinion(s)
            if text and CHAT_ID:
                await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML")
                sent += 1
                print(f"✅ [{s}] {tag}")
        except Exception as e:
            print(f"❌ [{s}] {e}")
    return sent


# ============================================================
# Handlers
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    text = (
        "🧠 <b>رأي المحلل</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "يُرسل رأي المحلل لكل عملة:\n"
        "⏰ كل ساعة\n"
        "⚡ عند تغير مفاجئ\n\n"
        f"📌 <b>Chat ID:</b> <code>{cid}</code>\n\n"
        "/report — الآن (كل العملات)\n"
        "/symbol BTC/USDT — عملة محددة"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري التحليل...")
    n = await send_all(context.bot, tag="manual")
    await update.message.reply_text(f"✅ تم إرسال {n} تقرير")


async def cmd_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم: /symbol BTC/USDT")
        return
    sym = context.args[0].upper()
    text = build_opinion(sym)
    if text:
        await update.message.reply_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ لا توجد بيانات لـ {sym}")


# ============================================================
# Scheduled Jobs
# ============================================================
async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"⏰ [HOURLY] {datetime.now(timezone.utc).strftime('%H:%M')}")
    await send_all(context.bot, tag="hourly")


async def anomaly_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        since = (datetime.now(timezone.utc) - timedelta(minutes=ANOMALY_MIN + 2)).isoformat()
        rows = (
            supabase.table("anomalies").select("*")
            .gte("created_at", since)
            .order("created_at", desc=True)
            .execute()
        ).data or []

        seen = set()
        for a in rows:
            sym = a.get("symbol")
            if not sym or sym in seen:
                continue
            seen.add(sym)
            if a.get("severity") not in ("high", "medium"):
                continue

            text = build_opinion(sym)
            if text and CHAT_ID:
                header = f"⚡ <b>تغير مفاجئ — {sym}</b>\n"
                header += f"📌 {a.get('type')}\n\n"
                await context.bot.send_message(
                    chat_id=CHAT_ID, text=header + text, parse_mode="HTML"
                )
                print(f"⚡ [{sym}] anomaly")
    except Exception as e:
        print(f"[anomaly] {e}")


# ============================================================
# Health Server
# ============================================================
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def do_HEAD(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass


def _run_health():
    port = int(os.getenv("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), _H).serve_forever()


# ============================================================
# Main
# ============================================================
def main():
    threading.Thread(target=_run_health, daemon=True).start()
    print("🧠 Analyst Bot يبدأ...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("symbol", cmd_symbol))

    if app.job_queue:
        app.job_queue.run_repeating(
            hourly_job, interval=HOURLY_MIN * 60, first=10, name="hourly"
        )
        app.job_queue.run_repeating(
            anomaly_job, interval=ANOMALY_MIN * 60, first=60, name="anomaly"
        )
        print(f"⏰ كل {HOURLY_MIN} دقيقة: كل العملات")
        print(f"⚡ كل {ANOMALY_MIN} دقيقة: تغيرات مفاجئة")

    print("✅ Bot جاهز")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
