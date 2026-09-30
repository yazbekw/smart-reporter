"""
backtest_config.py — إعدادات Backtest من متغيرات البيئة
"""
import os
from pathlib import Path


def _i(k, d): return int(os.getenv(k, d))
def _f(k, d): return float(os.getenv(k, d))
def _s(k, d): return os.getenv(k, d)


CFG = {
    # ============ عام ============
    "SYMBOLS": _s("BT_SYMBOLS",
        "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,AVAXUSDT,DOGEUSDT"
    ).split(","),
    "DAYS": _i("BT_DAYS", 180),
    "TRAIN_DAYS": _i("BT_TRAIN_DAYS", 120),
    "TEST_DAYS": _i("BT_TEST_DAYS", 60),
    "START_BALANCE": _f("BT_START_BALANCE", 1000.0),
    "MATRIX_TRAIN_PATH": _s("BT_MATRIX_TRAIN_PATH", "matrix_train.json"),
    "MATRIX_FULL_PATH": _s("BT_MATRIX_FULL_PATH", "matrix_15min.json"),

    # ============ Supabase ============
    "SUPABASE_URL": _s("SUPABASE_URL", ""),
    "SUPABASE_KEY": _s("SUPABASE_KEY", ""),

    # ============ الإشارة الفنية ============
    "SIGNAL_RULE": _s("BT_SIGNAL_RULE", "ma_cross"),
    "MA_FAST": _i("BT_MA_FAST", 10),
    "MA_SLOW": _i("BT_MA_SLOW", 30),
    "RSI_PERIOD": _i("BT_RSI_PERIOD", 14),
    "RSI_BUY": _f("BT_RSI_BUY", 30),
    "BT_RSI_SELL": _f("BT_RSI_SELL", 70),
    "RSI_SELL": _f("BT_RSI_SELL", 70),
    "SIGNAL_CONF": _i("BT_SIGNAL_CONF", 70),

    # ============ فلاتر ============
    "MIN_FINAL_CONF": _f("BT_MIN_FINAL_CONF", 60.0),
    "MIN_COMPOSITE": _f("BT_MIN_COMPOSITE", 45.0),
    "MAX_REVERSAL": _f("BT_MAX_REVERSAL", 60.0),

    # ============ SL ============
    "SL_MAE_MULT": _f("BT_SL_MAE_MULT", 1.5),
    "SL_CVAR_MULT": _f("BT_SL_CVAR_MULT", 1.2),
    "SL_STD_MULT": _f("BT_SL_STD_MULT", 2.0),
    "SL_MIN": _f("BT_SL_MIN", 0.2),
    "SL_MAX": _f("BT_SL_MAX", 3.0),

    # ============ TP ============
    "TP_CUM_MULT": _f("BT_TP_CUM_MULT", 0.7),
    "TP_MFE_MULT": _f("BT_TP_MFE_MULT", 0.85),
    "TP_MIN": _f("BT_TP_MIN", 0.25),
    "TP_MAX": _f("BT_TP_MAX", 5.0),

    # ============ R:R حسب التصنيف ============
    "RR_CALM": _f("BT_RR_CALM", 1.0),
    "RR_MEDIUM": _f("BT_RR_MEDIUM", 1.3),
    "RR_VOLATILE": _f("BT_RR_VOLATILE", 1.6),
    "CALM_MAX": _f("BT_CALM_MAX", 0.35),
    "VOLATILE_MIN": _f("BT_VOLATILE_MIN", 0.55),

    # ============ حجم الصفقة ============
    "KELLY_FRACTION": _f("BT_KELLY_FRACTION", 0.5),
    "MAX_POSITION_PCT": _f("BT_MAX_POSITION_PCT", 2.0),
    "MIN_POSITION_PCT": _f("BT_MIN_POSITION_PCT", 0.1),

    # ============ إدارة المخاطر ============
    "DAILY_LOSS_LIMIT": _f("BT_DAILY_LOSS_LIMIT", 3.0),
    "CONSECUTIVE_LOSSES": _i("BT_CONSECUTIVE_LOSSES", 3),
    "MIN_LOSS_THRESHOLD": _f("BT_MIN_LOSS_THRESHOLD", 0.02),

    # ============ الحدود ============
    "MAX_PER_SYMBOL": _i("BT_MAX_PER_SYMBOL", 2),
    "MAX_OPEN": _i("BT_MAX_OPEN", 5),
    "MAX_HOLD": _i("BT_MAX_HOLD", 4),

    # ============ الرسوم ============
    "FEE_PCT": _f("BT_FEE_PCT", 0.1),
    "SLIPPAGE_PCT": _f("BT_SLIPPAGE_PCT", 0.15),
}


def print_config():
    print("=" * 60)
    print("⚙️  Backtest Configuration")
    print("=" * 60)
    for k, v in CFG.items():
        if k in ("SUPABASE_KEY",) and v:
            v = v[:10] + "..."
        if k == "SYMBOLS":
            print(f"  {k:22s} = {','.join(v)}")
        else:
            print(f"  {k:22s} = {v}")
    print("=" * 60)
