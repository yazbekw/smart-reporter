"""
backtest_matrix.py — يبني مصفوفة Train من الشموع في Supabase
"""
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

from backtest_config import CFG
from backtest_data import load_candles
import math


DAYS_AR = ['الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد']


# ============================================================
# إحصاءات
# ============================================================
def _mean(xs): return sum(xs) / len(xs) if xs else 0.0


def _median(xs):
    if not xs: return 0.0
    s = sorted(xs); n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _stdev(xs):
    if len(xs) < 2: return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _percentile(xs, p):
    if not xs: return 0.0
    s = sorted(xs); k = (len(s) - 1) * p / 100
    f = int(math.floor(k)); c = int(math.ceil(k))
    if f == c: return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _t_stat(xs):
    if len(xs) < 2: return 0.0
    sd = _stdev(xs)
    if sd == 0: return 0.0
    return _mean(xs) / (sd / math.sqrt(len(xs)))


def _win_rate(xs):
    return 100.0 * sum(1 for x in xs if x > 0) / len(xs) if xs else 0.0


def _var95(xs):
    if not xs: return 0.0
    return _percentile(xs, 5)


def _cvar95(xs):
    if not xs: return 0.0
    cutoff = _percentile(xs, 5)
    tail = [x for x in xs if x <= cutoff]
    return _mean(tail) if tail else cutoff


# ============================================================
# بناء المصفوفة
# ============================================================
def build_matrix_for_symbol(candles, days_back):
    """
    يبني مصفوفة {day: {slot: stats}} من شموع عملة واحدة
    """
    cutoff_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp() * 1000
    )
    candles = [c for c in candles if c["open_time"] >= cutoff_ms]
    candles.sort(key=lambda x: x["open_time"])

    # مجمّعات: [day][slot] = []
    raw = defaultdict(lambda: defaultdict(lambda: {
        "return": [], "range": [], "volume": [], "taker": [],
        "next_up": [],
    }))

    # نوافذ تراكمية
    cum_windows = {2: [], 4: [], 8: []}
    mfe_windows = []
    target_hits = {0.5: [], 1.0: [], 2.0: []}
    reversal_list = []

    # فهارس سريعة
    n = len(candles)
    prev_slot_ret = {}

    for i, c in enumerate(candles):
        dt = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc)
        wd = dt.weekday()
        slot = dt.hour * 4 + dt.minute // 15
        o = c["open"]; cl = c["close"]
        if o <= 0:
            continue
        ret = (cl - o) / o * 100
        rng = (c["high"] - c["low"]) / o * 100
        raw[wd][slot]["return"].append(ret)
        raw[wd][slot]["range"].append(rng)
        raw[wd][slot]["volume"].append(c.get("quote_volume", 0))
        vol = c.get("volume", 0); tb = c.get("taker_buy_base", 0)
        if vol > 0 and tb > 0:
            raw[wd][slot]["taker"].append(tb / vol)

        # next_up_prob
        prev_key = ((slot - 1) % 96, wd if slot > 0 else (wd - 1) % 7)
        if prev_key in prev_slot_ret:
            raw[wd][slot]["next_up"].append(ret)
        prev_slot_ret[(slot, wd)] = ret

        # التراكمي
        for h in [2, 4, 8]:
            if i + h < n:
                future = candles[i + h]
                expected_slot = (slot + h) % 96
                fut_dt = datetime.fromtimestamp(future["open_time"] / 1000, tz=timezone.utc)
                if fut_dt.hour * 4 + fut_dt.minute // 15 == expected_slot:
                    fut_close = future["close"]
                    if fut_close > 0:
                        cum_ret = (fut_close - cl) / cl * 100
                        cum_windows[h].append((wd, slot, cum_ret))

        # MFE / MAE / target hits
        if i + 4 < n:
            max_high = c["high"]; min_low = c["low"]
            for j in range(1, 5):
                bar = candles[i + j]
                max_high = max(max_high, bar["high"])
                min_low = min(min_low, bar["low"])
            mfe_windows.append((wd, slot,
                (max_high - cl) / cl * 100,
                (min_low - cl) / cl * 100))
            for tgt in [0.5, 1.0, 2.0]:
                max_gain = (max_high - cl) / cl * 100
                target_hits[tgt].append((wd, slot, 1 if max_gain >= tgt else 0))

        # Reversal
        if i + 4 < n:
            rets = []
            for j in range(4):
                bar = candles[i + j]
                if bar["open"] > 0:
                    rets.append((bar["close"] - bar["open"]) / bar["open"])
            if len(rets) >= 2:
                changes = sum(1 for k in range(1, len(rets))
                              if (rets[k] > 0) != (rets[k - 1] > 0))
                reversal_list.append((wd, slot, 1 if changes >= 2 else 0))

    # تجميع التراكمي
    cum_map = {h: defaultdict(list) for h in [2, 4, 8]}
    cum_wr_map = {h: defaultdict(list) for h in [2, 4, 8]}
    for h, items in cum_windows.items():
        for wd, slot, val in items:
            cum_map[h][(wd, slot)].append(val)
            cum_wr_map[h][(wd, slot)].append(1 if val > 0 else 0)

    mfe_map = defaultdict(list)
    mae_map = defaultdict(list)
    for wd, slot, mfe, mae in mfe_windows:
        mfe_map[(wd, slot)].append(mfe)
        mae_map[(wd, slot)].append(mae)

    target_map = {t: defaultdict(list) for t in [0.5, 1.0, 2.0]}
    for tgt, items in target_hits.items():
        for wd, slot, hit in items:
            target_map[tgt][(wd, slot)].append(hit)

    reversal_map = defaultdict(list)
    for wd, slot, rev in reversal_list:
        reversal_map[(wd, slot)].append(rev)

    # بناء النتيجة
    result = {"days": []}
    for wd in range(7):
        slots_out = []
        for slot in range(96):
            d = raw[wd].get(slot, None)
            if not d or not d["return"]:
                stats = {
                    "slot": slot,
                    "utc": f"{slot//4:02d}:{(slot%4)*15:02d}",
                    "local": f"{(slot//4+3)%24:02d}:{(slot%4)*15:02d}",
                    "ret": 0.0, "wr": 0.0, "n": 0, "t": 0.0,
                }
                slots_out.append(stats)
                continue

            vals = d["return"]
            stats = {
                "slot": slot,
                "utc": f"{slot//4:02d}:{(slot%4)*15:02d}",
                "local": f"{(slot//4+3)%24:02d}:{(slot%4)*15:02d}",
                "ret": round(_mean(vals), 4),
                "wr": round(_win_rate(vals), 2),
                "n": len(vals),
                "t": round(_t_stat(vals), 3),
                # إضافات
                "return": round(_mean(vals), 4),
                "win_rate": round(_win_rate(vals), 2),
                "t_stat": round(_t_stat(vals), 3),
                "std": round(_stdev(vals), 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "p10": round(_percentile(vals, 10), 4),
                "p90": round(_percentile(vals, 90), 4),
                "var95": round(_var95(vals), 4),
                "cvar95": round(_cvar95(vals), 4),
                "avg_range": round(_mean(d["range"]), 4),
                "avg_volume": round(_mean(d["volume"]), 0),
                "avg_taker": round(_mean(d["taker"]), 4) if d["taker"] else 0,
                "next_up_prob": round(
                    100.0 * sum(1 for x in d["next_up"] if x > 0)
                    / len(d["next_up"]), 2
                ) if d["next_up"] else 0,
                "cum_ret_2": round(_mean(cum_map[2].get((wd, slot), [])), 4),
                "cum_ret_4": round(_mean(cum_map[4].get((wd, slot), [])), 4),
                "cum_ret_8": round(_mean(cum_map[8].get((wd, slot), [])), 4),
                "cum_wr_2": round(100.0 * _mean(cum_wr_map[2].get((wd, slot), [])), 2),
                "cum_wr_4": round(100.0 * _mean(cum_wr_map[4].get((wd, slot), [])), 2),
                "cum_wr_8": round(100.0 * _mean(cum_wr_map[8].get((wd, slot), [])), 2),
                "mfe_4": round(_mean(mfe_map.get((wd, slot), [])), 4),
                "mae_4": round(_mean(mae_map.get((wd, slot), [])), 4),
                "target_hit_0_5": round(100.0 * _mean(target_map[0.5].get((wd, slot), [])), 2),
                "target_hit_1_0": round(100.0 * _mean(target_map[1.0].get((wd, slot), [])), 2),
                "target_hit_2_0": round(100.0 * _mean(target_map[2.0].get((wd, slot), [])), 2),
                "reversal_prob": round(100.0 * _mean(reversal_map.get((wd, slot), [])), 2),
            }
            slots_out.append(stats)
        result["days"].append({"day": DAYS_AR[wd], "slots": slots_out})

    return result


def build_train_matrix(output_path=None, days=None, progress_cb=None):
    """يبني مصفوفة Train لكل العملات ويحفظها"""
    if days is None:
        days = CFG["TRAIN_DAYS"]
    if output_path is None:
        output_path = CFG["MATRIX_TRAIN_PATH"]

    matrix = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days_back": days,
        "purpose": "backtest_train",
        "combined": {"days": []},
        "symbols": {},
    }

    # نجمع كل الشموع للحساب combined
    all_candles = {}
    for sym in CFG["SYMBOLS"]:
        if progress_cb:
            progress_cb(f"  📥 {sym}: قراءة الشموع...")
        all_candles[sym] = load_candles(sym, days)
        if progress_cb:
            progress_cb(f"  ✅ {sym}: {len(all_candles[sym])} شمعة")

    # مصفوفة لكل عملة
    for sym in CFG["SYMBOLS"]:
        short = sym.replace("USDT", "")
        if progress_cb:
            progress_cb(f"  🔨 بناء مصفوفة {short}...")
        matrix["symbols"][short] = build_matrix_for_symbol(all_candles[sym], days)

    # مصفوفة combined (دمج بسيط لكل الشموع)
    if progress_cb:
        progress_cb(f"  🔨 بناء مصفوفة combined...")
    combined_candles = []
    for sym in CFG["SYMBOLS"]:
        combined_candles.extend(all_candles[sym])
    combined_candles.sort(key=lambda x: x["open_time"])
    matrix["combined"] = build_matrix_for_symbol(combined_candles, days)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(matrix, f, ensure_ascii=False)

    if progress_cb:
        progress_cb(f"  ✅ حُفظ في {output_path}")
    return matrix
