"""
Backtest كامل لنظام Smart Analyst + Matrix
"""
import os
import json
import math
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

from backtest_config import CFG, print_config
from backtest_data import prepare_all_data, load_candles


# ============================================================
# 1) أدوات المصفوفة
# ============================================================
ARABIC_DAYS = {
    "الإثنين": 0, "الثلاثاء": 1, "الأربعاء": 2,
    "الخميس": 3, "الجمعة": 4, "السبت": 5, "الأحد": 6,
}


def load_matrix():
    path = Path(CFG["MATRIX_PATH"])
    if not path.exists():
        print(f"⚠️ {path} غير موجود")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    def _norm_days(days_list):
        out = {}
        for d in days_list:
            wd = ARABIC_DAYS.get(d.get("day"))
            if wd is None:
                continue
            slots = {}
            for s in d.get("slots", []):
                slots[str(s["slot"])] = s
            out[str(wd)] = slots
        return out

    matrix = {}
    if "combined" in raw:
        matrix["__combined__"] = _norm_days(raw["combined"].get("days", []))
    for sym, data in raw.get("symbols", {}).items():
        matrix[sym.upper()] = _norm_days(data.get("days", []))
    return matrix


def get_slot_stats(matrix, symbol, dt):
    """يجلب إحصاءات السلوت للعملة في وقت محدد"""
    sym_key = symbol.replace("USDT", "").upper()
    wd = dt.weekday()
    slot = (dt.hour * 60 + dt.minute) // 15

    for key in [sym_key, "__combined__"]:
        if key in matrix and str(wd) in matrix[key] and str(slot) in matrix[key][str(wd)]:
            return matrix[key][str(wd)][str(slot)], key
    return None, None


# ============================================================
# 2) المؤشرات الفنية البسيطة
# ============================================================
def compute_ma(closes, period):
    """متوسط متحرك بسيط"""
    out = [None] * len(closes)
    if period <= 0 or len(closes) < period:
        return out
    s = sum(closes[:period])
    out[period - 1] = s / period
    for i in range(period, len(closes)):
        s += closes[i] - closes[i - period]
        out[i] = s / period
    return out


def compute_rsi(closes, period=14):
    """RSI بسيط"""
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out

    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period

    for i in range(period, len(closes)):
        if i > period:
            diff = closes[i] - closes[i - 1]
            g = max(0, diff)
            l = max(0, -diff)
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - (100 / (1 + rs))
    return out


def generate_signal(closes, i, ma_fast, ma_slow, rsi_arr):
    """يولّد إشارة LONG / SHORT / None"""
    rule = CFG["SIGNAL_RULE"]

    if rule == "ma_cross":
        if i < 1 or ma_fast[i] is None or ma_slow[i] is None:
            return None
        if ma_fast[i - 1] is None or ma_slow[i - 1] is None:
            return None
        # Crossover
        prev_diff = ma_fast[i - 1] - ma_slow[i - 1]
        curr_diff = ma_fast[i] - ma_slow[i]
        if prev_diff < 0 and curr_diff > 0:
            return "LONG"
        if prev_diff > 0 and curr_diff < 0:
            return "SHORT"
        return None

    if rule == "rsi":
        if i < 1 or rsi_arr[i] is None or rsi_arr[i - 1] is None:
            return None
        if rsi_arr[i - 1] < CFG["RSI_BUY"] and rsi_arr[i] >= CFG["RSI_BUY"]:
            return "LONG"
        if rsi_arr[i - 1] > CFG["RSI_SELL"] and rsi_arr[i] <= CFG["RSI_SELL"]:
            return "SHORT"
        return None

    if rule == "both":
        if i < 1:
            return None
        ma_long = (ma_fast[i] is not None and ma_slow[i] is not None
                   and ma_fast[i] > ma_slow[i])
        ma_short = (ma_fast[i] is not None and ma_slow[i] is not None
                    and ma_fast[i] < ma_slow[i])
        rsi = rsi_arr[i] if i < len(rsi_arr) else None
        if ma_long and rsi is not None and rsi < 50:
            return "LONG"
        if ma_short and rsi is not None and rsi > 50:
            return "SHORT"
        return None

    return None


# ============================================================
# 3) التصنيف التلقائي
# ============================================================
def classify_symbol(matrix, symbol):
    """يصنّف العملة من المصفوفة (هادئة/متوسطة/متقلبة)"""
    sym_key = symbol.replace("USDT", "").upper()
    key = sym_key if sym_key in matrix else "__combined__"
    if key not in matrix:
        return "medium", 0.4

    rng_values = []
    for wd in matrix[key].values():
        for slot in wd.values():
            r = slot.get("avg_range", 0)
            if r > 0:
                rng_values.append(r)

    if not rng_values:
        return "medium", 0.4

    rng_values.sort()
    median = rng_values[len(rng_values) // 2]

    if median < CFG["CALM_MAX"]:
        return "calm", median
    if median > CFG["VOLATILE_MIN"]:
        return "volatile", median
    return "medium", median


def min_rr_for(classification):
    return {
        "calm": CFG["RR_CALM"],
        "medium": CFG["RR_MEDIUM"],
        "volatile": CFG["RR_VOLATILE"],
    }.get(classification, 1.3)


# ============================================================
# 4) محرك الإشارة الكامل
# ============================================================
def compute_scores(slot, direction):
    """يحسب scores من إحصاءات السلوت"""
    wr = slot.get("win_rate", slot.get("wr", 0))
    ret = slot.get("return", slot.get("ret", 0))
    t = slot.get("t_stat", slot.get("t", 0))
    n = slot.get("n", 0)
    cum4 = slot.get("cum_ret_4", 0)
    mfe = slot.get("mfe_4", 0)
    mae = slot.get("mae_4", 0)

    # WR
    wr_score = wr if direction == "LONG" else (100 - wr)

    # Ret
    ret_score = 50 + (ret * 50) if direction == "LONG" else 50 - (ret * 50)
    ret_score = max(0, min(100, ret_score))

    # t
    t_abs = abs(t)
    t_score = min(100, (t_abs / 3) * 100)
    correct = (t > 0 and direction == "LONG") or (t < 0 and direction == "SHORT")
    if not correct:
        t_score = 100 - t_score

    # n
    if n >= 30:
        n_score = 100.0
    elif n < 10:
        n_score = 0.0
    else:
        n_score = ((n - 10) / 20) * 100

    # cum4
    cum_score = 50 + (cum4 * 25) if direction == "LONG" else 50 - (cum4 * 25)
    cum_score = max(0, min(100, cum_score))

    # MFE/MAE
    if direction == "SHORT":
        mfe_use, mae_use = abs(mae), abs(mfe)
    else:
        mfe_use = mfe
        mae_use = abs(mae) if mae < 0 else 0
    if mfe_use + mae_use <= 0:
        mfe_score = 50.0
    else:
        mfe_score = (mfe_use / (mfe_use + mae_use)) * 100

    composite = (
        wr_score * 0.25 + ret_score * 0.20 + t_score * 0.20 +
        n_score * 0.10 + cum_score * 0.15 + mfe_score * 0.10
    )

    return {
        "composite": composite,
        "wr_score": wr_score, "ret_score": ret_score,
        "t_score": t_score, "n_score": n_score,
        "cum_score": cum_score, "mfe_score": mfe_score,
        "wr": wr, "ret": ret, "t": t, "n": n,
        "cum4": cum4, "mfe": mfe, "mae": mae,
        "reversal": slot.get("reversal_prob", 30),
        "target_hit_05": slot.get("target_hit_0_5", 50),
        "target_hit_10": slot.get("target_hit_1_0", 25),
        "cvar95": slot.get("cvar95", 0),
        "std": slot.get("std", 0),
    }


def dynamic_weight(t_abs):
    if t_abs >= 3.0: return 0.50
    if t_abs >= 2.0: return 0.40
    if t_abs >= 1.5: return 0.30
    return 0.15


def compute_sl_tp_size(scores, direction, balance, classification, signal_conf):
    """
    يحسب SL/TP/Size من scores المصفوفة
    """
    # SL
    mae_abs = abs(scores["mae"])
    cvar_abs = abs(scores["cvar95"])
    std_abs = abs(scores["std"])
    sl_base = max(
        mae_abs * CFG["SL_MAE_MULT"],
        cvar_abs * CFG["SL_CVAR_MULT"],
        std_abs * CFG["SL_STD_MULT"],
    )
    reversal = scores["reversal"]
    if reversal >= 60:
        sl_base *= 1.3
    elif reversal >= 45:
        sl_base *= 1.15
    if abs(scores["t"]) < 1.5:
        sl_base *= 1.2
    sl_pct = max(CFG["SL_MIN"], min(sl_base, CFG["SL_MAX"]))

    # TP
    cum4 = abs(scores["cum4"])
    mfe = abs(scores["mfe"])
    th05 = scores["target_hit_05"]
    th10 = scores["target_hit_10"]

    if th10 >= 50:
        tp_pct = max(cum4 * 0.8, 1.0)
    elif th05 >= 60:
        tp_pct = max(cum4 * CFG["TP_CUM_MULT"], 0.5)
    else:
        tp_pct = max(cum4 * 0.5, 0.3)

    tp_pct = min(tp_pct, mfe * CFG["TP_MFE_MULT"], CFG["TP_MAX"])
    tp_pct = max(tp_pct, CFG["TP_MIN"])

    # R:R
    rr = tp_pct / sl_pct if sl_pct > 0 else 0
    min_rr = min_rr_for(classification)

    if rr < min_rr:
        return None

    # Kelly
    win_p = scores["wr"] / 100
    if rr > 0:
        kelly = (win_p * rr - (1 - win_p)) / rr
    else:
        kelly = 0

    t_factor = min(1.0, abs(scores["t"]) / 3.0)
    rev_factor = max(0.3, 1 - (reversal / 100))
    sample_factor = min(1.0, scores["n"] / 30)
    conf_factor = signal_conf / 100

    kelly_adj = (
        kelly * t_factor * rev_factor * sample_factor
        * conf_factor * CFG["KELLY_FRACTION"]
    )
    pos_pct = max(CFG["MIN_POSITION_PCT"], min(kelly_adj * 100, CFG["MAX_POSITION_PCT"]))
    pos_pct = pos_pct / 100  # إلى كسر

    return {
        "sl_pct": sl_pct,
        "tp_pct": tp_pct,
        "rr": rr,
        "pos_pct": pos_pct,
        "kelly_raw": kelly,
    }


def compute_final_confidence(signal_conf, scores, day_bias):
    composite = scores["composite"]

    # مكافأة اليوم
    day_bonus = 0
    # (للبساطة نُهمل day_bias في backtest، يمكن إضافتها)

    adj_composite = max(0, min(100, composite + day_bonus))

    w_m = dynamic_weight(abs(scores["t"]))
    w_s = 1 - w_m

    final = signal_conf * w_s + adj_composite * w_m
    return int(round(final)), composite, adj_composite, w_m, w_s


# ============================================================
# 5) محاكاة صفقة
# ============================================================
def simulate_trade(symbol, direction, candles, entry_idx,
                   sl_pct, tp_pct, pos_pct, balance,
                   fee_pct, slip_pct):
    """
    يحاكي صفقة من entry_idx حتى الإغلاق
    """
    entry_candle = candles[entry_idx]
    raw_entry = entry_candle["close"]

    # الانزلاق
    if direction == "LONG":
        entry = raw_entry * (1 + slip_pct / 100)
        sl = entry * (1 - sl_pct / 100)
        tp = entry * (1 + tp_pct / 100)
    else:
        entry = raw_entry * (1 - slip_pct / 100)
        sl = entry * (1 + sl_pct / 100)
        tp = entry * (1 - tp_pct / 100)

    size_usd = balance * pos_pct
    size_units = size_usd / entry

    exit_price = None
    exit_reason = None
    exit_idx = entry_idx

    for j in range(1, CFG["MAX_HOLD"] + 1):
        if entry_idx + j >= len(candles):
            break
        c = candles[entry_idx + j]
        exit_idx = entry_idx + j

        if direction == "LONG":
            if c["low"] <= sl:
                exit_price = sl
                exit_reason = "SL"
                break
            if c["high"] >= tp:
                exit_price = tp
                exit_reason = "TP"
                break
        else:
            if c["high"] >= sl:
                exit_price = sl
                exit_reason = "SL"
                break
            if c["low"] <= tp:
                exit_price = tp
                exit_reason = "TP"
                break

    if exit_price is None:
        # انتهى الوقت
        exit_candle = candles[exit_idx]
        exit_price = exit_candle["close"]
        if direction == "LONG":
            exit_price *= (1 - slip_pct / 100)
        else:
            exit_price *= (1 + slip_pct / 100)
        exit_reason = "TIME"

    # P&L
    if direction == "LONG":
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100

    pnl_pct -= fee_pct * 2  # عمولة ذهاب وعودة
    pnl_usd = size_usd * pnl_pct / 100

    return {
        "entry": entry,
        "exit": exit_price,
        "sl": sl,
        "tp": tp,
        "pnl_pct": pnl_pct,
        "pnl_usd": pnl_usd,
        "exit_reason": exit_reason,
        "hold_candles": exit_idx - entry_idx,
        "size_usd": size_usd,
    }


# ============================================================
# 6) المحرك الرئيسي
# ============================================================
def run_backtest():
    print_config()
    print("\n📥 تحميل المصفوفة...")
    matrix = load_matrix()
    print(f"  ✅ رموز: {list(matrix.keys())}")

    print("\n📥 تحميل الشموع...")
    con = prepare_all_data()

    # التصنيف التلقائي
    print("\n🏷️  تصنيف العملات:")
    classifications = {}
    for sym in CFG["SYMBOLS"]:
        cls, med = classify_symbol(matrix, sym)
        classifications[sym] = cls
        print(f"  {sym:12s} → {cls:10s} (avg_range={med:.3f}%)")

    # التشغيل
    print("\n" + "=" * 60)
    print("🚀 بدء المحاكاة...")
    print("=" * 60)

    balance = CFG["START_BALANCE"]
    initial_balance = balance

    all_trades = []
    open_trades = []  # {'symbol', 'close_idx', ...}
    daily_pnl = defaultdict(float)
    daily_count = defaultdict(int)
    consecutive_losses = 0
    killed_days = set()
    kill_reason = {}
    per_symbol_open = defaultdict(int)

    # نجمع كل الشموع
    symbol_candles = {}
    for sym in CFG["SYMBOLS"]:
        symbol_candles[sym] = load_candles(con, sym, CFG["DAYS"])
        print(f"  {sym:12s}: {len(symbol_candles[sym])} شمعة")

    # نبني فهرس زمني موحّد
    all_times = set()
    for sym, cds in symbol_candles.items():
        for c in cds:
            all_times.add(c["open_time"])
    all_times = sorted(all_times)
    print(f"\n  ⏱️  إجمالي النقاط الزمنية: {len(all_times):,}")

    # نبني فهارس سريعة
    idx_map = {}
    for sym, cds in symbol_candles.items():
        idx_map[sym] = {c["open_time"]: i for i, c in enumerate(cds)}

    # المؤشرات
    indicators = {}
    for sym, cds in symbol_candles.items():
        closes = [c["close"] for c in cds]
        indicators[sym] = {
            "ma_fast": compute_ma(closes, CFG["MA_FAST"]),
            "ma_slow": compute_ma(closes, CFG["MA_SLOW"]),
            "rsi": compute_rsi(closes, CFG["RSI_PERIOD"]),
        }

    # الحلقة الرئيسية
    trades_opened = 0
    for t_idx, t in enumerate(all_times):
        if t_idx % 10000 == 0:
            print(f"  ⏳ {t_idx:,}/{len(all_times):,} | رصيد: ${balance:.2f} | صفقات: {len(all_trades)}")

        dt = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")

        # فحص الإيقاف اليومي
        if day_key in killed_days:
            continue
        if daily_pnl[day_key] <= -CFG["DAILY_LOSS_LIMIT"] * initial_balance / 100:
            killed_days.add(day_key)
            kill_reason[day_key] = "daily_loss"
            continue
        if consecutive_losses >= CFG["CONSECUTIVE_LOSSES"]:
            killed_days.add(day_key)
            kill_reason[day_key] = "consecutive"
            continue

        # إغلاق الصفقات المنتهية
        still_open = []
        for tr in open_trades:
            if tr["close_idx"] <= t_idx:
                # أُغلقت — نُحدّث الرصيد
                balance += tr["trade"]["pnl_usd"]
                daily_pnl[day_key] += tr["trade"]["pnl_usd"]
                all_trades.append(tr["trade"])

                if tr["trade"]["pnl_usd"] < -CFG["MIN_LOSS_THRESHOLD"]:
                    consecutive_losses += 1
                elif tr["trade"]["pnl_usd"] > 0:
                    consecutive_losses = 0

                per_symbol_open[tr["symbol"]] -= 1
            else:
                still_open.append(tr)
        open_trades = still_open

        # فتح صفقات جديدة
        if len(open_trades) >= CFG["MAX_OPEN"]:
            continue

        for sym in CFG["SYMBOLS"]:
            if len(open_trades) >= CFG["MAX_OPEN"]:
                break
            if per_symbol_open[sym] >= CFG["MAX_PER_SYMBOL"]:
                continue

            i = idx_map[sym].get(t)
            if i is None or i < 50:
                continue

            cds = symbol_candles[sym]
            ind = indicators[sym]

            # إشارة فنية
            signal = generate_signal(
                [c["close"] for c in cds], i,
                ind["ma_fast"], ind["ma_slow"], ind["rsi"]
            )
            if signal is None:
                continue

            direction = signal
            signal_conf = CFG["SIGNAL_CONF"]

            # المصفوفة
            slot, source = get_slot_stats(matrix, sym, dt)
            if not slot:
                continue

            # حساب scores
            scores = compute_scores(slot, direction)
            final, composite, adj_comp, w_m, w_s = compute_final_confidence(
                signal_conf, scores, None
            )

            # فلترة
            if final < CFG["MIN_FINAL_CONF"]:
                continue
            if adj_comp < CFG["MIN_COMPOSITE"]:
                continue
            if scores["reversal"] >= CFG["MAX_REVERSAL"]:
                continue

            # SL/TP/Size
            sizing = compute_sl_tp_size(
                scores, direction, balance,
                classifications[sym], signal_conf
            )
            if sizing is None:
                continue

            # محاكاة
            trade = simulate_trade(
                sym, direction, cds, i,
                sizing["sl_pct"], sizing["tp_pct"], sizing["pos_pct"],
                balance, CFG["FEE_PCT"], CFG["SLIPPAGE_PCT"]
            )

            trade["symbol"] = sym
            trade["direction"] = direction
            trade["time_utc"] = dt.isoformat()
            trade["composite"] = adj_comp
            trade["final_conf"] = final
            trade["rr"] = sizing["rr"]
            trade["classification"] = classifications[sym]

            open_trades.append({
                "symbol": sym,
                "close_idx": i + trade["hold_candles"],
                "trade": trade,
            })
            per_symbol_open[sym] += 1
            trades_opened += 1

    # إنهاء — إغلاق كل الصفقات المفتوحة
    for tr in open_trades:
        balance += tr["trade"]["pnl_usd"]
        all_trades.append(tr["trade"])

    # ============================================================
    # التقرير
    # ============================================================
    return generate_report(
        all_trades, balance, initial_balance,
        killed_days, kill_reason,
        classifications, symbol_candles, matrix
    )


# ============================================================
# 7) التقرير
# ============================================================
def generate_report(trades, final_balance, initial_balance,
                    killed_days, kill_reason, classifications,
                    symbol_candles, matrix):
    Path(CFG["OUTPUT_DIR"]).mkdir(exist_ok=True)

    if not trades:
        print("\n⚠️ لا توجد صفقات!")
        return {"trades": 0}

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] < 0]

    win_rate = len(wins) / len(trades) * 100
    total_pnl = sum(t["pnl_usd"] for t in trades)
    avg_win = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0
    pf = (sum(t["pnl_usd"] for t in wins) /
          abs(sum(t["pnl_usd"] for t in losses))) if losses else 0

    # Drawdown
    equity = initial_balance
    peak = equity
    max_dd = 0
    for t in trades:
        equity += t["pnl_usd"]
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100
        max_dd = max(max_dd, dd)

    # أكبر سلسلة خسائر
    max_streak = 0
    cur_streak = 0
    for t in trades:
        if t["pnl_usd"] < 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    # Buy & Hold
    bh_results = {}
    for sym in CFG["SYMBOLS"]:
        cds = symbol_candles.get(sym, [])
        if len(cds) < 2:
            continue
        first = cds[0]["close"]
        last = cds[-1]["close"]
        bh_pct = (last - first) / first * 100
        bh_results[sym] = bh_pct

    avg_bh = sum(bh_results.values()) / len(bh_results) if bh_results else 0

    # إحصاءات حسب التصنيف
    by_class = defaultdict(list)
    for t in trades:
        by_class[t["classification"]].append(t)

    # إحصاءات حسب العملة
    by_symbol = defaultdict(list)
    for t in trades:
        by_sym = t["symbol"]
        by_symbol[by_sym].append(t)

    # التقرير
    lines = []
    lines.append("=" * 70)
    lines.append("📊 BACKTEST REPORT")
    lines.append("=" * 70)
    lines.append(f"Initial Balance:  ${initial_balance:,.2f}")
    lines.append(f"Final Balance:    ${final_balance:,.2f}")
    lines.append(f"Total P&L:        ${total_pnl:+,.2f} ({total_pnl/initial_balance*100:+.2f}%)")
    lines.append(f"Total Trades:     {len(trades)}")
    lines.append(f"Win Rate:         {win_rate:.2f}%")
    lines.append(f"Avg Win:          ${avg_win:+,.2f}")
    lines.append(f"Avg Loss:         ${avg_loss:+,.2f}")
    lines.append(f"Profit Factor:    {pf:.2f}")
    lines.append(f"Max Drawdown:     {max_dd:.2f}%")
    lines.append(f"Max Losing Streak:{max_streak}")
    lines.append(f"Killed Days:      {len(killed_days)} / {CFG['DAYS']}")

    kr_count = defaultdict(int)
    for d, r in kill_reason.items():
        kr_count[r] += 1
    lines.append(f"  - Daily loss:   {kr_count['daily_loss']}")
    lines.append(f"  - Consecutive:  {kr_count['consecutive']}")

    lines.append("")
    lines.append("-" * 70)
    lines.append("📈 الأداء حسب التصنيف")
    lines.append("-" * 70)
    for cls in ["calm", "medium", "volatile"]:
        ts = by_class.get(cls, [])
        if not ts:
            continue
        wr = sum(1 for t in ts if t["pnl_usd"] > 0) / len(ts) * 100
        pnl = sum(t["pnl_usd"] for t in ts)
        lines.append(f"  {cls:10s}: {len(ts):4d} صفقة | WR={wr:5.1f}% | P&L=${pnl:+.2f}")

    lines.append("")
    lines.append("-" * 70)
    lines.append("📈 الأداء حسب العملة")
    lines.append("-" * 70)
    for sym in CFG["SYMBOLS"]:
        ts = by_symbol.get(sym, [])
        if not ts:
            continue
        wr = sum(1 for t in ts if t["pnl_usd"] > 0) / len(ts) * 100
        pnl = sum(t["pnl_usd"] for t in ts)
        lines.append(f"  {sym:12s}: {len(ts):4d} صفقة | WR={wr:5.1f}% | P&L=${pnl:+.2f}")

    lines.append("")
    lines.append("-" * 70)
    lines.append("📊 مقارنة مع Buy & Hold")
    lines.append("-" * 70)
    lines.append(f"  Strategy:  {total_pnl/initial_balance*100:+.2f}%")
    lines.append(f"  Avg B&H:   {avg_bh:+.2f}%")
    for sym, bh in sorted(bh_results.items(), key=lambda x: -x[1]):
        lines.append(f"    {sym:12s}: {bh:+.2f}%")

    report = "\n".join(lines)
    print("\n" + report)

    # حفظ
    with open(Path(CFG["OUTPUT_DIR"]) / "report.txt", "w", encoding="utf-8") as f:
        f.write(report)

    with open(Path(CFG["OUTPUT_DIR"]) / "trades.json", "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=1)

    return {
        "trades": len(trades),
        "final_balance": final_balance,
        "total_pnl": total_pnl,
        "win_rate": win_rate,
        "profit_factor": pf,
        "max_dd": max_dd,
        "killed_days": len(killed_days),
    }


# ============================================================
# 8) نقطة الدخول
# ============================================================
if __name__ == "__main__":
    run_backtest()
