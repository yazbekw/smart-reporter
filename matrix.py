"""
تحميل المصفوفة + تطبيع البنية + حساب التوافق.
يدعم كل البنيات المحتملة.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

MATRIX_PATH = Path(__file__).resolve().parent / "matrix_15min.json"
_matrix = None

ARABIC_DAYS = {
    "الإثنين": 0, "الاثنين": 0, "Monday": 0,
    "الثلاثاء": 1, "Tuesday": 1,
    "الأربعاء": 2, "الاربعاء": 2, "Wednesday": 2,
    "الخميس": 3, "Thursday": 3,
    "الجمعة": 4, "Friday": 4,
    "السبت": 5, "Saturday": 5,
    "الأحد": 6, "الاحد": 6, "Sunday": 6,
}


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

    _matrix = _normalize(raw)
    print(f"📊 matrix loaded: {[k for k in _matrix if not k.startswith('_')]}")
    return _matrix


def _normalize(raw: dict) -> dict:
    normalized = {}

    for meta in ["generated_at", "days_back"]:
        if meta in raw:
            normalized[f"_meta_{meta}"] = raw[meta]

    source = raw
    if "symbols" in raw and isinstance(raw["symbols"], dict):
        source = raw["symbols"]

    if "combined" in raw and isinstance(raw["combined"], dict):
        normalized["__combined__"] = _normalize_symbol_data(raw["combined"])
    elif "__combined__" in source:
        normalized["__combined__"] = _normalize_symbol_data(source["__combined__"])

    for sym, data in source.items():
        if sym.startswith("_"):
            continue
        normalized[sym.upper()] = _normalize_symbol_data(data)

    return normalized


def _normalize_symbol_data(data) -> dict:
    if isinstance(data, dict):
        if "days" in data and isinstance(data["days"], dict):
            data = data["days"]
        return _normalize_weekday_dict(data)
    if isinstance(data, list):
        return _normalize_list_of_rows(data)
    return {}


def _normalize_weekday_dict(data: dict) -> dict:
    result = {}
    for day_key, slots_data in data.items():
        if str(day_key).startswith("_"):
            continue

        day_num = _day_to_num(day_key)
        if day_num is None:
            continue

        if isinstance(slots_data, dict):
            result[str(day_num)] = _normalize_slots(slots_data)
        elif isinstance(slots_data, list):
            result[str(day_num)] = _normalize_slots_list(slots_data)

    return result


def _day_to_num(day_key) -> int | None:
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


def _normalize_list_of_rows(data: list) -> dict:
    result = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        wd = row.get("weekday")
        slot = row.get("slot")
        if wd is None or slot is None:
            continue
        wd_str = str(int(wd))
        slot_str = str(int(slot))
        result.setdefault(wd_str, {})
        result[wd_str][slot_str] = _normalize_row(row)
    return result


def _normalize_row(row: dict) -> dict:
    return {
        "wr": float(row.get("win_rate", row.get("wr", 0))),
        "ar": float(row.get("avg_return", row.get("ret", row.get("ar", 0)))),
        "n": int(row.get("n", 0)),
        "t": float(row.get("t_stat", row.get("t", 0))),
    }


def _symbol_key(symbol: str) -> str:
    return symbol.split("/")[0].upper()


# ============================================================
# البحث
# ============================================================
def get_matrix_stats(symbol: str, dt: datetime | None = None,
                     allow_combined: bool = True) -> dict | None:
    m = _load()
    if not m:
        return None

    if dt is None:
        dt = datetime.now(timezone.utc)

    weekday = dt.weekday()
    slot = (dt.hour * 60 + dt.minute) // 15
    sym = _symbol_key(symbol)

    # جرب الرمز أولاً
    if sym in m:
        try:
            data = m[sym][str(weekday)][str(slot)]
            data["source"] = sym
            data["weekday"] = weekday
            data["slot"] = slot
            return data
        except (KeyError, TypeError):
            pass

    # fallback إلى __combined__
    if allow_combined and "__combined__" in m:
        try:
            data = m["__combined__"][str(weekday)][str(slot)]
            data["source"] = "__combined__"
            data["weekday"] = weekday
            data["slot"] = slot
            return data
        except (KeyError, TypeError):
            pass

    return None


def agreement_score(symbol: str, direction: str, dt: datetime | None = None,
                    min_samples: int = 10) -> dict:
    stats = get_matrix_stats(symbol, dt)

    if not stats:
        return {
            "available": False, "agreement": None,
            "win_rate": None, "avg_return": None,
            "n": 0, "t_stat": 0, "source": None,
            "reason": "السلوت غير موجود في المصفوفة",
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


def final_confidence(signal_conf: int, matrix_agreement: float | None,
                     direction: str = None, weight_signal: float = 0.6) -> int:
    """
    دمج ثقة الإشارة مع توافق المصفوفة.
    direction: معامل اختياري (متوافق مع الاستدعاءات القديمة).
    """
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
                lines.append(f"   wr={d['wr']}%, n={d['n']}, t={d['t']}")
            else:
                lines.append(f"   ⚠️ السلوت {slot} غير موجود")
        else:
            lines.append(f"⚠️ يوم {weekday} غير موجود")
    else:
        lines.append(f"⚠️ {sym} غير موجود")

    if "__combined__" in m:
        wd_key = str(weekday)
        if wd_key in m["__combined__"] and str(slot) in m["__combined__"][wd_key]:
            d = m["__combined__"][wd_key][str(slot)]
            lines.append("")
            lines.append(f"✅ __combined__: wr={d['wr']}%, n={d['n']}")

    return "\n".join(lines)
