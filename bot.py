"""
Smart Analyst — رأي المحلل + المصفوفة الإحصائية.
نسخة مخففة: نافذة أوسع + منع تكرار أقل صرامة + تشخيص مدمج
+ ميزة تقاطع EMA (مستقلة تماماً)
"""
import os
import threading
import logging
import ccxt                     # ═══ إضافة جديدة ═══
import pandas as pd             # ═══ إضافة جديدة ═══
import asyncio                  # ═══ إضافة جديدة ═══
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
# الإعدادات — معدّلة
# ============================================================
BOT_TOKEN = os.getenv("TELEGRAM_REPORT_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_REPORT_CHAT_ID")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

SYMBOLS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT",
           "XRP/USDT", "ADA/USDT", "AVAX/USDT", "DOGE/USDT"]

# ✅ الفواصل الزمنية (مخففة)
HOURLY_MIN = 60
ALERT_MIN = 5
ACTIVE_WINDOW_MIN = 180         # 3 ساعات
NOW_WINDOW_MIN = 180            # 3 ساعات
SYM_WINDOW_MIN = 360            # 6 ساعات
SIGNAL_COOLDOWN_MIN = 15        # 15 دقيقة
SCORE_CHANGE_THRESHOLD = 5      # تغير الدرجة

# ════════════════════════════════════════════════════════════
# ═══ إضافة جديدة: إعدادات ميزة تقاطع EMA ═══
# ════════════════════════════════════════════════════════════
CROSSOVER_TIMEFRAME = "15m"
CROSSOVER_EMA_FAST = 7
CROSSOVER_EMA_SLOW = 25
CROSSOVER_JOB_INTERVAL_MIN = 5

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def error_handler(update, context):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"⚠️ network: {err}")
        return
    logger.error(f"❌ error: {err}", exc_info=err)


# ============================================================
# Helpers
# ============================================================
def _short(symbol: str) -> str:
    return symbol.split("/")[0].upper()


def _confidence(snap: dict) -> int:
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
    s = _short(symbol)
    if "STRONG BUY" in state: return f"🟢🔥 اشتر بقوة {s}"
    if "EARLY BUY" in state: return f"🔵 فرصة شراء مبكرة — {s}"
    if "BUY" in state: return f"🟢 اشتر {s}"
    if "STRONG SELL" in state: return f"🔴🔥 بع بقوة {s}"
    if "EARLY SELL" in state: return f"🔵 فرصة بيع مبكرة — {s}"
    if "SELL" in state: return f"🔴 بع {s}"
    return None


def _is_buy_state(state: str) -> bool:
    return "BUY" in state


# ============================================================
# جلب snapshots
# ============================================================
def get_latest_snapshot(symbol: str) -> dict | None:
    try:
        res = (
            supabase.table("snapshots").select("*")
            .eq("symbol", symbol)
            .order("timestamp", desc=True)
            .limit(1).execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        logger.warning(f"get_latest({symbol}): {e}")
        return None


def get_active_signal(symbol: str, minutes: int = ACTIVE_WINDOW_MIN) -> dict | None:
    """يبحث عن أقوى إشارة نشطة في آخر N دقيقة"""
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
        logger.warning(f"get_active({symbol}): {e}")
        return None

    actives = [
        r for r in rows
        if r.get("state") and r.get("state") not in ("NO TRADE", "WATCH")
    ]
    if not actives:
        return None

    actives.sort(key=lambda r: abs(r.get("total_score", 0)), reverse=True)
    return actives[0]


def get_all_active_signals(symbol: str, minutes: int = ACTIVE_WINDOW_MIN) -> list:
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
    except Exception:
        return []
    return [
        r for r in rows
        if r.get("state") and r.get("state") not in ("NO TRADE", "WATCH")
    ]


# ============================================================
# منع التكرار (مخفف)
# ============================================================
_signal_cache: dict = {}

# ════════════════════════════════════════════════════════════
# ═══ إضافة جديدة: ذاكرة تقاطعات EMA + تهيئة المنصة ═══
# ════════════════════════════════════════════════════════════
_crossover_cache: dict = {}   # {symbol: {"direction": ..., "candle_ts": ...}}

try:
    _exchange = ccxt.binance({"enableRateLimit": True})
except Exception as _e:
    logger.warning(f"ccxt init failed: {_e}")
    _exchange = None


def should_send_signal(symbol: str, snap: dict,
                       cooldown_min: int = SIGNAL_COOLDOWN_MIN) -> bool:
    """
    يمنع التكرار فقط إذا:
    - نفس الحالة تماماً
    - خلال أقل من cooldown_min
    """
    state = snap.get("state", "")
    now = datetime.now(timezone.utc)

    last = _signal_cache.get(symbol)
    if last:
        last_state, last_score, last_time = last
        same_state = (last_state == state)

        if same_state:
            elapsed = (now - last_time).total_seconds() / 60
            if elapsed < cooldown_min:
                logger.info(f"⏭️ [{_short(symbol)}] {state} — مرسل قبل {elapsed:.0f}د")
                return False

    _signal_cache[symbol] = (state, abs(snap.get("total_score", 0)), now)
    return True


# ============================================================
# بناء الرسالة
# ============================================================
def build_short_signal(symbol: str, state: str, signal_conf: int,
                       direction: str = "LONG",
                       full_details: bool = False) -> str | None:
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

        direction_label = "احتمال صعود" if _is_buy_state(state) else "احتمال هبوط"
        lines.append(f"📊 توافق المصفوفة: <b>{result['composite_adjusted']}%</b> ({direction_label})")
        lines.append(f"⚡ <b>القرار النهائي: {result['final']}%</b>")

        forecast = result.get("forecast")
        if forecast and forecast.get("available"):
            h1 = forecast["horizons"]["1h"]
            lines.append(f"🔮 توقع ساعة: <b>{h1['expected']:+.3f}%</b> (نجاح {h1['window_wr']:.0f}%)")
            lines.append(
                f"🎯 هدف +0.5%: <b>{forecast['target_hit_0_5']:.0f}%</b> | "
                f"انعكاس: {forecast['reversal_prob']:.0f}%"
            )

        if result["composite_adjusted"] < 45:
            lines.append("🚨 <b>تحذير: المصفوفة لا تدعم الإشارة!</b>")

        day_ctx = result.get("day_ctx")
        if day_ctx and day_ctx.get("bias") != "neutral":
            lines.append(f"{day_ctx['emoji']} سياق اليوم ({day_ctx['day_name']}): <i>{day_ctx['bias']}</i>")

        if full_details:
            raw = result.get("raw", {})
            lines.append("")
            lines.append("<i>📈 تفصيل:</i>")
            lines.append(f"<i>• WR: {raw.get('wr', 0)}% | RET: {raw.get('ret', 0):+.4f}%</i>")
            lines.append(f"<i>• t: {raw.get('t', 0):+.3f} | n: {raw.get('n', 0)}</i>")
            lines.append(
                f"<i>• cum4: {raw.get('cum_ret_4', 0):+.4f}% | "
                f"MFE4: {raw.get('mfe_4', 0):+.4f}%</i>"
            )
    else:
        reason = result.get("reason", "لا توجد بيانات")
        lines.append(f"📊 المصفوفة: <i>{reason}</i>")
        lines.append(f"⚡ <b>القرار النهائي: {signal_conf}%</b>")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# ═══ إضافة جديدة: ميزة تقاطع EMA — دوال مستقلة ═══
# ════════════════════════════════════════════════════════════
async def detect_crossover(symbol: str, timeframe: str = CROSSOVER_TIMEFRAME) -> dict | None:
    """
    تكتشف تقاطع EMA السريع مع EMA البطيء في آخر شمعة مغلقة.
    ترجع dict عند وجود تقاطع جديد، أو None.
    """
    if _exchange is None:
        return None
    try:
        ohlcv = await asyncio.to_thread(
            _exchange.fetch_ohlcv, symbol, timeframe, limit=120
        )
        if not ohlcv or len(ohlcv) < CROSSOVER_EMA_SLOW + 5:
            return None

        df = pd.DataFrame(
            ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["ema_fast"] = df["close"].ewm(span=CROSSOVER_EMA_FAST, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=CROSSOVER_EMA_SLOW, adjust=False).mean()

        # نستخدم الشمعة المغلقة الأخيرة (-2) لتجنب إشارات وهمية
        curr = -2
        prev = -3

        cf, cs = df["ema_fast"].iloc[curr], df["ema_slow"].iloc[curr]
        pf, ps = df["ema_fast"].iloc[prev], df["ema_slow"].iloc[prev]

        bullish = (cf > cs) and (pf <= ps)
        bearish = (cf < cs) and (pf >= ps)
        if not (bullish or bearish):
            return None

        direction = "bullish" if bullish else "bearish"
        candle_ts = int(df["timestamp"].iloc[curr])

        last = _crossover_cache.get(symbol)
        if last and last.get("candle_ts") == candle_ts and last.get("direction") == direction:
            return None

        _crossover_cache[symbol] = {"direction": direction, "candle_ts": candle_ts}

        return {
            "symbol": symbol,
            "direction": direction,
            "ema_fast": round(float(cf), 4),
            "ema_slow": round(float(cs), 4),
            "price": round(float(df["close"].iloc[curr]), 4),
            "candle_ts": candle_ts,
            "timeframe": timeframe,
        }
    except Exception as e:
        logger.warning(f"detect_crossover({symbol}): {e}")
        return None


def build_crossover_message(cross: dict) -> str:
    """
    تبني رسالة التقاطع، مدموجة مع بيانات Supabase والمصفوفة.
    """
    symbol = cross["symbol"]
    short = _short(symbol)
    is_bull = (cross["direction"] == "bullish")
    dir_str = "LONG" if is_bull else "SHORT"

    # وقت الشمعة
    try:
        candle_time = datetime.fromtimestamp(
            cross["candle_ts"] / 1000, tz=timezone.utc
        ).strftime("%H:%M UTC")
    except Exception:
        candle_time = "?"

    # ====== العنوان ======
    if is_bull:
        lines = [f"🚀 <b>إشارة تقاطع EMA — {short}</b>", "━━━━━━━━━━━━━━━━━━━"]
        lines.append("📈 <b>التقاطع: صاعد 🟢</b>")
    else:
        lines = [f"🔻 <b>إشارة تقاطع EMA — {short}</b>", "━━━━━━━━━━━━━━━━━━━"]
        lines.append("📉 <b>التقاطع: هابط 🔴</b>")

    # ====== تفاصيل التقاطع ======
    lines.append(
        f"• EMA{CROSSOVER_EMA_FAST}: {cross['ema_fast']} | "
        f"EMA{CROSSOVER_EMA_SLOW}: {cross['ema_slow']}"
    )
    lines.append(f"• السعر عند الإغلاق: {cross['price']}")
    lines.append(f"• الفريم: {cross['timeframe']}")
    lines.append(f"• وقت الشمعة: {candle_time}")

    # ====== القسم 1: قاعدة البيانات ======
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("🗄️ <b>من قاعدة البيانات:</b>")

    snap = get_latest_snapshot(symbol)
    signal_conf = 0
    if snap:
        state = snap.get("state", "NO TRADE")
        score = snap.get("total_score", 0)
        signal_conf = _confidence(snap)
        lines.append(f"• الحالة: {state}")
        lines.append(f"• الدرجة: {score}")
        lines.append(f"• الثقة: <b>{signal_conf}%</b>")
    else:
        lines.append("• <i>لا يوجد snapshot حديث</i>")

    # ====== القسم 2: المصفوفة ======
    lines.append("")
    lines.append("📊 <b>من المصفوفة:</b>")

    result = final_confidence(signal_conf, symbol, dir_str)
    matrix_ok = False

    if result.get("available"):
        source = result.get("source", "?")
        if source == "__combined__":
            lines.append(f"⚠️ <i>لا توجد بيانات {short} — متوسط السوق</i>")

        direction_label = "احتمال صعود" if is_bull else "احتمال هبوط"
        composite = result["composite_adjusted"]
        lines.append(f"• توافق المصفوفة: <b>{composite}%</b> ({direction_label})")
        lines.append(f"• القرار النهائي: <b>{result['final']}%</b>")

        forecast = result.get("forecast")
        if forecast and forecast.get("available"):
            h1 = forecast["horizons"]["1h"]
            lines.append(
                f"• 🔮 توقع ساعة: <b>{h1['expected']:+.3f}%</b> "
                f"(نجاح {h1['window_wr']:.0f}%)"
            )
            lines.append(
                f"• 🎯 هدف +0.5%: <b>{forecast['target_hit_0_5']:.0f}%</b> | "
                f"انعكاس: {forecast['reversal_prob']:.0f}%"
            )

        day_ctx = result.get("day_ctx")
        if day_ctx and day_ctx.get("bias") != "neutral":
            lines.append(
                f"• {day_ctx['emoji']} سياق اليوم ({day_ctx['day_name']}): "
                f"<i>{day_ctx['bias']}</i>"
            )

        matrix_ok = composite >= 55 and result["final"] >= 60
    else:
        lines.append(f"• <i>{result.get('reason', 'لا بيانات')}</i>")

    # ====== الخلاصة ======
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("⚖️ <b>الخلاصة:</b>")

    if matrix_ok:
        lines.append("التقاطع الفني ✅ + دعم المصفوفة ✅ (متوافق)")
    elif result.get("available"):
        lines.append("التقاطع الفني ✅ + دعم المصفوفة ⚠️ (ضعيف)")
    else:
        lines.append("التقاطع الفني ✅ + المصفوفة ❓ (لا بيانات)")

    return "\n".join(lines)


# ============================================================
# Handlers
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    text = (
        "🧠 <b>رأي المحلل + المصفوفة</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔧 <b>الإعدادات الحالية:</b>\n"
        f"• نافذة الإشارات: {ACTIVE_WINDOW_MIN} دقيقة\n"
        f"• منع التكرار: {SIGNAL_COOLDOWN_MIN} دقيقة\n\n"
        f"📌 <b>Chat ID:</b> <code>{cid}</code>\n\n"
        "<b>الأوامر:</b>\n"
        "/now — فحص فوري\n"
        "/sym BTC/USDT — رمز محدد\n"
        "/matrix BTC — إحصاء المصفوفة\n"
        "/full BTC — تقرير غني\n"
        "/test — 🔍 تشخيص مفصل\n"
        "/raw — تشخيص المصفوفة\n"
        "/cross — 🔀 فحص تقاطعات EMA"     # ═══ إضافة جديدة ═══
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ جاري الفحص...")
    sent = 0
    for symbol in SYMBOLS:
        snap = get_active_signal(symbol, minutes=NOW_WINDOW_MIN)
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
            f"⚪ لا إشارات نشطة خلال آخر {NOW_WINDOW_MIN} دقيقة.\n"
            f"<i>جرّب /test للتشخيص</i>",
            parse_mode="HTML",
        )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تشخيص شامل"""
    now = datetime.now(timezone.utc)
    lines = [f"🔍 <b>تشخيص</b> — {now.strftime('%H:%M')} UTC\n"]

    total_snapshots = 0
    total_actives = 0

    for symbol in SYMBOLS:
        # آخر snapshot
        latest = get_latest_snapshot(symbol)
        # الإشارات النشطة في النافذة
        actives = get_all_active_signals(symbol, ACTIVE_WINDOW_MIN)

        total_snapshots += 1 if latest else 0
        total_actives += len(actives)

        if latest:
            state = latest.get("state", "?")
            ts = latest.get("timestamp", "")[:19]
            score = latest.get("total_score", 0)

            is_active = state not in ("NO TRADE", "WATCH")
            icon = "🟢" if is_active else "⚪"
            lines.append(
                f"{icon} <b>{_short(symbol)}</b>: {state} ({score}) "
                f"| نشطة={len(actives)} | آخر={ts[11:16]}"
            )
        else:
            lines.append(f"❌ <b>{_short(symbol)}</b>: لا بيانات")

    lines.append("")
    lines.append(f"📊 عملات ببيانات: {total_snapshots}/{len(SYMBOLS)}")
    lines.append(f"🎯 إجمالي الإشارات النشطة: {total_actives}")
    lines.append(f"🚫 في الكاش: {len(_signal_cache)}")

    if _signal_cache:
        lines.append("\n<b>الكاش:</b>")
        for sym, (state, score, t) in list(_signal_cache.items())[:8]:
            ago = (now - t).total_seconds() / 60
            lines.append(f"   {_short(sym)}: {state} — قبل {ago:.0f}د")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم: /sym BTC/USDT")
        return

    sym = context.args[0].upper()
    if "/" not in sym:
        sym = sym + "/USDT"

    snap = get_active_signal(sym, minutes=SYM_WINDOW_MIN) or get_latest_snapshot(sym)
    if not snap:
        await update.message.reply_text(f"❌ لا بيانات لـ {sym}")
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
    sym = "BTC/USDT"
    if context.args:
        s = context.args[0].upper()
        sym = s if "/" in s else s + "/USDT"

    now = datetime.now(timezone.utc)
    m = agreement_score(sym, "LONG", now)

    if not m["available"]:
        debug = debug_matrix(sym, now)
        safe = debug.replace("<", "&lt;").replace(">", "&gt;")
        await update.message.reply_text(
            f"📊 <b>{sym}</b>\n❌ {m.get('reason')}\n\n<code>{safe}</code>",
            parse_mode="HTML",
        )
        return

    comp_long = matrix_composite_score(sym, "LONG", now)
    comp_short = matrix_composite_score(sym, "SHORT", now)
    day_ctx = day_context(sym, dt=now)
    fc = final_confidence(0, sym, "LONG", dt=now)
    forecast = fc.get("forecast") if fc.get("available") else None

    text = (
        f"📊 <b>{sym}</b>\n━━━━━━━━━━━━━━━━━━━\n"
        f"• WR: <b>{m['win_rate']}%</b>\n"
        f"• RET: {m['avg_return']:+.4f}%\n"
        f"• n: {m['n']} | t: {m['t_stat']:+.3f}\n"
        f"• المصدر: {m['source']}\n"
    )

    if comp_long and comp_short:
        text += f"\n<b>الدرجة المركبة:</b>\n• 🟢 {comp_long['composite']}% | 🔴 {comp_short['composite']}%\n"

    if day_ctx:
        text += f"\n<b>اليوم:</b> {day_ctx['emoji']} {day_ctx['bias']}\n"

    if forecast and forecast.get("available"):
        h1 = forecast["horizons"]["1h"]
        text += (
            f"\n<b>توقع:</b>\n"
            f"• ساعة: {h1['expected']:+.3f}%\n"
            f"• هدف +0.5%: {forecast['target_hit_0_5']:.0f}%\n"
            f"• انعكاس: {forecast['reversal_prob']:.0f}%\n"
        )

    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_full(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sym = "BTC/USDT"
    if context.args:
        s = context.args[0].upper()
        sym = s if "/" in s else s + "/USDT"

    snap = get_active_signal(sym, minutes=SYM_WINDOW_MIN) or get_latest_snapshot(sym)
    if not snap:
        await update.message.reply_text(f"❌ لا بيانات لـ {sym}")
        return

    state = snap.get("state", "NO TRADE")
    conf = _confidence(snap)
    direction = "LONG" if _is_buy_state(state) else "SHORT"
    result = final_confidence(conf, sym, direction)
    msg = format_matrix_message(sym, result)
    safe = msg.replace("<", "&lt;").replace(">", "&gt;")
    await update.message.reply_text(f"<pre>{safe}</pre>", parse_mode="HTML")


async def cmd_raw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    debug = debug_matrix("BTC/USDT")
    safe = debug.replace("<", "&lt;").replace(">", "&gt;")
    await update.message.reply_text(f"<code>{safe}</code>", parse_mode="HTML")


# ════════════════════════════════════════════════════════════
# ═══ إضافة جديدة: أمر /cross للفحص اليدوي ═══
# ════════════════════════════════════════════════════════════
async def cmd_cross(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فحص تقاطعات EMA يدوياً (يتجاهل الذاكرة)"""
    await update.message.reply_text("🔍 جاري فحص التقاطعات...")
    found = 0
    for symbol in SYMBOLS:
        _crossover_cache.pop(symbol, None)  # تجاهل الذاكرة
        cross = await detect_crossover(symbol, CROSSOVER_TIMEFRAME)
        if cross:
            msg = build_crossover_message(cross)
            if msg:
                await update.message.reply_text(msg, parse_mode="HTML")
                found += 1
    if found == 0:
        await update.message.reply_text(
            f"⚪ لا توجد تقاطعات جديدة على فريم {CROSSOVER_TIMEFRAME}"
        )


# ============================================================
# Jobs
# ============================================================
async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"\n⏰ [HOURLY] {datetime.now(timezone.utc).strftime('%H:%M')}")
    sent = 0
    skipped = 0

    for symbol in SYMBOLS:
        try:
            snap = get_active_signal(symbol, minutes=ACTIVE_WINDOW_MIN)
            if not snap:
                continue

            if not should_send_signal(symbol, snap):
                skipped += 1
                continue

            state = snap.get("state", "NO TRADE")
            conf = _confidence(snap)
            direction = "LONG" if _is_buy_state(state) else "SHORT"
            msg = build_short_signal(symbol, state, conf, direction)

            if msg and CHAT_ID:
                await context.bot.send_message(
                    chat_id=CHAT_ID, text=msg, parse_mode="HTML",
                )
                sent += 1
                print(f"✅ [{symbol}] {state} → أُرسل")
        except Exception as e:
            print(f"❌ [{symbol}] {e}")

    print(f"📊 أُرسل={sent}, تخطي={skipped}")


_state_cache: dict = {}


async def alert_job(context: ContextTypes.DEFAULT_TYPE):
    """كل 5 دقائق — يفعل على تغير الحالة أو الدرجة"""
    try:
        for symbol in SYMBOLS:
            snap = get_active_signal(symbol, minutes=ACTIVE_WINDOW_MIN)
            if not snap:
                _state_cache.pop(symbol, None)
                continue

            state = snap.get("state")
            score = abs(snap.get("total_score", 0))
            last = _state_cache.get(symbol)

            is_new = (last is None)
            changed = (last is not None and (
                last[0] != state or
                abs(last[1] - score) >= SCORE_CHANGE_THRESHOLD
            ))

            if is_new or changed:
                if not should_send_signal(symbol, snap, cooldown_min=15):
                    _state_cache[symbol] = (state, score)
                    continue

                conf = _confidence(snap)
                direction = "LONG" if _is_buy_state(state) else "SHORT"
                msg = build_short_signal(symbol, state, conf, direction)

                if msg and CHAT_ID:
                    if is_new:
                        header = f"🆕 <b>إشارة جديدة — {_short(symbol)}</b>\n\n"
                    elif last[0] != state:
                        header = f"⚡ <b>تغير مفاجئ — {_short(symbol)}</b>\n<i>{last[0]} → {state}</i>\n\n"
                    else:
                        header = f"📊 <b>تحديث الدرجة — {_short(symbol)}</b>\n<i>{last[1]} → {score}</i>\n\n"

                    await context.bot.send_message(
                        chat_id=CHAT_ID, text=header + msg, parse_mode="HTML",
                    )
                    print(f"{'🆕' if is_new else '⚡'} [{symbol}] {state}")

            _state_cache[symbol] = (state, score)
    except Exception as e:
        print(f"[alert_job] {e}")


# ════════════════════════════════════════════════════════════
# ═══ إضافة جديدة: Job مستقل لفحص تقاطعات EMA ═══
# ════════════════════════════════════════════════════════════
async def crossover_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Job مستقل يفحص تقاطعات EMA كل 5 دقائق على جميع العملات.
    لا يتدخل في alert_job أو hourly_job.
    """
    try:
        found = 0
        for symbol in SYMBOLS:
            cross = await detect_crossover(symbol, CROSSOVER_TIMEFRAME)
            if not cross:
                continue

            msg = build_crossover_message(cross)
            if msg and CHAT_ID:
                try:
                    await context.bot.send_message(
                        chat_id=CHAT_ID, text=msg, parse_mode="HTML"
                    )
                    emoji = "🚀" if cross["direction"] == "bullish" else "🔻"
                    print(f"{emoji} [CROSSOVER] {symbol} → {cross['direction']}")
                    found += 1
                except Exception as e:
                    print(f"❌ إرسال تقاطع {symbol}: {e}")

        if found:
            print(f"📊 تقاطعات مرسلة: {found}")
    except Exception as e:
        print(f"[crossover_job] {e}")


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
    print(f"🔎 نافذة: {ACTIVE_WINDOW_MIN}د | cooldown: {SIGNAL_COOLDOWN_MIN}د")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("sym", cmd_symbol))
    app.add_handler(CommandHandler("matrix", cmd_matrix))
    app.add_handler(CommandHandler("full", cmd_full))
    app.add_handler(CommandHandler("raw", cmd_raw))
    app.add_handler(CommandHandler("test", cmd_test))   # ← جديد
    app.add_handler(CommandHandler("cross", cmd_cross)) # ═══ إضافة جديدة ═══
    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(hourly_job, interval=HOURLY_MIN * 60, first=10, name="hourly")
        app.job_queue.run_repeating(alert_job, interval=ALERT_MIN * 60, first=30, name="alerts")
        # ═══ إضافة جديدة: Job مستقل لتقاطعات EMA ═══
        app.job_queue.run_repeating(
            crossover_job,
            interval=CROSSOVER_JOB_INTERVAL_MIN * 60,
            first=60,
            name="crossover",
        )
        print(f"⏰ hourly كل {HOURLY_MIN}د | alerts كل {ALERT_MIN}د")
        print(f"🔀 crossover كل {CROSSOVER_JOB_INTERVAL_MIN}د | فريم {CROSSOVER_TIMEFRAME}") # ═══ إضافة جديدة ═══

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
