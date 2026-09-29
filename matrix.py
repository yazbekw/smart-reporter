"""
تحميل المصفوفة والبحث فيها + حساب التوافق.
- يحاول الرمز المحدد أولاً
- ثم __combined__ كـ fallback
"""
import json
from pathlib import Path
from datetime import datetime, timezone

# ⚠️ عدّل المسار حسب مكان الملف
MATRIX_PATH = Path(__file__).resolve().parent / "matrix_15min.json"

# تحميل مرة واحدة
_matrix = None


def _load():
    global _matrix
    if _matrix is None:
        if not MATRIX_PATH.exists():
            print(f"⚠️ {MATRIX_PATH} غير موجود")
            _matrix = {}
        else:
            with open(MATRIX_PATH, "r", encoding="utf-8") as f:
                _matrix = json.load(f)
            print(f"📊 تم تحميل المصفوفة: {len(_matrix)} رموز")
    return _matrix


def _symbol_key(symbol: str) -> str:
    """BTC/USDT → BTC"""
    return symbol.split("/")[0].upper()


def get_matrix_stats(symbol: str, dt: datetime | None = None) -> dict | None:
    """
    يبحث عن الرمز + اليوم + السلوت.
    إذا لم يجد الرمز → يستخدم __combined__ كـ fallback.
    """
    m = _load()
    if not m:
        return None

    if dt is None:
        dt = datetime.now(timezone.utc)

    weekday = dt.weekday()                          # 0 = الإثنين
    slot = (dt.hour * 60 + dt.minute) // 15         # 0-95
    sym = _symbol_key(symbol)

    # جرب الرمز أولاً، ثم __combined__
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
    """
    يحسب نسبة التوافق مع المصفوفة.
    - LONG → win_rate كما هو
    - SHORT → 100 - win_rate
    - إذا n < min_samples → غير متوفر
    """
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
    """
    دمج ثقة الإشارة مع توافق المصفوفة.
    - weight_signal = 0.6 → 60% إشارة + 40% مصفوفة
    """
    if matrix_agreement is None:
        return signal_conf
    weight_matrix = 1.0 - weight_signal
    combined = (signal_conf * weight_signal) + (matrix_agreement * weight_matrix)
    return int(round(combined))


def debug_matrix(symbol: str, dt: datetime | None = None) -> str:
    """
    معلومات تشخيصية للسلوت الحالي — للاستخدام في /matrix
    """
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

    # الرموز المتاحة
    keys = list(m.keys())
    lines.append(f"📋 الرموز: {', '.join(keys)}")
    lines.append("")

    # افحص الرمز
    if sym in m:
        wd_key = str(weekday)
        if wd_key in m[sym]:
            slots = list(m[sym][wd_key].keys())
            lines.append(f"✅ {sym} متاح — {len(slots)} سلوت")
            if str(slot) in m[sym][wd_key]:
                d = m[sym][wd_key][str(slot)]
                lines.append(f"   البيانات: wr={d['wr']}%, n={d['n']}")
            else:
                lines.append(f"   ⚠️ السلوت {slot} غير موجود")
        else:
            lines.append(f"⚠️ يوم {weekday} غير موجود في {sym}")
    else:
        lines.append(f"⚠️ {sym} غير موجود")

    # افحص __combined__
    if "__combined__" in m:
        wd_key = str(weekday)
        if wd_key in m["__combined__"]:
            if str(slot) in m["__combined__"][wd_key]:
                d = m["__combined__"][wd_key][str(slot)]
                lines.append("")
                lines.append(f"✅ __combined__: wr={d['wr']}%, n={d['n']}")

    return "\n".join(lines)
