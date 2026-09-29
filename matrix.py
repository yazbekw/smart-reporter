"""
matrix_engine.py
================
محرك المصفوفة الزمنية (15min) لدعم قرارات التداول

المزايا:
- تحميل المصفوفة + تطبيع البنية (مفاتيح أيام عربية → 0-6)
- درجة مركبة تستخدم كل الحقول (wr, ret, t, n)
- سياق اليوم (Day Strength Indicator)
- وزن ديناميكي حسب قوة |t|
- تحديد الحالة (state) ونص العمل (_action_text)
- تحذيرات ذكية + رسالة غنية للبوت
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# ============================================================
# الإعدادات
# ============================================================
MATRIX_PATH = Path(__file__).resolve().parent / "matrix_15min.json"
_matrix: Optional[dict] = None

ARABIC_DAYS = {
    "الإثنين": 0, "الاثنين": 0, "Monday": 0,
    "الثلاثاء": 1, "Tuesday": 1,
    "الأربعاء": 2, "الاربعاء": 2, "Wednesday": 2,
    "الخميس": 3, "Thursday": 3,
    "الجمعة": 4, "Friday": 4,
    "السبت": 5, "Saturday": 5,
    "الأحد": 6, "الاحد": 6, "Sunday": 6,
}

ARABIC_DAY_NAMES = {
    0: "الإثنين", 1: "الثلاثاء", 2: "الأربعاء",
    3: "الخميس", 4: "الجمعة", 5: "السبت", 6: "الأحد",
}

# أوزان الدرجة المركبة (قابلة للتعديل)
COMPOSITE_WEIGHTS = {
    "wr": 0.35,    # معدل الفوز
    "ret": 0.25,   # العائد المتوقع
    "t": 0.25,     # الثقة الإحصائية
    "n": 0.15,     # كفاية العينة
}

# عتبات تحديد الحالة (state) بناءً على الثقة النهائية
STATE_THRESHOLDS = {
    "STRONG": 85,   # ثقة عالية جداً
    "EARLY": 72,    # فرصة مبكرة
    "MIN": 60,      # الحد الأدنى للدخول
}


# ============================================================
# 1) أدوات مساعدة عامة
# ============================================================
def _short(symbol: str) -> str:
    """يختصر رمز العملة: 'BTCUSDT' → 'BTC'"""
    return symbol.split("/")[0].upper().replace("USDT", "").replace("USD", "").strip()


def _symbol_key(symbol: str) -> str:
    """مفتاح البحث في المصفوفة"""
    return symbol.split("/")[0].upper().replace("USDT", "").replace("USD", "").strip()


# ============================================================
# 2) التحميل والتطبيع
# ============================================================
def _load() -> dict:
    global _matrix
    if _matrix is not None:
        return _matrix

    if not MATRIX_PATH.exists():
        print(f"⚠️ {MATRIX_PATH} غير موجود")
        _matrix = {}
        return _matrix

    with open(MATRIX_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    _matrix = _normalize(raw)
    print(f"📊 matrix loaded: {[k for k in _matrix if not k.startswith('_')]}")
    return _matrix


def _normalize(raw: dict) -> dict:
    """يوحّد البنية إلى: { 'BTC': { '0': {slot: {...}} }, '__combined__': {...} }"""
    normalized = {}

    if "generated_at" in raw:
        normalized["_meta_generated_at"] = raw["generated_at"]
    if "days_back" in raw:
        normalized["_meta_days_back"] = raw["days_back"]

    if "combined" in raw and isinstance(raw["combined"], dict):
        normalized["__combined__"] = _normalize_symbol(raw["combined"])

    source = raw.get("symbols", {})
    if isinstance(source, dict):
        for sym, data in source.items():
            if sym.startswith("_"):
                continue
            normalized[sym.upper()] = _normalize_symbol(data)

    return normalized


def _normalize_symbol(data) -> dict:
    if not isinstance(data, dict):
        return {}

    result = {}

    # البنية الفعلية: {"days": [{"day": ..., "slots": [...]}, ...]}
    if "days" in data and isinstance(data["days"], list):
        for day_obj in data["days"]:
            if not isinstance(day_obj, dict):
                continue
            day_num = _day_to_num(day_obj.get("day"))
            if day_num is None:
                continue
            slots_list = day_obj.get("slots", [])
            if isinstance(slots_list, list):
                result[str(day_num)] = _normalize_slots_list(slots_list)
            elif isinstance(slots_list, dict):
                result[str(day_num)] = _normalize_slots(slots_list)
        return result

    # البنية القديمة: {"الإثنين": {...}, ...}
    for day_key, slots_data in data.items():
        if str(day_key).startswith("_"):
            continue
        if day_key in ("days", "symbols", "combined"):
            continue
        day_num = _day_to_num(day_key)
        if day_num is None:
            continue
        if isinstance(slots_data, dict):
            result[str(day_num)] = _normalize_slots(slots_data)
        elif isinstance(slots_data, list):
            result[str(day_num)] = _normalize_slots_list(slots_data)

    return result


def _day_to_num(day_key) -> Optional[int]:
    if day_key is None:
        return None
    s = str(day_key).strip()
    if s.isdigit() and 0 <= int(s) <= 6:
        return int(s)
    if s in ARABIC_DAYS:
        return ARABIC_DAYS[s]
    for name, num in ARABIC_DAYS.items():
        if name in s or s in name:
            return num
    return None


def _normalize_slots(data: dict) -> dict:
    result = {}
    for k, v in data.items():
        k_str = str(k).replace("slot_", "").replace("slot", "").strip()
        if k_str.isdigit() and isinstance(v, dict):
            result[k_str] = _normalize_row(v)
    return result


def _normalize_slots_list(data: list) -> dict:
    result = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        slot = row.get("slot")
        if slot is None:
            continue
        result[str(int(slot))] = _normalize_row(row)
    return result


def _normalize_row(row: dict) -> dict:
    """يوحّد حقول الصف: wr, ar (avg_return), n, t"""
    return {
        "wr": float(row.get("win_rate", row.get("wr", 0))),
        "ar": float(row.get("avg_return", row.get("ret", row.get("ar", 0)))),
        "n": int(row.get("n", 0)),
        "t": float(row.get("t_stat", row.get("t", 0))),
    }


# ============================================================
# 3) جلب بيانات السلوت
# ============================================================
def get_matrix_stats(symbol: str, dt: Optional[datetime] = None) -> Optional[dict]:
    """يجلب إحصائيات السلوت الحالي للرمز (مع fallback إلى __combined__)"""
    m = _load()
    if not m:
        return None

    if dt is None:
        dt = datetime.now(timezone.utc)

    weekday = dt.weekday()
    slot = (dt.hour * 60 + dt.minute) // 15
    sym = _symbol_key(symbol)

    for key in [sym, "__combined__"]:
        if key not in m:
            continue
        try:
            data = m[key][str(weekday)][str(slot)]
            data = dict(data)
            data["source"] = key
            data["weekday"] = weekday
            data["slot"] = slot
            return data
        except (KeyError, TypeError):
            continue
    return None


# ============================================================
# 4) الدرجة المركبة (Composite Score)
# ============================================================
def _wr_score(wr: float, direction: str) -> float:
    """درجة معدل الفوز (0-100) حسب الاتجاه"""
    return wr if direction.upper() == "LONG" else (100 - wr)


def _ret_score(ret: float, direction: str) -> float:
    """درجة العائد (0-100): +1% → 100، -1% → 0"""
    if direction.upper() == "LONG":
        score = 50 + (ret * 50)
    else:
        score = 50 - (ret * 50)
    return max(0.0, min(100.0, score))


def _t_score(t: float, direction: str) -> float:
    """درجة الثقة الإحصائية (0-100) مع مراعاة اتجاه t"""
    t_abs = abs(t)
    score = min(100.0, (t_abs / 3.0) * 100)

    correct = (t > 0 and direction.upper() == "LONG") or \
              (t < 0 and direction.upper() == "SHORT")
    if not correct:
        score = 100 - score
    return score


def _n_score(n: int) -> float:
    """درجة كفاية العينة (0-100): n>=30 → 100، n<10 → 0"""
    if n >= 30:
        return 100.0
    if n < 10:
        return 0.0
    return ((n - 10) / 20.0) * 100


def matrix_composite_score(symbol: str, direction: str,
                           dt: Optional[datetime] = None) -> Optional[dict]:
    """
    يحسب درجة مركبة (0-100) تستخدم كل حقول المصفوفة: wr + ret + t + n
    """
    stats = get_matrix_stats(symbol, dt)
    if not stats:
        return None

    wr = stats["wr"]
    ret = stats["ar"]
    t = stats["t"]
    n = stats["n"]

    s_wr = _wr_score(wr, direction)
    s_ret = _ret_score(ret, direction)
    s_t = _t_score(t, direction)
    s_n = _n_score(n)

    composite = (
        s_wr * COMPOSITE_WEIGHTS["wr"] +
        s_ret * COMPOSITE_WEIGHTS["ret"] +
        s_t * COMPOSITE_WEIGHTS["t"] +
        s_n * COMPOSITE_WEIGHTS["n"]
    )

    return {
        "composite": round(composite, 1),
        "wr_score": round(s_wr, 1),
        "ret_score": round(s_ret, 1),
        "t_score": round(s_t, 1),
        "n_score": round(s_n, 1),
        "raw": {"wr": wr, "ret": ret, "t": t, "n": n},
        "source": stats["source"],
        "weekday": stats["weekday"],
        "slot": stats["slot"],
    }


# ============================================================
# 5) سياق اليوم (Day Strength)
# ============================================================
def day_context(symbol: str, weekday: Optional[int] = None,
                dt: Optional[datetime] = None) -> Optional[dict]:
    """
    يحسب متوسط سلوك اليوم كاملاً (كل الـ 96 سلوت)
    ليعطينا صورة عن 'مزاج' اليوم
    """
    m = _load()
    if not m:
        return None

    if dt is None:
        dt = datetime.now(timezone.utc)
    if weekday is None:
        weekday = dt.weekday()

    sym = _symbol_key(symbol)
    key = sym if sym in m else "__combined__"

    if key not in m or str(weekday) not in m[key]:
        return None

    slots = m[key][str(weekday)]
    if not slots:
        return None

    count = len(slots)
    avg_wr = sum(s["wr"] for s in slots.values()) / count
    avg_ret = sum(s["ar"] for s in slots.values()) / count
    avg_t = sum(s["t"] for s in slots.values()) / count

    if avg_wr > 52 and avg_ret > 0.01:
        bias, emoji = "bullish", "🟢"
    elif avg_wr < 48 and avg_ret < -0.01:
        bias, emoji = "bearish", "🔴"
    else:
        bias, emoji = "neutral", "⚪"

    return {
        "weekday": weekday,
        "day_name": ARABIC_DAY_NAMES.get(weekday, str(weekday)),
        "avg_wr": round(avg_wr, 2),
        "avg_ret": round(avg_ret, 4),
        "avg_t": round(avg_t, 3),
        "samples": count,
        "bias": bias,
        "emoji": emoji,
        "source": key,
    }


def _day_bonus(day_ctx: Optional[dict], direction: str) -> int:
    """مكافأة/عقوبة للدرجة المركبة حسب توافق سياق اليوم مع الاتجاه"""
    if not day_ctx:
        return 0
    bias = day_ctx["bias"]
    if direction.upper() == "LONG":
        if bias == "bullish":
            return +5
        if bias == "bearish":
            return -10
    else:
        if bias == "bearish":
            return +5
        if bias == "bullish":
            return -10
    return 0


# ============================================================
# 6) الثقة النهائية
# ============================================================
def _dynamic_weight(t_abs: float) -> float:
    """وزن المصفوفة بناءً على قوة t-statistic"""
    if t_abs >= 3.0:
        return 0.50
    if t_abs >= 2.0:
        return 0.40
    if t_abs >= 1.5:
        return 0.30
    return 0.15


def agreement_score(symbol: str, direction: str,
                    dt: Optional[datetime] = None,
                    min_samples: int = 10) -> dict:
    """
    يحسب اتفاق المصفوفة (نسخة مبسطة متوافقة مع الإصدار السابق)
    """
    stats = get_matrix_stats(symbol, dt)
    if not stats:
        return {
            "available": False, "agreement": None,
            "win_rate": None, "avg_return": None,
            "n": 0, "t_stat": 0, "source": None,
            "reason": "السلوت غير موجود",
        }

    if stats["n"] < min_samples:
        return {
            "available": False, "agreement": None,
            "win_rate": stats["wr"], "avg_return": stats["ar"],
            "n": stats["n"], "t_stat": stats["t"],
            "source": stats["source"],
            "reason": f"عينة صغيرة (n={stats['n']})",
        }

    wr = stats["wr"]
    agreement = (100 - wr) if direction.upper() == "SHORT" else wr

    return {
        "available": True,
        "agreement": round(agreement, 1),
        "win_rate": wr,
        "avg_return": stats["ar"],
        "n": stats["n"],
        "t_stat": stats["t"],
        "source": stats["source"],
    }


def final_confidence(signal_conf: int, symbol: str, direction: str,
                     dt: Optional[datetime] = None,
                     min_samples: int = 10,
                     use_day_context: bool = True) -> dict:
    """
    يحسب الثقة النهائية بنظام متكامل:
      1. الدرجة المركبة (wr + ret + t + n)
      2. مكافأة/عقوبة من سياق اليوم
      3. وزن ديناميكي حسب |t|

    يعيد dict غنياً يحتوي كل التفاصيل لبناء رسالة البوت.
    """
    stats = get_matrix_stats(symbol, dt)

    # حالة عدم توفر بيانات
    if not stats:
        return {
            "final": signal_conf,
            "available": False,
            "reason": "السلوت غير موجود في المصفوفة",
            "composite": None,
            "day_ctx": None,
            "weight_matrix": 0.0,
            "weight_signal": 1.0,
            "direction": direction.upper(),
            "signal_conf": signal_conf,
        }

    if stats["n"] < min_samples:
        return {
            "final": signal_conf,
            "available": False,
            "reason": f"عينة صغيرة (n={stats['n']})",
            "composite": None,
            "day_ctx": None,
            "weight_matrix": 0.0,
            "weight_signal": 1.0,
            "direction": direction.upper(),
            "signal_conf": signal_conf,
        }

    # 1) الدرجة المركبة
    comp_result = matrix_composite_score(symbol, direction, dt)
    if not comp_result:
        return {
            "final": signal_conf,
            "available": False,
            "reason": "فشل حساب الدرجة المركبة",
            "composite": None,
            "day_ctx": None,
            "weight_matrix": 0.0,
            "weight_signal": 1.0,
            "direction": direction.upper(),
            "signal_conf": signal_conf,
        }

    composite = comp_result["composite"]

    # 2) سياق اليوم
    day_ctx = None
    bonus = 0
    if use_day_context:
        day_ctx = day_context(symbol, dt=dt)
        bonus = _day_bonus(day_ctx, direction)

    adjusted_composite = max(0.0, min(100.0, composite + bonus))

    # 3) الوزن الديناميكي
    t_abs = abs(comp_result["raw"]["t"])
    weight_matrix = _dynamic_weight(t_abs)
    weight_signal = 1.0 - weight_matrix

    # 4) الدمج النهائي
    final = (signal_conf * weight_signal) + (adjusted_composite * weight_matrix)
    final = int(round(final))

    return {
        "final": final,
        "available": True,
        "direction": direction.upper(),
        "signal_conf": signal_conf,
        # الدرجة المركبة
        "composite": composite,
        "composite_adjusted": round(adjusted_composite, 1),
        "day_bonus": bonus,
        "wr_score": comp_result["wr_score"],
        "ret_score": comp_result["ret_score"],
        "t_score": comp_result["t_score"],
        "n_score": comp_result["n_score"],
        # البيانات الخام
        "raw": comp_result["raw"],
        # الأوزان
        "weight_matrix": round(weight_matrix, 2),
        "weight_signal": round(weight_signal, 2),
        # السياق
        "day_ctx": day_ctx,
        "source": comp_result["source"],
        "weekday": comp_result["weekday"],
        "slot": comp_result["slot"],
    }


# ============================================================
# 7) تحديد الحالة + نص العمل
# ============================================================
def _state_from_confidence(final: int, direction: str,
                           available: bool = True) -> str:
    """
    يحوّل الثقة النهائية + الاتجاه إلى حالة (state) نصية
    تُستخدمها _action_text لتوليد نص العمل المناسب.

    الحالات الممكنة:
        - STRONG BUY   : LONG  + ثقة ≥ 85
        - EARLY BUY    : LONG  + ثقة 72-84  (فرصة مبكرة)
        - BUY          : LONG  + ثقة 60-71
        - STRONG SELL  : SHORT + ثقة ≥ 85
        - EARLY SELL   : SHORT + ثقة 72-84
        - SELL         : SHORT + ثقة 60-71
        - ""           : لا إجراء
    """
    if not available:
        return ""

    direction = direction.upper()
    strong = STATE_THRESHOLDS["STRONG"]
    early = STATE_THRESHOLDS["EARLY"]
    min_th = STATE_THRESHOLDS["MIN"]

    if final < min_th:
        return ""

    if direction == "LONG":
        if final >= strong:
            return "STRONG BUY"
        if final >= early:
            return "EARLY BUY"
        return "BUY"

    if direction == "SHORT":
        if final >= strong:
            return "STRONG SELL"
        if final >= early:
            return "EARLY SELL"
        return "SELL"

    return ""


def _action_text(state: str, symbol: str) -> str | None:
    """يبني نص العمل بناءً على الحالة والرمز"""
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


# ============================================================
# 8) التحذيرات الذكية
# ============================================================
def build_warnings(result: dict) -> list[str]:
    """يبني قائمة تحذيرات بناءً على نتيجة final_confidence"""
    warnings = []
    if not result.get("available"):
        return warnings

    raw = result.get("raw", {})
    t = raw.get("t", 0)
    n = raw.get("n", 0)
    wr = raw.get("wr", 0)
    ret = raw.get("ret", 0)
    direction = result.get("direction", "LONG")

    if n < 15:
        warnings.append(f"⚠️ عينة صغيرة (n={n}) — الإحصائية أقل موثوقية")

    if abs(t) < 1.5:
        warnings.append(f"⚠️ ثقة إحصائية ضعيفة (t={t}) — النمط قد يكون ضوضاء")

    if abs(t) >= 3:
        sign = "موجب" if t > 0 else "سالب"
        warnings.append(f"✅ ثقة إحصائية قوية (t={t}, {sign})")

    if wr > 55 and ret < -0.05:
        warnings.append("⚠️ تعارض: WR مرتفع لكن العائد سلبي (خسائر كبيرة محتملة)")
    if wr < 45 and ret > 0.05:
        warnings.append("⚠️ تعارض: WR منخفض لكن العائد موجب (مكاسب كبيرة محتملة)")

    if direction == "LONG" and ret < -0.05:
        warnings.append("⚠️ العائد التاريخي سالب — يخالف اتجاه LONG")
    if direction == "SHORT" and ret > 0.05:
        warnings.append("⚠️ العائد التاريخي موجب — يخالف اتجاه SHORT")

    day_ctx = result.get("day_ctx")
    if day_ctx:
        if day_ctx["bias"] == "bullish" and direction == "SHORT":
            warnings.append("⚠️ سياق اليوم صاعد — يخالف SHORT")
        elif day_ctx["bias"] == "bearish" and direction == "LONG":
            warnings.append("⚠️ سياق اليوم هابط — يخالف LONG")

    return warnings


# ============================================================
# 9) بناء رسالة غنية للبوت
# ============================================================
def format_matrix_message(symbol: str, result: dict,
                          dt: Optional[datetime] = None,
                          include_action: bool = True) -> str:
    """يبني رسالة نصية غنية تعرض كل تفاصيل المصفوفة + نص العمل"""
    if dt is None:
        dt = datetime.now(timezone.utc)

    lines = []

    if not result.get("available"):
        lines.append(f"📊 مصفوفة {_short(symbol)}")
        lines.append(f"⚠️ غير متاحة: {result.get('reason', 'سبب غير معروف')}")
        return "\n".join(lines)

    direction = result["direction"]
    dir_emoji = "🟢" if direction == "LONG" else "🔴"

    lines.append(f"📊 مصفوفة 15min — {_short(symbol)}")
    lines.append(f"{dir_emoji} الاتجاه: {direction}")
    lines.append(f"🕐 {dt.strftime('%Y-%m-%d %H:%M')} UTC")
    lines.append("━" * 30)

    # نص العمل
    if include_action:
        state = _state_from_confidence(
            result["final"], direction, result.get("available", True)
        )
        action = _action_text(state, symbol)
        if action:
            lines.append(f"🎬 الإجراء: {action}")
        else:
            lines.append("🎬 الإجراء: ⏸️ لا إجراء (الثقة منخفضة)")
        lines.append("")

    # الثقة النهائية
    lines.append(f"🎯 الثقة النهائية: {result['final']}%")
    lines.append(
        f"   = الإشارة {result['signal_conf']} × {result['weight_signal']}"
        f" + المصفوفة {result['composite_adjusted']} × {result['weight_matrix']}"
    )
    lines.append("")

    # تفصيل الدرجة المركبة
    lines.append("📈 تفصيل الدرجة المركبة:")
    lines.append(f"   • معدل الفوز:      {result['wr_score']:>5.1f}/100")
    lines.append(f"   • العائد المتوقع:  {result['ret_score']:>5.1f}/100")
    lines.append(f"   • الثقة الإحصائية: {result['t_score']:>5.1f}/100")
    lines.append(f"   • كفاية العينة:    {result['n_score']:>5.1f}/100")
    lines.append(f"   ─────────────────────")
    lines.append(f"   🎯 الدرجة المركبة: {result['composite']:>5.1f}/100")

    if result.get("day_bonus", 0) != 0:
        sign = "+" if result["day_bonus"] > 0 else ""
        lines.append(f"   📅 مكافأة اليوم:   {sign}{result['day_bonus']}")
        lines.append(f"   ✅ الدرجة المعدّلة: {result['composite_adjusted']:>5.1f}/100")

    lines.append("")

    # البيانات الخام
    raw = result["raw"]
    lines.append("🔢 البيانات الخام:")
    lines.append(f"   • wr  = {raw['wr']}%")
    lines.append(f"   • ret = {raw['ret']:+.4f}%")
    lines.append(f"   • t   = {raw['t']:+.3f}")
    lines.append(f"   • n   = {raw['n']}")
    lines.append(f"   • المصدر: {result['source']}")
    lines.append("")

    # سياق اليوم
    day_ctx = result.get("day_ctx")
    if day_ctx:
        lines.append(f"📅 سياق اليوم ({day_ctx['day_name']}):")
        lines.append(f"   {day_ctx['emoji']} المزاج: {day_ctx['bias']}")
        lines.append(f"   • متوسط WR:  {day_ctx['avg_wr']}%")
        lines.append(f"   • متوسط RET: {day_ctx['avg_ret']:+.4f}%")
        lines.append("")

    # التحذيرات
    warnings = build_warnings(result)
    if warnings:
        lines.append("⚠️ ملاحظات:")
        for w in warnings:
            lines.append(f"   {w}")

    return "\n".join(lines)


# ============================================================
# 10) أدوات التشخيص
# ============================================================
def list_available_symbols() -> dict:
    m = _load()
    return {
        "specific": [k for k in m.keys()
                     if not k.startswith('_') and k != '__combined__'],
        "has_combined": '__combined__' in m,
    }


def check_symbols(symbols: list[str]) -> dict:
    m = _load()
    return {
        sym.upper(): {
            "in_file": sym.upper() in m,
            "fallback": "__combined__" if sym.upper() not in m else None,
        }
        for sym in symbols
    }


def debug_matrix(symbol: str, dt: Optional[datetime] = None) -> str:
    m = _load()
    if not m:
        return "⚠️ المصفوفة غير محملة"

    if dt is None:
        dt = datetime.now(timezone.utc)

    weekday = dt.weekday()
    slot = (dt.hour * 60 + dt.minute) // 15
    sym = _symbol_key(symbol)

    lines = [
        f"🕐 {dt.strftime('%Y-%m-%d %H:%M')} UTC",
        f"📅 weekday: {weekday} ({ARABIC_DAY_NAMES.get(weekday, '?')})",
        f"🎯 slot: {slot}",
        "",
    ]

    keys = [k for k in m.keys() if not k.startswith("_")]
    lines.append(f"📋 الرموز المتاحة: {', '.join(keys)}")
    lines.append("")

    if "_meta_generated_at" in m:
        lines.append(f"🕒 generated: {m['_meta_generated_at']}")
    if "_meta_days_back" in m:
        lines.append(f"📅 days_back: {m['_meta_days_back']}")
    lines.append("")

    if sym in m:
        days_available = sorted(m[sym].keys(), key=lambda x: int(x))
        lines.append(f"📅 أيام {sym}: {days_available}")
        wd_key = str(weekday)
        if wd_key in m[sym]:
            slots = sorted(m[sym][wd_key].keys(), key=lambda x: int(x))
            lines.append(f"✅ يوم {wd_key} — {len(slots)} سلوت")
            if str(slot) in m[sym][wd_key]:
                d = m[sym][wd_key][str(slot)]
                lines.append(
                    f"   wr={d['wr']}%, ret={d['ar']:+.4f}%, "
                    f"t={d['t']:+.3f}, n={d['n']}"
                )
            else:
                lines.append(f"   ⚠️ السلوت {slot} غير موجود")
        else:
            lines.append(f"⚠️ يوم {weekday} غير موجود")
    else:
        lines.append(f"⚠️ {sym} غير موجود")

    ctx = day_context(symbol, dt=dt)
    if ctx:
        lines.append("")
        lines.append(
            f"📅 سياق اليوم ({ctx['day_name']}): "
            f"{ctx['emoji']} {ctx['bias']} "
            f"(WR={ctx['avg_wr']}%, RET={ctx['avg_ret']:+.4f}%)"
        )

    return "\n".join(lines)


# ============================================================
# 11) مثال استخدام
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("🔍 الرموز المتاحة")
    print("=" * 60)
    print(json.dumps(list_available_symbols(), indent=2, ensure_ascii=False))

    print("\n" + "=" * 60)
    print("📋 فحص الرموز")
    print("=" * 60)
    symbols_to_check = ["BTC", "ETH", "BNB", "DOGE", "XRP", "SOL", "ADA", "AVAX"]
    for sym, info in check_symbols(symbols_to_check).items():
        status = "✅" if info["in_file"] else f"⚠️ → {info['fallback']}"
        print(f"   {sym}: {status}")

    print("\n" + "=" * 60)
    print("🎬 اختبار دالة _action_text مباشرةً")
    print("=" * 60)
    test_states = [
        ("STRONG BUY", "BTCUSDT"),
        ("EARLY BUY", "ETHUSDT"),
        ("BUY", "BNBUSDT"),
        ("STRONG SELL", "ADAUSDT"),
        ("EARLY SELL", "XRPUSDT"),
        ("SELL", "SOLUSDT"),
        ("", "DOGEUSDT"),
    ]
    for state, sym in test_states:
        action = _action_text(state, sym)
        print(f"   state={state!r:<15} → {action}")

    print("\n" + "=" * 60)
    print("🎯 قرار كامل — BTC LONG")
    print("=" * 60)
    result = final_confidence(signal_conf=80, symbol="BTC", direction="LONG")
    print(format_matrix_message("BTC", result))

    print("\n" + "=" * 60)
    print("🎯 قرار كامل — ADA SHORT")
    print("=" * 60)
    result = final_confidence(signal_conf=75, symbol="ADA", direction="SHORT")
    print(format_matrix_message("ADA", result))

    print("\n" + "=" * 60)
    print("🔍 تشخيص")
    print("=" * 60)
    print(debug_matrix("AVAX"))
