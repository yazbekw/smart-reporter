"""
Smart Analyst — رأي المحلل + المصفوفة الإحصائية.
يدعم:
- EARLY BUY / EARLY SELL
- تفسير اتجاه المصفوفة (احتمال صعود/هبوط)
- إشعارات التغيرات المفاجئة
- الدرجة المركبة + سياق اليوم + أوزان ديناميكية
- توقع متعدد الآفاق (30د، ساعة، ساعتان)
- أهداف الربح + احتمالية الانعكاس
- نافذة زمنية للبحث عن الإشارات النشطة
- منع تكرار الإشارة (Cooldown)
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

from matrix import (
    agreement_score,
    final_confidence,
    matrix_composite_score,
    day_context,
    debug_matrix,
    format_matrix_message,
)

load_dotenv()

# ============================================================
# الإعدادات
# ============================================================
BOT_TOKEN = os.getenv("TELEGRAM_REPORT_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_REPORT_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
           "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOGE/USDT"]

# الفواصل الزمنية
HOURLY_MIN = 60                         # كل ساعة
ALERT_MIN = 5                           # كل 5 دقائق
ACTIVE_WINDOW_MIN = 30                  # نافذة البحث في hourly/alert
NOW_WINDOW_MIN = 60                     # نافذة البحث في /now
SYM_WINDOW_MIN = 120                    # نافذة البحث في /sym و /full
SIGNAL_COOLDOWN_MIN = 60                # منع تكرار نفس الإشارة خلال X دقيقة

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

    if "STRONG BUY" in state:
        return f"🟢🔥 اشتر بقوة {s}"
    if "EARLY BUY" in state:
        return f"🔵 فرصة شراء مبكرة — {s}"
    if "BUY" in state:
        return f"🟢 اشتر {s}"

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
# جلب snapshots
# ============================================================
def get_latest_snapshot(symbol: str) -> dict | None:
    """آخر snapshot فقط (بغض النظر عن الحالة)"""
    try:
        res = (
            supabase.table("snapshots").select("*")
            .eq("symbol", symbol)
            .order("timestamp", desc=True)
            .limit(1).execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        logger.warning(f"get_latest_snapshot({symbol}): {e}")
        return None


def get_active_signal(symbol: str, minutes: int = ACTIVE_WINDOW_MIN) -> dict | None:
    """
    يبحث عن آخر snapshot فيه إشارة نشطة (ليس NO TRADE/WATCH)
    خلال آخر N دقيقة.

    - يقرأ كل snapshots النافذة
    - يعيد الأقوى (بـ abs(total_score))
    - إذا لا يوجد → None
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    try:
        res = (
            supabase.table("snapshots").select("*")
            .eq("symbol", symbol)
            .gte("timestamp", cutoff)
            .order("timestamp", desc=True)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        logger.warning(f"get_active_signal({symbol}): {e}")
        return None

    # فلترة الإشارات النشطة
    actives = [
        r for r in rows
        if r.get("state") and r.get("state") not in ("NO TRADE", "WATCH")
    ]
    if not actives:
        return None

    # الأقوى بـ abs(total_score)
    actives.sort(key=lambda r: abs(r.get("total_score", 0)), reverse=True)
    return actives[0]


def get_all_active_signals(symbol: str, minutes: int = ACTIVE_WINDOW_MIN) -> list:
    """يجلب كل الإشارات النشطة في النافذة (بدون فلترة)"""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    try:
        res = (
            supabase.table("snapshots").select("*")
            .eq("symbol", symbol)
            .gte("timestamp", cutoff)
            .order("timestamp", desc=True)
            .execute()
        )
        rows = res.data or []
    except Exception as e:
        logger.warning(f"get_all_active_signals({symbol}): {e}")
        return []

    return [
        r for r in rows
        if r.get("state") and r.get("state") not in ("NO TRADE", "WATCH")
    ]


# ============================================================
# منع تكرار الإشارة (Cooldown)
# ============================================================
# symbol -> (state, total_score, timestamp)
_signal_cache: dict = {}


def should_send_signal(symbol: str, snap: dict,
                       cooldown_min: int = SIGNAL_COOLDOWN_MIN) -> bool:
    """
    يفحص إذا كان يجب إرسال الإشارة (لم تُرسل مؤخراً بنفس الحالة).
    يحدّث الـ cache إذا وافق.
    """
    state = snap.get("state", "")
    score = abs(snap.get("total_score", 0))
    now = datetime.now(timezone.utc)

    last = _signal_cache.get(symbol)
    if last:
        last_state, last_score, last_time = last
        # نفس الحالة تقريباً؟
        same_state = (last_state == state)
        # الفرق في الدرجة صغير؟
        close_score = abs(last_score - score) <= 3

        if same_state and close_score:
            elapsed = (now - last_time).total_seconds() / 60
            if elapsed < cooldown_min:
                logger.info(
                    f"⏭️ [{_short(symbol)}] تخطي (مُرسل قبل {elapsed:.0f} دقيقة)"
                )
                return False

    _signal_cache[symbol] = (state, score, now)
    return True


# ============================================================
# بناء الرسالة المختصرة
# ============================================================
def build_short_signal(symbol: str, state: str, signal_conf: int,
                       direction: str = "LONG",
                       full_details: bool = False) -> str | None:
    """
    رسالة مختصرة تحتوي:
    - الإجراء
    - ثقة الإشارة
    - توافق المصفوفة (الدرجة المركبة)
    - القرار النهائي
    - توقع ساعة + هدف + انعكاس
    - سياق اليوم
    - تفاصيل (اختياري)
    """
    action = _action_text(state, symbol)
    if not action:
        return None

    result = final_confidence(signal_conf, symbol, direction)

    lines = [action]
    lines.append(f"🎯 ثقة الإشارة: <b>{signal_conf}%</b>")

    if result.get("available"):
        source = result.get("source", "?")
        if source == "__combined__":
            lines.append(f"⚠️ <i>لا توجد بيانات {_short(symbol)} — متوسط السوق</i>")

        is_buy = _is_buy_state(state)
        direction_label = "احتمال صعود" if is_buy else "احتمال هبوط"

        lines.append(
            f"📊 توافق المصفوفة: <b>{result['composite_adjusted']}%</b> ({direction_label})"
        )
        lines.append(f"⚡ <b>القرار النهائي: {result['final']}%</b>")

        forecast = result.get("forecast")
        if forecast and forecast.get("available"):
            h1 = forecast["horizons"]["1h"]
            lines.append(
                f"🔮 توقع ساعة: <b>{h1['expected']:+.3f}%</b> "
                f"(نجاح {h1['window_wr']:.0f}%)"
            )
            lines.append(
                f"🎯 هدف +0.5%: <b>{forecast['target_hit_0_5']:.0f}%</b> | "
                f"انعكاس: {forecast['reversal_prob']:.0f}%"
            )

        if result["composite_adjusted"] < 45:
            lines.append("🚨 <b>تحذير: المصفوفة لا تدعم الإشارة!</b>")

        day_ctx = result.get("day_ctx")
        if day_ctx and day_ctx.get("bias") != "neutral":
            lines.append(
                f"{day_ctx['emoji']} سياق اليوم ({day_ctx['day_name']}): "
                f"<i>{day_ctx['bias']}</i>"
            )

        if full_details:
            raw = result.get("raw", {})
            lines.append("")
            lines.append("<i>📈 تفصيل:</i>")
            lines.append(
                f"<i>• WR: {raw.get('wr', 0)}% | RET: {raw.get('ret', 0):+.4f}%</i>"
            )
            lines.append(
                f"<i>• t: {raw.get('t', 0):+.3f} | n: {raw.get('n', 0)}</i>"
            )
            lines.append(
                f"<i>• cum4: {raw.get('cum_ret_4', 0):+.4f}% | "
                f"MFE4: {raw.get('mfe_4', 0):+.4f}% | "
                f"MAE4: {raw.get('mae_4', 0):+.4f}%</i>"
            )
            lines.append(
                f"<i>• أوزان: إشارة {result['weight_signal']} | "
                f"مصفوفة {result['weight_matrix']}</i>"
            )
            hb = result.get("horizon_bonus", 0)
            if hb != 0:
                sign = "+" if hb > 0 else ""
                lines.append(f"<i>• مكافأة الأفق: {sign}{hb}</i>")
            if result.get("day_bonus", 0) != 0:
                sign = "+" if result["day_bonus"] > 0 else ""
                lines.append(f"<i>• مكافأة اليوم: {sign}{result['day_bonus']}</i>")
            if forecast and forecast.get("available"):
                h30 = forecast["horizons"]["30m"]
                h2 = forecast["horizons"]["2h"]
                lines.append(
                    f"<i>• 30د: {h30['expected']:+.3f}% | "
                    f"2س: {h2['expected']:+.3f}%</i>"
                )
                lines.append(
                    f"<i>• هدف +1%: {forecast['target_hit_1_0']:.0f}% | "
                    f"هدف +2%: {forecast['target_hit_2_0']:.0f}%</i>"
                )
    else:
        reason = result.get("reason", "لا توجد بيانات")
        lines.append(f"📊 المصفوفة: <i>{reason}</i>")
        lines.append(f"⚡ <b>القرار النهائي: {signal_conf}%</b>")

    return "\n".join(lines)


# ============================================================
# Handlers
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    text = (
        "🧠 <b>رأي المحلل + المصفوفة</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "الرسائل تحتوي:\n"
        "• الإجراء (اشتر/بع)\n"
        "• ثقة الإشارة\n"
        "• توافق المصفوفة (احتمال صعود/هبوط)\n"
        "• القرار النهائي\n"
        "• توقع ساعة + هدف + انعكاس\n"
        "• سياق اليوم\n\n"
        f"📌 <b>Chat ID:</b> <code>{cid}</code>\n\n"
        "<b>الأوامر:</b>\n"
        "/now — فحص فوري (كل العملات)\n"
        "/sym BTC/USDT — رمز محدد\n"
        "/matrix BTC — إحصاء المصفوفة\n"
        "/full BTC — تقرير غني كامل\n"
        "/raw — تشخيص المصفوفة"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فحص فوري — يستخدم نافذة 60 دقيقة"""
    await update.message.reply_text("⏳ جاري الفحص...")
    sent = 0
    checked = 0

    for symbol in SYMBOLS:
        # ✅ البحث عن إشارات نشطة في آخر 60 دقيقة
        snap = get_active_signal(symbol, minutes=NOW_WINDOW_MIN)
        checked += 1

        if not snap:
            continue

        state = snap.get("state", "NO TRADE")
        conf = _confidence(snap)
        direction = "LONG" if _is_buy_state(state) else "SHORT"
        msg = build_short_signal(symbol, state, conf, direction, full_details=True)

        if msg:
            await update.message.reply_text(msg, parse_mode="HTML")
            sent += 1

    if sent == 0:
        await update.message.reply_text(
            f"⚪ لا توجد إشارات نشطة خلال آخر {NOW_WINDOW_MIN} دقيقة.\n"
            f"<i>تم فحص {checked} عملة.</i>",
            parse_mode="HTML",
        )


async def cmd_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم: /sym BTC/USDT")
        return

    sym = context.args[0].upper()
    if "/" not in sym:
        sym = sym + "/USDT"

    # نحاول أولاً الإشارة النشطة
    snap = get_active_signal(sym, minutes=SYM_WINDOW_MIN)

    # fallback: آخر snapshot
    if not snap:
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
    """يعرض إحصائيات المصفوفة + الدرجة المركبة + سياق اليوم"""
    sym = "BTC/USDT"
    if context.args:
        s = context.args[0].upper()
        sym = s if "/" in s else s + "/USDT"

    now = datetime.now(timezone.utc)

    m = agreement_score(sym, "LONG", now)

    if not m["available"]:
        debug = debug_matrix(sym, now)
        safe = debug.replace("<", "&lt;").replace(">", "&gt;")
        text = (
            f"📊 <b>{sym}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"❌ {m.get('reason', 'غير معروف')}\n\n"
            f"<b>تشخيص:</b>\n"
            f"<code>{safe}</code>"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        return

    comp_long = matrix_composite_score(sym, "LONG", now)
    comp_short = matrix_composite_score(sym, "SHORT", now)
    day_ctx = day_context(sym, dt=now)

    fc = final_confidence(0, sym, "LONG", dt=now)
    forecast = fc.get("forecast") if fc.get("available") else None

    text = (
        f"📊 <b>{sym}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"<b>البيانات الخام:</b>\n"
        f"• Win Rate: <b>{m['win_rate']}%</b>\n"
        f"• متوسط العائد: {m['avg_return']:+.4f}%\n"
        f"• العينة: {m['n']} صفقة\n"
        f"• t-stat: {m['t_stat']:+.3f}\n"
        f"• المصدر: {m['source']}\n"
    )

    if comp_long and comp_short:
        text += (
            f"\n<b>الدرجة المركبة:</b>\n"
            f"• 🟢 LONG: <b>{comp_long['composite']}%</b>\n"
            f"• 🔴 SHORT: <b>{comp_short['composite']}%</b>\n"
        )

    if day_ctx:
        text += (
            f"\n<b>سياق اليوم ({day_ctx['day_name']}):</b>\n"
            f"{day_ctx['emoji']} {day_ctx['bias']} "
            f"(WR={day_ctx['avg_wr']}%)\n"
        )

    if forecast and forecast.get("available"):
        h1 = forecast["horizons"]["1h"]
        h2 = forecast["horizons"]["2h"]
        text += (
            f"\n<b>توقع متعدد الآفاق:</b>\n"
            f"• ساعة: {h1['expected']:+.3f}% (نجاح {h1['window_wr']:.0f}%)\n"
            f"• ساعتان: {h2['expected']:+.3f}% (نجاح {h2['window_wr']:.0f}%)\n"
            f"• هدف +0.5%: {forecast['target_hit_0_5']:.0f}%\n"
            f"• هدف +1.0%: {forecast['target_hit_1_0']:.0f}%\n"
            f"• انعكاس: {forecast['reversal_prob']:.0f}%\n"
        )

    text += (
        f"\n<i>تفسير:</i>\n"
        f"• LONG → {m['win_rate']}% (احتمال صعود)\n"
        f"• SHORT → {round(100 - m['win_rate'], 1)}% (احتمال هبوط)"
    )

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تقرير غني كامل للمصفوفة"""
    sym = "BTC/USDT"
    if context.args:
        s = context.args[0].upper()
        sym = s if "/" in s else s + "/USDT"

    snap = get_active_signal(sym, minutes=SYM_WINDOW_MIN) or get_latest_snapshot(sym)
    if not snap:
        await update.message.reply_text(f"❌ لا توجد بيانات لـ {sym}")
        return

    state = snap.get("state", "NO TRADE")
    conf = _confidence(snap)
    direction = "LONG" if _is_buy_state(state) else "SHORT"

    result = final_confidence(conf, sym, direction)
    msg = format_matrix_message(sym, result)

    safe = msg.replace("<", "&lt;").replace(">", "&gt;")
    await update.message.reply_text(f"<pre>{safe}</pre>", parse_mode="HTML")


async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تشخيص المصفوفة"""
    debug = debug_matrix("BTC/USDT")
    safe = debug.replace("<", "&lt;").replace(">", "&gt;")
    await update.message.reply_text(f"<code>{safe}</code>", parse_mode="HTML")


async def cmd_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يعرض حالة آخر 30 دقيقة لكل عملة"""
    now = datetime.now(timezone.utc)
    lines = [f"🔍 <b>حالة آخر {ACTIVE_WINDOW_MIN} دقيقة</b>\n"]
    for symbol in SYMBOLS:
        actives = get_all_active_signals(symbol, ACTIVE_WINDOW_MIN)
        latest = get_latest_snapshot(symbol)

        latest_state = latest.get("state", "?") if latest else "—"
        latest_ts = latest.get("timestamp", "")[:19] if latest else "—"

        if actives:
            best = max(actives, key=lambda r: abs(r.get("total_score", 0)))
            lines.append(
                f"✅ <b>{_short(symbol)}</b>: {len(actives)} إشارة نشطة | "
                f"أقوى: {best.get('state')} ({best.get('total_score')})"
            )
        else:
            lines.append(
                f"⚪ <b>{_short(symbol)}</b>: لا نشطة | "
                f"آخر: {latest_state}"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ============================================================
# Scheduled Jobs
# ============================================================
async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    """كل ساعة — يبحث عن إشارات نشطة في آخر 30 دقيقة"""
    print(f"\n⏰ [HOURLY] {datetime.now(timezone.utc).strftime('%H:%M')}")
    sent = 0
    skipped = 0

    for symbol in SYMBOLS:
        try:
            # ✅ البحث عن إشارة نشطة (ليس NO TRADE)
            snap = get_active_signal(symbol, minutes=ACTIVE_WINDOW_MIN)
            if not snap:
                continue

            # ✅ منع التكرار
            if not should_send_signal(symbol, snap):
                skipped += 1
                continue

            state = snap.get("state", "NO TRADE")
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
                print(f"✅ [{symbol}] {state} (score={snap.get('total_score')}) → أُرسل")
        except Exception as e:
            print(f"❌ [{symbol}] {e}")

    if sent == 0:
        print(f"⚠️ لا إشارات جديدة (تخطي: {skipped})")


async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    """كل 5 دقائق — يبحث عن تغيير في الإشارات النشطة"""
    try:
        for symbol in SYMBOLS:
            snap = get_active_signal(symbol, minutes=ACTIVE_WINDOW_MIN)
            if not snap:
                # لا إشارة نشطة → نمسح الكاش
                _state_cache.pop(symbol, None)
                continue

            state = snap.get("state")
            score = abs(snap.get("total_score", 0))
            last = _state_cache.get(symbol)

            # إشارة جديدة أو تغيّرت بشكل كبير؟
            is_new = (last is None)
            changed = (last is not None and last[0] != state)

            if is_new or changed:
                # منع التكرار
                if not should_send_signal(symbol, snap, cooldown_min=30):
                    _state_cache[symbol] = (state, score)
                    continue

                conf = _confidence(snap)
                direction = "LONG" if _is_buy_state(state) else "SHORT"
                msg = build_short_signal(symbol, state, conf, direction)

                if msg and CHAT_ID:
                    if is_new:
                        header = f"🆕 <b>إشارة جديدة — {_short(symbol)}</b>\n\n"
                    else:
                        header = f"⚡ <b>تغير مفاجئ — {_short(symbol)}</b>\n"
                        header += f"<i>{last[0]} → {state}</i>\n\n"

                    await context.bot.send_message(
                        chat_id=CHAT_ID,
                        text=header + msg,
                        parse_mode="HTML",
                    )
                    print(f"{'🆕' if is_new else '⚡'} [{symbol}] {state}")

            _state_cache[symbol] = (state, score)
    except Exception as e:
        print(f"[alert_job] {e}")


# cache للإشارات السابقة في alert_job
_state_cache: dict = {}


# ============================================================
# Health Server (لـ UptimeRobot)
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
    print(f"🔎 نافذة البحث: {ACTIVE_WINDOW_MIN} دقيقة")
    print(f"🚫 منع التكرار: {SIGNAL_COOLDOWN_MIN} دقيقة")

    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("sym", cmd_symbol))
    app.add_handler(CommandHandler("matrix", cmd_matrix))
    app.add_handler(CommandHandler("full", cmd_full))
    app.add_handler(CommandHandler("raw", cmd_raw))
    app.add_handler(CommandHandler("cache", cmd_cache))
    app.add_error_handler(error_handler)

    # Jobs
    if app.job_queue:
        app.job_queue.run_repeating(
            hourly_job, interval=HOURLY_MIN * 60, first=10, name="hourly"
        )
        app.job_queue.run_repeating(
            alert_job, interval=ALERT_MIN * 60, first=30, name="alerts"
        )
        print(f"⏰ كل {HOURLY_MIN} دقيقة: فحص الإشارات")
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
