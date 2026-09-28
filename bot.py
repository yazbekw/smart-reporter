"""
Smart Analyst — زبدة التحليل + إشعارات فورية
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
ALERT_MIN = 5  # فحص التغيرات كل 5 دقائق


# ============================================================
# Helpers
# ============================================================
def _num(n, digits=4):
    try:
        n = float(n)
        if n >= 1000: return f"{n:,.2f}"
        if n >= 1: return f"{n:.{digits}f}"
        return f"{n:.6f}"
    except Exception:
        return str(n)


def _emoji(state):
    if "STRONG BUY" in state: return "🟢🔥"
    if "BUY" in state: return "🟢"
    if state == "EARLY OPPORTUNITY": return "🔵"
    if "WATCH" in state: return "🟡"
    if "STRONG SELL" in state: return "🔴🔥"
    if "SELL" in state: return "🔴"
    return "⚪"


def _confidence(snap) -> int:
    """
    يحسب نسبة الثقة بناءً على عدة عوامل:
    - Score
    - Regime
    - انسجام العوامل
    """
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

    # الأساس: نسبة من الـ score
    base = min(score * 3, 60)

    # Regime bonus
    regime_bonus = 0
    if regime == "trending": regime_bonus = 20
    elif regime == "ranging": regime_bonus = 5
    elif regime == "low_vol": regime_bonus = 0
    elif regime == "high_vol": regime_bonus = -10

    # انسجام: عدد العوامل الموجبة
    positive = sum(1 for b in breakdown if b > 2)
    negative = sum(1 for b in breakdown if b < -2)
    harmony = (positive - negative) * 5

    confidence = base + regime_bonus + harmony
    return max(0, min(100, int(confidence)))


# ============================================================
# زبدة التحليل
# ============================================================
def build_essence(symbol: str) -> str | None:
    """يبني زبدة التحليل — مختصر مفيد"""
    try:
        res = (
            supabase.table("snapshots").select("*")
            .eq("symbol", symbol)
            .order("timestamp", desc=True)
            .limit(1).execute()
        )
        if not res.data:
            return None
        snap = res.data[0]

        state = snap.get("state", "NO TRADE")
        score = snap.get("total_score", 0)
        price = snap.get("price", 0)
        details = snap.get("details") or {}
        regime = (details.get("regime") or {}).get("regime", "")
        levels = details.get("levels") or {}
        warnings = details.get("warnings") or []

        confidence = _confidence(snap)

        # ===== القرار =====
        if "STRONG BUY" in state:
            decision = "🟢🔥 <b>ادخل الآن — قوي</b>"
            decision_short = "🟢🔥 ادخل الآن"
        elif state == "BUY SETUP":
            decision = "🟢 <b>ادخل بحذر</b>"
            decision_short = "🟢 ادخل بحذر"
        elif state == "WAIT FOR CONFIRMATION":
            decision = "🔵 <b>انتظر تأكيد</b>"
            decision_short = "🔵 انتظر تأكيد"
        elif state == "WATCH":
            decision = "🟡 <b>راقب فقط</b>"
            decision_short = "🟡 راقب فقط"
        elif "STRONG SELL" in state:
            decision = "🔴🔥 <b>اخرج — بيع حاد</b>"
            decision_short = "🔴🔥 اخرج"
        elif state == "SELL SETUP":
            decision = "🔴 <b>بيع محتمل</b>"
            decision_short = "🔴 بيع محتمل"
        else:
            decision = "⚪ <b>لا تداول</b>"
            decision_short = "⚪ لا تداول"

        # ===== السبب الرئيسي =====
        bd = {
            "الاتجاه": snap.get("trend_score", 0),
            "الزخم": snap.get("momentum_score", 0),
            "الحجم": snap.get("volume_score", 0),
            "تدفق الأوامر": snap.get("orderflow_score", 0),
            "البنية": snap.get("structure_score", 0),
            "السياق": snap.get("context_score", 0),
        }

        positives = [k for k, v in bd.items() if v > 3]
        negatives = [k for k, v in bd.items() if v < -2]

        # جملة السبب
        if positives:
            why = " + ".join(positives[:3]) + " إيجابي"
        else:
            why = "لا توجد عوامل قوية"

        if negatives:
            why += f" | {', '.join(negatives[:2])} سلبي"

        # ===== التحذيرات الحرجة =====
        critical = [w for w in warnings if "R:R" in w or "مقاومة" in w or "Order Flow" in w]

        # ===== بناء النص =====
        lines = []
        lines.append(f"🧠 <b>{symbol}</b> — {decision_short}")
        lines.append("━━━━━━━━━━━━━━━━━━━")
        lines.append(f"💰 {_num(price)} | 📊 {score} | 🎯 ثقة {confidence}%")
        lines.append("")
        lines.append(f"💡 <b>لماذا؟</b>")
        lines.append(f"  {why}")

        if regime:
            regime_ar = {
                "trending": "📈 اتجاه قوي",
                "ranging": "↔️ سوق جانبي",
                "low_vol": "😴 تقلب منخفض",
                "high_vol": "🔥 تقلب عالٍ",
                "neutral": "⚖️ محايد",
            }.get(regime, "")
            if regime_ar:
                lines.append(f"  {regime_ar}")

        # التحذيرات (1-2 فقط)
        if critical:
            lines.append("")
            lines.append("⚠️ <b>تحذير:</b>")
            for w in critical[:2]:
                lines.append(f"  • {w}")

        # خطة (فقط BUY/SELL)
        if levels and "BUY" in state or levels and "SELL" in state:
            lines.append("")
            lines.append("━━━━━━━━━━━━━━━━━━━")
            lines.append(f"📍 {_num(levels.get('entry_low'), 4)} – {_num(levels.get('entry_high'), 4)}")
            lines.append(f"🛑 {_num(levels.get('stop_loss'), 4)}")
            lines.append(f"🎯 {_num(levels.get('tp1'), 4)} | {_num(levels.get('tp2'), 4)}")
            lines.append(f"⚖️ R:R 1:{levels.get('rr', '—')}")

        lines.append("")
        lines.append(f"🕐 {datetime.now(timezone.utc).strftime('%H:%M')}")

        return "\n".join(lines)

    except Exception as e:
        print(f"[essence {symbol}] {e}")
        return None


# ============================================================
# إرسال كل العملات
# ============================================================
async def send_all(bot, tag=""):
    sent = 0
    for s in SYMBOLS:
        try:
            text = build_essence(s)
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
        "🧠 <b>زبدة التحليل</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "ملخص ذكي لكل عملة + إشعارات فورية.\n\n"
        f"📌 <b>Chat ID:</b> <code>{cid}</code>\n\n"
        "<b>الأوامر:</b>\n"
        "/report — كل العملات الآن\n"
        "/symbol BTC/USDT — عملة محددة\n"
        "/alerts — حالة الإشعارات"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري التحليل...")
    n = await send_all(context.bot, tag="manual")
    await update.message.reply_text(f"✅ {n} تقرير")


async def cmd_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم: /symbol BTC/USDT")
        return
    sym = context.args[0].upper()
    text = build_essence(sym)
    if text:
        await update.message.reply_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ لا بيانات لـ {sym}")


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔔 <b>الإشعارات النشطة</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ <b>تغيرات مفاجئة</b>\n"
        "   فحص كل 5 دقائق\n"
        "   يُرسل عند:\n"
        "   • تغير الحالة (WATCH→BUY)\n"
        "   • تغير Score ≥ 10 نقاط\n"
        "   • Anomaly (high/medium)\n\n"
        "🕐 <b>تقرير كل ساعة</b>\n"
        "   8 عملات\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ============================================================
# Alert System — للكشف عن التغيرات
# ============================================================
_state_cache = {}  # {symbol: (state, score)}


async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    """فحص كل 5 دقائق للتغيرات المفاجئة"""
    try:
        for symbol in SYMBOLS:
            res = (
                supabase.table("snapshots").select("*")
                .eq("symbol", symbol)
                .order("timestamp", desc=True)
                .limit(2).execute()
            )
            if not res.data:
                continue

            curr = res.data[0]
            prev = res.data[1] if len(res.data) > 1 else None

            curr_state = curr.get("state")
            curr_score = curr.get("total_score", 0)
            curr_price = curr.get("price", 0)

            # الحالة السابقة
            last_state, last_score = _state_cache.get(symbol, (None, None))

            # كشف التغير
            reasons = []

            # 1. تغير الحالة (مهم!)
            if last_state and last_state != curr_state:
                reasons.append(f"🔄 تغير الحالة: {last_state} → {curr_state}")

            # 2. تغير score ≥ 10
            if last_score is not None and abs(curr_score - last_score) >= 10:
                delta = curr_score - last_score
                reasons.append(f"📊 تغير Score: {last_score} → {curr_score} ({delta:+d})")

            # 3. عند الانتقال إلى BUY/SELL SETUP (مهم جداً!)
            if last_state in (None, "NO TRADE", "WATCH", "WAIT FOR CONFIRMATION"):
                if curr_state in ("BUY SETUP", "STRONG BUY SETUP", "SELL SETUP", "STRONG SELL SETUP"):
                    reasons.append(f"🚀 إشارة جديدة: {curr_state}")

            # إرسال الإشعار
            if reasons:
                text = build_essence(symbol)
                if text:
                    header = f"⚡ <b>تغير مفاجئ — {symbol}</b>\n"
                    header += "━━━━━━━━━━━━━━━━━━━\n"
                    for r in reasons:
                        header += f"{r}\n"
                    header += "\n"
                    await context.bot.send_message(
                        chat_id=CHAT_ID,
                        text=header + text,
                        parse_mode="HTML",
                    )
                    print(f"⚡ [{symbol}] تغير مفاجئ")

            # حفظ الحالة
            _state_cache[symbol] = (curr_state, curr_score)

    except Exception as e:
        print(f"[alert_job] {e}")


# ============================================================
# Anomaly Alert — للكشف عن anomalies جديدة
# ============================================================
async def anomaly_job(context: ContextTypes.DEFAULT_TYPE):
    """فحص anomalies كل 5 دقائق"""
    try:
        since = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
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

            text = build_essence(sym)
            if text and CHAT_ID:
                anomaly_type = a.get("type", "")
                severity_icon = "🚨" if a.get("severity") == "high" else "⚡"
                
                # ترجمة نوع anomaly
                type_ar = {
                    "volume_spike": "قفزة في الحجم",
                    "orderflow_extreme": "انقلاب في دفتر الأوامر",
                    "taker_aggressive_buy": "شراء تنفيذي عنيف",
                    "taker_aggressive_sell": "بيع تنفيذي عنيف",
                    "sharp_move": "تحرك حاد",
                    "btc_shock": "صدمة BTC",
                }.get(anomaly_type, anomaly_type)

                header = f"{severity_icon} <b>حدث مفاجئ — {sym}</b>\n"
                header += f"📌 {type_ar}\n"
                header += "━━━━━━━━━━━━━━━━━━━\n\n"

                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text=header + text,
                    parse_mode="HTML",
                )
                print(f"{severity_icon} [{sym}] {anomaly_type}")
    except Exception as e:
        print(f"[anomaly_job] {e}")


# ============================================================
# Scheduled — تقرير كل ساعة
# ============================================================
async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"⏰ [HOURLY] {datetime.now(timezone.utc).strftime('%H:%M')}")
    await send_all(context.bot, tag="hourly")


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

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("symbol", cmd_symbol))
    app.add_handler(CommandHandler("alerts", cmd_alerts))

    # Jobs
    if app.job_queue:
        # تقرير كل ساعة
        app.job_queue.run_repeating(
            hourly_job, interval=HOURLY_MIN * 60, first=10, name="hourly"
        )
        # فحص التغيرات كل 5 دقائق
        app.job_queue.run_repeating(
            alert_job, interval=ALERT_MIN * 60, first=30, name="alerts"
        )
        # فحص anomalies كل 5 دقائق
        app.job_queue.run_repeating(
            anomaly_job, interval=ALERT_MIN * 60, first=45, name="anomalies"
        )
        print(f"⏰ كل {HOURLY_MIN} دقيقة: تقرير")
        print(f"⚡ كل {ALERT_MIN} دقائق: تغيرات + anomalies")

    print("✅ Bot جاهز")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
