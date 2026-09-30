"""
backtest_engine.py — محرك Backtest الرئيسي
- يستخدم مصفوفة Train
- يشغّل الاختبار على 180 يوماً
- يقارن مع Buy & Hold
"""
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

from backtest_config import CFG, print_config
from backtest_data import (
    load_candles, prepare_all_data,
    create_backtest_run, update_backtest_run, save_backtest_trades,
)


# ============================================================
# تحميل المصفوفة
# ============================================================
ARABIC_DAYS = {
    "الإثنين": 0, "الثلاثاء": 1, "الأربعاء": 2,
    "الخميس": 3, "الجمعة": 4, "السبت": 5, "الأحد": 6,
}


def load_matrix(path):
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
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

    m = {}
    if "combined" in raw:
        m["__combined__"] = _norm_days(raw["combined"].get("days", []))
    for sym, data in raw.get("symbols", {}).items():
        m[sym.upper()] = _norm_days(data.get("days", []))
    return m


def get_slot(matrix, symbol, dt):
    key = symbol.replace("USDT", "").upper()
    wd = dt.weekday()
    slot = (dt.hour * 60 + dt.minute) // 15
    for k in [key, "__combined__"]:
        if k in matrix and str(wd) in matrix[k] and str(slot) in matrix[k][str(wd)]:
            return matrix[k][str(wd)][str(slot)], k
    return None, None


# ============================================================
# المؤشرات
# ============================================================
def compute_ma(closes, period):
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
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0: gains += diff
        else: losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    for i in range(period, len(closes)):
        if i > period:
            diff = closes[i] - closes[i - 1]
            g = max(0, diff); l = max(0, -diff)
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - (100 / (1 + rs))
    return out


def signal_at(closes, i, ma_f, ma_s, rsi):
    rule = CFG["SIGNAL_RULE"]
    if rule == "ma_cross":
        if i < 1 or ma_f[i] is None or ma_s[i] is None:
            return None
        if ma_f[i - 1] is None or ma_s[i - 1] is None:
            return None
        if ma_f[i - 1] < ma_s[i - 1] and ma_f[i] > ma_s[i]:
            return "LONG"
        if ma_f[i - 1] > ma_s[i - 1] and ma_f[i] < ma_s[i]:
            return "SHORT"
        return None
    if rule == "rsi":
        if i < 1 or rsi[i] is None or rsi[i - 1] is None:
            return None
        if rsi[i - 1] < CFG["RSI_BUY"] and rsi[i] >= CFG["RSI_BUY"]:
            return "LONG"
        if rsi[i - 1] > CFG["RSI_SELL"] and rsi[i] <= CFG["RSI_SELL"]:
            return "SHORT"
        return None
    return None


# ============================================================
# التصنيف
# ============================================================
def classify(matrix, symbol):
    key = symbol.replace("USDT", "").upper()
    k = key if key in matrix else "__combined__"
    if k not in matrix:
        return "medium", 0.4
    vals = []
    for wd in matrix[k].values():
        for s in wd.values():
            r = s.get("avg_range", 0)
            if r > 0: vals.append(r)
    if not vals:
        return "medium", 0.4
    vals.sort()
    med = vals[len(vals) // 2]
    if med < CFG["CALM_MAX"]: return "calm", med
    if med > CFG["VOLATILE_MIN"]: return "volatile", med
    return "medium", med


def min_rr(cls):
    return {"calm": CFG["RR_CALM"], "medium": CFG["RR_MEDIUM"],
            "volatile": CFG["RR_VOLATILE"]}.get(cls, 1.3)


# ============================================================
# حسابات
# ============================================================
def scores_from_slot(slot, direction):
    wr = slot.get("win_rate", slot.get("wr", 0))
    ret = slot.get("return", slot.get("ret", 0))
    t = slot.get("t_stat", slot.get("t", 0))
    n = slot.get("n", 0)
    cum4 = slot.get("cum_ret_4", 0)
    mfe = slot.get("mfe_4", 0)
    mae = slot.get("mae_4", 0)

    wr_s = wr if direction == "LONG" else (100 - wr)
    ret_s = 50 + ret * 50 if direction == "LONG" else 50 - ret * 50
    ret_s = max(0, min(100, ret_s))
    t_abs = abs(t)
    t_s = min(100, (t_abs / 3) * 100)
    correct = (t > 0 and direction == "LONG") or (t < 0 and direction == "SHORT")
    if not correct: t_s = 100 - t_s
    if n >= 30: n_s = 100.0
    elif n < 10: n_s = 0.0
    else: n_s = ((n - 10) / 20) * 100
    cum_s = 50 + cum4 * 25 if direction == "LONG" else 50 - cum4 * 25
    cum_s = max(0, min(100, cum_s))
    if direction == "SHORT":
        mfe_u, mae_u = abs(mae), abs(mfe)
    else:
        mfe_u = mfe; mae_u = abs(mae) if mae < 0 else 0
    mfe_s = 50.0 if (mfe_u + mae_u) <= 0 else (mfe_u / (mfe_u + mae_u)) * 100

    composite = (wr_s * 0.25 + ret_s * 0.20 + t_s * 0.20 +
                 n_s * 0.10 + cum_s * 0.15 + mfe_s * 0.10)

    return {
        "composite": composite, "wr": wr, "ret": ret, "t": t, "n": n,
        "cum4": cum4, "mfe": mfe, "mae": mae,
        "reversal": slot.get("reversal_prob", 30),
        "target_hit_05": slot.get("target_hit_0_5", 50),
        "target_hit_10": slot.get("target_hit_1_0", 25),
        "cvar95": slot.get("cvar95", 0), "std": slot.get("std", 0),
    }


def dyn_weight(t_abs):
    if t_abs >= 3.0: return 0.50
    if t_abs >= 2.0: return 0.40
    if t_abs >= 1.5: return 0.30
    return 0.15


def compute_final(signal_conf, sc):
    w_m = dyn_weight(abs(sc["t"]))
    w_s = 1 - w_m
    adj = max(0, min(100, sc["composite"]))
    final = signal_conf * w_s + adj * w_m
    return int(round(final)), adj, w_m, w_s


def compute_sl_tp(sc, direction, cls, signal_conf):
    mae_a = abs(sc["mae"]); cvar_a = abs(sc["cvar95"]); std_a = abs(sc["std"])
    sl_base = max(
        mae_a * CFG["SL_MAE_MULT"],
        cvar_a * CFG["SL_CVAR_MULT"],
        std_a * CFG["SL_STD_MULT"],
    )
    if sc["reversal"] >= 60: sl_base *= 1.3
    elif sc["reversal"] >= 45: sl_base *= 1.15
    if abs(sc["t"]) < 1.5: sl_base *= 1.2
    sl_pct = max(CFG["SL_MIN"], min(sl_base, CFG["SL_MAX"]))

    cum4 = abs(sc["cum4"]); mfe_a = abs(sc["mfe"])
    th05 = sc["target_hit_05"]; th10 = sc["target_hit_10"]
    if th10 >= 50:
        tp_pct = max(cum4 * 0.8, 1.0)
    elif th05 >= 60:
        tp_pct = max(cum4 * CFG["TP_CUM_MULT"], 0.5)
    else:
        tp_pct = max(cum4 * 0.5, 0.3)
    tp_pct = min(tp_pct, mfe_a * CFG["TP_MFE_MULT"], CFG["TP_MAX"])
    tp_pct = max(tp_pct, CFG["TP_MIN"])

    rr = tp_pct / sl_pct if sl_pct > 0 else 0
    if rr < min_rr(cls):
        return None

    win_p = sc["wr"] / 100
    kelly = (win_p * rr - (1 - win_p)) / rr if rr > 0 else 0

    t_f = min(1.0, abs(sc["t"]) / 3.0)
    rev_f = max(0.3, 1 - sc["reversal"] / 100)
    smp_f = min(1.0, sc["n"] / 30)
    conf_f = signal_conf / 100

    kelly_adj = kelly * t_f * rev_f * smp_f * conf_f * CFG["KELLY_FRACTION"]
    pos_pct = max(CFG["MIN_POSITION_PCT"],
                  min(kelly_adj * 100, CFG["MAX_POSITION_PCT"])) / 100

    return {"sl_pct": sl_pct, "tp_pct": tp_pct, "rr": rr, "pos_pct": pos_pct}


# ============================================================
# محاكاة صفقة
# ============================================================
def simulate(symbol, direction, candles, entry_idx,
             sl_pct, tp_pct, pos_pct, balance):
    c0 = candles[entry_idx]
    raw_entry = c0["close"]
    fee = CFG["FEE_PCT"]; slip = CFG["SLIPPAGE_PCT"]

    if direction == "LONG":
        entry = raw_entry * (1 + slip / 100)
        sl = entry * (1 - sl_pct / 100)
        tp = entry * (1 + tp_pct / 100)
    else:
        entry = raw_entry * (1 - slip / 100)
        sl = entry * (1 + sl_pct / 100)
        tp = entry * (1 - tp_pct / 100)

    size_usd = balance * pos_pct
    exit_price = None; exit_reason = None
    exit_idx = entry_idx

    for j in range(1, CFG["MAX_HOLD"] + 1):
        if entry_idx + j >= len(candles): break
        c = candles[entry_idx + j]
        exit_idx = entry_idx + j
        if direction == "LONG":
            if c["low"] <= sl:
                exit_price = sl; exit_reason = "SL"; break
            if c["high"] >= tp:
                exit_price = tp; exit_reason = "TP"; break
        else:
            if c["high"] >= sl:
                exit_price = sl; exit_reason = "SL"; break
            if c["low"] <= tp:
                exit_price = tp; exit_reason = "TP"; break

    if exit_price is None:
        ec = candles[exit_idx]
        exit_price = ec["close"]
        if direction == "LONG":
            exit_price *= (1 - slip / 100)
        else:
            exit_price *= (1 + slip / 100)
        exit_reason = "TIME"

    if direction == "LONG":
        pnl_pct = (exit_price - entry) / entry * 100
    else:
        pnl_pct = (entry - exit_price) / entry * 100
    pnl_pct -= fee * 2
    pnl_usd = size_usd * pnl_pct / 100

    return {
        "entry": entry, "exit": exit_price, "sl": sl, "tp": tp,
        "sl_pct": sl_pct, "tp_pct": tp_pct,
        "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
        "exit_reason": exit_reason,
        "hold_candles": exit_idx - entry_idx,
        "size_usd": size_usd,
    }


# ============================================================
# المحرك الرئيسي
# ============================================================
def run_backtest(progress_cb=None):
    """يشغّل Backtest كاملاً ويعيد التقرير"""
    def _log(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    _log("=" * 60)
    _log("🚀 بدء Backtest")
    _log("=" * 60)
    print_config()

    # 1) مصفوفة Train
    _log("\n📥 تحميل مصفوفة Train...")
    train_matrix = load_matrix(CFG["MATRIX_TRAIN_PATH"])
    if not train_matrix:
        return {"ok": False, "error": "matrix_train.json غير موجود"}
    _log(f"  ✅ {len([k for k in train_matrix if k != '__combined__'])} عملة")

    # 2) شموع
    _log("\n📥 تحميل الشموع من Supabase...")
    symbol_candles = {}
    for sym in CFG["SYMBOLS"]:
        cds = load_candles(sym, CFG["DAYS"])
        symbol_candles[sym] = cds
        _log(f"  {sym:12s}: {len(cds):5d} شمعة")

    # 3) تصنيف
    _log("\n🏷️  تصنيف العملات:")
    cls_map = {}
    for sym in CFG["SYMBOLS"]:
        c, med = classify(train_matrix, sym)
        cls_map[sym] = c
        _log(f"  {sym:12s} → {c:10s} (avg_range={med:.3f}%)")

    # 4) مؤشرات
    _log("\n🔧 حساب المؤشرات...")
    indicators = {}
    for sym, cds in symbol_candles.items():
        closes = [c["close"] for c in cds]
        indicators[sym] = {
            "ma_f": compute_ma(closes, CFG["MA_FAST"]),
            "ma_s": compute_ma(closes, CFG["MA_SLOW"]),
            "rsi": compute_rsi(closes, CFG["RSI_PERIOD"]),
        }

    # 5) فهارس
    idx_map = {sym: {c["open_time"]: i for i, c in enumerate(cds)}
               for sym, cds in symbol_candles.items()}

    # 6) كل الأوقات
    all_times = set()
    for cds in symbol_candles.values():
        for c in cds:
            all_times.add(c["open_time"])
    all_times = sorted(all_times)
    _log(f"\n  ⏱️  {len(all_times):,} نقطة زمنية")

    # 7) المحاكاة
    _log("\n" + "=" * 60)
    _log("🎯 بدء المحاكاة")
    _log("=" * 60)

    balance = CFG["START_BALANCE"]
    initial = balance
    all_trades = []
    open_trades = []
    daily_pnl = defaultdict(float)
    killed_days = set()
    kill_reason = {}
    per_sym_open = defaultdict(int)
    consecutive_losses = 0

    for t_idx, t in enumerate(all_times):
        if t_idx % 5000 == 0:
            _log(f"  ⏳ {t_idx:,}/{len(all_times):,} | رصيد=${balance:.2f} | صفقات={len(all_trades)}")

        dt = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")

        # kill switches
        if day_key in killed_days:
            continue
        if daily_pnl[day_key] <= -CFG["DAILY_LOSS_LIMIT"] * initial / 100:
            killed_days.add(day_key); kill_reason[day_key] = "daily"
            continue
        if consecutive_losses >= CFG["CONSECUTIVE_LOSSES"]:
            killed_days.add(day_key); kill_reason[day_key] = "consecutive"
            continue

        # إغلاق
        still_open = []
        for tr in open_trades:
            if tr["close_idx"] <= t_idx:
                trade = tr["trade"]
                balance += trade["pnl_usd"]
                daily_pnl[day_key] += trade["pnl_usd"]
                all_trades.append(trade)
                if trade["pnl_usd"] < -CFG["MIN_LOSS_THRESHOLD"]:
                    consecutive_losses += 1
                elif trade["pnl_usd"] > 0:
                    consecutive_losses = 0
                per_sym_open[tr["symbol"]] -= 1
            else:
                still_open.append(tr)
        open_trades = still_open

        if len(open_trades) >= CFG["MAX_OPEN"]:
            continue

        # فتح
        for sym in CFG["SYMBOLS"]:
            if len(open_trades) >= CFG["MAX_OPEN"]: break
            if per_sym_open[sym] >= CFG["MAX_PER_SYMBOL"]: continue

            i = idx_map[sym].get(t)
            if i is None or i < 50: continue

            cds = symbol_candles[sym]
            ind = indicators[sym]
            closes = [c["close"] for c in cds]

            sig = signal_at(closes, i, ind["ma_f"], ind["ma_s"], ind["rsi"])
            if sig is None: continue

            direction = sig
            signal_conf = CFG["SIGNAL_CONF"]

            slot, source = get_slot(train_matrix, sym, dt)
            if not slot: continue

            sc = scores_from_slot(slot, direction)
            final, comp, _, _ = compute_final(signal_conf, sc)

            if final < CFG["MIN_FINAL_CONF"]: continue
            if comp < CFG["MIN_COMPOSITE"]: continue
            if sc["reversal"] >= CFG["MAX_REVERSAL"]: continue

            sizing = compute_sl_tp(sc, direction, cls_map[sym], signal_conf)
            if sizing is None: continue

            trade = simulate(sym, direction, cds, i,
                             sizing["sl_pct"], sizing["tp_pct"],
                             sizing["pos_pct"], balance)
            trade.update({
                "symbol": sym, "direction": direction,
                "time_utc": dt.isoformat(),
                "rr": sizing["rr"],
                "composite": comp, "final_conf": final,
                "classification": cls_map[sym],
            })

            open_trades.append({
                "symbol": sym,
                "close_idx": i + trade["hold_candles"],
                "trade": trade,
            })
            per_sym_open[sym] += 1

    # إغلاق كل شيء
    for tr in open_trades:
        balance += tr["trade"]["pnl_usd"]
        all_trades.append(tr["trade"])

    _log(f"\n✅ اكتمل — {len(all_trades)} صفقة | رصيد نهائي=${balance:.2f}")

    # التقرير
    return _build_report(all_trades, balance, initial, killed_days,
                         kill_reason, cls_map, symbol_candles)


# ============================================================
# التقرير
# ============================================================
def _build_report(trades, final_balance, initial, killed_days,
                  kill_reason, cls_map, symbol_candles):
    if not trades:
        return {"ok": False, "error": "لا توجد صفقات"}

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] < 0]
    wr = len(wins) / len(trades) * 100
    pnl = sum(t["pnl_usd"] for t in trades)
    pf = (sum(t["pnl_usd"] for t in wins) /
          abs(sum(t["pnl_usd"] for t in losses))) if losses else 0

    # DD
    eq = initial; peak = eq; max_dd = 0
    for t in trades:
        eq += t["pnl_usd"]
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100
        max_dd = max(max_dd, dd)

    # streak
    mx_strk = cur = 0
    for t in trades:
        if t["pnl_usd"] < 0:
            cur += 1; mx_strk = max(mx_strk, cur)
        else:
            cur = 0

    # B&H
    bh = {}
    for sym, cds in symbol_candles.items():
        if len(cds) >= 2:
            bh[sym] = (cds[-1]["close"] - cds[0]["close"]) / cds[0]["close"] * 100
    avg_bh = sum(bh.values()) / len(bh) if bh else 0

    # حسب التصنيف
    by_cls = defaultdict(list)
    for t in trades: by_cls[t["classification"]].append(t)

    # حسب العملة
    by_sym = defaultdict(list)
    for t in trades: by_sym[t["symbol"]].append(t)

    kr_count = defaultdict(int)
    for r in kill_reason.values(): kr_count[r] += 1

    # نص التقرير
    lines = []
    lines.append("=" * 70)
    lines.append("📊 BACKTEST REPORT")
    lines.append("=" * 70)
    lines.append(f"Initial:         ${initial:,.2f}")
    lines.append(f"Final:           ${final_balance:,.2f}")
    lines.append(f"P&L:             ${pnl:+,.2f} ({pnl/initial*100:+.2f}%)")
    lines.append(f"Trades:          {len(trades)}")
    lines.append(f"Win Rate:        {wr:.2f}%")
    lines.append(f"Profit Factor:   {pf:.2f}")
    lines.append(f"Max Drawdown:    {max_dd:.2f}%")
    lines.append(f"Max Losing Strk: {mx_strk}")
    lines.append(f"Killed Days:     {len(killed_days)}/{CFG['DAYS']}")
    lines.append(f"  - Daily:       {kr_count['daily']}")
    lines.append(f"  - Consecutive: {kr_count['consecutive']}")
    lines.append("")
    lines.append("─" * 70)
    lines.append("📈 حسب التصنيف")
    lines.append("─" * 70)
    for c in ["calm", "medium", "volatile"]:
        ts = by_cls.get(c, [])
        if not ts: continue
        w = sum(1 for t in ts if t["pnl_usd"] > 0) / len(ts) * 100
        p = sum(t["pnl_usd"] for t in ts)
        lines.append(f"  {c:10s}: {len(ts):4d} | WR={w:5.1f}% | P&L=${p:+.2f}")
    lines.append("")
    lines.append("─" * 70)
    lines.append("📈 حسب العملة")
    lines.append("─" * 70)
    for sym in CFG["SYMBOLS"]:
        ts = by_sym.get(sym, [])
        if not ts: continue
        w = sum(1 for t in ts if t["pnl_usd"] > 0) / len(ts) * 100
        p = sum(t["pnl_usd"] for t in ts)
        lines.append(f"  {sym:12s}: {len(ts):4d} | WR={w:5.1f}% | P&L=${p:+.2f}")
    lines.append("")
    lines.append("─" * 70)
    lines.append("📊 مقارنة مع Buy & Hold")
    lines.append("─" * 70)
    lines.append(f"  Strategy: {pnl/initial*100:+.2f}%")
    lines.append(f"  Avg B&H:  {avg_bh:+.2f}%")
    for sym, v in sorted(bh.items(), key=lambda x: -x[1]):
        lines.append(f"    {sym:12s}: {v:+.2f}%")

    report_text = "\n".join(lines)
    print("\n" + report_text)

    return {
        "ok": True,
        "report_text": report_text,
        "summary": {
            "trades": len(trades),
            "final_balance": final_balance,
            "pnl": pnl,
            "pnl_pct": pnl / initial * 100,
            "win_rate": wr,
            "profit_factor": pf,
            "max_dd": max_dd,
            "max_streak": mx_strk,
            "killed_days": len(killed_days),
            "killed_daily": kr_count["daily"],
            "killed_consecutive": kr_count["consecutive"],
            "bh_avg": avg_bh,
            "bh_by_symbol": bh,
        },
        "trades": trades,
    }
