"""
تحميل المصفوفة + تطبيع البنية + البحث.
يدعم كل البنيات المحتملة.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

MATRIX_PATH = Path(__file__).resolve().parent / "matrix_15min.json"
_matrix = None


# ============================================================
# التحميل والتطبيع
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

    _matrix = _normalize(raw)
    print(f"📊 matrix loaded: {[k for k in _matrix if not k.startswith('_')]}")
    return _matrix


def _normalize(raw: dict) -> dict:
    normalized = {}

    # الميتا
    for meta in ["generated_at", "days_back"]:
        if meta in raw:
            normalized[f"_meta_{meta}"] = raw[meta]

    # مصدر الرموز
    source = raw
    if "symbols" in raw and isinstance(raw["symbols"], dict):
        source = raw["symbols"]

    # combined
    if "combined" in raw and isinstance(raw["combined"], dict):
        normalized["__combined__"] = _normalize_symbol_data(raw["combined"])
    elif "__combined__" in source:
        normalized["__combined__"] = _normalize_symbol_data(source["__combined__"])

    # الرموز
    for sym, data in source.items():
        if sym.startswith("_"):
            continue
        normalized[sym.upper()] = _normalize_symbol_data(data)

    return normalized


def _normalize_symbol_data(data) -> dict:
    if isinstance(data, dict):
        if _looks_like_weekday_dict(data):
            return _normalize_weekday_dict(data)
        if "days" in data and isinstance(data["days"], dict):
            return _normalize_weekday_dict(data["days"])
        return data
    if isinstance(data, list):
        return _normalize_list_of_rows(data)
    return {}


def _looks_like_weekday_dict(data: dict) -> bool:
    keys = [k for k in data.keys() if not str(k).startswith("_")]
    if not keys:
        return False
    day_keys = [k for k in keys if str(k).isdigit() and 0 <= int(k) <= 6]
    return len(day_keys) >= 3


def _normalize_weekday_dict(data: dict) -> dict:
    result = {}
    for wd_key, slots_data in data.items():
        if str(wd_key).startswith("_") or not str(wd_key).isdigit():
            continue
        if isinstance(slots_data, dict):
            result[str(wd_key)] = _normalize_slots_dict(slots_data)
        elif isinstance(slots_data, list):
            result[str(wd_key)] = _normalize_slots_list(slots_data)
    return result


def _normalize_slots_dict(data: dict) -> dict:
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
        "ar": float(row.get("avg_return", row.get("ar", 0))),
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
        days_available = list(m[sym].keys())
        lines.append(f"📅 أيام {sym}: {days_available}")
        wd_key = str(weekday)
        if wd_key in m[sym]:
            slots = list(m[sym][wd_key].keys())
            lines.append(f"✅ يوم {wd_key} — {len(slots)} سلوت: {slots[:5]}...")
            if str(slot) in m[sym][wd_key]:
                d = m[sym][wd_key][str(slot)]
                lines.append(f"   البيانات: wr={d['wr']}%, n={d['n']}, t={d['t']}")
            else:
                lines.append(f"   ⚠️ السلوت {slot} غير موجود")
        else:
            lines.append(f"⚠️ يوم {weekday} غير موجود في {sym}")
    else:
        lines.append(f"⚠️ {sym} غير موجود")

    if "__combined__" in m:
        wd_key = str(weekday)
        if wd_key in m["__combined__"] and str(slot) in m["__combined__"][wd_key]:
            d = m["__combined__"][wd_key][str(slot)]
            lines.append("")
            lines.append(f"✅ __combined__: wr={d['wr']}%, n={d['n']}")

    return "\n".join(lines)
