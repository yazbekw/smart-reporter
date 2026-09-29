"""
matrix.py
=========
محرك المصفوفة الزمنية (15min) لدعم قرارات التداول

المزايا:
- تحميل المصفوفة + تطبيع البنية (مفاتيح أيام عربية → 0-6)
- الحفاظ على كل الحقول: الأساسية + التراكمي + MFE/MAE + الأهداف + الانعكاس
- درجة مركبة غنية (wr + ret + t + n + cum_ret + mfe/mae + target)
- سياق اليوم (Day Strength Indicator)
- توقع متعدد الآفاق (30د، ساعة، ساعتان)
- وزن ديناميكي حسب قوة |t|
- تحذيرات ذكية + رسالة غنية للبوت
"""

# ⚠️ تنبيه: هذا ملف matrix.py — لا تضع أي "from matrix import" في هذا الملف

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

# الحقول التي نحتفظ بها من كل صف
# (تطابق ما يُنتجه app.py في MATRIX_KEYS)
CORE_KEYS = [
    "return", "median", "std", "min", "max",
    "p10", "p25", "p75", "p90",
    "win_rate", "n", "t_stat", "p_value",
    "sharpe", "sortino", "var95", "cvar95", "skew", "kurt",
    "avg_range", "avg_volume", "vol_ratio", "avg_taker",
    "up_streak", "down_streak", "next_up_prob",
    "cum_ret_2", "cum_ret_4", "cum_ret_8",
    "cum_wr_2", "cum_wr_4", "cum_wr_8",
    "mfe_4", "mae_4",
    "target_hit_0_5", "target_hit_1_0", "target_hit_2_0",
    "reversal_prob",
]

# أوزان الدرجة المركبة الأساسية
COMPOSITE_WEIGHTS = {
    "wr": 0.25,        # معدل الفوز
    "ret": 0.20,       # العائد المتوقع (15 دقيقة)
    "t": 0.20,         # الثقة الإحصائية
    "n": 0.10,         # كفاية العينة
    "cum": 0.15,       # العائد التراكمي (ساعة)
    "mfe": 0.10,       # أقصى ربح
}

# عتبات تحديد الحالة (state)
STATE_THRESHOLDS = {
    "STRONG": 85,
    "EARLY": 72,
    "MIN": 60,
}


# ============================================================
# 1) أدوات مساعدة
# ============================================================
def _short(symbol: str) -> str:
    return symbol.split("/")[0].upper().replace("USDT", "").replace("USD", "").strip()


def _symbol_key(symbol: str) -> str:
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
    if "metric_keys" in raw:
        normalized["_meta_metric_keys"] = raw["metric_keys"]

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

    # البنية الجديدة: {"days": [{"day": ..., "slots": [...]}, ...]}
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

    # البنية القديمة
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
    """
    يوحّد حقول الصف مع الحفاظ على كل الحقول الجديدة.
    الملف الجديد يحتوي:
      ret, wr, n, t  (أسماء قياسية)
      + return, win_rate, t_stat, cum_ret_4, mfe_4, ...
    """
    out = {}

    # الحقول القياسية الأربعة (مع fallback للأسماء القديمة)
    out["wr"] = float(row.get("win_rate", row.get("wr", 0)))
    out["ar"] = float(row.get("return", row.get("avg_return",
                        row.get("ret", row.get("ar", 0)))))
    out["n"] = int(row.get("n", 0))
    out["t"] = float(row.get("t_stat", row.get("t", 0)))

    # كل الحقول الإضافية
    for key in CORE_KEYS:
        if key in row:
            try:
                val = row[key]
                if isinstance(val, (int, float)):
                    out[key] = float(val)
            except (TypeError, ValueError):
                continue

    # ضمان وجود مفاتيح أساسية بقيم افتراضية
    out.setdefault("return", out["ar"])
    out.setdefault("win_rate", out["wr"])
    out.setdefault("t_stat", out["t"])
    out.setdefault("cum_ret_4", 0.0)
    out.setdefault("cum_wr_4", 0.0)
    out.setdefault("mfe_4", 0.0)
    out.setdefault("mae_4", 0.0)
    out.setdefault("target_hit_0_5", 0.0)
    out.setdefault("target_hit_1_0", 0.0)
    out.setdefault("reversal_prob", 0.0)

    return out


# ============================================================
# 3) جلب بيانات السلوت
# ============================================================
def get_matrix_stats(symbol: str, dt: Optional[datetime] = None) -> Optional[dict]:
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
# 4) الدرجة المركبة
# ============================================================
def _wr_score(wr: float, direction: str) -> float:
    return wr if direction.upper() == "LONG" else (100 - wr)


def _ret_score(ret: float, direction: str) -> float:
    if direction.upper() == "LONG":
        score = 50 + (ret * 50)
    else:
        score = 50 - (ret * 50)
    return max(0.0, min(100.0, score))


def _t_score(t: float, direction: str) -> float:
    t_abs = abs(t)
    score = min(100.0, (t_abs / 3.0) * 100)
    correct = (t > 0 and direction.upper() == "LONG") or \
              (t < 0 and direction.upper() == "SHORT")
    if not correct:
        score = 100 - score
    return score


def _n_score(n: int) -> float:
    if n >= 30:
        return 100.0
    if n < 10:
        return 0.0
    return ((n - 10) / 20.0) * 100


def _cum_score(cum_ret: float, direction: str) -> float:
    """درجة العائد التراكمي (ساعة)"""
    if direction.upper() == "LONG":
        score = 50 + (cum_ret * 25)   # ±2% → 100/0
    else:
        score = 50 - (cum_ret * 25)
    return max(0.0, min(100.0, score))


def _mfe_score(mfe: float, mae: float, direction: str) -> float:
    """درجة الأفضلية (MFE/MAE) — كلما زاد MFE وقلّ MAE كان أفضل"""
    if direction.upper() == "SHORT":
        # للبيع: نريد MAE سالب كبير (هبوط)
        mfe_use = abs(mae)
        mae_use = abs(mfe)
    else:
        mfe_use = mfe
        mae_use = abs(mae) if mae < 0 else 0

    if mfe_use + mae_use <= 0:
        return 50.0
    ratio = mfe_use / (mfe_use + mae_use)  # 0-1
    return ratio * 100


def matrix_composite_score(symbol: str, direction: str,
                           dt: Optional[datetime] = None) -> Optional[dict]:
    """
    درجة مركبة (0-100) تستخدم كل الحقول الأساسية + التراكمي + MFE
    """
    stats = get_matrix_stats(symbol, dt)
    if not stats:
        return None

    wr = stats["wr"]
    ret = stats["ar"]
    t = stats["t"]
    n = stats["n"]
    cum_ret = stats.get("cum_ret_4", 0.0)
    mfe = stats.get("mfe_4", 0.0)
    mae = stats.get("mae_4", 0.0)

    s_wr = _wr_score(wr, direction)
    s_ret = _ret_score(ret, direction)
    s_t = _t_score(t, direction)
    s_n = _n_score(n)
    s_cum = _cum_score(cum_ret, direction)
    s_mfe = _mfe_score(mfe, mae, direction)

    composite = (
        s_wr * COMPOSITE_WEIGHTS["wr"] +
        s_ret * COMPOSITE_WEIGHTS["ret"] +
        s_t * COMPOSITE_WEIGHTS["t"] +
        s_n * COMPOSITE_WEIGHTS["n"] +
        s_cum * COMPOSITE_WEIGHTS["cum"] +
        s_mfe * COMPOSITE_WEIGHTS["mfe"]
    )

    return {
        "composite": round(composite, 1),
        "wr_score": round(s_wr, 1),
        "ret_score": round(s_ret, 1),
        "t_score": round(s_t, 1),
        "n_score": round(s_n, 1),
        "cum_score": round(s_cum, 1),
        "mfe_score": round(s_mfe, 1),
        "raw": {
            "wr": wr, "ret": ret, "t": t, "n": n,
            "cum_ret_4": cum_ret, "mfe_4": mfe, "mae_4": mae,
        },
        "source": stats["source"],
        "weekday": stats["weekday"],
        "slot": stats["slot"],
    }


# ============================================================
# 5) توقع متعدد الآفاق (من الحقول التراكمية مباشرةً)
# ============================================================
def multi_slot_forecast(symbol: str, direction: str,
                        dt: Optional[datetime] = None) -> dict:
    """
    يستخدم الحقول الجاهزة cum_ret_2, cum_ret_4, cum_ret_8
    ليعطي توقعاً على 30 دقيقة / ساعة / ساعتين.
    """
    stats = get_matrix_stats(symbol, dt)
    if not stats:
        return {"available": False, "reason": "السلوت غير موجود"}

    def _orient(v):
        return -v if direction.upper() == "SHORT" else v

    horizons = {
        "30m": {
            "ret": stats.get("cum_ret_2", 0.0),
            "wr": stats.get("cum_wr_2", 0.0),
        },
        "1h": {
            "ret": stats.get("cum_ret_4", 0.0),
            "wr": stats.get("cum_wr_4", 0.0),
        },
        "2h": {
            "ret": stats.get("cum_ret_8", 0.0),
            "wr": stats.get("cum_wr_8", 0.0),
        },
    }

    for k in horizons:
        horizons[k]["expected"] = _orient(horizons[k]["ret"])
        # window_wr معكوس للبيع
        if direction.upper() == "SHORT":
            horizons[k]["window_wr"] = 100 - horizons[k]["wr"]
        else:
            horizons[k]["window_wr"] = horizons[k]["wr"]

    return {
        "available": True,
        "direction": direction.upper(),
        "horizons": horizons,
        "mfe_1h": stats.get("mfe_4", 0.0),
        "mae_1h": stats.get("mae_4", 0.0),
        "target_hit_0_5": stats.get("target_hit_0_5", 0.0),
        "target_hit_1_0": stats.get("target_hit_1_0", 0.0),
        "target_hit_2_0": stats.get("target_hit_2_0", 0.0),
        "reversal_prob": stats.get("reversal_prob", 0.0),
    }


# ============================================================
# 6) سياق اليوم
# ============================================================
def day_context(symbol: str, weekday: Optional[int] = None,
                dt: Optional[datetime] = None) -> Optional[dict]:
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
    avg_cum = sum(s.get("cum_ret_4", 0.0) for s in slots.values()) / count

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
        "avg_cum_ret_4": round(avg_cum, 4),
        "samples": count,
        "bias": bias,
        "emoji": emoji,
        "source": key,
    }


def _day_bonus(day_ctx: Optional[dict], direction: str) -> int:
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
# 7) الثقة النهائية
# ============================================================
def _dynamic_weight(t_abs: float) -> float:
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
    """اتفاق بسيط (كما كان)"""
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
                     use_day_context: bool = True,
                     use_forecast: bool = True) -> dict:
    """
    الثقة النهائية بنظام متكامل:
      1. الدرجة المركبة (wr + ret + t + n + cum + mfe)
      2. مكافأة/عقوبة من سياق اليوم
      3. وزن ديناميكي حسب |t|
      4. تعديل أخير من توقع المدى المتوسط (cum_ret)
    """
    stats = get_matrix_stats(symbol, dt)

    if not stats:
        return {
            "final": signal_conf, "available": False,
            "reason": "السلوت غير موجود في المصفوفة",
            "composite": None, "day_ctx": None,
            "weight_matrix": 0.0, "weight_signal": 1.0,
            "direction": direction.upper(), "signal_conf": signal_conf,
        }

    if stats["n"] < min_samples:
        return {
            "final": signal_conf, "available": False,
            "reason": f"عينة صغيرة (n={stats['n']})",
            "composite": None, "day_ctx": None,
            "weight_matrix": 0.0, "weight_signal": 1.0,
            "direction": direction.upper(), "signal_conf": signal_conf,
        }

    comp_result = matrix_composite_score(symbol, direction, dt)
    if not comp_result:
        return {
            "final": signal_conf, "available": False,
            "reason": "فشل حساب الدرجة المركبة",
            "composite": None, "day_ctx": None,
            "weight_matrix": 0.0, "weight_signal": 1.0,
            "direction": direction.upper(), "signal_conf": signal_conf,
        }

    composite = comp_result["composite"]

    # سياق اليوم
    day_ctx = None
    bonus = 0
    if use_day_context:
        day_ctx = day_context(symbol, dt=dt)
        bonus = _day_bonus(day_ctx, direction)

    adjusted_composite = max(0.0, min(100.0, composite + bonus))

    # الوزن الديناميكي
    t_abs = abs(comp_result["raw"]["t"])
    weight_matrix = _dynamic_weight(t_abs)
    weight_signal = 1.0 - weight_matrix

    # الدمج الأساسي
    final = (signal_conf * weight_signal) + (adjusted_composite * weight_matrix)

    # تعديل أخير من توقع المدى المتوسط
    forecast = None
    horizon_bonus = 0
    if use_forecast:
        forecast = multi_slot_forecast(symbol, direction, dt)
        if forecast.get("available"):
            h1 = forecast["horizons"]["1h"]
            if h1["expected"] > 0 and h1["window_wr"] >= 55:
                horizon_bonus = +3
            elif h1["expected"] < -0.05:
                horizon_bonus = -7
            # عقوبة إذا احتمالية الانعكاس عالية
            if forecast["reversal_prob"] >= 60:
                horizon_bonus -= 3
            final += horizon_bonus

    final = int(round(max(0.0, min(100.0, final))))

    return {
        "final": final,
        "available": True,
        "direction": direction.upper(),
        "signal_conf": signal_conf,
        "composite": composite,
        "composite_adjusted": round(adjusted_composite, 1),
        "day_bonus": bonus,
        "horizon_bonus": horizon_bonus,
        # تفاصيل
        "wr_score": comp_result["wr_score"],
        "ret_score": comp_result["ret_score"],
        "t_score": comp_result["t_score"],
        "n_score": comp_result["n_score"],
        "cum_score": comp_result["cum_score"],
        "mfe_score": comp_result["mfe_score"],
        "raw": comp_result["raw"],
        "weight_matrix": round(weight_matrix, 2),
        "weight_signal": round(weight_signal, 2),
        "day_ctx": day_ctx,
        "forecast": forecast,
        "source": comp_result["source"],
        "weekday": comp_result["weekday"],
        "slot": comp_result["slot"],
    }


# ============================================================
# 8) تحديد الحالة + نص العمل
# ============================================================
def _state_from_confidence(final: int, direction: str,
                           available: bool = True) -> str:
    if not available:
        return ""
    direction = direction.upper()
    strong = STATE_THRESHOLDS["STRONG"]
    early = STATE_THRESHOLDS["EARLY"]
    min_th = STATE_THRESHOLDS["MIN"]

    if final < min_th:
        return ""

    if direction == "LONG":
        if final >= strong: return "STRONG BUY"
        if final >= early:  return "EARLY BUY"
        return "BUY"

    if direction == "SHORT":
        if final >= strong: return "STRONG SELL"
        if final >= early:  return "EARLY SELL"
        return "SELL"
    return ""


def _action_text(state: str, symbol: str) -> str | None:
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
# 9) التحذيرات الذكية
# ============================================================
def build_warnings(result: dict) -> list[str]:
    warnings = []
    if not result.get("available"):
        return warnings

    raw = result.get("raw", {})
    t = raw.get("t", 0)
    n = raw.get("n", 0)
    wr = raw.get("wr", 0)
    ret = raw.get("ret", 0)
    cum = raw.get("cum_ret_4", 0)
    direction = result.get("direction", "LONG")

    if n < 15:
        warnings.append(f"⚠️ عينة صغيرة (n={n})")
    if abs(t) < 1.5:
        warnings.append(f"⚠️ ثقة إحصائية ضعيفة (t={t})")
    if abs(t) >= 3:
        sign = "موجب" if t > 0 else "سالب"
        warnings.append(f"✅ ثقة إحصائية قوية (t={t}, {sign})")

    if wr > 55 and ret < -0.05:
        warnings.append("⚠️ تعارض: WR مرتفع لكن العائد سلبي")
    if wr < 45 and ret > 0.05:
        warnings.append("⚠️ تعارض: WR منخفض لكن العائد موجب")

    if direction == "LONG" and ret < -0.05:
        warnings.append("⚠️ العائد التاريخي سالب — يخالف LONG")
    if direction == "SHORT" and ret > 0.05:
        warnings.append("⚠️ العائد التاريخي موجب — يخالف SHORT")

    # تحذيرات جديدة
    if direction == "LONG" and cum < -0.1:
        warnings.append(f"⚠️ العائد التراكمي على ساعة سالب ({cum:+.3f}%)")
    if direction == "SHORT" and cum > 0.1:
        warnings.append(f"⚠️ العائد التراكمي على ساعة موجب ({cum:+.3f}%) — يخالف SHORT")

    forecast = result.get("forecast")
    if forecast and forecast.get("available"):
        rev = forecast.get("reversal_prob", 0)
        if rev >= 60:
            warnings.append(f"⚠️ احتمالية انعكاس مرتفعة ({rev:.0f}%)")

    day_ctx = result.get("day_ctx")
    if day_ctx:
        if day_ctx["bias"] == "bullish" and direction == "SHORT":
            warnings.append("⚠️ سياق اليوم صاعد — يخالف SHORT")
        elif day_ctx["bias"] == "bearish" and direction == "LONG":
            warnings.append("⚠️ سياق اليوم هابط — يخالف LONG")

    return warnings


# ============================================================
# 10) رسالة غنية
# ============================================================
def format_matrix_message(symbol: str, result: dict,
                          dt: Optional[datetime] = None,
                          include_action: bool = True,
                          include_forecast: bool = True) -> str:
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
    hb = result.get("horizon_bonus", 0)
    hb_str = f" {'+' if hb >= 0 else ''}{hb}" if hb != 0 else ""
    lines.append(
        f"   = الإشارة {result['signal_conf']} × {result['weight_signal']}"
        f" + المصفوفة {result['composite_adjusted']} × {result['weight_matrix']}"
        f"{hb_str}"
    )
    lines.append("")

    # تفصيل الدرجة المركبة
    lines.append("📈 تفصيل الدرجة المركبة:")
    lines.append(f"   • معدل الفوز:      {result['wr_score']:>5.1f}/100")
    lines.append(f"   • العائد 15د:      {result['ret_score']:>5.1f}/100")
    lines.append(f"   • الثقة الإحصائية: {result['t_score']:>5.1f}/100")
    lines.append(f"   • كفاية العينة:    {result['n_score']:>5.1f}/100")
    lines.append(f"   • العائد التراكمي: {result['cum_score']:>5.1f}/100")
    lines.append(f"   • أفضليّة MFE/MAE: {result['mfe_score']:>5.1f}/100")
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
    lines.append(f"   • cum4 = {raw.get('cum_ret_4', 0):+.4f}%")
    lines.append(f"   • MFE4 = {raw.get('mfe_4', 0):+.4f}% | MAE4 = {raw.get('mae_4', 0):+.4f}%")
    lines.append(f"   • المصدر: {result['source']}")
    lines.append("")

    # توقع المدى المتوسط
    forecast = result.get("forecast")
    if include_forecast and forecast and forecast.get("available"):
        lines.append("🔮 توقع متعدد الآفاق:")
        for name, label in [("30m", "30 دقيقة"), ("1h", "ساعة"), ("2h", "ساعتان")]:
            h = forecast["horizons"][name]
            lines.append(
                f"   • {label}: عائد متوقع "
                f"{h['expected']:+.3f}% | نجاح {h['window_wr']:.0f}%"
            )
        lines.append(
            f"   • هدف +0.5%: {forecast['target_hit_0_5']:.0f}% | "
            f"هدف +1.0%: {forecast['target_hit_1_0']:.0f}%"
        )
        lines.append(f"   • احتمالية الانعكاس: {forecast['reversal_prob']:.0f}%")
        lines.append("")

    # سياق اليوم
    day_ctx = result.get("day_ctx")
    if day_ctx:
        lines.append(f"📅 سياق اليوم ({day_ctx['day_name']}):")
        lines.append(f"   {day_ctx['emoji']} المزاج: {day_ctx['bias']}")
        lines.append(f"   • متوسط WR:  {day_ctx['avg_wr']}%")
        lines.append(f"   • متوسط RET: {day_ctx['avg_ret']:+.4f}%")
        if day_ctx.get("avg_cum_ret_4") is not None:
            lines.append(f"   • متوسط cum4: {day_ctx['avg_cum_ret_4']:+.4f}%")
        lines.append("")

    warnings = build_warnings(result)
    if warnings:
        lines.append("⚠️ ملاحظات:")
        for w in warnings:
            lines.append(f"   {w}")

    return "\n".join(lines)


# ============================================================
# 11) أدوات التشخيص
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
    if "_meta_metric_keys" in m:
        lines.append(f"📊 metric_keys: {len(m['_meta_metric_keys'])} حقل")
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
                lines.append(
                    f"   cum4={d.get('cum_ret_4', 0):+.4f}%, "
                    f"MFE4={d.get('mfe_4', 0):+.4f}%, "
                    f"MAE4={d.get('mae_4', 0):+.4f}%"
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
# 12) مثال استخدام
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
