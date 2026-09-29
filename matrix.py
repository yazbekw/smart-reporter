"""
تحميل المصفوفة والبحث فيها + حساب التوافق.
- يدعم بنيتين:
  1) { "BTC": {...}, "__combined__": {...} }
  2) { "symbols": {...}, "combined": {...}, "generated_at": ... }
"""
import json
from pathlib import Path
from datetime import datetime, timezone

MATRIX_PATH = Path(__file__).resolve().parent / "matrix_15min.json"

_matrix = None


# ============================================================
# تحميل المصفوفة + تطبيع البنية
# ============================================================
def _load():
    global _matrix
    if _matrix is not None:
        return _matrix

    if not MATRIX_PATH.exists():
        print(f"⚠️ {MATRIX_PATH} غير موجود")
        _matrix = {}
        return _matrix

    with open(MATRIX_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    normalized = {}

    # ============ الحالة 1: بنية مع "symbols" و "combined" ============
    if "symbols" in raw or "combined" in raw:
        if "symbols" in raw and isinstance(raw["symbols"], dict):
            for sym, data in raw["symbols"].items():
                normalized[sym.upper()] = data

        if "combined" in raw and isinstance(raw["combined"], dict):
            normalized["__combined__"] = raw["combined"]

        # احفظ معلومات إضافية
        if "generated_at" in raw:
            normalized["_meta_generated_at"] = raw["generated_at"]
        if "days_back" in raw:
            normalized["_meta_days_back"] = raw["days_back"]

        _matrix = normalized
        print(f"📊 محملة (normalized): {len([k for k in normalized if not k.startswith('_')])} رموز")

    # ============ الحالة 2: بنية مسطحة قديمة ============
    else:
        _matrix = raw
        print(f"📊 محملة (مسطحة): {list(raw.keys())}")

    return _matrix


def _symbol_key(symbol: str) -> str:
    return symbol.split("/")[0].upper()


# ============================================================
# جلب إحصائيات
# ============================================================
def get_matrix_stats(symbol: str, dt: datetime | None = None) -> dict | None:
    m = _load()
    if not m:
        return None

    if dt is None:
        dt = datetime.now(timezone.utc)

    weekday = dt.weekday()
    slot = (dt.hour * 60 + dt.minute) // 15
    sym = _symbol_key(symbol)

    # جرب الرمز ثم __combined__
    for key in [sym, "__combined__"]:
        if key not in m:
            continue
        try:
            data = m[key][str(weekday)][str(slot)]
            data["source"] = key
            data["weekday"] = weekday
            data["slot"] = slot
            return data
        except (KeyError, TypeError):
            continue

    return None


def agreement_score(symbol: str, direction: str, dt: datetime | None = None,
                    min_samples: int = 10) -> dict:
    stats = get_matrix_stats(symbol, dt)

    if not stats:
        return {
            "available": False,
            "agreement": None,
            "win_rate": None,
            "avg_return": None,
            "n": 0,
            "t_stat": 0,
            "source": None,
            "reason": "السلوت غير موجود في المصفوفة",
        }

    if stats["n"] < min_samples:
        return {
            "available": False,
            "agreement": None,
            "win_rate": stats["wr"],
            "avg_return": stats["ar"],
            "n": stats["n"],
            "t_stat": stats["t"],
            "source": stats["source"],
            "reason": f"عينة صغيرة (n={stats['n']} < {min_samples})",
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


def final_confidence(signal_conf: int, matrix_agreement: float | None,
                     weight_signal: float = 0.6) -> int:
    if matrix_agreement is None:
        return signal_conf
    weight_matrix = 1.0 - weight_signal
    combined = (signal_conf * weight_signal) + (matrix_agreement * weight_matrix)
    return int(round(combined))


# ============================================================
# تشخيص
# ============================================================
def debug_matrix(symbol: str, dt: datetime | None = None) -> str:
    m = _load()
    if not m:
        return "⚠️ المصفوفة غير محملة"

    if dt is None:
        dt = datetime.now(timezone.utc)

    weekday = dt.weekday()
    slot = (dt.hour * 60 + dt.minute) // 15
    sym = _symbol_key(symbol)

    lines = [
        f"🕐 {dt.strftime('%H:%M')} UTC",
        f"📅 weekday: {weekday}",
        f"🎯 slot: {slot}",
        "",
    ]

    keys = [k for k in m.keys() if not k.startswith("_")]
    lines.append(f"📋 الرموز: {', '.join(keys)}")
    lines.append("")

    # معلومات الميتا
    if "_meta_generated_at" in m:
        lines.append(f"🕒 generated: {m['_meta_generated_at']}")
    if "_meta_days_back" in m:
        lines.append(f"📅 days_back: {m['_meta_days_back']}")
    lines.append("")

    # فحص الرمز
    if sym in m:
        wd_key = str(weekday)
        if wd_key in m[sym]:
            slots = list(m[sym][wd_key].keys())
            lines.append(f"✅ {sym} — {len(slots)} سلوت")
            if str(slot) in m[sym][wd_key]:
                d = m[sym][wd_key][str(slot)]
                lines.append(f"   wr={d['wr']}%, n={d['n']}, t={d['t']}")
            else:
                lines.append(f"   ⚠️ السلوت {slot} غير موجود")
        else:
            lines.append(f"⚠️ يوم {weekday} غير موجود في {sym}")
    else:
        lines.append(f"⚠️ {sym} غير موجود")

    # __combined__
    if "__combined__" in m:
        wd_key = str(weekday)
        if wd_key in m["__combined__"]:
            if str(slot) in m["__combined__"][wd_key]:
                d = m["__combined__"][wd_key][str(slot)]
                lines.append("")
                lines.append(f"✅ __combined__: wr={d['wr']}%, n={d['n']}")

    return "\n".join(lines)
