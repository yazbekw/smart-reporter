"""
تحميل المصفوفة + تطبيع البنية (مفاتيح أيام عربية → 0-6).
"""
import json
from pathlib import Path
from datetime import datetime, timezone

MATRIX_PATH = Path(__file__).resolve().parent / "matrix_15min.json"
_matrix = None

# خريطة الأيام العربية → رقم (Python weekday)
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
    """يوحّد البنية إلى: { 'BTC': { '0': {slot: {...}} }, '__combined__': {...} }"""
    normalized = {}

    if "generated_at" in raw:
        normalized["_meta_generated_at"] = raw["generated_at"]
    if "days_back" in raw:
        normalized["_meta_days_back"] = raw["days_back"]

    # ===== combined =====
    if "combined" in raw and isinstance(raw["combined"], dict):
        normalized["__combined__"] = _normalize_symbol(raw["combined"])

    # ===== symbols =====
    source = raw.get("symbols", {})
    if isinstance(source, dict):
        for sym, data in source.items():
            if sym.startswith("_"):
                continue
            normalized[sym.upper()] = _normalize_symbol(data)

    return normalized


def _normalize_symbol(data) -> dict:
    """
    يحوّل بنية الملف الفعلية:
      { "days": [ {"day": "الإثنين", "slots": [...]}, ... ] }
    أو البنية القديمة:
      { "الإثنين": {...}, ... }
    إلى:
      { "0": { "0": {...}, "1": {...} }, "1": {...} }
    """
    if not isinstance(data, dict):
        return {}

    result = {}

    # ===== البنية الفعلية: {"days": [{"day": ..., "slots": [...]}, ...]} =====
    if "days" in data and isinstance(data["days"], list):
        for day_obj in data["days"]:
            if not isinstance(day_obj, dict):
                continue
            day_name = day_obj.get("day")
            slots_list = day_obj.get("slots", [])

            day_num = _day_to_num(day_name)
            if day_num is None:
                continue

            if isinstance(slots_list, list):
                result[str(day_num)] = _normalize_slots_list(slots_list)
            elif isinstance(slots_list, dict):
                result[str(day_num)] = _normalize_slots(slots_list)
        return result

    # ===== البنية القديمة: {"الإثنين": {...}, ...} =====
    for day_key, slots_data in data.items():
        if str(day_key).startswith("_"):
            continue

        # تخطي المفاتيح غير المتعلقة بالأيام
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


def _day_to_num(day_key) -> int | None:
    """يحوّل مفتاح اليوم إلى 0-6"""
    if day_key is None:
        return None

    s = str(day_key).strip()

    # رقم مباشر
    if s.isdigit() and 0 <= int(s) <= 6:
        return int(s)

    # اسم عربي/إنجليزي (بحث دقيق)
    if s in ARABIC_DAYS:
        return ARABIC_DAYS[s]

    # بحث جزئي (احتياطي)
    for name, num in ARABIC_DAYS.items():
        if name in s or s in name:
            return num

    return None


def _normalize_slots(data: dict) -> dict:
    """يحوّل مفاتيح السلوت إلى أرقام نظيفة"""
    result = {}
    for k, v in data.items():
        k_str = str(k).replace("slot_", "").replace("slot", "").strip()
        if k_str.isdigit() and isinstance(v, dict):
            result[k_str] = _normalize_row(v)
    return result


def _normalize_slots_list(data: list) -> dict:
    """يحوّل قائمة السلوتات إلى قاموس بمفاتيح رقمية"""
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
    يوحّد حقول الصف. الملف الفعلي يستخدم:
      ret (وليس ar), wr (وليس win_rate), n, t
    """
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
def get_matrix_stats(symbol: str, dt: datetime | None = None) -> dict | None:
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
            data = dict(data)  # نسخة لتجنب التعديل على الأصل
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


def final_confidence(signal_conf: int, matrix_agreement: float | None,
                     weight_signal: float = 0.6) -> int:
    if matrix_agreement is None:
        return signal_conf
    weight_matrix = 1.0 - weight_signal
    return int(round((signal_conf * weight_signal) +
                     (matrix_agreement * weight_matrix)))


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
