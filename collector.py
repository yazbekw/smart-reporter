"""
collector.py
============
خدمة Backtest مستقلة مع واجهة احترافية
- Flask app
- Supabase كقاعدة بيانات
- تشغيل Jobs في الخلفية
- واجهة HTML داكنة مع تقارير ورسوم
"""
import os
import json
import threading
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, Response, request
from dotenv import load_dotenv

# تحميل env
load_dotenv()

# Supabase
from supabase import create_client

# Backtest modules
from backtest_config import CFG, print_config
from backtest_data import (
    load_candles, prepare_all_data,
    create_backtest_run, update_backtest_run, save_backtest_trades,
    _sb_client, cache_has_data,
)
from backtest_matrix import build_train_matrix
from backtest_engine import run_backtest


# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ============================================================
# Flask App
# ============================================================
app = Flask(__name__)

# حالة العملية الحالية
_state = {
    "job": None,           # collect | build | run | None
    "status": "idle",      # idle | running | done | error
    "progress": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
    "result": None,        # نتيجة آخر Backtest
    "run_id": None,
}
_lock = threading.Lock()


# ============================================================
# Helpers
# ============================================================
def _set_state(**kwargs):
    with _lock:
        _state.update(kwargs)


def _reset_for_job(job_name):
    _set_state(
        job=job_name,
        status="running",
        progress=f"بدء {job_name}...",
        error=None,
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        result=None,
    )


def _progress_cb(msg):
    """يعرض آخر 6 أسطر فقط لتجنب الفيضان"""
    lines = msg.split("\n")
    short = "\n".join(lines[-6:]) if len(lines) > 6 else msg
    _set_state(progress=short)


# ============================================================
# Jobs
# ============================================================
def _job_collect():
    try:
        _reset_for_job("collect")
        total = prepare_all_data(progress_cb=_progress_cb)
        _set_state(
            status="done",
            progress=f"✅ تم جمع {total} شمعة",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        import traceback
        _set_state(
            status="error",
            error=str(e),
            progress=traceback.format_exc()[-500:],
            finished_at=datetime.now(timezone.utc).isoformat(),
        )


def _job_build_matrix():
    try:
        _reset_for_job("build")
        matrix = build_train_matrix(
            output_path=CFG["MATRIX_TRAIN_PATH"],
            days=CFG["TRAIN_DAYS"],
            progress_cb=_progress_cb,
        )
        n_symbols = len(matrix.get("symbols", {}))
        _set_state(
            status="done",
            progress=f"✅ matrix_train.json جاهز ({n_symbols} عملة)",
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        import traceback
        _set_state(
            status="error",
            error=str(e),
            progress=traceback.format_exc()[-500:],
            finished_at=datetime.now(timezone.utc).isoformat(),
        )


def _job_run():
    try:
        _reset_for_job("run")

        run_id = None
        try:
            run_id = create_backtest_run(CFG)
            _set_state(run_id=run_id)
        except Exception as e:
            logger.warning(f"run_id create: {e}")

        report = run_backtest(progress_cb=_progress_cb)

        if not report.get("ok"):
            _set_state(
                status="error",
                error=report.get("error", "unknown"),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            if run_id:
                update_backtest_run(run_id, status="error",
                                    error_message=report.get("error"))
            return

        _set_state(
            status="done",
            progress=f"✅ اكتمل — {report['summary']['trades']} صفقة",
            result={
                "summary": report["summary"],
                "report_text": report["report_text"],
                "trades": report["trades"][:2000],  # نحفظ 2000 صفقة للعرض
                "run_id": run_id,
            },
            finished_at=datetime.now(timezone.utc).isoformat(),
        )

        if run_id:
            update_backtest_run(
                run_id, status="done",
                summary_json=report["summary"],
                report_text=report["report_text"],
                trades_count=len(report["trades"]),
            )
            save_backtest_trades(run_id, report["trades"])

    except Exception as e:
        import traceback
        _set_state(
            status="error",
            error=str(e),
            progress=traceback.format_exc()[-800:],
            finished_at=datetime.now(timezone.utc).isoformat(),
        )


def _start_job(job_name):
    with _lock:
        if _state["status"] == "running":
            return False, "عملية قيد التشغيل"
        if job_name == "collect":
            target = _job_collect
        elif job_name == "build":
            target = _job_build_matrix
        elif job_name == "run":
            target = _job_run
        else:
            return False, "job غير معروف"

    threading.Thread(target=target, daemon=True).start()
    return True, "ok"


# ============================================================
# Endpoints
# ============================================================
@app.route('/')
def index():
    return Response(HTML_PAGE, mimetype='text/html; charset=utf-8')


@app.route('/api/health')
def api_health():
    return jsonify({"ok": True, "time": datetime.now(timezone.utc).isoformat()})


@app.route('/api/status')
def api_status():
    with _lock:
        s = dict(_state)
    # لا نُرسل trades في status لتخفيف الرد
    if s.get("result"):
        s["result"] = {
            "summary": s["result"]["summary"],
            "run_id": s["result"].get("run_id"),
        }
    return jsonify(s)

@app.route('/api/diagnose')
def api_diagnose():
    """تشخيص شامل — Supabase + Binance Vision"""
    out = {
        "supabase": {},
        "binance_vision": {},
        "config": {},
    }

    # 1. فحص Supabase
    try:
        from backtest_data import _sb_client, diagnose_table
        diag = diagnose_table()
        out["supabase"] = diag
    except Exception as e:
        out["supabase"] = {"ok": False, "error": str(e)}

    # 2. فحص Binance Vision
    try:
        import requests
        url = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/15m/BTCUSDT-15m-2026-06.zip"
        r = requests.head(url, timeout=10)
        out["binance_vision"] = {
            "url": url,
            "status_code": r.status_code,
            "ok": r.status_code == 200,
            "size": r.headers.get("Content-Length", "unknown"),
        }
    except Exception as e:
        out["binance_vision"] = {"ok": False, "error": str(e)}

    # 3. الإعدادات
    from backtest_config import CFG
    out["config"] = {
        "SUPABASE_URL": CFG["SUPABASE_URL"][:40] + "..." if CFG["SUPABASE_URL"] else "MISSING",
        "SUPABASE_KEY_len": len(CFG["SUPABASE_KEY"]) if CFG["SUPABASE_KEY"] else 0,
        "SYMBOLS": CFG["SYMBOLS"],
        "DAYS": CFG["DAYS"],
    }

    return jsonify(out)

@app.route('/api/config')
def api_config():
    """يعرض الإعدادات الحالية"""
    safe = {}
    for k, v in CFG.items():
        if k in ("SUPABASE_KEY",) and v:
            v = str(v)[:10] + "..."
        safe[k] = v
    return jsonify(safe)


@app.route('/api/cache_status')
def api_cache_status():
    """حالة الشموع المخزّنة لكل عملة"""
    out = {}
    for sym in CFG["SYMBOLS"]:
        try:
            has = cache_has_data(sym, CFG["DAYS"])
            cutoff = int(
                (datetime.now(timezone.utc) - timedelta(days=CFG["DAYS"])).timestamp() * 1000
            )
            sb = _sb_client()
            res = (
                sb.table("candles")
                .select("open_time", count="exact")
                .eq("symbol", sym)
                .gte("open_time", cutoff)
                .execute()
            )
            out[sym] = {"count": res.count or 0, "ready": has}
        except Exception as e:
            out[sym] = {"error": str(e)[:100], "ready": False}
    return jsonify(out)


@app.route('/api/collect', methods=['POST'])
def api_collect():
    ok, msg = _start_job("collect")
    return jsonify({"ok": ok, "msg": msg})


@app.route('/api/build', methods=['POST'])
def api_build():
    ok, msg = _start_job("build")
    return jsonify({"ok": ok, "msg": msg})


@app.route('/api/run', methods=['POST'])
def api_run():
    ok, msg = _start_job("run")
    return jsonify({"ok": ok, "msg": msg})


@app.route('/api/report')
def api_report():
    with _lock:
        result = _state.get("result")
    if not result:
        return jsonify({"ok": False, "msg": "لا يوجد تقرير"}), 404
    return jsonify({
        "ok": True,
        "summary": result["summary"],
        "report_text": result["report_text"],
        "trades": result["trades"],
        "run_id": result.get("run_id"),
    })


@app.route('/api/runs')
def api_runs():
    """قائمة التشغيلات السابقة من Supabase"""
    try:
        sb = _sb_client()
        res = (
            sb.table("backtest_runs")
            .select("id,started_at,finished_at,status,trades_count,summary_json")
            .order("id", desc=True)
            .limit(20)
            .execute()
        )
        return jsonify({"ok": True, "runs": res.data or []})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/run/<int:run_id>')
def api_run_detail(run_id):
    try:
        sb = _sb_client()
        res = (
            sb.table("backtest_runs")
            .select("*")
            .eq("id", run_id)
            .single()
            .execute()
        )
        if not res.data:
            return jsonify({"ok": False, "msg": "not found"}), 404
        return jsonify({"ok": True, "run": res.data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/run/<int:run_id>/trades')
def api_run_trades_csv(run_id):
    """يصدّر صفقات تشغيل كـ CSV"""
    try:
        sb = _sb_client()
        res = (
            sb.table("backtest_trades")
            .select("*")
            .eq("run_id", run_id)
            .order("id")
            .limit(5000)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return "لا توجد صفقات", 404

        header = ["id", "symbol", "direction", "entry_time", "entry_price",
                  "exit_price", "exit_reason", "sl_pct", "tp_pct", "rr",
                  "position_usd", "pnl_usd", "pnl_pct", "hold_candles",
                  "classification", "final_conf", "composite"]
        lines = [",".join(header)]
        for r in rows:
            lines.append(",".join(str(r.get(h, "")) for h in header))

        csv_data = "\n".join(lines)
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={
                "Content-Disposition":
                    f"attachment; filename=run_{run_id}_trades.csv"
            },
        )
    except Exception as e:
        return f"error: {e}", 500


@app.route('/api/current_report_csv')
def api_current_report_csv():
    """يصدّر تقرير التشغيل الحالي كـ CSV"""
    with _lock:
        result = _state.get("result")
    if not result:
        return "لا يوجد تقرير", 404
    trades = result.get("trades", [])
    if not trades:
        return "لا توجد صفقات", 404

    header = ["symbol", "direction", "entry_time", "entry_price",
              "exit_price", "exit_reason", "sl_pct", "tp_pct", "rr",
              "position_usd", "pnl_usd", "pnl_pct", "hold_candles",
              "classification", "final_conf", "composite"]
    lines = [",".join(header)]
    for t in trades:
        lines.append(",".join(str(t.get(h, "")) for h in header))

    return Response(
        "\n".join(lines),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=current_trades.csv"
        },
    )


# ============================================================
# HTML Page
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest Studio — Smart Analyst</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0a0e14;
    --card: #131820;
    --card2: #1a2029;
    --line: #232a35;
    --txt: #e6edf3;
    --muted: #7d8590;
    --acc: #58a6ff;
    --acc2: #1f6feb;
    --green: #3fb950;
    --red: #f85149;
    --orange: #d29922;
    --purple: #a371f7;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--txt);
    font-family: 'Segoe UI', Tahoma, sans-serif;
    font-size: 13px; padding: 0;
  }
  .top-bar {
    background: linear-gradient(90deg, #0d1117, #131820);
    border-bottom: 1px solid var(--line);
    padding: 14px 20px;
    display: flex; justify-content: space-between; align-items: center;
    position: sticky; top: 0; z-index: 100;
  }
  .top-bar h1 { font-size: 18px; margin: 0; font-weight: 600; }
  .top-bar h1 span { color: var(--acc); }
  .live-dot {
    display: inline-block; width: 8px; height: 8px;
    background: var(--green); border-radius: 50%;
    margin-left: 6px; animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%,100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .container { max-width: 1500px; margin: 0 auto; padding: 20px; }

  .actions-bar {
    display: flex; gap: 10px; flex-wrap: wrap;
    margin-bottom: 20px;
  }
  .btn {
    background: var(--acc2); color: #fff; border: none;
    padding: 10px 18px; border-radius: 8px; cursor: pointer;
    font-size: 13px; font-weight: 600;
    transition: all 0.2s;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(88,166,255,0.3); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn.green { background: #238636; }
  .btn.purple { background: #6e5494; }
  .btn.orange { background: #bb8009; }
  .btn.secondary { background: var(--card2); color: var(--txt); border: 1px solid var(--line); }
  .btn.sm { padding: 6px 12px; font-size: 12px; }

  .status-panel {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 10px; padding: 14px; margin-bottom: 20px;
    display: none;
  }
  .status-panel.show { display: block; }
  .status-panel .header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 10px;
  }
  .status-badge {
    padding: 4px 10px; border-radius: 6px; font-size: 11px;
    font-weight: 600;
  }
  .badge-running { background: rgba(210,153,34,0.15); color: var(--orange); }
  .badge-done { background: rgba(63,185,80,0.15); color: var(--green); }
  .badge-error { background: rgba(248,81,73,0.15); color: var(--red); }
  .badge-idle { background: rgba(125,133,144,0.15); color: var(--muted); }
  .progress-bar {
    height: 4px; background: var(--card2); border-radius: 2px;
    overflow: hidden; margin: 8px 0;
  }
  .progress-bar .fill {
    height: 100%; background: var(--acc);
    width: 0%; transition: width 0.4s;
    animation: shimmer 2s infinite;
  }
  @keyframes shimmer {
    0% { opacity: 0.6; }
    50% { opacity: 1; }
    100% { opacity: 0.6; }
  }
  .log-box {
    background: #050810; border: 1px solid var(--line);
    border-radius: 6px; padding: 10px;
    font-family: 'Consolas', 'Monaco', monospace;
    font-size: 11px; color: #b8c4d0;
    white-space: pre-wrap; max-height: 150px; overflow-y: auto;
    direction: ltr; text-align: left;
  }

  .grid { display: grid; gap: 16px; }
  .grid-4 { grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }
  .grid-2 { grid-template-columns: repeat(auto-fit, minmax(400px, 1fr)); }

  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 10px; padding: 16px;
  }
  .card h3 {
    font-size: 13px; margin: 0 0 12px; font-weight: 600;
    color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px;
  }

  .metric {
    background: var(--card2); border: 1px solid var(--line);
    border-radius: 8px; padding: 14px;
  }
  .metric .label {
    color: var(--muted); font-size: 10px; margin-bottom: 4px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .metric .value {
    font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums;
    line-height: 1.2;
  }
  .metric .sub { font-size: 11px; color: var(--muted); margin-top: 4px; }

  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neu { color: var(--muted); }

  table {
    width: 100%; border-collapse: collapse; font-size: 11.5px;
    font-variant-numeric: tabular-nums;
  }
  th, td {
    padding: 7px 8px; text-align: right;
    border-bottom: 1px solid var(--line);
  }
  th {
    color: var(--muted); font-weight: 600;
    background: var(--card2); text-align: right;
    position: sticky; top: 0; z-index: 1;
  }
  tr:hover td { background: rgba(88,166,255,0.04); }
  .table-scroll {
    max-height: 500px; overflow-y: auto;
    border: 1px solid var(--line); border-radius: 8px;
  }

  .tabs {
    display: flex; gap: 4px; border-bottom: 1px solid var(--line);
    margin-bottom: 14px; overflow-x: auto;
  }
  .tab {
    background: transparent; color: var(--muted); border: none;
    padding: 10px 16px; cursor: pointer; font-size: 12px;
    font-weight: 600; border-bottom: 2px solid transparent;
    white-space: nowrap;
  }
  .tab:hover { color: var(--txt); }
  .tab.active {
    color: var(--acc); border-bottom-color: var(--acc);
  }
  .tab-content { display: none; }
  .tab-content.active { display: block; }

  canvas { max-height: 320px; }

  .runs-list {
    display: flex; flex-direction: column; gap: 6px;
  }
  .run-item {
    background: var(--card2); border: 1px solid var(--line);
    border-radius: 8px; padding: 10px 14px;
    display: flex; justify-content: space-between; align-items: center;
    cursor: pointer; transition: all 0.15s;
  }
  .run-item:hover {
    border-color: var(--acc); background: rgba(88,166,255,0.04);
  }
  .run-item .info { display: flex; gap: 14px; align-items: center; }
  .run-item .id {
    background: var(--acc2); color: #fff;
    padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
  }
  .run-item .date { color: var(--muted); font-size: 11px; }
  .run-item .stats { color: var(--muted); font-size: 11px; }

  .footer {
    color: var(--muted); font-size: 11px;
    text-align: center; padding: 30px 0; border-top: 1px solid var(--line);
    margin-top: 40px;
  }

  .hidden { display: none; }

  .config-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 8px;
  }
  .config-item {
    background: #050810; padding: 8px 10px;
    border-radius: 6px; font-family: monospace;
    font-size: 11px; direction: ltr; text-align: left;
  }
  .config-item .key { color: var(--muted); }
  .config-item .val { color: var(--acc); }

  .empty-state {
    text-align: center; padding: 40px 20px; color: var(--muted);
  }
  .empty-state .icon { font-size: 48px; margin-bottom: 12px; }
</style>
</head>
<body>

<div class="top-bar">
  <h1>🎯 <span>Backtest Studio</span> — Smart Analyst</h1>
  <div>
    <span id="stateIndicator" class="status-badge badge-idle">جاهز</span>
    <span class="live-dot"></span>
  </div>
</div>

<div class="container">

  <!-- Actions -->
  <div class="actions-bar">
    <button class="btn" id="btnCollect" onclick="startJob('collect')">
      📥 جمع الشموع
    </button>
    <button class="btn purple" id="btnBuild" onclick="startJob('build')">
      🔨 بناء مصفوفة Train
    </button>
    <button class="btn green" id="btnRun" onclick="startJob('run')">
      🚀 تشغيل Backtest
    </button>
    <button class="btn orange sm" onclick="loadRuns()">🔄 تحديث السجل</button>
    <button class="btn secondary sm" onclick="showConfig()">⚙️ الإعدادات</button>
  </div>

  <!-- Status Panel -->
  <div class="status-panel" id="statusPanel">
    <div class="header">
      <div>
        <strong id="statusTitle">—</strong>
        <span id="statusBadge" class="status-badge badge-running">يعمل</span>
      </div>
      <div style="color: var(--muted); font-size: 11px;" id="statusTime">—</div>
    </div>
    <div class="progress-bar"><div class="fill" id="progressFill"></div></div>
    <div class="log-box" id="logBox">—</div>
  </div>

  <!-- Cache status -->
  <div class="card" style="margin-bottom: 20px;">
    <h3>💾 حالة الشموع في Supabase</h3>
    <div id="cacheStatus" class="grid grid-4">
      <div class="empty-state" style="grid-column: 1/-1;">اضغط "تحديث" لفحص الحالة</div>
    </div>
  </div>

  <!-- Config -->
  <div class="card hidden" id="configCard" style="margin-bottom: 20px;">
    <h3>⚙️ الإعدادات الحالية (من متغيرات البيئة)</h3>
    <div class="config-grid" id="configGrid"></div>
  </div>

  <!-- Report -->
  <div id="reportSection" class="hidden">
    <div class="grid grid-4" id="summaryMetrics" style="margin-bottom: 20px;"></div>

    <div class="card" style="margin-bottom: 20px;">
      <div class="tabs">
        <button class="tab active" onclick="switchTab(event, 'tabOverview')">📊 نظرة عامة</button>
        <button class="tab" onclick="switchTab(event, 'tabSymbols')">💎 حسب العملة</button>
        <button class="tab" onclick="switchTab(event, 'tabClass')">🏷️ حسب التصنيف</button>
        <button class="tab" onclick="switchTab(event, 'tabTrades')">📋 الصفقات</button>
        <button class="tab" onclick="switchTab(event, 'tabReport')">📄 التقرير النصي</button>
      </div>

      <div id="tabOverview" class="tab-content active">
        <div class="grid grid-2">
          <div>
            <h3>📈 منحنى الرصيد (Equity Curve)</h3>
            <canvas id="equityChart"></canvas>
          </div>
          <div>
            <h3>🎯 الأداء حسب العملة</h3>
            <canvas id="symbolChart"></canvas>
          </div>
        </div>
        <div style="margin-top: 20px;">
          <h3>🔄 توزيع نتائج الصفقات</h3>
          <canvas id="pnlDistribution" style="max-height: 220px;"></canvas>
        </div>
      </div>

      <div id="tabSymbols" class="tab-content">
        <div class="table-scroll">
          <table id="tblSymbols"></table>
        </div>
      </div>

      <div id="tabClass" class="tab-content">
        <div class="table-scroll">
          <table id="tblClass"></table>
        </div>
      </div>

      <div id="tabTrades" class="tab-content">
        <div style="margin-bottom: 10px;">
          <button class="btn sm secondary" onclick="downloadCurrentCSV()">
            ⬇️ تحميل CSV
          </button>
        </div>
        <div class="table-scroll">
          <table id="tblTrades"></table>
        </div>
      </div>

      <div id="tabReport" class="tab-content">
        <div class="log-box" style="max-height: 600px; font-size: 12px;" id="reportText"></div>
      </div>
    </div>
  </div>

  <!-- Runs history -->
  <div class="card">
    <h3>📚 التشغيلات السابقة (آخر 20)</h3>
    <div class="runs-list" id="runsList">
      <div class="empty-state">
        <div class="icon">📭</div>
        <div>لا توجد تشغيلات بعد</div>
      </div>
    </div>
  </div>

</div>

<div class="footer">
  Backtest Studio • Smart Analyst System • <span id="footerTime"></span>
</div>

<script>
const STATE_LABELS = {
  'idle': {text: 'جاهز', cls: 'badge-idle'},
  'running': {text: 'يعمل', cls: 'badge-running'},
  'done': {text: 'اكتمل', cls: 'badge-done'},
  'error': {text: 'خطأ', cls: 'badge-error'},
};

let pollTimer = null;
let charts = {};
let currentReport = null;

// ============================================================
// Utils
// ============================================================
function cls(v) { return v > 0 ? 'pos' : (v < 0 ? 'neg' : 'neu'); }
function fmt(v, d=2) { 
  if (v === null || v === undefined) return '—';
  return (v >= 0 ? '+' : '') + Number(v).toFixed(d);
}
function fmtUsd(v, d=2) { 
  if (v === null || v === undefined) return '—';
  return (v >= 0 ? '+$' : '-$') + Math.abs(Number(v)).toFixed(d);
}
function fmtNum(v, d=0) {
  if (v === null || v === undefined) return '—';
  return Number(v).toLocaleString(undefined, {maximumFractionDigits: d});
}
function nowStr() {
  const d = new Date();
  return d.toLocaleTimeString('ar-SY', {hour12: false});
}

// ============================================================
// Jobs
// ============================================================
async function startJob(job) {
  const map = {
    'collect': {btn: 'btnCollect', label: 'جمع الشموع'},
    'build': {btn: 'btnBuild', label: 'بناء المصفوفة'},
    'run': {btn: 'btnRun', label: 'تشغيل Backtest'},
  };
  const info = map[job];
  if (!confirm(`هل تريد ${info.label}؟`)) return;

  try {
    const r = await fetch(`/api/${job}`, {method: 'POST'});
    const j = await r.json();
    if (!j.ok) { alert(j.msg || 'فشل بدء العملية'); return; }
    startPolling();
  } catch (e) {
    alert('خطأ: ' + e);
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  document.getElementById('statusPanel').classList.add('show');
  pollStatus();
  pollTimer = setInterval(pollStatus, 2000);
}

async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    updateStatusUI(s);

    if (s.status === 'done' || s.status === 'error') {
      clearInterval(pollTimer);
      pollTimer = null;
      enableButtons(true);
      if (s.status === 'done' && s.result) {
        await loadReport();
        loadRuns();
      }
    }
  } catch (e) {
    console.warn(e);
  }
}

function updateStatusUI(s) {
  const panel = document.getElementById('statusPanel');
  panel.classList.add('show');

  const badge = document.getElementById('statusBadge');
  const ind = document.getElementById('stateIndicator');
  const info = STATE_LABELS[s.status] || STATE_LABELS.idle;

  badge.textContent = info.text;
  badge.className = 'status-badge ' + info.cls;
  ind.textContent = info.text;
  ind.className = 'status-badge ' + info.cls;

  const titles = {collect: '📥 جمع الشموع', build: '🔨 بناء المصفوفة', run: '🚀 تشغيل Backtest'};
  document.getElementById('statusTitle').textContent = titles[s.job] || 'جاهز';
  document.getElementById('statusTime').textContent = s.started_at ? 
    new Date(s.started_at).toLocaleString('ar-SY') : '—';

  document.getElementById('logBox').textContent = s.progress || '—';

  const fill = document.getElementById('progressFill');
  if (s.status === 'running') {
    fill.style.width = '70%';
  } else if (s.status === 'done') {
    fill.style.width = '100%';
    fill.style.background = 'var(--green)';
  } else if (s.status === 'error') {
    fill.style.width = '100%';
    fill.style.background = 'var(--red)';
  } else {
    fill.style.width = '0%';
  }

  enableButtons(s.status !== 'running');
}

function enableButtons(en) {
  ['btnCollect', 'btnBuild', 'btnRun'].forEach(id => {
    document.getElementById(id).disabled = !en;
  });
}

// ============================================================
// Report
// ============================================================
async function loadReport() {
  try {
    const r = await fetch('/api/report');
    if (!r.ok) return;
    const j = await r.json();
    if (!j.ok) return;
    currentReport = j;
    renderReport(j);
  } catch (e) { console.warn(e); }
}

function renderReport(rep) {
  document.getElementById('reportSection').classList.remove('hidden');
  const s = rep.summary;

  // Metrics cards
  const cards = [
    {label: 'P&L', value: fmtUsd(s.pnl), sub: fmt(s.pnl_pct, 2) + '%', cls: cls(s.pnl)},
    {label: 'Win Rate', value: s.win_rate.toFixed(1) + '%', sub: s.trades + ' صفقة', cls: s.win_rate >= 50 ? 'pos' : 'neg'},
    {label: 'Profit Factor', value: s.profit_factor.toFixed(2), sub: 'الأرباح/الخسائر', cls: s.profit_factor >= 1.3 ? 'pos' : (s.profit_factor >= 1 ? 'neu' : 'neg')},
    {label: 'Max Drawdown', value: s.max_dd.toFixed(2) + '%', sub: 'أقصى انخفاض', cls: s.max_dd < 15 ? 'pos' : (s.max_dd < 25 ? 'neu' : 'neg')},
    {label: 'Max Streak', value: s.max_streak, sub: 'خسائر متتالية', cls: s.max_streak <= 5 ? 'pos' : 'neg'},
    {label: 'أيام مُوقفة', value: s.killed_days, sub: `يومي:${s.killed_daily} | متتالٍ:${s.killed_consecutive}`, cls: s.killed_days < 20 ? 'pos' : 'neu'},
    {label: 'B&H Average', value: fmt(s.bh_avg, 2) + '%', sub: 'مقارنة مرجعية', cls: cls(s.bh_avg)},
    {label: 'الفارق vs B&H', value: fmt(s.pnl_pct - s.bh_avg, 2) + '%', sub: s.pnl_pct > s.bh_avg ? '✅ تجاوز' : '❌ دون المرجع', cls: cls(s.pnl_pct - s.bh_avg)},
  ];

  document.getElementById('summaryMetrics').innerHTML = cards.map(c => `
    <div class="metric">
      <div class="label">${c.label}</div>
      <div class="value ${c.cls}">${c.value}</div>
      <div class="sub">${c.sub}</div>
    </div>
  `).join('');

  // Tables
  renderTradesTable(rep.trades);
  renderSymbolsTable(rep.trades);
  renderClassTable(rep.trades);
  document.getElementById('reportText').textContent = rep.report_text;

  // Charts
  renderCharts(rep.trades, s);
}

function renderTradesTable(trades) {
  if (!trades || !trades.length) {
    document.getElementById('tblTrades').innerHTML = '<tr><td>لا توجد صفقات</td></tr>';
    return;
  }
  const head = `<thead><tr>
    <th>#</th><th>العملة</th><th>الاتجاه</th><th>الوقت</th>
    <th>الدخول</th><th>الخروج</th><th>السبب</th>
    <th>RR</th><th>P&L $</th><th>P&L %</th><th>مدة</th><th>تصنيف</th>
  </tr></thead><tbody>`;
  const rows = trades.slice(0, 500).map((t, i) => {
    const dt = t.time_utc ? new Date(t.time_utc).toLocaleString('ar-SY', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
    }) : '—';
    const reasonColor = t.exit_reason === 'TP' ? 'pos' : (t.exit_reason === 'SL' ? 'neg' : 'neu');
    return `<tr>
      <td>${i+1}</td>
      <td><b>${t.symbol.replace('USDT','')}</b></td>
      <td class="${t.direction === 'LONG' ? 'pos' : 'neg'}">${t.direction === 'LONG' ? '🟢' : '🔴'} ${t.direction}</td>
      <td>${dt}</td>
      <td>${Number(t.entry).toFixed(4)}</td>
      <td>${Number(t.exit).toFixed(4)}</td>
      <td class="${reasonColor}">${t.exit_reason}</td>
      <td>${Number(t.rr).toFixed(2)}</td>
      <td class="${cls(t.pnl_usd)}">${fmtUsd(t.pnl_usd)}</td>
      <td class="${cls(t.pnl_pct)}">${fmt(t.pnl_pct, 3)}%</td>
      <td>${t.hold_candles}</td>
      <td>${t.classification || '—'}</td>
    </tr>`;
  }).join('');
  document.getElementById('tblTrades').innerHTML = head + rows + '</tbody>';
}

function renderSymbolsTable(trades) {
  const by = {};
  trades.forEach(t => {
    const s = t.symbol;
    if (!by[s]) by[s] = {n: 0, wins: 0, pnl: 0, pnl_pct: 0};
    by[s].n++;
    if (t.pnl_usd > 0) by[s].wins++;
    by[s].pnl += t.pnl_usd;
    by[s].pnl_pct += t.pnl_pct;
  });
  const head = `<thead><tr>
    <th>العملة</th><th>صفقات</th><th>Win Rate</th>
    <th>P&L $</th><th>متوسط P&L%</th>
  </tr></thead><tbody>`;
  const rows = Object.entries(by)
    .sort((a,b) => b[1].pnl - a[1].pnl)
    .map(([sym, d]) => {
      const wr = d.wins / d.n * 100;
      return `<tr>
        <td><b>${sym.replace('USDT','')}</b></td>
        <td>${d.n}</td>
        <td class="${wr >= 50 ? 'pos' : 'neg'}">${wr.toFixed(1)}%</td>
        <td class="${cls(d.pnl)}">${fmtUsd(d.pnl)}</td>
        <td class="${cls(d.pnl_pct)}">${fmt(d.pnl_pct / d.n, 3)}%</td>
      </tr>`;
    }).join('');
  document.getElementById('tblSymbols').innerHTML = head + rows + '</tbody>';
}

function renderClassTable(trades) {
  const by = {};
  trades.forEach(t => {
    const c = t.classification || 'unknown';
    if (!by[c]) by[c] = {n: 0, wins: 0, pnl: 0, pnl_pct: 0};
    by[c].n++;
    if (t.pnl_usd > 0) by[c].wins++;
    by[c].pnl += t.pnl_usd;
    by[c].pnl_pct += t.pnl_pct;
  });
  const head = `<thead><tr>
    <th>التصنيف</th><th>صفقات</th><th>Win Rate</th>
    <th>P&L $</th><th>متوسط P&L%</th>
  </tr></thead><tbody>`;
  const rows = Object.entries(by).map(([c, d]) => {
    const wr = d.wins / d.n * 100;
    const label = c === 'calm' ? '🟢 هادئة' : (c === 'volatile' ? '🔴 متقلبة' : '🟡 متوسطة');
    return `<tr>
      <td>${label}</td>
      <td>${d.n}</td>
      <td class="${wr >= 50 ? 'pos' : 'neg'}">${wr.toFixed(1)}%</td>
      <td class="${cls(d.pnl)}">${fmtUsd(d.pnl)}</td>
      <td class="${cls(d.pnl_pct)}">${fmt(d.pnl_pct / d.n, 3)}%</td>
    </tr>`;
  }).join('');
  document.getElementById('tblClass').innerHTML = head + rows + '</tbody>';
}

// ============================================================
// Charts
// ============================================================
function renderCharts(trades, summary) {
  // Destroy old
  for (const k in charts) { try { charts[k].destroy(); } catch(e) {} }
  charts = {};

  // Equity Curve
  let equity = CFG_START || 1000;
  const eqData = [equity];
  const eqLabels = ['بداية'];
  trades.forEach((t, i) => {
    equity += t.pnl_usd;
    eqData.push(equity);
    eqLabels.push(i + 1);
  });

  const eqCtx = document.getElementById('equityChart');
  if (eqCtx) {
    charts.equity = new Chart(eqCtx, {
      type: 'line',
      data: {
        labels: eqLabels,
        datasets: [{
          label: 'الرصيد ($)',
          data: eqData,
          borderColor: '#58a6ff',
          backgroundColor: 'rgba(88,166,255,0.1)',
          fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2,
        }]
      },
      options: {
        responsive: true,
        plugins: {legend: {labels: {color: '#e6edf3'}}},
        scales: {
          x: {ticks: {color: '#7d8590', maxTicksLimit: 10}, grid: {color: '#232a35'}},
          y: {ticks: {color: '#7d8590'}, grid: {color: '#232a35'}},
        }
      }
    });
  }

  // Symbols Chart
  const by = {};
  trades.forEach(t => {
    if (!by[t.symbol]) by[t.symbol] = 0;
    by[t.symbol] += t.pnl_usd;
  });
  const symbols = Object.keys(by).sort((a,b) => by[b] - by[a]);
  const symCtx = document.getElementById('symbolChart');
  if (symCtx) {
    charts.symbols = new Chart(symCtx, {
      type: 'bar',
      data: {
        labels: symbols.map(s => s.replace('USDT','')),
        datasets: [{
          label: 'P&L ($)',
          data: symbols.map(s => by[s]),
          backgroundColor: symbols.map(s => by[s] >= 0 ? '#3fb950' : '#f85149'),
        }]
      },
      options: {
        responsive: true,
        plugins: {legend: {display: false}},
        scales: {
          x: {ticks: {color: '#7d8590'}, grid: {display: false}},
          y: {ticks: {color: '#7d8590'}, grid: {color: '#232a35'}},
        }
      }
    });
  }

  // P&L Distribution
  const buckets = {};
  const step = 0.5;
  trades.forEach(t => {
    const b = Math.floor(t.pnl_pct / step) * step;
    const k = b.toFixed(1);
    buckets[k] = (buckets[k] || 0) + 1;
  });
  const sortedKeys = Object.keys(buckets).sort((a,b) => parseFloat(a) - parseFloat(b));
  const distCtx = document.getElementById('pnlDistribution');
  if (distCtx) {
    charts.dist = new Chart(distCtx, {
      type: 'bar',
      data: {
        labels: sortedKeys.map(k => k + '%'),
        datasets: [{
          data: sortedKeys.map(k => buckets[k]),
          backgroundColor: sortedKeys.map(k => parseFloat(k) >= 0 ? '#3fb950' : '#f85149'),
        }]
      },
      options: {
        responsive: true,
        plugins: {legend: {display: false}},
        scales: {
          x: {ticks: {color: '#7d8590'}, grid: {display: false}},
          y: {ticks: {color: '#7d8590'}, grid: {color: '#232a35'}},
        }
      }
    });
  }
}

let CFG_START = 1000;

// ============================================================
// Runs History
// ============================================================
async function loadRuns() {
  try {
    const r = await fetch('/api/runs');
    const j = await r.json();
    if (!j.ok) return;
    const runs = j.runs || [];
    const el = document.getElementById('runsList');
    if (!runs.length) {
      el.innerHTML = '<div class="empty-state"><div class="icon">📭</div><div>لا توجد تشغيلات بعد</div></div>';
      return;
    }
    el.innerHTML = runs.map(run => {
      const s = run.summary_json || {};
      const date = run.started_at ? new Date(run.started_at).toLocaleString('ar-SY') : '—';
      const pnl = s.pnl != null ? fmtUsd(s.pnl) : '—';
      const pnlCls = s.pnl != null ? cls(s.pnl) : 'neu';
      const status = run.status === 'done' ? '✅' : (run.status === 'error' ? '❌' : '⏳');
      return `<div class="run-item" onclick="openRun(${run.id})">
        <div class="info">
          <span class="id">#${run.id}</span>
          <span>${status}</span>
          <span class="date">${date}</span>
        </div>
        <div class="stats">
          ${run.trades_count || 0} صفقة | 
          WR: ${s.win_rate ? s.win_rate.toFixed(1)+'%' : '—'} |
          P&L: <span class="${pnlCls}">${pnl}</span>
        </div>
        <button class="btn sm secondary" onclick="event.stopPropagation(); downloadRun(${run.id})">
          ⬇️
        </button>
      </div>`;
    }).join('');
  } catch (e) { console.warn(e); }
}

function downloadRun(runId) {
  window.location.href = `/api/run/${runId}/trades`;
}

async function openRun(runId) {
  try {
    const r = await fetch(`/api/run/${runId}`);
    const j = await r.json();
    if (!j.ok) { alert('فشل التحميل'); return; }
    const run = j.run;
    if (run.report_text) {
      alert(run.report_text);
    }
  } catch (e) { console.warn(e); }
}

// ============================================================
// Cache Status
// ============================================================
async function loadCacheStatus() {
  try {
    const r = await fetch('/api/cache_status');
    const j = await r.json();
    const el = document.getElementById('cacheStatus');
    const entries = Object.entries(j);
    if (!entries.length) {
      el.innerHTML = '<div class="empty-state" style="grid-column:1/-1;">لا توجد بيانات</div>';
      return;
    }
    el.innerHTML = entries.map(([sym, info]) => {
      const ready = info.ready;
      const count = info.count || 0;
      const cls = ready ? 'pos' : 'neg';
      const icon = ready ? '✅' : '⚠️';
      return `<div class="metric">
        <div class="label">${sym.replace('USDT','')}</div>
        <div class="value ${cls}" style="font-size:14px;">${icon} ${fmtNum(count)}</div>
        <div class="sub">${ready ? 'جاهز' : 'يحتاج جمع'}</div>
      </div>`;
    }).join('');
  } catch (e) { console.warn(e); }
}

// ============================================================
// Config
// ============================================================
async function showConfig() {
  const card = document.getElementById('configCard');
  if (!card.classList.contains('hidden')) {
    card.classList.add('hidden');
    return;
  }
  try {
    const r = await fetch('/api/config');
    const j = await r.json();
    CFG_START = j.START_BALANCE || 1000;
    const grid = document.getElementById('configGrid');
    grid.innerHTML = Object.entries(j).map(([k, v]) => {
      const val = Array.isArray(v) ? v.join(', ') : v;
      return `<div class="config-item"><span class="key">${k}</span> = <span class="val">${val}</span></div>`;
    }).join('');
    card.classList.remove('hidden');
  } catch (e) { console.warn(e); }
}

// ============================================================
// Tabs
// ============================================================
function switchTab(ev, tabId) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  ev.target.classList.add('active');
  document.getElementById(tabId).classList.add('active');
}

function downloadCurrentCSV() {
  window.location.href = '/api/current_report_csv';
}

// ============================================================
// Init
// ============================================================
window.addEventListener('load', () => {
  document.getElementById('footerTime').textContent = new Date().toLocaleString('ar-SY');
  loadCacheStatus();
  loadRuns();

  // فحص الحالة عند الدخول (لأن العملية قد تكون قيد التشغيل)
  fetch('/api/status').then(r => r.json()).then(s => {
    if (s.status === 'running') {
      startPolling();
    } else if (s.status === 'done' && s.result) {
      loadReport();
    }
  });

  // تحديث كل 30 ثانية
  setInterval(() => {
    if (!pollTimer) {
      loadRuns();
    }
  }, 30000);
});

// فحص Cache كل دقيقة
setInterval(loadCacheStatus, 60000);
</script>

</body>
</html>
"""


# ============================================================
# Main
# ============================================================
if __name__ == '__main__':
    # نطبع الإعدادات عند البدء
    print("=" * 60)
    print("🎯 Backtest Studio - Smart Analyst")
    print("=" * 60)
    print_config()
    print("=" * 60)

    port = int(os.getenv("PORT", os.getenv("COLLECTOR_PORT", "5001")))
    print(f"\n🌐 Starting on port {port}")
    print(f"   Health:   http://0.0.0.0:{port}/api/health")
    print(f"   UI:       http://0.0.0.0:{port}/")

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
