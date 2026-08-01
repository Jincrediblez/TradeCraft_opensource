#!/usr/bin/env python3
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests

# Ensure app/ is on sys.path for 'from app.xxx' imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ibkr_parser
from app.ibkr_parser import parse_activity_statement_snapshot, safe_float
from app.settings import load_settings, save_settings
from app.trade_plans import SETUP_TYPES, get_trade_plan, save_trade_plan
from app.watchlist_triggers import load_triggers as load_watchlist_triggers, save_triggers as save_watchlist_triggers
from app.models import KlineResult, OtherPnlSnapshot, SymbolSnapshot, Trade
from app import state_store
from app import kline_cache
from app import watchlist_manager
from app.symbols import normalize_symbol
from app.schemas import trades_from_rows
from app.i18n import translate


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT
CACHE_DIR = ROOT / "cache"
DATA_ROOT = ROOT / "data"
INBOX_DIR = DATA_ROOT / "inbox"
HISTORICAL_INBOX_DIR = DATA_ROOT / "historical_inbox"
ARCHIVE_DIR = DATA_ROOT / "archive"
STATE_DIR = DATA_ROOT / "state"
PERIODS_STATE_DIR = STATE_DIR / "periods"
KLINE_CACHE_DIR = CACHE_DIR / "kline"
STATIC_DIR = ROOT / "static"
LOG_DIR = ROOT / "logs"
APP_LOG_FILE = LOG_DIR / "app.log"
TRADES_STATE_FILE = STATE_DIR / "trades.json"
TRADES_ALL_STATE_FILE = STATE_DIR / "trades_all.json"
MTM_STATE_FILE = STATE_DIR / "mtm.json"
MANIFEST_STATE_FILE = STATE_DIR / "manifest.json"
STATEMENT_SNAPSHOT_FILE = STATE_DIR / "statement_snapshot.json"
IBKR_OPEN_POSITIONS_FILE = STATE_DIR / "ibkr_open_positions.json"
POSITION_RECONCILIATION_FILE = STATE_DIR / "position_reconciliation.json"
STATE_METADATA_FILE = STATE_DIR / "_metadata.json"
RECENT_SYMBOLS_FILE = STATE_DIR / "recent_symbols.json"
SYMBOL_NAMES_FILE = STATE_DIR / "symbol_names.json"
KLINE_MANIFEST_FILE = KLINE_CACHE_DIR / "_manifest.json"
AUDIT_PERIOD_OUTPUT_FILES = [
    "closed_trades.json",
    "open_positions.json",
    "audit_coverage.json",
    "theme_attribution.json",
    "audit_scores.json",
    "audit_summary.json",
]
AUDIT_ROOT_FILES = ["trades.json", "mtm.json", *AUDIT_PERIOD_OUTPUT_FILES]
AUDIT_PIPELINE_LOCK = threading.Lock()

KLINE_ADJUST_MODE = "yahoo"
KLINE_RECONCILE_DAYS = 7
INTRADAY_REFRESH_SECONDS = 300
YAHOO_TIMEOUT_SECONDS = int(os.environ.get("YAHOO_TIMEOUT_SECONDS", "20"))
YAHOO_NAME_CHART_FALLBACK_LIMIT = int(os.environ.get("YAHOO_NAME_CHART_FALLBACK_LIMIT", "20"))
YAHOO_ERROR_RETRY_SECONDS = int(os.environ.get("YAHOO_ERROR_RETRY_SECONDS", "900"))

MARKET_CLOSE_CONFIGS = {
    "US": {"tz": "America/New_York", "close_hour": 16, "close_minute": 0, "label": "US stocks"},
    "HK": {"tz": "Asia/Hong_Kong", "close_hour": 16, "close_minute": 10, "label": "Hong Kong stocks"},
}

DEFAULT_SYMBOL = "INTC.US"
CURRENT_YEAR = datetime.now().year
CURRENT_PERIOD = f"{CURRENT_YEAR}YTD"
MA_WINDOWS = [5, 10, 20, 60, 120, 200, 850]
REPLAY_PAYLOAD_CACHE: Dict[Tuple, dict] = {}
REFRESH_STATE = {
    "running": False,
    "progress": 0,
    "total": 0,
    "step": "",
    "message": "",
    "result": None,
    "error": None,
}
REFRESH_LOCK = threading.Lock()


def safe_float(value: str) -> float:
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return 0.0


def _avg(values: List[float]) -> float:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def configure_logging() -> None:
    state_store.configure_logging(APP_LOG_FILE)


def app_log(message: str, level: str = "info", **fields) -> None:
    state_store.app_log(message, level=level, **fields)


def ensure_data_dirs() -> None:
    for path in (INBOX_DIR, HISTORICAL_INBOX_DIR, ARCHIVE_DIR, STATE_DIR, PERIODS_STATE_DIR, KLINE_CACHE_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def demo_mode_active() -> bool:
    from app.demo_data import demo_status
    return bool(demo_status(ROOT).get("active"))


def _loadable_trade_rows(rows) -> List[dict]:
    allow_demo = demo_mode_active()
    return [
        row
        for row in rows
        if isinstance(row, dict) and (allow_demo or row.get("source") != "demo")
    ]


def read_json(path: Path, default):
    return state_store.read_json(path, default)


def atomic_write_text(path: Path, content: str) -> None:
    state_store.atomic_write_text(path, content)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    state_store.atomic_write_bytes(path, content)


def payload_record_count(payload) -> Optional[int]:
    return state_store.payload_record_count(payload)


def state_metadata_entry(path: Path, payload) -> Optional[dict]:
    return state_store.state_metadata_entry(path, payload, root=ROOT)


def update_state_metadata(path: Path, payload) -> None:
    state_store.update_state_metadata(path, payload, root=ROOT, metadata_file=STATE_METADATA_FILE)


def write_json(path: Path, payload) -> None:
    state_store.write_json(path, payload, root=ROOT, metadata_file=STATE_METADATA_FILE)


def relative_path_text(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def git_runtime_info() -> dict:
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        ).stdout.strip())
        return {"branch": branch, "commit": commit, "dirty": dirty}
    except Exception as exc:
        return {"error": str(exc)}


def runtime_info() -> dict:
    return {
        "cwd": str(Path.cwd()),
        "root": str(ROOT),
        "python": sys.executable,
        "port": int(os.environ.get("PORT", "8888")),
    }


def normalize_period(period: str = "") -> str:
    raw = (period or "").strip().upper()
    if not raw:
        raw = str(load_settings().get("defaultPeriod") or "").strip().upper()
    if not raw or raw in {"CURRENT", "YTD"}:
        return CURRENT_PERIOD
    if raw == "ALL":
        return "ALL"
    if raw.endswith("YTD") and raw[:-3].isdigit():
        return raw
    if raw.isdigit() and len(raw) == 4:
        return raw
    return CURRENT_PERIOD


def period_state_dir(period: str) -> Path:
    return PERIODS_STATE_DIR / normalize_period(period)


def period_state_file(period: str, name: str) -> Path:
    return period_state_dir(period) / name


def period_sort_key(period: str) -> Tuple[int, int]:
    p = normalize_period(period)
    if p == "ALL":
        return (99999, 2)
    year = int(p[:4]) if p[:4].isdigit() else 0
    return (year, 1 if p.endswith("YTD") else 0)


def list_available_periods() -> List[str]:
    periods = {CURRENT_PERIOD}
    if PERIODS_STATE_DIR.exists():
        for path in PERIODS_STATE_DIR.iterdir():
            if path.is_dir():
                periods.add(normalize_period(path.name))
    if TRADES_ALL_STATE_FILE.exists():
        periods.add("ALL")
    return sorted(periods, key=period_sort_key, reverse=True)


def trades_file_for_period(period: str) -> Path:
    p = normalize_period(period)
    if p == "ALL":
        return TRADES_ALL_STATE_FILE if TRADES_ALL_STATE_FILE.exists() else TRADES_STATE_FILE
    candidate = period_state_file(p, "trades.json")
    if candidate.exists():
        return candidate
    return TRADES_STATE_FILE if p == CURRENT_PERIOD else candidate


def mtm_file_for_period(period: str) -> Path:
    p = normalize_period(period)
    candidate = period_state_file(p, "mtm.json")
    if candidate.exists():
        return candidate
    return MTM_STATE_FILE if p == CURRENT_PERIOD else candidate


def period_label(period: str) -> str:
    p = normalize_period(period)
    if p == "ALL":
        return "All"
    return p


def _copy_file_if_exists(src: Path, dst: Path) -> None:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _snapshot_state_files(names: List[str]) -> Tuple[Path, Dict[str, Optional[Path]]]:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    backup_dir = STATE_DIR / ".audit_backup" / stamp
    backup: Dict[str, Optional[Path]] = {}
    for name in names:
        src = STATE_DIR / name
        if src.exists():
            dst = backup_dir / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            backup[name] = dst
        else:
            backup[name] = None
    return backup_dir, backup


def _restore_state_files(backup: Dict[str, Optional[Path]]) -> None:
    for name, backup_path in backup.items():
        target = STATE_DIR / name
        if backup_path and backup_path.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_path, target)
        elif target.exists():
            target.unlink()


def _write_period_audit_outputs(period: str) -> None:
    for name in AUDIT_PERIOD_OUTPUT_FILES:
        _copy_file_if_exists(STATE_DIR / name, period_state_file(period, name))


def run_audit_pipeline_for_period(period: str) -> dict:
    # Historical runs temporarily project a period into the legacy root-state
    # pipeline. Serialize the whole transaction so concurrent API requests can
    # never observe or overwrite another period's working files.
    with AUDIT_PIPELINE_LOCK:
        return _run_audit_pipeline_for_period_unlocked(period)


def _run_audit_pipeline_for_period_unlocked(period: str) -> dict:
    p = normalize_period(period)
    if p == "ALL":
        raise ValueError("AI audit generation is unavailable for the All period; select a year or YTD.")

    trades_path = trades_file_for_period(p)
    mtm_path = mtm_file_for_period(p)
    if not trades_path.exists():
        raise FileNotFoundError(f"{p} is missing trades.json")
    if not mtm_path.exists():
        raise FileNotFoundError(f"{p} is missing mtm.json")

    from app.position_matcher import run_matching
    from app.theme_classifier import run_theme_attribution
    from app.scoring_engine import run_scoring

    # Start the run with a clean K-line cache so we never score against stale data.
    kline_cache.clear()

    if p == CURRENT_PERIOD:
        match_result = run_matching()
        attr_result = run_theme_attribution()
        scoring_result = run_scoring()
        summary = read_json(STATE_DIR / "audit_summary.json", {})
        if summary:
            summary["period"] = p
            summary["periodLabel"] = period_label(p)
            if isinstance(summary.get("behavior_radar"), dict):
                summary["behavior_radar"]["period"] = p
            write_json(STATE_DIR / "audit_summary.json", summary)
            write_json(period_state_file(p, "audit_summary.json"), summary)
        _write_period_audit_outputs(p)
        return {"match": match_result, "attribution": attr_result, "scoring": scoring_result, "summary": summary}

    backup_dir, backup = _snapshot_state_files(AUDIT_ROOT_FILES)
    try:
        shutil.copy2(trades_path, STATE_DIR / "trades.json")
        shutil.copy2(mtm_path, STATE_DIR / "mtm.json")
        for name in AUDIT_PERIOD_OUTPUT_FILES:
            path = STATE_DIR / name
            if path.exists():
                path.unlink()

        match_result = run_matching()
        attr_result = run_theme_attribution()
        scoring_result = run_scoring()
        summary = read_json(STATE_DIR / "audit_summary.json", {})
        if summary:
            summary["period"] = p
            summary["periodLabel"] = period_label(p)
            if isinstance(summary.get("behavior_radar"), dict):
                summary["behavior_radar"]["period"] = p
            write_json(STATE_DIR / "audit_summary.json", summary)
        _write_period_audit_outputs(p)
        return {"match": match_result, "attribution": attr_result, "scoring": scoring_result, "summary": summary}
    finally:
        _restore_state_files(backup)
        shutil.rmtree(backup_dir, ignore_errors=True)


def build_all_period_audit_summary() -> dict:
    from app.scoring_engine import compute_behavior_radar, compute_period_metrics

    trades = [asdict(t) for t in load_period_trades("ALL")]
    closed = []
    for period in list_available_periods():
        if period == "ALL":
            continue
        path = period_state_file(period, "closed_trades.json")
        if path.exists():
            closed.extend(read_json(path, []))
    metrics = compute_period_metrics(trades, closed) if trades else {}
    radar = compute_behavior_radar(trades, closed, metrics, "ALL")
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": "ALL",
        "periodLabel": period_label("ALL"),
        "scores": {},
        "style_drift": {},
        "metrics": metrics,
        "behavior_radar": radar,
        "alerts": [],
        "evidence": {},
        "theme_attribution": [],
        "audit_coverage": {},
        "ytd_mtm_contribution": load_period_mtm_summary("ALL"),
        "mtm_rankings": {},
    }


def build_audit_workbench_for_period(period: str, persist: bool = True) -> dict:
    """Build the deterministic, versioned audit workbench for one period."""
    from app.audit_workbench import (
        REPORT_META_FILE,
        build_workbench,
        evaluate_rule_cycles,
        load_round_trip_notes,
        save_snapshot,
    )
    from app.kline_cache import get_kline

    p = normalize_period(period)
    if p == "ALL":
        summary = build_all_period_audit_summary()
        trades = [asdict(trade) for trade in load_period_trades("ALL")]
        closed_trades = []
        for data_period in _available_data_periods():
            closed_path = period_state_file(data_period, "closed_trades.json")
            closed_trades.extend(read_json(closed_path, []))
        statement_snapshot = read_json(
            period_state_file(CURRENT_PERIOD, "statement_snapshot.json"),
            read_json(STATE_DIR / "statement_snapshot.json", {}),
        )
    else:
        summary_path = period_state_file(p, "audit_summary.json")
        if not summary_path.exists() and p == CURRENT_PERIOD:
            summary_path = STATE_DIR / "audit_summary.json"
        summary = read_json(summary_path, {})
        if not summary:
            raise FileNotFoundError(f"{p} is missing audit_summary.json; refresh audit data first")
        summary["period"] = p
        summary["periodLabel"] = period_label(p)
        trades_path = trades_file_for_period(p)
        trades = read_json(trades_path, []) if trades_path.exists() else [asdict(trade) for trade in load_period_trades(p)]
        closed_path = period_state_file(p, "closed_trades.json")
        if not closed_path.exists() and p == CURRENT_PERIOD:
            closed_path = STATE_DIR / "closed_trades.json"
        closed_trades = read_json(closed_path, [])
        statement_path = period_state_file(p, "statement_snapshot.json")
        if not statement_path.exists() and p == CURRENT_PERIOD:
            statement_path = STATE_DIR / "statement_snapshot.json"
        statement_snapshot = read_json(statement_path, {})

    period_dir = period_state_dir(p)
    report_path = period_dir / "audit_report.md"
    if not report_path.exists() and p == CURRENT_PERIOD:
        report_path = STATE_DIR / "audit_report.md"
    report_meta_path = period_dir / REPORT_META_FILE
    notes = load_round_trip_notes(STATE_DIR)
    from app.audit_workbench import ROUND_TRIPS_FILE
    previous_round_trips = read_json(period_dir / ROUND_TRIPS_FILE, {})
    for old_id, new_id in (previous_round_trips.get("aliases", {}) or {}).items():
        if old_id in notes and new_id not in notes:
            notes[new_id] = dict(notes[old_id], roundTripId=new_id, migratedFrom=old_id)
    snapshot, round_trips, executions = build_workbench(
        period=p,
        summary=summary,
        trades=trades,
        closed_trades=closed_trades,
        statement_snapshot=statement_snapshot,
        settings=load_settings(),
        report_path=report_path,
        report_meta_path=report_meta_path,
        kline_loader=get_kline,
        notes=notes,
    )
    review_state = evaluate_rule_cycles(STATE_DIR, snapshot, persist=persist)
    snapshot["feedback"] = {
        key: value
        for key, value in review_state.get("feedback", {}).items()
        if value.get("period") == p
    }
    snapshot["ruleCycles"] = [
        cycle for cycle in review_state.get("ruleCycles", [])
        if cycle.get("period") == p
    ]
    for finding in snapshot.get("findings", []):
        feedback = snapshot["feedback"].get(f"{p}:{finding.get('findingId')}")
        if feedback:
            finding["userDecision"] = feedback.get("decision", "undecided")
            finding["userNote"] = feedback.get("note", "")
    if persist:
        save_snapshot(period_dir, snapshot, round_trips, executions)
    return snapshot


def generate_audit_report_for_period(period: str, force: bool = False) -> Tuple[str, bool, bool]:
    """Returns (report_path, cached, stale).

    ``stale`` is True only when the Kimi call failed and we fell back to a
    previously generated report — so the UI can warn the user it isn't fresh.
    """
    p = normalize_period(period)
    report_path = period_state_file(p, "audit_report.md")
    today_report_path = period_state_file(
        p, f"audit_report_{datetime.now().strftime('%Y-%m-%d')}.md"
    )

    summary_path = period_state_file(p, "audit_summary.json")
    if not summary_path.exists():
        pipeline = run_audit_pipeline_for_period(p)
        summary = pipeline.get("summary", {})
    else:
        summary = read_json(summary_path, {})

    if not summary:
        raise RuntimeError(f"The {p} audit context is empty; an AI audit cannot be generated.")

    from app.audit_workbench import REPORT_META_FILE, WORKBENCH_FILE

    summary_hash = hashlib.sha256(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    report_meta_path = period_state_file(p, REPORT_META_FILE)
    report_meta = read_json(report_meta_path, {})
    workbench = read_json(period_state_file(p, WORKBENCH_FILE), {})
    snapshot_id = str(workbench.get("snapshotId") or "")

    if (
        not force
        and report_path.exists()
        and report_meta.get("summaryHash") == summary_hash
        and report_meta.get("language") == "en"
    ):
        if not snapshot_id or report_meta.get("snapshotId") == snapshot_id:
            app_log("audit report cache hit", period=p, report=str(report_path.relative_to(ROOT)))
            return str(report_path), True, False

    from app.llm_reporter import (
        build_audit_context,
        call_kimi_api_with_error,
        cap_context_json,
        get_api_key,
        load_prompt,
    )

    api_key = get_api_key()
    if not api_key:
        app_log("audit report missing kimi key", level="warning", period=p)
        raise RuntimeError("KIMI_API_KEY is not configured; an AI trading-quality report cannot be generated.")

    audit_context = build_audit_context(summary)
    audit_context["period"] = p
    audit_context["period_label"] = period_label(p)
    if workbench:
        audit_context["snapshot_id"] = workbench.get("snapshotId", "")
        audit_context["workbench"] = {
            "scorecards": workbench.get("scorecards", {}),
            "outcome": workbench.get("outcome", {}),
            "data_quality": workbench.get("dataQuality", {}),
            "priority_findings": (workbench.get("findings") or [])[:10],
            "advantages": (workbench.get("advantages") or [])[:5],
            "behavior_comparison": workbench.get("behavior", {}),
        }
    audit_json_str = cap_context_json(json.dumps(audit_context, ensure_ascii=False, indent=2))
    prompt = load_prompt().replace("{{AUDIT_CONTEXT}}", audit_json_str).replace("{{AUDIT_JSON}}", audit_json_str)
    content, kimi_error = call_kimi_api_with_error(prompt, api_key)
    if content is None:
        app_log("audit report kimi failed", level="warning", period=p, error=kimi_error)
        if report_path.exists():
            # Kimi failed — serve the previous report but flag it as stale.
            return str(report_path), True, True
        raise RuntimeError(
            f"Kimi API call failed and no AI trading-quality report was generated. "
            f"{kimi_error or 'Check the API key, account status, or network connection.'}"
        )
    if re.search(r"\bX\b|\{\{|TODO|TBD", content):
        app_log("audit report rejected placeholders", level="warning", period=p)
        if report_path.exists():
            return str(report_path), True, True
        raise RuntimeError("The AI summary contains unfinished placeholders and was not saved.")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(report_path, content)
    atomic_write_text(today_report_path, content)
    write_json(
        report_meta_path,
        {
            "schemaVersion": 3,
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "period": p,
            "language": "en",
            "snapshotId": snapshot_id,
            "summaryHash": summary_hash,
            "reportPath": str(report_path.relative_to(ROOT)),
        },
    )
    app_log("audit report generated", period=p, report=str(today_report_path.relative_to(ROOT)), chars=len(content))
    return str(today_report_path), False, False


def generate_demo_audit_report_for_period(period: str, snapshot: Optional[dict] = None) -> Tuple[str, bool, bool]:
    """Generate a clearly labeled offline AI-style report from synthetic data."""

    from app.audit_workbench import REPORT_META_FILE
    from app.demo_data import render_demo_audit_report
    p = normalize_period(period)
    workbench = snapshot or build_audit_workbench_for_period(p, persist=True)
    report_path = period_state_file(p, "audit_report.md")
    dated_path = period_state_file(
        p, f"audit_report_{datetime.now().strftime('%Y-%m-%d')}.md"
    )
    content = render_demo_audit_report(workbench)
    atomic_write_text(report_path, content)
    atomic_write_text(dated_path, content)
    write_json(
        period_state_file(p, REPORT_META_FILE),
        {
            "schemaVersion": 3,
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "period": p,
            "language": "en",
            "snapshotId": str(workbench.get("snapshotId") or ""),
            "summaryHash": str(workbench.get("sourceDigest") or ""),
            "reportPath": str(report_path.relative_to(ROOT)),
            "demo": True,
            "synthetic": True,
            "generationMode": "offline-demo-template",
        },
    )
    app_log(
        "demo audit report generated",
        period=p,
        report=str(report_path.relative_to(ROOT)),
    )
    return str(report_path), False, False


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidate_data_files(pattern: str) -> List[Path]:
    paths = list(INBOX_DIR.glob(pattern)) if INBOX_DIR.exists() else []
    paths.extend(DATA_DIR.glob(pattern))
    return sorted(set(paths), key=lambda p: str(p))


def trade_key(trade: Trade) -> str:
    parts = [
        trade.symbol_lb,
        trade.side,
        f"{trade.quantity:.8f}",
        f"{trade.price:.8f}",
        f"{trade.gross_amount:.8f}",
        trade.trade_date,
        trade.trade_time,
        trade.exchange,
        trade.currency,
        trade.source,
    ]
    return "|".join(parts)


def clear_runtime_caches() -> None:
    for fn in (
        load_ibkr_trades,
        load_mtm_summary,
        load_other_mtm_summary,
        get_report_end_date,
        load_period_trades,
        load_period_mtm_summary,
        load_period_other_mtm_summary,
        build_overview_payload,
    ):
        fn.cache_clear()
    clear_replay_payload_cache()
    kline_cache.clear()


def clear_replay_payload_cache() -> None:
    REPLAY_PAYLOAD_CACHE.clear()


def invalidate_market_data_caches(symbol: Optional[str] = None) -> None:
    """Drop replay/K-line memory caches after market data changes."""
    if symbol:
        symbol_lb = normalize_symbol(symbol)
        for key in list(REPLAY_PAYLOAD_CACHE.keys()):
            if key and key[0] == symbol_lb:
                REPLAY_PAYLOAD_CACHE.pop(key, None)
    else:
        clear_replay_payload_cache()
    kline_cache.clear()


def path_mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def replay_cache_key(symbol_input: str) -> Tuple:
    symbol_lb = normalize_symbol(symbol_input)
    return (
        symbol_lb,
        today_text(),
        path_mtime(TRADES_STATE_FILE),
        path_mtime(MTM_STATE_FILE),
        path_mtime(symbol_kline_cache_file(symbol_lb)),
        path_mtime(KLINE_MANIFEST_FILE),
    )


def parse_period_end(period_text: str) -> Optional[datetime.date]:
    if " - " not in period_text:
        return None
    try:
        return datetime.strptime(period_text.split(" - ", 1)[1].strip(), "%B %d, %Y").date()
    except Exception:
        return None


def parse_period_start(period_text: str) -> Optional[datetime.date]:
    if " - " not in period_text:
        return None
    try:
        return datetime.strptime(period_text.split(" - ", 1)[0].strip(), "%B %d, %Y").date()
    except Exception:
        return None


def historical_year_from_statement_period(period_text: str) -> Optional[str]:
    start = parse_period_start(period_text or "")
    end = parse_period_end(period_text or "")
    if not start or not end:
        return None
    if start.year == end.year and end.year < CURRENT_YEAR:
        return str(end.year)
    return None


def parse_multipart(body: bytes, boundary: str) -> List[dict]:
    boundary_bytes = b"--" + boundary.encode()
    parts = []
    segments = body.split(boundary_bytes)
    for segment in segments[1:]:
        if not segment or segment == b"--\r\n" or segment == b"--\n":
            break
        if segment.startswith(b"\r\n"):
            segment = segment[2:]
        elif segment.startswith(b"\n"):
            segment = segment[1:]
        header_end = segment.find(b"\r\n\r\n")
        if header_end == -1:
            header_end = segment.find(b"\n\n")
            if header_end != -1:
                headers_bytes = segment[: header_end + 2]
                payload = segment[header_end + 4 :]
            else:
                continue
        else:
            headers_bytes = segment[: header_end + 2]
            payload = segment[header_end + 4 :]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        elif payload.endswith(b"\n"):
            payload = payload[:-1]
        headers = {}
        for line in headers_bytes.decode("utf-8", errors="ignore").strip().split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disposition = headers.get("content-disposition", "")
        name = None
        filename = None
        for item in disposition.split(";"):
            item = item.strip()
            if item.startswith("name="):
                name = item[5:].strip('"\'')
            elif item.startswith("filename="):
                filename = item[9:].strip('"\'')
        parts.append(
            {
                "name": name,
                "filename": filename,
                "content_type": headers.get("content-type", ""),
                "payload": payload,
            }
        )
    return parts


def validate_upload_file(filename: str, payload: bytes) -> Tuple[bool, str]:
    if not filename:
        return False, "Filename must not be empty"
    suffix = Path(filename).suffix.lower()
    if suffix == ".tlg":
        text = payload.decode("utf-8", errors="ignore")
        if "ACCOUNT_INFORMATION" in text or "STK_TRD" in text:
            return True, "TLG trade log"
        return False, "Invalid .tlg file: trade-record marker is missing"
    if suffix == ".csv":
        text = payload.decode("utf-8", errors="ignore")
        lines = text.splitlines()[:10]
        for line in lines:
            if "Activity Statement" in line:
                return True, "Activity Statement"
            if "MTM Summary" in line:
                return True, "MTM Summary"
        return False, "Invalid CSV file: Activity Statement or MTM Summary marker is missing"
    return False, f"Unsupported file type: {suffix}"


def upload_target_for_file(filename: str, payload: bytes) -> Tuple[Path, str]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        text = payload.decode("utf-8", errors="ignore")
        title = None
        period = None
        for row in csv.reader(text.splitlines()):
            if len(row) < 4:
                continue
            tag = row[0].lstrip("﻿")
            if tag == "Statement" and row[1] == "Data" and row[2] == "Title":
                title = row[3].strip()
            if tag == "Statement" and row[1] == "Data" and row[2] == "Period":
                period = row[3].strip()
            if title and period:
                break
        year = historical_year_from_statement_period(period or "")
        if title == "Activity Statement" and year:
            target_dir = HISTORICAL_INBOX_DIR / year
            return target_dir / filename, f"Historical {year} Activity Statement"
    if suffix == ".tlg":
        temp_path = Path("__upload_probe__.tlg")
        # Avoid writing a temp file: parse the STK_TRD dates directly from text.
        text = payload.decode("utf-8", errors="ignore")
        years = set()
        for line in text.splitlines():
            if line.startswith("STK_TRD|"):
                parts = line.split("|")
                if len(parts) > 7 and len(parts[7].strip()) >= 4:
                    years.add(parts[7].strip()[:4])
        if len(years) == 1:
            year = next(iter(years))
            if year.isdigit() and int(year) < CURRENT_YEAR:
                target_dir = HISTORICAL_INBOX_DIR / year
                return target_dir / filename, f"Historical {year} TLG trade log"
    return INBOX_DIR / filename, "Current YTD file"


def get_statement_title_and_period(path: Path) -> Tuple[Optional[str], Optional[str]]:
    with path.open(newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        title = None
        period = None
        for row in reader:
            if len(row) < 4:
                continue
            tag = row[0].lstrip("\ufeff")
            if tag == "Statement" and row[1] == "Data" and row[2] == "Title":
                title = row[3].strip()
            if tag == "Statement" and row[1] == "Data" and row[2] == "Period":
                period = row[3].strip()
            if title and period:
                break
        return title, period


def pick_statement_file(title_name: str) -> Optional[Path]:
    best: Optional[Tuple[datetime.date, Path]] = None
    for path in candidate_data_files("*.csv"):
        title, period = get_statement_title_and_period(path)
        if title != title_name or not period:
            continue
        end_date = parse_period_end(period)
        if not end_date:
            continue
        if best is None or end_date > best[0]:
            best = (end_date, path)
    return best[1] if best else None


def parse_tlg(path: Path) -> List[Trade]:
    return ibkr_parser.parse_tlg(path)


def parse_csv_transactions(path: Path) -> List[Trade]:
    return ibkr_parser.parse_csv_transactions(path)


def parse_mtm_file(path: Path) -> Tuple[Dict[str, dict], List[OtherPnlSnapshot]]:
    return ibkr_parser.parse_mtm_file(path)


@lru_cache(maxsize=1)
def load_ibkr_trades() -> List[Trade]:
    if TRADES_ALL_STATE_FILE.exists():
        rows = read_json(TRADES_ALL_STATE_FILE, [])
        return trades_from_rows(_loadable_trade_rows(rows))
    if TRADES_STATE_FILE.exists():
        rows = read_json(TRADES_STATE_FILE, [])
        return trades_from_rows(_loadable_trade_rows(rows))

    tlg_files = candidate_data_files("*.tlg")
    if tlg_files:
        all_trades: List[Trade] = []
        for path in tlg_files:
            all_trades.extend(parse_tlg(path))
        return all_trades

    all_trades: List[Trade] = []
    for path in candidate_data_files("*.csv"):
        title, _ = get_statement_title_and_period(path)
        if title in {"Activity Statement", "MTM Summary"}:
            continue
        all_trades.extend(parse_csv_transactions(path))
    return all_trades


@lru_cache(maxsize=32)
def load_period_trades(period: str) -> List[Trade]:
    p = normalize_period(period)
    path = trades_file_for_period(p)
    if path.exists():
        rows = read_json(path, [])
        return trades_from_rows(_loadable_trade_rows(rows))
    if p == "ALL":
        return load_ibkr_trades()
    if p.endswith("YTD"):
        year = p[:4]
        return [t for t in load_ibkr_trades() if t.trade_date.startswith(year)]
    if p.isdigit():
        return [t for t in load_ibkr_trades() if t.trade_date.startswith(p)]
    return load_ibkr_trades()


def _available_data_periods() -> List[str]:
    return [p for p in list_available_periods() if p != "ALL"]


def _period_mtm_payload(period: str) -> dict:
    path = mtm_file_for_period(period)
    if path.exists():
        return read_json(path, {})
    if normalize_period(period) == CURRENT_PERIOD and MTM_STATE_FILE.exists():
        return read_json(MTM_STATE_FILE, {})
    return {}


@lru_cache(maxsize=32)
def load_period_mtm_summary(period: str) -> Dict[str, dict]:
    p = normalize_period(period)
    if p == "ALL":
        merged: Dict[str, dict] = {}
        for data_period in _available_data_periods():
            for symbol, row in _period_mtm_payload(data_period).get("stocks", {}).items():
                item = merged.setdefault(symbol, dict(row, mtmTotal=0.0))
                item["mtmTotal"] = round(float(item.get("mtmTotal", 0.0) or 0.0) + float(row.get("mtmTotal", 0.0) or 0.0), 6)
                item["symbol"] = row.get("symbol", symbol)
                item["displayName"] = row.get("displayName", item.get("displayName", symbol))
        return merged

    path = mtm_file_for_period(p)
    if path.exists():
        return read_json(path, {}).get("stocks", {})
    if p == CURRENT_PERIOD:
        return load_mtm_summary()
    return {}


@lru_cache(maxsize=32)
def load_period_other_mtm_summary(period: str) -> List[OtherPnlSnapshot]:
    p = normalize_period(period)
    if p == "ALL":
        merged: Dict[Tuple[str, str, str], OtherPnlSnapshot] = {}
        for data_period in _available_data_periods():
            for row in _period_mtm_payload(data_period).get("otherPnl", []):
                key = (row.get("assetCategory", ""), row.get("symbol", ""), row.get("displayName", row.get("symbol", "")))
                item = merged.get(key)
                if item is None:
                    item = OtherPnlSnapshot(
                        asset_category=key[0],
                        symbol=key[1],
                        display_name=key[2],
                        mtm_total=0.0,
                    )
                    merged[key] = item
                item.mtm_total = round(float(item.mtm_total or 0.0) + float(row.get("mtmTotal", 0.0) or 0.0), 6)
        return list(merged.values())

    path = mtm_file_for_period(p)
    if path.exists():
        rows = read_json(path, {}).get("otherPnl", [])
        return [
            OtherPnlSnapshot(
                asset_category=row.get("assetCategory", ""),
                symbol=row.get("symbol", ""),
                display_name=row.get("displayName", row.get("symbol", "")),
                mtm_total=row.get("mtmTotal", 0.0),
            )
            for row in rows
        ]
    if p == CURRENT_PERIOD:
        return load_other_mtm_summary()
    return []


@lru_cache(maxsize=1)
def load_mtm_summary() -> Dict[str, dict]:
    if MTM_STATE_FILE.exists():
        return read_json(MTM_STATE_FILE, {}).get("stocks", {})

    path = pick_statement_file("Activity Statement")
    if not path:
        return {}
    mtm, _ = parse_mtm_file(path)
    return mtm


@lru_cache(maxsize=1)
def load_other_mtm_summary() -> List[OtherPnlSnapshot]:
    if MTM_STATE_FILE.exists():
        return [
            OtherPnlSnapshot(
                asset_category=row["assetCategory"],
                symbol=row["symbol"],
                display_name=row["displayName"],
                mtm_total=row["mtmTotal"],
            )
            for row in read_json(MTM_STATE_FILE, {}).get("otherPnl", [])
        ]

    path = pick_statement_file("Activity Statement")
    if not path:
        return []
    _, other = parse_mtm_file(path)
    return other


@lru_cache(maxsize=1)
def get_report_end_date() -> datetime.date:
    if MTM_STATE_FILE.exists():
        value = read_json(MTM_STATE_FILE, {}).get("reportEndDate")
        if value:
            return datetime.strptime(value, "%Y-%m-%d").date()

    candidates: List[datetime.date] = []
    for title_name in ("Activity Statement", "MTM Summary"):
        path = pick_statement_file(title_name)
        if not path:
            continue
        _, period = get_statement_title_and_period(path)
        if period:
            end_date = parse_period_end(period)
            if end_date:
                candidates.append(end_date)
    if candidates:
        return max(candidates)
    trades = load_ibkr_trades()
    if trades:
        return max(datetime.strptime(t.trade_date, "%Y%m%d").date() for t in trades)
    return datetime.now().date()


def get_market_data_end_date() -> datetime.date:
    return max(get_report_end_date(), datetime.now().date())


def compute_ma(values: List[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if window <= 0:
        return out
    running = 0.0
    for idx, value in enumerate(values):
        running += value
        if idx >= window:
            running -= values[idx - window]
        if idx >= window - 1:
            out[idx] = running / window
    return out


def candle_day(row: dict) -> str:
    return str(row.get("time", ""))[:10]


def normalize_candles(rows: List[dict]) -> List[dict]:
    by_day = {}
    for row in rows:
        day = candle_day(row)
        if day:
            by_day[day] = row
    return [by_day[day] for day in sorted(by_day)]


def kline_manifest_key(symbol_lb: str) -> str:
    return f"{symbol_lb}|{KLINE_ADJUST_MODE}"


def symbol_kline_cache_file(symbol_lb: str) -> Path:
    return KLINE_CACHE_DIR / f"{symbol_lb}_{KLINE_ADJUST_MODE}_day.json"


def today_text() -> str:
    return datetime.now().date().strftime("%Y-%m-%d")


def now_iso_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def manifest_time_recent(value: str, ttl_seconds: int) -> bool:
    if not value:
        return False
    try:
        dt = datetime.fromisoformat(value)
    except Exception:
        return False
    return (datetime.now() - dt).total_seconds() < ttl_seconds


def yahoo_retry_allowed(manifest_entry: dict) -> bool:
    if not manifest_entry.get("lastError"):
        return True
    return not manifest_time_recent(
        str(manifest_entry.get("lastAttemptAt") or ""),
        YAHOO_ERROR_RETRY_SECONDS,
    )


def kline_manifest() -> dict:
    return read_json(KLINE_MANIFEST_FILE, {})


def save_kline_manifest(payload: dict) -> None:
    write_json(KLINE_MANIFEST_FILE, payload)


def cache_bounds(rows: List[dict]) -> Tuple[str, str]:
    if not rows:
        return "", ""
    return candle_day(rows[0]), candle_day(rows[-1])


def previous_weekday(value):
    value -= timedelta(days=1)
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def expected_kline_end_date(requested_end: str, symbol_lb: str = "", now: Optional[datetime] = None) -> str:
    """Return the latest completed market day that may be required from a daily cache."""
    requested = datetime.strptime(requested_end, "%Y-%m-%d").date()
    market = normalize_symbol(symbol_lb).rsplit(".", 1)[-1] if symbol_lb else ""
    config = MARKET_CLOSE_CONFIGS.get(market)
    if config:
        timezone = ZoneInfo(config["tz"])
        if now is None:
            market_now = datetime.now(timezone)
        elif now.tzinfo is None:
            market_now = now.replace(tzinfo=timezone)
        else:
            market_now = now.astimezone(timezone)
        completed = market_now.date()
        cutoff = market_now.replace(
            hour=config["close_hour"],
            minute=config["close_minute"],
            second=0,
            microsecond=0,
        )
        if completed.weekday() >= 5:
            while completed.weekday() >= 5:
                completed -= timedelta(days=1)
        elif market_now < cutoff:
            completed = previous_weekday(completed)
    else:
        completed = (now or datetime.now()).date()
        while completed.weekday() >= 5:
            completed -= timedelta(days=1)
    return min(requested, completed).strftime("%Y-%m-%d")


def latest_candle_market_status(symbol_lb: str, latest_day: str) -> dict:
    market = symbol_lb.rsplit(".", 1)[-1] if "." in symbol_lb else ""
    config = MARKET_CLOSE_CONFIGS.get(market)
    if not config or not latest_day:
        return {"market": market, "intradayLikely": False, "exchangeDate": "", "exchangeTime": ""}
    now = datetime.now(ZoneInfo(config["tz"]))
    exchange_today = now.date().strftime("%Y-%m-%d")
    cutoff = now.replace(hour=config["close_hour"], minute=config["close_minute"], second=0, microsecond=0)
    intraday_likely = latest_day == exchange_today and now < cutoff
    return {
        "market": market,
        "marketLabel": config["label"],
        "intradayLikely": intraday_likely,
        "exchangeDate": exchange_today,
        "exchangeTime": now.strftime("%H:%M"),
    }


def kline_freshness(
    symbol_lb: str,
    rows: List[dict],
    requested_end: str,
    attempted_today: bool = False,
    updated: bool = False,
    error: str = "",
    skipped_today: bool = False,
) -> dict:
    cache_start, cache_end = cache_bounds(rows)
    expected_end = expected_kline_end_date(requested_end, symbol_lb)
    return {
        "symbol": symbol_lb,
        "cacheStart": cache_start,
        "cacheEnd": cache_end,
        "requestedEnd": requested_end,
        "expectedEnd": expected_end,
        "attemptedToday": attempted_today,
        "updated": updated,
        "stale": bool(cache_end and cache_end < expected_end),
        "error": error,
        "skippedToday": skipped_today,
    }


def build_health_payload() -> dict:
    from app.health_service import build_health_payload as _build

    return _build(
        root=ROOT,
        manifest_file=MANIFEST_STATE_FILE,
        state_metadata_file=STATE_METADATA_FILE,
        app_log_file=APP_LOG_FILE,
        kline_state=kline_manifest(),
        expected_kline_end_date=expected_kline_end_date,
        adjust_mode=KLINE_ADJUST_MODE,
        app_log=app_log,
        port=int(os.environ.get("PORT", "8888")),
        git_info=git_runtime_info(),
        runtime_info=runtime_info(),
        cache_issues=kline_cache_health_issues(),
        ai_cache=ai_cache_health(),
    )


def log_exception(context: str, exc: Exception, **fields) -> None:
    app_log(
        context,
        level="error",
        error=str(exc),
        traceback=traceback.format_exc(limit=8),
        **fields,
    )


def update_kline_manifest(symbol_lb: str, rows: List[dict], requested_end: str, error: str = "", intraday_attempt: bool = False) -> None:
    manifest = kline_manifest()
    cache_start, cache_end = cache_bounds(rows)
    today = today_text()
    manifest_key = kline_manifest_key(symbol_lb)
    current = manifest.get(manifest_key, {})
    current.update(
        {
            "lastAttemptDate": today,
            "cacheStart": cache_start,
            "cacheEnd": cache_end,
            "requestedEnd": requested_end,
            "lastError": error,
            "lastAttemptAt": now_iso_text(),
        }
    )
    if intraday_attempt:
        current["lastIntradayAttemptAt"] = now_iso_text()
    if rows and not error:
        current["lastSuccessDate"] = today
    manifest[manifest_key] = current
    save_kline_manifest(manifest)


def load_kline_cache(symbol_lb: str) -> List[dict]:
    cache_file = symbol_kline_cache_file(symbol_lb)
    if cache_file.exists():
        return normalize_candles(read_json(cache_file, []))
    return []


def load_legacy_raw_kline_cache(symbol_lb: str) -> List[dict]:
    legacy_file = KLINE_CACHE_DIR / f"{symbol_lb}_day.json"
    if legacy_file.exists():
        return normalize_candles(read_json(legacy_file, []))
    return []


def save_kline_cache(symbol_lb: str, rows: List[dict]) -> None:
    write_json(symbol_kline_cache_file(symbol_lb), normalize_candles(rows))


def kline_cache_health_issues(limit: int = 30) -> List[dict]:
    issues: List[dict] = []
    if not KLINE_CACHE_DIR.exists():
        return [{"type": "missing_dir", "path": relative_path_text(KLINE_CACHE_DIR)}]
    for path in sorted(KLINE_CACHE_DIR.glob("*_day.json")):
        if path.name == "_manifest.json":
            continue
        symbol = path.name.replace(f"_{KLINE_ADJUST_MODE}_day.json", "").replace("_day.json", "")
        issue = {"symbol": symbol, "path": relative_path_text(path), "type": ""}
        try:
            rows = read_json(path, [])
            normalized = normalize_candles(rows)
            if not normalized:
                issue["type"] = "empty"
            else:
                days = [candle_day(row) for row in normalized]
                if days != sorted(days):
                    issue["type"] = "date_order"
                elif not path.name.endswith(f"_{KLINE_ADJUST_MODE}_day.json"):
                    issue["type"] = "adjust_mode_mismatch"
                else:
                    continue
            issues.append(issue)
        except Exception as exc:
            issue["type"] = "read_error"
            issue["error"] = str(exc)
            issues.append(issue)
        if len(issues) >= limit:
            break
    return issues


def ai_cache_health() -> dict:
    today = today_text()
    audit_reports = sorted(STATE_DIR.glob(f"audit_report_{today}.md"))
    return {
        "date": today,
        "auditReportCount": len(audit_reports),
        "replayReviewCount": 0,
        "technicalAnalysisCount": 0,
        "auditReports": [relative_path_text(path) for path in audit_reports[:20]],
        "replayReviews": [],
    }


def yahoo_symbol(symbol_lb: str) -> str:
    symbol = normalize_symbol(symbol_lb)
    if symbol.endswith(".US"):
        return symbol[:-3]
    if symbol.endswith(".HK"):
        code = symbol[:-3]
        return f"{code.zfill(4)}.HK" if code.isdigit() else symbol
    if symbol.endswith(".SH"):
        code = symbol[:-3]
        return f"{code}.SS" if code.isdigit() else symbol
    if symbol.endswith(".SZ"):
        code = symbol[:-3]
        return f"{code}.SZ" if code.isdigit() else symbol
    return symbol.replace(".US", "")


class YahooThrottleError(RuntimeError):
    """Yahoo rejected the request because this client is temporarily throttled."""


def is_yahoo_throttle_error(error: str) -> bool:
    text = str(error or "").lower()
    return "yahoo" in text and ("403" in text or "429" in text or "too many requests" in text)


def fetch_yahoo_kline(symbol_lb: str, start_date: str, end_date: str) -> List[dict]:
    if start_date > end_date:
        return []
    yahoo_sym = yahoo_symbol(symbol_lb)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    period1 = int(datetime.combine(start_dt, datetime.min.time()).timestamp())
    # Yahoo period2 is exclusive; add one day so the requested end date is included.
    period2 = int(datetime.combine(end_dt + timedelta(days=1), datetime.min.time()).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    app_log("yahoo kline request", symbol=symbol_lb, yahooSymbol=yahoo_sym, start=start_date, end=end_date)
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=YAHOO_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout as exc:
        app_log("yahoo kline timeout", level="warning", symbol=symbol_lb, start=start_date, end=end_date, timeout=YAHOO_TIMEOUT_SECONDS)
        raise RuntimeError(f"Yahoo Finance call timed out after {YAHOO_TIMEOUT_SECONDS}s") from exc
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", None)
        app_log("yahoo kline failed", level="warning", symbol=symbol_lb, start=start_date, end=end_date, status=status, error=str(exc)[:800])
        if status in {403, 429}:
            raise YahooThrottleError(f"Yahoo Finance throttled the request ({status}): {exc}") from exc
        raise RuntimeError(f"Yahoo Finance call failed: {exc}") from exc
    except requests.exceptions.RequestException as exc:
        app_log("yahoo kline failed", level="warning", symbol=symbol_lb, start=start_date, end=end_date, error=str(exc)[:800])
        raise RuntimeError(f"Yahoo Finance call failed: {exc}") from exc

    chart = data.get("chart", {})
    chart_error = chart.get("error")
    if chart_error:
        message = chart_error.get("description") or chart_error.get("code") or "Yahoo Finance chart error"
        app_log("yahoo kline error", level="warning", symbol=symbol_lb, start=start_date, end=end_date, error=message[:800])
        raise RuntimeError(message)
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo Finance returned no chart data for {symbol_lb}.")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adjusted = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    rows = []
    def value_at(values, idx, default=None):
        if not isinstance(values, list) or idx >= len(values):
            return default
        return values[idx]

    def adjusted_price(value, ratio):
        if value is None:
            return None
        return round(float(value) * ratio, 6)

    for idx, ts in enumerate(timestamps):
        close_val = value_at(quote.get("close"), idx)
        if close_val is None:
            continue
        open_val = value_at(quote.get("open"), idx)
        high_val = value_at(quote.get("high"), idx)
        low_val = value_at(quote.get("low"), idx)
        volume_val = value_at(quote.get("volume"), idx, 0) or 0
        adj_val = value_at(adjusted, idx)
        adjusted_close = float(adj_val if adj_val is not None else close_val)
        ratio = adjusted_close / float(close_val) if close_val else 1.0
        day = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        rows.append({
            "time": day,
            "open": adjusted_price(open_val if open_val is not None else close_val, ratio),
            "high": adjusted_price(high_val if high_val is not None else close_val, ratio),
            "low": adjusted_price(low_val if low_val is not None else close_val, ratio),
            "close": round(adjusted_close, 6),
            "adjusted_close": round(adjusted_close, 6),
            "volume": volume_val,
            "turnover": round(adjusted_close * float(volume_val), 6),
            "source": "yahoo",
        })
    app_log("yahoo kline ok", symbol=symbol_lb, yahooSymbol=yahoo_sym, start=start_date, end=end_date, rows=len(rows))
    return rows


def rows_latest_intraday_likely(symbol_lb: str, rows: List[dict]) -> bool:
    normalized = normalize_candles(rows)
    if not normalized:
        return False
    return bool(latest_candle_market_status(symbol_lb, candle_day(normalized[-1])).get("intradayLikely"))


def fetch_kline_history(symbol_lb: str, start_date: str, end_date: str, cache_only: bool = False, force_refresh: bool = False) -> KlineResult:
    if demo_mode_active():
        cache_only = True
        force_refresh = False
    KLINE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = load_kline_cache(symbol_lb)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    expected_end_dt = datetime.strptime(expected_kline_end_date(end_date, symbol_lb), "%Y-%m-%d").date()
    latest_fetch_end_dt = min(end_dt, expected_end_dt)
    manifest_entry = kline_manifest().get(kline_manifest_key(symbol_lb), {})
    attempted_today_already = manifest_entry.get("lastAttemptDate") == today_text()
    retry_allowed = yahoo_retry_allowed(manifest_entry)
    updated = False
    error = ""
    skipped_today = False
    attempted_now = False

    if cache_only:
        # Prefer the active Yahoo cache. If it does not exist yet, use the
        # legacy raw cache only as an immediate placeholder; the full replay
        # path will try to create the Yahoo cache when allowed.
        source_rows = cached or load_legacy_raw_kline_cache(symbol_lb)
        rows = [row for row in normalize_candles(source_rows) if start_date <= candle_day(row) <= end_date]
        return KlineResult(
            rows=rows,
            freshness=kline_freshness(
                symbol_lb,
                rows,
                end_date,
                attempted_today=attempted_today_already,
                updated=False,
                error=manifest_entry.get("lastError", ""),
                skipped_today=False,
            ),
        )

    if cached:
        cached_start = datetime.strptime(candle_day(cached[0]), "%Y-%m-%d").date()
        cached_end = datetime.strptime(candle_day(cached[-1]), "%Y-%m-%d").date()
        missing_ranges: List[Tuple[datetime.date, datetime.date, str]] = []
        intraday_refresh = False
        market_status_for_cache = latest_candle_market_status(symbol_lb, candle_day(cached[-1]) if cached else "")
        settings = load_settings()
        intraday_refresh_seconds = int(
            settings.get("yahooRefreshSeconds")
            or settings.get("longbridgeRefreshSeconds")
            or INTRADAY_REFRESH_SECONDS
        )
        intraday_attempt_recent = manifest_time_recent(
            manifest_entry.get("lastIntradayAttemptAt", ""),
            intraday_refresh_seconds,
        )
        if cached_end < expected_end_dt:
            reconcile_start = max(start_dt, cached_end - timedelta(days=KLINE_RECONCILE_DAYS))
            # Re-fetch a small trailing window so an older intraday daily bar gets replaced
            # by the finalized close on the next trading day update.
            missing_ranges.append((reconcile_start, latest_fetch_end_dt, "latest"))
        elif market_status_for_cache.get("intradayLikely") and not intraday_attempt_recent:
            intraday_refresh = True
            reconcile_start = max(start_dt, cached_end - timedelta(days=KLINE_RECONCILE_DAYS))
            # During market hours the last daily candle can move. Re-fetch a short
            # trailing window and merge by day so the in-progress candle is replaced
            # without resetting the user's visible range.
            missing_ranges.append((reconcile_start, latest_fetch_end_dt, "latest"))
        elif force_refresh:
            reconcile_start = max(start_dt, cached_end - timedelta(days=KLINE_RECONCILE_DAYS))
            missing_ranges.append((reconcile_start, latest_fetch_end_dt, "latest"))
        if start_dt < cached_start:
            # Backfill older history after the recent window. New/special tickers
            # can reject pre-listing ranges, but that should not block latest price.
            missing_ranges.append((start_dt, cached_start - timedelta(days=1), "history"))
        if missing_ranges and not retry_allowed and not intraday_refresh and not force_refresh:
            skipped_today = True
            error = manifest_entry.get("lastError", "")
            rows = [row for row in normalize_candles(cached) if start_date <= candle_day(row) <= end_date]
            return KlineResult(
                rows=rows,
                freshness=kline_freshness(
                    symbol_lb,
                    rows,
                    end_date,
                    attempted_today=True,
                    updated=False,
                    error=error,
                    skipped_today=True,
                ),
            )
        for range_start, range_end, range_kind in missing_ranges:
            attempted_now = True
            try:
                fetched = fetch_yahoo_kline(
                    symbol_lb,
                    range_start.strftime("%Y-%m-%d"),
                    range_end.strftime("%Y-%m-%d"),
                )
                cached.extend(fetched)
                updated = updated or bool(fetched)
            except Exception as exc:
                if range_kind == "latest" or not updated:
                    error = str(exc)
                rows = normalize_candles(cached)
                update_kline_manifest(symbol_lb, rows, end_date, error=error, intraday_attempt=intraday_refresh)
                if not rows:
                    raise
                break
        save_kline_cache(symbol_lb, cached)
        if updated or (force_refresh and attempted_now and not error):
            invalidate_market_data_caches(symbol_lb)
        rows = [row for row in normalize_candles(cached) if start_date <= candle_day(row) <= end_date]
        if missing_ranges:
            update_kline_manifest(
                symbol_lb,
                normalize_candles(cached),
                end_date,
                error=error,
                intraday_attempt=intraday_refresh or rows_latest_intraday_likely(symbol_lb, cached),
            )
    else:
        legacy_rows = [row for row in normalize_candles(load_legacy_raw_kline_cache(symbol_lb)) if start_date <= candle_day(row) <= end_date]
        if attempted_today_already and legacy_rows and not force_refresh:
            skipped_today = True
            error = manifest_entry.get("lastError", "")
            return KlineResult(
                rows=legacy_rows,
                freshness=kline_freshness(
                    symbol_lb,
                    legacy_rows,
                    end_date,
                    attempted_today=True,
                    updated=False,
                    error=error,
                    skipped_today=True,
                ),
            )
        try:
            attempted_now = True
            cached = fetch_yahoo_kline(symbol_lb, start_date, latest_fetch_end_dt.strftime("%Y-%m-%d"))
            updated = bool(cached)
            save_kline_cache(symbol_lb, cached)
            if updated:
                invalidate_market_data_caches(symbol_lb)
            rows = [row for row in normalize_candles(cached) if start_date <= candle_day(row) <= end_date]
            update_kline_manifest(
                symbol_lb,
                normalize_candles(cached),
                end_date,
                intraday_attempt=rows_latest_intraday_likely(symbol_lb, cached),
            )
        except Exception as exc:
            error = str(exc)
            update_kline_manifest(symbol_lb, legacy_rows, end_date, error=error)
            if legacy_rows:
                return KlineResult(
                    rows=legacy_rows,
                    freshness=kline_freshness(
                        symbol_lb,
                        legacy_rows,
                        end_date,
                        attempted_today=True,
                        updated=False,
                        error=error,
                        skipped_today=False,
                    ),
                )
            raise

    rows = locals().get("rows") or [row for row in normalize_candles(cached) if start_date <= candle_day(row) <= end_date]
    return KlineResult(
        rows=rows,
        freshness=kline_freshness(
            symbol_lb,
            rows,
            end_date,
            attempted_today=bool(attempted_now or updated or error or skipped_today),
            updated=updated,
            error=error,
            skipped_today=skipped_today,
        ),
    )


def recent_symbols() -> List[str]:
    return read_json(RECENT_SYMBOLS_FILE, [])


def record_recent_symbol(symbol_lb: str) -> None:
    ensure_data_dirs()
    symbols = [s for s in recent_symbols() if s != symbol_lb]
    symbols.insert(0, symbol_lb)
    write_json(RECENT_SYMBOLS_FILE, symbols[:30])


def build_refresh_prefetch_symbols(overview: dict, trades: List[Trade], mtm_stocks: dict, recent: Optional[List[str]] = None) -> List[str]:
    symbols = []
    seen = set()

    def add_symbol(value) -> None:
        raw = str(value or "").strip()
        if not raw:
            return
        normalized = normalize_symbol(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            symbols.append(normalized)

    for symbol in recent if recent is not None else recent_symbols():
        add_symbol(symbol)
    add_symbol(overview.get("defaultSymbol") if isinstance(overview, dict) else "")
    add_symbol(load_settings().get("defaultSymbol"))
    for item in overview.get("recentlyTraded", []) if isinstance(overview, dict) else []:
        if isinstance(item, dict):
            add_symbol(item.get("symbol"))
    for item in overview.get("availableSymbols", []) if isinstance(overview, dict) else []:
        if isinstance(item, dict):
            add_symbol(item.get("symbol"))
    for trade in trades:
        add_symbol(trade.symbol_lb)
    for symbol in (mtm_stocks or {}).keys():
        add_symbol(symbol)

    return symbols


def compute_drawdowns_from_performance() -> Tuple[Optional[float], Optional[float]]:
    """Return (max_drawdown, current_drawdown) from performance.json series."""
    try:
        data = read_json(STATE_DIR / "performance.json", {})
        series = data.get("series", [])
        if not series:
            return None, None
        values = [float(item.get("value", 0.0) or 0.0) for item in series]
        peak = values[0]
        max_dd = 0.0
        for v in values:
            if v > peak:
                peak = v
            dd = (v - peak) / peak if peak else 0.0
            if dd < max_dd:
                max_dd = dd
        current_dd = (values[-1] - max(values)) / max(values) if values and max(values) else 0.0
        return round(max_dd, 6), round(current_dd, 6)
    except Exception:
        return None, None


def build_position_reconciliation(fifo_positions: List[dict], ibkr_positions: List[dict]) -> dict:
    fifo_map = {p.get("symbol", ""): safe_float(p.get("quantity", 0)) for p in fifo_positions}
    ibkr_map = {p.get("symbolLb", ""): safe_float(p.get("quantity", 0)) for p in ibkr_positions}

    only_in_fifo = []
    only_in_ibkr = []
    mismatch = []

    for sym, qty in fifo_map.items():
        if not sym:
            continue
        if sym not in ibkr_map:
            only_in_fifo.append({"symbol": sym, "fifoQty": qty, "ibkrQty": 0.0})
        elif qty != ibkr_map[sym]:
            mismatch.append({"symbol": sym, "fifoQty": qty, "ibkrQty": ibkr_map[sym]})

    for sym, qty in ibkr_map.items():
        if not sym:
            continue
        if sym not in fifo_map:
            only_in_ibkr.append({"symbol": sym, "fifoQty": 0.0, "ibkrQty": qty})

    return {
        "onlyInFifo": only_in_fifo,
        "onlyInIbkr": only_in_ibkr,
        "quantityMismatch": mismatch,
        "status": "ok" if not (only_in_fifo or only_in_ibkr or mismatch) else "diff",
        "diffCount": len(only_in_fifo) + len(only_in_ibkr) + len(mismatch),
    }


def build_account_snapshot_payload(period: str = CURRENT_PERIOD) -> dict:
    period = normalize_period(period)

    stmt_file = period_state_file(period, "statement_snapshot.json")
    if not stmt_file.exists() and period == CURRENT_PERIOD:
        stmt_file = STATEMENT_SNAPSHOT_FILE
    stmt_data = read_json(stmt_file, {}) if stmt_file.exists() else {}

    account_snapshot = {}
    risk_snapshot = {}
    if isinstance(stmt_data, dict) and "accountSnapshot" in stmt_data:
        account_snapshot = stmt_data.get("accountSnapshot", {}) or {}
        risk_snapshot = dict(stmt_data.get("riskSnapshot", {}) or {})

    recon_file = period_state_file(period, "position_reconciliation.json")
    if not recon_file.exists() and period == CURRENT_PERIOD:
        recon_file = POSITION_RECONCILIATION_FILE
    reconciliation = read_json(recon_file, {}) if recon_file.exists() else {}

    ibkr_pos_file = period_state_file(period, "ibkr_open_positions.json")
    if not ibkr_pos_file.exists() and period == CURRENT_PERIOD:
        ibkr_pos_file = IBKR_OPEN_POSITIONS_FILE
    ibkr_open_positions = read_json(ibkr_pos_file, []) if ibkr_pos_file.exists() else []

    max_dd, current_dd = compute_drawdowns_from_performance()
    risk_snapshot["maxDrawdown"] = max_dd
    risk_snapshot["currentDrawdown"] = current_dd

    return {
        "period": period,
        "periodLabel": period_label(period),
        "accountSnapshot": account_snapshot,
        "riskSnapshot": risk_snapshot,
        "positionReconciliation": reconciliation,
        "ibkrOpenPositions": ibkr_open_positions,
    }


@lru_cache(maxsize=32)
def build_overview_payload(period: str = CURRENT_PERIOD) -> dict:
    period = normalize_period(period)
    trades = load_period_trades(period)
    mtm_summary = load_period_mtm_summary(period)
    other_mtm = load_period_other_mtm_summary(period)

    amount_by_symbol = defaultdict(float)
    trade_count_by_symbol = Counter()
    last_trade_by_symbol: dict = {}

    for trade in trades:
        amount_by_symbol[trade.symbol_lb] += trade.gross_amount
        trade_count_by_symbol[trade.symbol_lb] += 1
        dt = f"{trade.trade_date} {trade.trade_time}"
        if trade.symbol_lb not in last_trade_by_symbol or dt > last_trade_by_symbol[trade.symbol_lb]["dt"]:
            last_trade_by_symbol[trade.symbol_lb] = {"dt": dt, "date": trade.trade_date}

    recently_traded = sorted(
        [
            {
                "symbol": sym,
                "displayName": sym,
                "lastTradeDate": info["date"],
                "totalAmount": round(amount_by_symbol.get(sym, 0.0), 2),
            }
            for sym, info in last_trade_by_symbol.items()
        ],
        key=lambda x: x["lastTradeDate"],
        reverse=True,
    )

    symbol_universe = set(amount_by_symbol) | set(trade_count_by_symbol) | set(mtm_summary)
    snapshots: List[SymbolSnapshot] = []
    for symbol_lb in sorted(symbol_universe):
        snapshots.append(
            SymbolSnapshot(
                symbol=symbol_lb,
                display_name=symbol_lb,
                amount=round(amount_by_symbol.get(symbol_lb, 0.0), 4),
                trade_count=trade_count_by_symbol.get(symbol_lb, 0),
                mtm_total=round(mtm_summary.get(symbol_lb, {}).get("mtmTotal", 0.0), 6),
                has_trades=symbol_lb in amount_by_symbol or symbol_lb in trade_count_by_symbol,
                has_mtm=symbol_lb in mtm_summary,
            )
        )

    def snap_dict(item: SymbolSnapshot) -> dict:
        return {
            "symbol": item.symbol,
            "displayName": item.display_name,
            "amount": item.amount,
            "tradeCount": item.trade_count,
            "mtmTotal": item.mtm_total,
            "hasTrades": item.has_trades,
            "hasMtm": item.has_mtm,
        }

    ranked_amount = sorted(snapshots, key=lambda x: (x.amount, x.trade_count, x.symbol), reverse=True)
    ranked_count = sorted(snapshots, key=lambda x: (x.trade_count, x.amount, x.symbol), reverse=True)
    winners = sorted([x for x in snapshots if x.has_mtm], key=lambda x: x.mtm_total, reverse=True)
    losers = sorted([x for x in snapshots if x.has_mtm], key=lambda x: x.mtm_total)

    def other_dict(item: OtherPnlSnapshot) -> dict:
        return {
            "assetCategory": item.asset_category,
            "symbol": item.symbol,
            "displayName": item.display_name,
            "mtmTotal": item.mtm_total,
        }

    other_ranked = sorted(other_mtm, key=lambda x: abs(x.mtm_total), reverse=True)

    daily_stats: Dict[str, Dict[str, float]] = defaultdict(lambda: {"tradeCount": 0, "tradeAmount": 0.0})
    for trade in trades:
        day = datetime.strptime(trade.trade_date, "%Y%m%d").strftime("%Y-%m-%d")
        daily_stats[day]["tradeCount"] += 1
        daily_stats[day]["tradeAmount"] += trade.gross_amount

    configured_symbol = str(load_settings().get("defaultSymbol") or "").strip()
    default_symbol = configured_symbol or (recently_traded[0]["symbol"] if recently_traded else DEFAULT_SYMBOL)

    account_payload = build_account_snapshot_payload(period)

    return {
        "period": period,
        "periodLabel": period_label(period),
        "availablePeriods": list_available_periods(),
        "defaultSymbol": default_symbol,
        "topByAmount": [snap_dict(x) for x in ranked_amount[:10]],
        "topByCount": [snap_dict(x) for x in ranked_count[:10]],
        "topWinners": [snap_dict(x) for x in winners[:10]],
        "topLosers": [snap_dict(x) for x in losers[:10]],
        "otherPnl": [other_dict(x) for x in other_ranked],
        "availableSymbols": [snap_dict(x) for x in ranked_amount if x.has_trades or x.has_mtm],
        "recentlyTraded": recently_traded,
        "dailyStats": [
            {"time": day, "tradeCount": int(v["tradeCount"]), "tradeAmount": round(v["tradeAmount"], 2)}
            for day, v in sorted(daily_stats.items())
        ],
        "accountSnapshot": account_payload.get("accountSnapshot", {}),
        "riskSnapshot": account_payload.get("riskSnapshot", {}),
        "positionReconciliation": account_payload.get("positionReconciliation", {}),
        "ibkrOpenPositions": account_payload.get("ibkrOpenPositions", []),
    }


def parse_period_files(paths: List[Path]) -> Tuple[List[Trade], dict, dict, dict]:
    tlg_files = [path for path in paths if path.suffix.lower() == ".tlg"]
    csv_files = [path for path in paths if path.suffix.lower() == ".csv"]
    all_trades: Dict[str, Trade] = {}
    parsed_tlg_count = 0
    parsed_csv_count = 0
    if tlg_files:
        for path in tlg_files:
            for trade in parse_tlg(path):
                all_trades[trade_key(trade)] = trade
                parsed_tlg_count += 1
    else:
        for path in csv_files:
            title, _ = get_statement_title_and_period(path)
            if title in {"Activity Statement", "MTM Summary"}:
                continue
            for trade in parse_csv_transactions(path):
                all_trades[trade_key(trade)] = trade
                parsed_csv_count += 1
    activity_candidates: List[Tuple[datetime.date, Path]] = []
    for path in csv_files:
        title, period = get_statement_title_and_period(path)
        if title == "Activity Statement" and period:
            end_date = parse_period_end(period)
            if end_date:
                activity_candidates.append((end_date, path))
    activity_path = max(activity_candidates, key=lambda x: x[0])[1] if activity_candidates else None
    report_end_date = ""
    stocks_mtm: Dict[str, dict] = {}
    other_mtm: List[OtherPnlSnapshot] = []
    snapshot: dict = {}
    if activity_path:
        _, period_text = get_statement_title_and_period(activity_path)
        end_date = parse_period_end(period_text or "")
        report_end_date = end_date.strftime("%Y-%m-%d") if end_date else ""
        stocks_mtm, other_mtm = parse_mtm_file(activity_path)
        snapshot = parse_activity_statement_snapshot(activity_path)
    file_records = []
    for path in paths:
        title, period_text = (None, None)
        if path.suffix.lower() == ".csv":
            title, period_text = get_statement_title_and_period(path)
        file_records.append({
            "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
            "sha256": file_hash(path),
            "size": path.stat().st_size,
            "title": title,
            "period": period_text,
        })
    trades_sorted = sorted(
        [t for t in all_trades.values() if t.source != "demo"],
        key=lambda t: (t.trade_date, t.trade_time, t.symbol_lb, t.side, t.quantity, t.price),
    )
    mtm_payload = {
        "source": str(activity_path.relative_to(ROOT)) if activity_path and activity_path.is_relative_to(ROOT) else (str(activity_path) if activity_path else ""),
        "reportEndDate": report_end_date,
        "stocks": stocks_mtm,
        "otherPnl": [
            {
                "assetCategory": item.asset_category,
                "symbol": item.symbol,
                "displayName": item.display_name,
                "mtmTotal": item.mtm_total,
            }
            for item in other_mtm
        ],
    }
    manifest = {
        "builtAt": datetime.now().isoformat(timespec="seconds"),
        "reportEndDate": report_end_date,
        "files": file_records,
        "tradeCount": len(trades_sorted),
        "symbolCount": len({t.symbol_lb for t in trades_sorted}),
        "parsedTlgRows": parsed_tlg_count,
        "parsedCsvRows": parsed_csv_count,
        "frozen": True,
    }
    return trades_sorted, mtm_payload, manifest, snapshot


def freeze_historical_periods() -> dict:
    ensure_data_dirs()
    results = []
    if not HISTORICAL_INBOX_DIR.exists():
        return {"ok": True, "periods": results}
    all_rows = {trade_key(Trade(**row)): row for row in read_json(TRADES_ALL_STATE_FILE, []) if row.get("source") != "demo"}
    for year_dir in sorted([p for p in HISTORICAL_INBOX_DIR.iterdir() if p.is_dir()]):
        period = normalize_period(year_dir.name)
        paths = sorted([p for p in year_dir.iterdir() if p.suffix.lower() in {".tlg", ".csv"}])
        if not paths:
            continue
        trades, mtm_payload, manifest, snapshot = parse_period_files(paths)
        trade_rows = [asdict(t) for t in trades]
        write_json(period_state_file(period, "trades.json"), trade_rows)
        write_json(period_state_file(period, "mtm.json"), mtm_payload)
        write_json(period_state_file(period, "manifest.json"), manifest)
        if snapshot:
            write_json(period_state_file(period, "statement_snapshot.json"), snapshot)
            write_json(period_state_file(period, "ibkr_open_positions.json"), snapshot.get("ibkrOpenPositions", []))
            # For historical periods, FIFO open positions may not exist; store a placeholder reconciliation
            write_json(
                period_state_file(period, "position_reconciliation.json"),
                {"status": "unknown", "onlyInFifo": [], "onlyInIbkr": [], "quantityMismatch": [], "diffCount": 0},
            )
        for row in trade_rows:
            all_rows[trade_key(Trade(**row))] = row
        results.append({"period": period, "tradeCount": len(trade_rows), "reportEndDate": mtm_payload.get("reportEndDate", "")})
    if results and all_rows:
        write_json(TRADES_ALL_STATE_FILE, sorted(all_rows.values(), key=lambda r: (r.get("trade_date", ""), r.get("trade_time", ""), r.get("symbol_lb", ""))))
    clear_runtime_caches()
    return {"ok": True, "periods": results}


def archive_inbox_files(paths: List[Path]) -> List[str]:
    archived = []
    archive_day = ARCHIVE_DIR / datetime.now().strftime("%Y-%m-%d")
    archive_day.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.parent != INBOX_DIR or not path.exists():
            continue
        target = archive_day / path.name
        if target.exists():
            stem = target.stem
            suffix = target.suffix
            idx = 2
            while target.exists():
                target = archive_day / f"{stem}-{idx}{suffix}"
                idx += 1
        shutil.move(str(path), str(target))
        archived.append(str(target.relative_to(ROOT)))
    return archived


def prefetch_symbols_for_refresh(symbols: List[str], on_progress=None, force_refresh: bool = False) -> List[dict]:
    results = []
    ordered_symbols = list(dict.fromkeys(normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)))
    total = len(ordered_symbols)
    throttled_error = ""
    for idx, symbol_lb in enumerate(ordered_symbols):
        if on_progress:
            on_progress(idx + 1, total, symbol_lb)
        if throttled_error:
            results.append({
                "symbol": symbol_lb,
                "ok": False,
                "skipped": True,
                "error": "Yahoo is rate-limiting requests; remaining market-data requests were stopped",
            })
            continue
        trades = get_symbol_trades(symbol_lb)
        if trades:
            trade_dates = [datetime.strptime(t.trade_date, "%Y%m%d").date() for t in trades]
            start = min(trade_dates) - timedelta(days=1500)
            end = get_market_data_end_date()
        else:
            end = get_market_data_end_date()
            start = end - timedelta(days=1500)
        try:
            result = fetch_kline_history(
                symbol_lb,
                start.strftime("%Y-%m-%d"),
                end.strftime("%Y-%m-%d"),
                force_refresh=force_refresh,
            )
            rows = result.rows
            freshness = result.freshness
            error = freshness.get("error", "")
            ok = not (freshness.get("stale") and error)
            results.append({
                "symbol": symbol_lb,
                "ok": ok,
                "rows": len(rows),
                "cacheTo": candle_day(rows[-1]) if rows else "",
                "freshness": freshness,
                **({"error": error} if not ok else {}),
            })
            if is_yahoo_throttle_error(error):
                throttled_error = error
        except Exception as exc:
            error = str(exc)
            results.append({"symbol": symbol_lb, "ok": False, "error": error})
            if isinstance(exc, YahooThrottleError) or is_yahoo_throttle_error(error):
                throttled_error = error
    return results


def backup_latest_good_state() -> dict:
    backup_root = STATE_DIR / "backups" / "latest_good"
    files_to_backup = [
        TRADES_STATE_FILE,
        TRADES_ALL_STATE_FILE,
        MTM_STATE_FILE,
        MANIFEST_STATE_FILE,
        STATE_METADATA_FILE,
        RECENT_SYMBOLS_FILE,
        KLINE_MANIFEST_FILE,
        STATE_DIR / "performance.json",
        STATE_DIR / "audit_summary.json",
        STATE_DIR / "audit_report.md",
    ]
    for name in AUDIT_PERIOD_OUTPUT_FILES:
        files_to_backup.append(period_state_file(CURRENT_PERIOD, name))
    for name in ("trades.json", "mtm.json", "manifest.json"):
        files_to_backup.append(period_state_file(CURRENT_PERIOD, name))

    copied = []
    errors = []
    backup_root.parent.mkdir(parents=True, exist_ok=True)
    tmp_root = backup_root.with_name(f".latest_good.{os.getpid()}.{threading.get_ident()}.tmp")
    if tmp_root.exists():
        shutil.rmtree(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    for src in files_to_backup:
        if not src.exists() or not src.is_file():
            continue
        try:
            rel = src.relative_to(ROOT)
            dest = tmp_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(str(rel))
        except Exception as exc:
            errors.append({"path": str(src), "error": str(exc)})
    if backup_root.exists():
        shutil.rmtree(backup_root)
    os.replace(tmp_root, backup_root)
    result = {
        "path": relative_path_text(backup_root),
        "fileCount": len(copied),
        "files": copied,
        "errors": errors,
        "createdAt": datetime.now().isoformat(timespec="seconds"),
    }
    app_log("latest good state backup created", fileCount=len(copied), errorCount=len(errors))
    return result


def refresh_data(prefetch: bool = True, archive: bool = True) -> dict:
    try:
        return _refresh_data_impl(prefetch=prefetch, archive=archive)
    except Exception as exc:
        REFRESH_STATE["running"] = False
        REFRESH_STATE["error"] = str(exc)
        if not REFRESH_STATE.get("message") or not str(REFRESH_STATE.get("message")).startswith("Refresh failed"):
            REFRESH_STATE["message"] = f"Refresh failed: {exc}"
        log_exception("refresh failed", exc, prefetch=prefetch, archive=archive, step=REFRESH_STATE.get("step", ""))
        raise


def _refresh_data_impl(prefetch: bool = True, archive: bool = True) -> dict:
    global REFRESH_STATE
    app_log("refresh started", prefetch=prefetch, archive=archive)
    REFRESH_STATE["running"] = True
    REFRESH_STATE["progress"] = 0
    REFRESH_STATE["total"] = 100
    REFRESH_STATE["step"] = "init"
    REFRESH_STATE["message"] = "Preparing refresh..."
    REFRESH_STATE["result"] = None
    REFRESH_STATE["error"] = None

    def set_progress(progress, step, message):
        REFRESH_STATE["progress"] = progress
        REFRESH_STATE["step"] = step
        REFRESH_STATE["message"] = message

    ensure_data_dirs()
    set_progress(3, "backup", "Backing up the current valid data...")
    backup_snapshot = backup_latest_good_state()
    set_progress(5, "scan", "Scanning trade files...")
    processed_paths = candidate_data_files("*.tlg") + candidate_data_files("*.csv")
    app_log("refresh files scanned", fileCount=len(processed_paths), files=[str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p) for p in processed_paths])
    file_records = []
    for path in processed_paths:
        if not path.exists():
            continue
        title, period = (None, None)
        if path.suffix.lower() == ".csv":
            title, period = get_statement_title_and_period(path)
        file_records.append(
            {
                "path": str(path.relative_to(ROOT)),
                "sha256": file_hash(path),
                "size": path.stat().st_size,
                "title": title,
                "period": period,
            }
        )

    set_progress(10, "parse", "Parsing trade files...")
    all_trades: Dict[str, Trade] = {}
    for row in read_json(TRADES_STATE_FILE, []):
        trade = Trade(**row)
        if trade.source == "demo":
            continue
        all_trades[trade_key(trade)] = trade

    tlg_files = candidate_data_files("*.tlg")
    parsed_tlg_count = 0
    for path in tlg_files:
        for trade in parse_tlg(path):
            all_trades[trade_key(trade)] = trade
            parsed_tlg_count += 1

    parsed_csv_count = 0
    if not tlg_files:
        for path in candidate_data_files("*.csv"):
            title, _ = get_statement_title_and_period(path)
            if title in {"Activity Statement", "MTM Summary"}:
                continue
            for trade in parse_csv_transactions(path):
                all_trades[trade_key(trade)] = trade
                parsed_csv_count += 1

    set_progress(25, "trades", "Normalizing trading data...")
    trades_sorted = sorted(
        [t for t in all_trades.values() if t.source != "demo"],
        key=lambda t: (t.trade_date, t.trade_time, t.symbol_lb, t.side, t.quantity, t.price),
    )
    trades_payload = [asdict(t) for t in trades_sorted]
    write_json(TRADES_STATE_FILE, trades_payload)
    write_json(period_state_file(CURRENT_PERIOD, "trades.json"), trades_payload)
    all_trade_rows = {trade_key(Trade(**row)): row for row in read_json(TRADES_ALL_STATE_FILE, []) if row.get("source") != "demo"}
    for row in trades_payload:
        all_trade_rows[trade_key(Trade(**row))] = row
    write_json(TRADES_ALL_STATE_FILE, sorted(all_trade_rows.values(), key=lambda r: (r.get("trade_date", ""), r.get("trade_time", ""), r.get("symbol_lb", ""))))

    set_progress(35, "mtm", "Reading P&L data...")
    activity_path = pick_statement_file("Activity Statement")
    report_end_date = ""
    stocks_mtm: Dict[str, dict] = {}
    other_mtm: List[OtherPnlSnapshot] = []
    if activity_path:
        _, period = get_statement_title_and_period(activity_path)
        end_date = parse_period_end(period or "")
        report_end_date = end_date.strftime("%Y-%m-%d") if end_date else ""
        stocks_mtm, other_mtm = parse_mtm_file(activity_path)
    mtm_payload = {
        "source": str(activity_path.relative_to(ROOT)) if activity_path else "",
        "reportEndDate": report_end_date,
        "stocks": stocks_mtm,
        "otherPnl": [
            {
                "assetCategory": item.asset_category,
                "symbol": item.symbol,
                "displayName": item.display_name,
                "mtmTotal": item.mtm_total,
            }
            for item in other_mtm
        ],
    }
    write_json(MTM_STATE_FILE, mtm_payload)
    write_json(period_state_file(CURRENT_PERIOD, "mtm.json"), mtm_payload)

    set_progress(40, "snapshot", "Reading account snapshot...")
    snapshot = parse_activity_statement_snapshot(activity_path) if activity_path else {}
    if snapshot:
        account_snapshot = snapshot.get("accountSnapshot", {})
        ibkr_open_positions = snapshot.get("ibkrOpenPositions", [])
        write_json(STATEMENT_SNAPSHOT_FILE, snapshot)
        write_json(period_state_file(CURRENT_PERIOD, "statement_snapshot.json"), snapshot)
        write_json(IBKR_OPEN_POSITIONS_FILE, ibkr_open_positions)
        write_json(period_state_file(CURRENT_PERIOD, "ibkr_open_positions.json"), ibkr_open_positions)
        fifo_positions = read_json(STATE_DIR / "open_positions.json", [])
        reconciliation = build_position_reconciliation(fifo_positions, ibkr_open_positions)
        write_json(POSITION_RECONCILIATION_FILE, reconciliation)
        write_json(period_state_file(CURRENT_PERIOD, "position_reconciliation.json"), reconciliation)

    set_progress(45, "overview", "Building overview data...")
    clear_runtime_caches()
    overview = build_overview_payload()
    prefetch_symbols = build_refresh_prefetch_symbols(overview, trades_sorted, stocks_mtm)

    set_progress(50, "kline", f"Refreshing recent market data for {len(prefetch_symbols)} symbols...")
    def on_kline_progress(current, total, symbol):
        set_progress(50 + int(current / total * 45), "kline", f"Refresh recent quotes/candles: {symbol} ({current}/{total})")

    prefetch_results = prefetch_symbols_for_refresh(prefetch_symbols, on_progress=on_kline_progress) if prefetch else []

    set_progress(98, "archive", "Archiving files...")
    archived = archive_inbox_files(processed_paths) if archive else []
    manifest = {
        "lastRefreshAt": datetime.now().isoformat(timespec="seconds"),
        "reportEndDate": report_end_date,
        "marketDataEndDate": get_market_data_end_date().strftime("%Y-%m-%d"),
        "files": file_records,
        "archived": archived,
        "tradeCount": len(trades_sorted),
        "symbolCount": len({t.symbol_lb for t in trades_sorted}),
        "parsedTlgRows": parsed_tlg_count,
        "parsedCsvRows": parsed_csv_count,
        "prefetchEnabled": prefetch,
        "prefetch": prefetch_results,
        "backup": backup_snapshot,
    }
    write_json(MANIFEST_STATE_FILE, manifest)
    write_json(period_state_file(CURRENT_PERIOD, "manifest.json"), manifest)
    clear_runtime_caches()
    set_progress(100, "done", "Data refresh complete")
    REFRESH_STATE["running"] = False
    REFRESH_STATE["result"] = manifest
    app_log("refresh completed", tradeCount=len(trades_sorted), symbolCount=len({t.symbol_lb for t in trades_sorted}), reportEndDate=report_end_date, archived=len(archived), prefetchFailures=sum(1 for item in prefetch_results if not item.get("ok")))
    return {
        "ok": True,
        "message": "Data refresh complete",
        "manifest": manifest,
    }


def get_symbol_trades(symbol_lb: str) -> List[Trade]:
    trades = [t for t in load_ibkr_trades() if t.symbol_lb == symbol_lb]
    return sorted(trades, key=lambda t: (t.trade_date, t.trade_time, t.side))


def build_replay_payload(symbol_input: str, cache_only: bool = False, force_refresh: bool = False) -> dict:
    symbol_lb = normalize_symbol(symbol_input)
    record_recent_symbol(symbol_lb)
    overview = build_overview_payload()
    meta = {x["symbol"]: x for x in overview["availableSymbols"]}.get(
        symbol_lb,
        {
            "symbol": symbol_lb,
            "displayName": symbol_lb,
            "amount": 0.0,
            "tradeCount": 0,
            "mtmTotal": 0.0,
            "hasTrades": False,
            "hasMtm": False,
        },
    )

    trades = get_symbol_trades(symbol_lb)
    if trades:
        trade_dates = [datetime.strptime(t.trade_date, "%Y%m%d").date() for t in trades]
        start = min(trade_dates) - timedelta(days=1500)
        end = max(max(trade_dates) + timedelta(days=10), get_market_data_end_date())
        message = ""
    else:
        end = get_market_data_end_date()
        start = end - timedelta(days=1500)
        message = "There are no personal trades in this period. Only market data and indicators are shown."

    end_text = end.strftime("%Y-%m-%d")
    kline_result = fetch_kline_history(symbol_lb, start.strftime("%Y-%m-%d"), end_text, cache_only=cache_only, force_refresh=force_refresh)
    candles = kline_result.rows
    if not candles:
        if cache_only:
            return {"symbol": symbol_lb, "displayName": meta["displayName"], "hasTrades": bool(trades), "cacheMiss": True}
        return {"error": f"No kline returned for {symbol_lb}."}
    freshness = kline_result.freshness
    freshness_manifest = kline_manifest().get(kline_manifest_key(symbol_lb), {})
    freshness["lastAttemptAt"] = freshness_manifest.get("lastAttemptAt", "")
    freshness["retryAfterSeconds"] = YAHOO_ERROR_RETRY_SECONDS
    freshness["retryAllowed"] = yahoo_retry_allowed(freshness_manifest)
    market_status = latest_candle_market_status(symbol_lb, candle_day(candles[-1]) if candles else "")
    if freshness.get("stale"):
        detail = f"Market data is incomplete; using local cache through {freshness.get('cacheEnd') or '--'}."
        if freshness.get("skippedToday"):
            detail += " An update was already attempted today; duplicate request skipped."
        if freshness.get("error"):
            detail += f" Error: {freshness['error']}"
        message = f"{message} {detail}".strip()
    elif freshness.get("error"):
        detail = f"Yahoo Finance refresh failed; using the local cache. Error: {freshness['error']}"
        message = f"{message} {detail}".strip()

    closes = [safe_float(c["close"]) for c in candles]
    ma_series: Dict[str, List[dict]] = {}
    for window in MA_WINDOWS:
        values = compute_ma(closes, window)
        points = []
        for idx, value in enumerate(values):
            if value is not None:
                points.append({"time": candles[idx]["time"][:10], "value": round(value, 6)})
        ma_series[f"MA{window}"] = points

    # Load setups for enriching trades with per-trade setup_type
    from app.trade_setup_manager import get_setup_for_trade

    by_day: Dict[str, List[Trade]] = defaultdict(list)
    for trade in trades:
        day = datetime.strptime(trade.trade_date, "%Y%m%d").strftime("%Y-%m-%d")
        by_day[day].append(trade)

    rows = []
    for candle in candles:
        day = candle["time"][:10]
        day_trades = []
        for t in by_day.get(day, []):
            td = asdict(t)
            # Enrich with per-trade setup_type (symbol + trade_date + trade_time)
            setup = get_setup_for_trade(t.symbol_lb, t.trade_date, t.trade_time)
            if setup:
                td["setup_type"] = setup.get("setupType", "")
                td["setup_note"] = setup.get("note", "")
            else:
                td["setup_type"] = ""
                td["setup_note"] = ""
            day_trades.append(td)
        rows.append(
            {
                "time": day,
                "open": safe_float(candle["open"]),
                "high": safe_float(candle["high"]),
                "low": safe_float(candle["low"]),
                "close": safe_float(candle["close"]),
                "turnover": safe_float(candle.get("turnover", "0")),
                "myTrades": day_trades,
            }
        )

    total_buy = sum(t.quantity for t in trades if t.side == "BUY")
    total_sell = sum(t.quantity for t in trades if t.side == "SELL")
    buy_amt = sum(t.gross_amount for t in trades if t.side == "BUY")
    sell_amt = sum(t.gross_amount for t in trades if t.side == "SELL")
    avg_buy = buy_amt / total_buy if total_buy else 0.0
    avg_sell = sell_amt / total_sell if total_sell else 0.0

    day_net = {}
    for day, items in by_day.items():
        day_net[day] = sum(x.quantity if x.side == "BUY" else -x.quantity for x in items)

    max_add = max(day_net.items(), key=lambda x: x[1]) if day_net else ("--", 0.0)
    max_reduce = min(day_net.items(), key=lambda x: x[1]) if day_net else ("--", 0.0)
    insufficient_ma = [f"MA{window}" for window in MA_WINDOWS if len(candles) < window]

    # Win rate from closed trades
    win_rate = 0.0
    try:
        closed_path = STATE_DIR / "closed_trades.json"
        if closed_path.exists():
            closed = json.loads(closed_path.read_text(encoding="utf-8"))
            symbol_closed = [t for t in closed if t.get("symbol") == symbol_lb]
            if symbol_closed:
                wins = sum(1 for t in symbol_closed if t.get("realized_pnl", 0) > 0)
                win_rate = round(wins / len(symbol_closed) * 100, 1)
    except Exception:
        pass

    return {
        "symbol": symbol_lb,
        "displayName": meta["displayName"],
        "hasTrades": bool(trades),
        "message": message,
        "klineFreshness": freshness,
        "marketStatus": market_status,
        "tradePlan": get_trade_plan(symbol_lb),
        "setupTypes": SETUP_TYPES,
        "candles": rows,
        "maSeries": ma_series,
        "summary": {
            "buyCount": sum(1 for t in trades if t.side == "BUY"),
            "sellCount": sum(1 for t in trades if t.side == "SELL"),
            "winRate": win_rate,
            "avgBuyPrice": round(avg_buy, 1),
            "avgSellPrice": round(avg_sell, 1),
            "maxAddDay": {"date": max_add[0], "shares": max_add[1]},
            "maxReduceDay": {"date": max_reduce[0], "shares": max_reduce[1]},
            "tradeAmount": round(meta.get("amount", 0.0), 4),
            "tradeCount": int(meta.get("tradeCount", 0)),
            "mtmTotal": round(meta.get("mtmTotal", 0.0), 6),
        },
        "insufficientMa": insufficient_ma,
    }


def build_replay_payload_cached(symbol_input: str, cache_only: bool = False, force_refresh: bool = False) -> dict:
    symbol_lb = normalize_symbol(symbol_input)
    record_recent_symbol(symbol_lb)
    if cache_only:
        return build_replay_payload(symbol_lb, cache_only=True)
    key = replay_cache_key(symbol_lb)
    if not force_refresh:
        cached = REPLAY_PAYLOAD_CACHE.get(key)
        if cached is not None:
            return cached
    payload = build_replay_payload(symbol_lb, force_refresh=force_refresh)
    REPLAY_PAYLOAD_CACHE[replay_cache_key(symbol_lb)] = payload
    return payload


def watchlist_position_map() -> Dict[str, dict]:
    rows = read_json(IBKR_OPEN_POSITIONS_FILE, [])
    if not rows:
        rows = read_json(STATE_DIR / "open_positions.json", [])
    positions = {}
    for row in rows if isinstance(rows, list) else []:
        symbol = normalize_symbol(row.get("symbolLb") or row.get("symbol_lb") or row.get("symbol") or "")
        if symbol:
            positions[symbol] = row
    return positions


def watchlist_quote_map(symbols: List[str]) -> Dict[str, dict]:
    quotes: Dict[str, dict] = {}
    for symbol in sorted({normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)}):
        try:
            candles = normalize_candles(load_kline_cache(symbol))
        except Exception as exc:
            app_log("watchlist quote cache unavailable", level="warning", symbol=symbol, error=str(exc))
            candles = []
        latest = candles[-1] if candles else {}
        previous = candles[-2] if len(candles) >= 2 else {}
        close = safe_float(latest.get("close", 0)) if latest else 0
        prev_close = safe_float(previous.get("close", 0)) if previous else 0
        change_pct = ((close / prev_close - 1) * 100) if close and prev_close else None
        quotes[symbol] = {
            "latestDate": latest.get("time", "") if latest else "",
            "latestClose": close or None,
            "changePct": round(change_pct, 2) if change_pct is not None else None,
        }
    return quotes


def read_symbol_name_cache() -> Dict[str, dict]:
    raw = read_json(SYMBOL_NAMES_FILE, {})
    if not isinstance(raw, dict):
        return {}
    cache: Dict[str, dict] = {}
    for symbol, value in raw.items():
        normalized = normalize_symbol(symbol)
        if not normalized:
            continue
        if isinstance(value, dict):
            name = str(value.get("name") or "").strip()
            if name:
                cache[normalized] = {**value, "name": name}
        else:
            name = str(value or "").strip()
            if name:
                cache[normalized] = {"name": name, "source": "legacy"}
    return cache


def write_symbol_name_cache(cache: Dict[str, dict]) -> None:
    payload = {}
    for symbol, value in sorted(cache.items()):
        name = str((value or {}).get("name") or "").strip()
        if name:
            payload[normalize_symbol(symbol)] = {**value, "name": name}
    write_json(SYMBOL_NAMES_FILE, payload)


def yahoo_quote_symbol_map(symbols: List[str]) -> Dict[str, str]:
    mapped: Dict[str, str] = {}
    for raw in symbols:
        symbol = normalize_symbol(raw)
        if not symbol:
            continue
        yahoo_sym = yahoo_symbol(symbol)
        if yahoo_sym:
            mapped[symbol] = yahoo_sym
    return mapped


def fetch_yahoo_company_names(symbols: List[str]) -> Dict[str, str]:
    yahoo_by_symbol = yahoo_quote_symbol_map(symbols)
    if not yahoo_by_symbol:
        return {}
    symbol_by_yahoo = {yahoo_sym: symbol for symbol, yahoo_sym in yahoo_by_symbol.items()}
    url = "https://query1.finance.yahoo.com/v7/finance/quote"
    results: Dict[str, str] = {}
    yahoo_symbols = sorted(symbol_by_yahoo)
    app_log("yahoo name request", count=len(yahoo_symbols))
    quote_requests_ok = 0
    for offset in range(0, len(yahoo_symbols), 50):
        batch = yahoo_symbols[offset : offset + 50]
        try:
            resp = requests.get(
                url,
                params={"symbols": ",".join(batch)},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=YAHOO_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            app_log("yahoo name request failed", level="warning", error=str(exc), count=len(batch))
            continue
        quote_requests_ok += 1
        for row in data.get("quoteResponse", {}).get("result", []) if isinstance(data, dict) else []:
            if not isinstance(row, dict):
                continue
            yahoo_sym = str(row.get("symbol") or "").upper()
            symbol = symbol_by_yahoo.get(yahoo_sym)
            name = str(row.get("longName") or row.get("shortName") or row.get("displayName") or "").strip()
            if symbol and name:
                results[symbol] = name
    missing = [symbol for symbol in yahoo_by_symbol if symbol not in results]
    if quote_requests_ok and len(missing) <= YAHOO_NAME_CHART_FALLBACK_LIMIT:
        results.update(fetch_yahoo_chart_names(missing))
    return results


def fetch_yahoo_chart_names(symbols: List[str]) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for symbol, yahoo_sym in yahoo_quote_symbol_map(symbols).items():
        try:
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}",
                params={"range": "1d", "interval": "1d"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=YAHOO_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            app_log("yahoo chart name request failed", level="warning", symbol=symbol, error=str(exc))
            continue
        result = (data.get("chart", {}).get("result") or [{}])[0] if isinstance(data, dict) else {}
        meta = result.get("meta", {}) if isinstance(result, dict) else {}
        name = str(meta.get("longName") or meta.get("shortName") or "").strip()
        if name:
            results[symbol] = name
    return results


def refresh_watchlist_company_names(symbols: List[str], force: bool = False) -> dict:
    normalized = sorted({normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)})
    cache = read_symbol_name_cache()
    local_names = watchlist_company_name_map(include_cache=False)
    missing = [symbol for symbol in normalized if force or not (local_names.get(symbol) or cache.get(symbol, {}).get("name"))]
    fetched = fetch_yahoo_company_names(missing)
    now = datetime.now().isoformat(timespec="seconds")
    for symbol, name in fetched.items():
        cache[symbol] = {"name": name, "source": "yahoo", "updatedAt": now, "yahooSymbol": yahoo_symbol(symbol)}
    if fetched:
        write_symbol_name_cache(cache)
    return {
        "requested": len(normalized),
        "missing": len(missing),
        "fetched": len(fetched),
        "unresolved": [symbol for symbol in missing if symbol not in fetched],
    }


def watchlist_company_name_map(include_cache: bool = True) -> Dict[str, str]:
    names: Dict[str, str] = {}

    def add_row(symbol_hint: str, row: dict) -> None:
        if not isinstance(row, dict):
            return
        normalized = normalize_symbol(row.get("symbolLb") or row.get("symbol_lb") or symbol_hint)
        description = str(row.get("description") or row.get("displayName") or "").strip()
        if normalized and description and normalized not in names:
            names[normalized] = description

    for path in (STATE_DIR / "statement_snapshot.json", period_state_file(CURRENT_PERIOD, "statement_snapshot.json")):
        snapshot = read_json(path, {})
        if not isinstance(snapshot, dict):
            continue
        for section_name in ("financialInstrumentInfo", "positions", "ibkrOpenPositions"):
            section = snapshot.get(section_name, {})
            if isinstance(section, dict):
                for symbol, row in section.items():
                    add_row(symbol, row)
            elif isinstance(section, list):
                for row in section:
                    add_row("", row)
        for symbol, row in snapshot.items():
            add_row(symbol, row)
    if include_cache:
        for symbol, row in read_symbol_name_cache().items():
            name = str(row.get("name") or "").strip()
            if symbol and name and symbol not in names:
                names[symbol] = name
    return names


def enrich_watchlist_items(items: List[dict]) -> List[dict]:
    positions = watchlist_position_map()
    quotes = watchlist_quote_map([item.get("symbol", "") for item in items])
    company_names = watchlist_company_name_map()
    for item in items:
        position = positions.get(item.get("symbol", ""))
        quote = quotes.get(item.get("symbol", ""), {})
        item["hasPosition"] = bool(position)
        item["positionSize"] = safe_float(position.get("quantity", 0)) if position else 0
        item["positionValue"] = safe_float(position.get("value", 0)) if position else 0
        item["unrealizedPnl"] = safe_float(position.get("unrealizedPl", position.get("unrealized_pnl", 0))) if position else 0
        item["latestDate"] = quote.get("latestDate", "")
        item["latestClose"] = quote.get("latestClose")
        item["changePct"] = quote.get("changePct")
        item["companyName"] = item.get("name") or company_names.get(item.get("symbol", ""), "")
    return items


def watchlist_state_payload() -> dict:
    payload = watchlist_manager.app_state()
    payload["items"] = enrich_watchlist_items(payload.get("items", []))
    positioned = sum(1 for item in payload["items"] if item.get("hasPosition"))
    payload.setdefault("summary", {})["positioned"] = positioned
    if not payload.get("defaultSymbol") and payload["items"]:
        payload["defaultSymbol"] = payload["items"][0]["symbol"]
    return payload


def watchlist_symbol_payload(symbol_input: str, refresh: bool = False) -> dict:
    symbol = normalize_symbol(symbol_input)
    try:
        replay = build_replay_payload_cached(symbol, force_refresh=refresh)
    except Exception as exc:
        app_log("watchlist symbol replay unavailable", level="warning", symbol=symbol, error=str(exc))
        position = watchlist_position_map().get(symbol, {})
        return {
            "ok": True,
            "symbol": symbol,
            "items": enrich_watchlist_items(watchlist_manager.get_symbol_items(symbol)),
            "chart": {
                "candles": [],
                "maSeries": {},
                "klineFreshness": {"symbol": symbol, "error": str(exc), "stale": True},
                "marketStatus": {},
                "error": str(exc),
            },
            "quote": {
                "latestDate": "",
                "latestClose": None,
                "changePct": 0,
                "volume": None,
            },
            "ibkr": {
                "hasPosition": bool(position),
                "positionSize": safe_float(position.get("quantity", 0)) if position else 0,
                "positionValue": safe_float(position.get("value", 0)) if position else 0,
                "unrealizedPnl": safe_float(position.get("unrealizedPl", position.get("unrealized_pnl", 0))) if position else 0,
                "tradeCount": 0,
                "summary": {},
            },
        }
    candles = replay.get("candles", [])
    latest = candles[-1] if candles else {}
    previous = candles[-2] if len(candles) >= 2 else {}
    close = safe_float(latest.get("close", 0)) if latest else 0
    prev_close = safe_float(previous.get("close", 0)) if previous else 0
    change_pct = ((close / prev_close - 1) * 100) if close and prev_close else 0
    position = watchlist_position_map().get(symbol, {})
    trades = replay.get("trades", [])
    return {
        "ok": True,
        "symbol": symbol,
        "items": enrich_watchlist_items(watchlist_manager.get_symbol_items(symbol)),
        "chart": {
            "candles": candles,
            "maSeries": replay.get("maSeries", {}),
            "klineFreshness": replay.get("klineFreshness", {}),
            "marketStatus": replay.get("marketStatus", {}),
        },
        "quote": {
            "latestDate": latest.get("time", ""),
            "latestClose": close or None,
            "changePct": round(change_pct, 2),
            "volume": latest.get("volume"),
        },
        "ibkr": {
            "hasPosition": bool(position),
            "positionSize": safe_float(position.get("quantity", 0)) if position else 0,
            "positionValue": safe_float(position.get("value", 0)) if position else 0,
            "unrealizedPnl": safe_float(position.get("unrealizedPl", position.get("unrealized_pnl", 0))) if position else 0,
            "tradeCount": len(trades),
            "summary": replay.get("summary", {}),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/":
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            return self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
        if parsed.path.startswith("/static/"):
            file_path = STATIC_DIR / parsed.path[len("/static/"):]
            if file_path.exists() and file_path.is_file() and str(file_path.resolve()).startswith(str(STATIC_DIR.resolve())):
                content_type = "application/octet-stream"
                if parsed.path.endswith(".png"):
                    content_type = "image/png"
                elif parsed.path.endswith(".jpg") or parsed.path.endswith(".jpeg"):
                    content_type = "image/jpeg"
                elif parsed.path.endswith(".svg"):
                    content_type = "image/svg+xml"
                elif parsed.path.endswith(".css"):
                    content_type = "text/css"
                elif parsed.path.endswith(".js"):
                    content_type = "application/javascript"
                elif parsed.path.endswith(".json"):
                    content_type = "application/json; charset=utf-8"
                return self._send(200, content_type, file_path.read_bytes())
            return self._send(404, "text/plain", b"file not found")
        if parsed.path in (
            "/site-icon.png",
            "/favicon.png",
            "/favicon.ico",
            "/tradecraft-favicon.svg",
            "/tradecraft-mark.svg",
            "/tradecraft-mark-reverse.svg",
            "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png",
        ):
            icon_path = STATIC_DIR / {
                "/favicon.png": "favicon.png",
                "/favicon.ico": "favicon.png",
                "/tradecraft-favicon.svg": "tradecraft-favicon.svg",
                "/tradecraft-mark.svg": "tradecraft-mark.svg",
                "/tradecraft-mark-reverse.svg": "tradecraft-mark-reverse.svg",
                "/apple-touch-icon.png": "apple-touch-icon.png",
                "/apple-touch-icon-precomposed.png": "apple-touch-icon.png",
            }.get(parsed.path, "site-icon.png")
            if icon_path.exists():
                content_type = "image/svg+xml" if icon_path.suffix == ".svg" else "image/png"
                return self._send(200, content_type, icon_path.read_bytes())
            return self._send(404, "text/plain", b"icon not found")
        if parsed.path == "/api/periods":
            periods = list_available_periods()
            return self._json({
                "currentPeriod": CURRENT_PERIOD,
                "periods": [{"id": p, "label": period_label(p)} for p in periods],
            })
        if parsed.path == "/api/health":
            try:
                return self._json(build_health_payload())
            except Exception as exc:
                app_log("health endpoint failed", level="error", error=str(exc))
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/settings":
            try:
                return self._json({"settings": load_settings(), "availablePeriods": list_available_periods()})
            except Exception as exc:
                app_log("settings endpoint failed", level="error", error=str(exc))
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/watchlist-triggers":
            try:
                return self._json({"ok": True, "triggers": load_watchlist_triggers()})
            except Exception as exc:
                app_log("watchlist triggers endpoint failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/watchlist":
            try:
                return self._json(watchlist_state_payload())
            except Exception as exc:
                app_log("watchlist endpoint failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/watchlist/symbol":
            try:
                symbol = params.get("symbol", [DEFAULT_SYMBOL])[0]
                refresh = params.get("refresh", ["0"])[0] == "1"
                return self._json(watchlist_symbol_payload(symbol, refresh=refresh))
            except Exception as exc:
                app_log("watchlist symbol endpoint failed", level="error", query=parsed.query, error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/overview":
            period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
            return self._json(build_overview_payload(period))
        if parsed.path == "/api/account-snapshot":
            period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
            return self._json(build_account_snapshot_payload(period))
        if parsed.path == "/api/replay":
            try:
                symbol = params.get("symbol", [DEFAULT_SYMBOL])[0]
                cache_only = params.get("cache_only", ["0"])[0] == "1"
                force_refresh = params.get("refresh", ["0"])[0] == "1"
                payload = build_replay_payload_cached(symbol, cache_only=cache_only, force_refresh=force_refresh)
                if cache_only and payload.get("cacheMiss"):
                    return self._json(payload, code=404)
                return self._json(payload)
            except Exception as exc:
                app_log("replay endpoint failed", level="error", path=parsed.path, query=parsed.query, error=str(exc))
                return self._json({"error": str(exc)}, code=500)
        if parsed.path in {"/api/demo", "/api/demo/status"}:
            from app.demo_data import demo_status
            status = demo_status(ROOT)
            return self._json({
                **status,
                "messageKey": "api.demoActive" if status.get("active") else "api.demoExited",
                "messageParams": {},
            })
        if parsed.path == "/api/trade-plan":
            try:
                symbol = params.get("symbol", [DEFAULT_SYMBOL])[0]
                return self._json({"symbol": normalize_symbol(symbol), "plan": get_trade_plan(symbol), "setupTypes": SETUP_TYPES})
            except Exception as exc:
                app_log("trade plan get failed", level="error", query=parsed.query, error=str(exc))
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/audit":
            try:
                period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
                audit_path = period_state_file(period, "audit_summary.json")
                if not audit_path.exists() and period == CURRENT_PERIOD:
                    audit_path = STATE_DIR / "audit_summary.json"
                if audit_path.exists():
                    data = json.loads(audit_path.read_text(encoding="utf-8"))
                    data["period"] = period
                    data["periodLabel"] = period_label(period)
                    if isinstance(data.get("behavior_radar"), dict):
                        data["behavior_radar"]["period"] = period
                    return self._json(data)
                if period == "ALL":
                    return self._json(build_all_period_audit_summary())
                return self._json({"error": f"Audit not run yet for {period}."}, code=404)
            except Exception as exc:
                app_log("performance endpoint failed", level="error", error=str(exc))
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/audit/workbench":
            try:
                period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
                return self._json(
                    build_audit_workbench_for_period(period, persist=False)
                )
            except Exception as exc:
                app_log("audit workbench endpoint failed", level="error", query=parsed.query, error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/audit/evidence":
            try:
                from app.audit_workbench import ROUND_TRIPS_FILE, evidence_page

                period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
                snapshot = build_audit_workbench_for_period(period, persist=False)
                round_trip_path = period_state_file(period, ROUND_TRIPS_FILE)
                round_trip_payload = read_json(round_trip_path, {})
                if round_trip_payload.get("snapshotId") != snapshot.get("snapshotId"):
                    build_audit_workbench_for_period(period, persist=True)
                    round_trip_payload = read_json(round_trip_path, {})
                finding_id = params.get("findingId", [""])[0]
                finding = next(
                    (item for item in snapshot.get("findings", []) if item.get("findingId") == finding_id),
                    None,
                )
                if finding_id and not finding:
                    return self._json({"ok": False, "error": "Audit finding not found"}, code=404)
                result = evidence_page(
                    round_trip_payload,
                    finding=finding,
                    page=int(params.get("page", ["1"])[0]),
                    page_size=int(params.get("pageSize", ["50"])[0]),
                    symbol=params.get("symbol", [""])[0],
                    setup=params.get("setup", [""])[0],
                )
                return self._json(result)
            except (TypeError, ValueError) as exc:
                return self._json({"ok": False, "error": str(exc)}, code=400)
            except Exception as exc:
                app_log("audit evidence endpoint failed", level="error", query=parsed.query, error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/audit/report":
            try:
                period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
                report_path = period_state_file(period, "audit_report.md")
                if not report_path.exists() and period == CURRENT_PERIOD:
                    report_path = STATE_DIR / "audit_report.md"
                if report_path.exists():
                    content = report_path.read_text(encoding="utf-8")
                    return self._send(200, "text/markdown; charset=utf-8", content.encode("utf-8"))
                return self._send(404, "text/plain", f"Report not generated yet for {period}.".encode("utf-8"))
            except Exception as exc:
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/performance":
            try:
                from app.performance_tracker import build_performance_payload
                return self._json(build_performance_payload())
            except Exception as exc:
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/refresh-progress":
            return self._json({
                "ok": True,
                "running": REFRESH_STATE["running"],
                "progress": REFRESH_STATE["progress"],
                "total": REFRESH_STATE["total"],
                "step": REFRESH_STATE["step"],
                "message": REFRESH_STATE["message"],
                "error": REFRESH_STATE["error"],
            })
        if parsed.path == "/api/closed-trades/recent":
            try:
                from app.state_store import read_json as _read
                from app.trade_setup_manager import get_setup_for_trade
                trades = _read(STATE_DIR / "closed_trades.json", [])
                raw_trades = _read(STATE_DIR / "trades.json", [])
                limit = int(params.get("limit", ["20"])[0])
                # Sort by close_date desc, return most recent
                recent = sorted(trades, key=lambda t: t.get("close_date", ""), reverse=True)[:limit]
                # Enrich with direction and per-trade setup from raw trades
                from collections import defaultdict
                by_sym_date = defaultdict(list)
                for t in raw_trades:
                    by_sym_date[(t.get("symbol_lb"), t.get("trade_date"))].append(t)
                for ct in recent:
                    sym = ct.get("symbol", "")
                    od = ct.get("open_date", "")
                    cd = ct.get("close_date", "")
                    ts_open = sorted(by_sym_date.get((sym, od), []), key=lambda x: x.get("trade_time", ""))
                    ts_close = sorted(by_sym_date.get((sym, cd), []), key=lambda x: x.get("trade_time", ""))
                    ct["direction"] = ts_open[0].get("side", "BUY") if ts_open else "BUY"
                    # Enrich with per-trade setup types (first BUY / first SELL of the day)
                    first_buy = next((t for t in ts_open if t.get("side") == "BUY"), None)
                    first_sell = next((t for t in ts_close if t.get("side") == "SELL"), None)
                    if first_buy:
                        setup = get_setup_for_trade(sym, od, first_buy.get("trade_time", ""))
                        if setup:
                            ct["setup_type"] = setup.get("setupType", ct.get("setup_type", ""))
                    if first_sell:
                        exit_setup = get_setup_for_trade(sym, cd, first_sell.get("trade_time", ""))
                        if exit_setup:
                            ct["exit_setup_type"] = exit_setup.get("setupType", ct.get("exit_setup_type", ""))
                return self._json({"ok": True, "trades": recent, "total": len(trades)})
            except Exception as exc:
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/trades":
            try:
                from app.trade_setup_manager import get_setup_for_trade
                period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
                trades = load_period_trades(period)
                rows = []
                for t in trades:
                    d = asdict(t)
                    setup = get_setup_for_trade(t.symbol_lb, t.trade_date, t.trade_time)
                    d["setup_type"] = setup.get("setupType", "") if setup else ""
                    rows.append(d)
                # Group by symbol, sort within group by (date, time) desc
                groups_map = {}
                for r in rows:
                    sym = r.get("symbol_lb") or r.get("symbol", "")
                    groups_map.setdefault(sym, []).append(r)
                groups = []
                for sym in sorted(groups_map.keys()):
                    sym_trades = sorted(
                        groups_map[sym],
                        key=lambda x: (x.get("trade_date", ""), x.get("trade_time", "")),
                        reverse=True,
                    )
                    groups.append({
                        "symbol": sym,
                        "tradeCount": len(sym_trades),
                        "trades": sym_trades,
                    })
                return self._json({"ok": True, "groups": groups, "total": len(rows)})
            except Exception as exc:
                return self._json({"error": str(exc)}, code=500)
        # --- Trader Memory API ---
        if parsed.path == "/api/trader-memory":
            try:
                from app.state_store import read_json as _read
                data = _read(STATE_DIR / "trader_memory.json", {})
                return self._json({"ok": True, "data": data})
            except Exception as exc:
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/style-drift":
            try:
                from app.state_store import read_json as _read
                data = _read(STATE_DIR / "style_drift.json", {})
                return self._json({"ok": True, "data": data})
            except Exception as exc:
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/trade-setup":
            try:
                from app.trade_setup_manager import get_setup_for_trade, SETUP_TYPES
                symbol = params.get("symbol", [DEFAULT_SYMBOL])[0]
                open_date = params.get("openDate", [""])[0]
                setup = get_setup_for_trade(symbol, open_date)
                return self._json({
                    "ok": True,
                    "symbol": symbol,
                    "openDate": open_date,
                    "setup": setup,
                    "setupTypes": SETUP_TYPES,
                })
            except Exception as exc:
                return self._json({"error": str(exc)}, code=500)
        if parsed.path == "/api/trade-setups/pending":
            try:
                from app.state_store import read_json as _read
                setups = _read(STATE_DIR / "trade_setups.json", {})
                pending = [
                    v for v in setups.values()
                    if v.get("source") == "auto"
                ]
                limit = int(params.get("limit", ["50"])[0])
                offset = int(params.get("offset", ["0"])[0])
                return self._json({
                    "ok": True,
                    "total": len(pending),
                    "items": pending[offset:offset + limit],
                    "setupTypes": SETUP_TYPES,
                })
            except Exception as exc:
                return self._json({"error": str(exc)}, code=500)
        return self._json({"error": "not found"}, code=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/api/demo/reset":
            try:
                initialize_demo_workspace(force=True)
                from app.demo_data import demo_status
                return self._json({
                    **demo_status(ROOT),
                    "messageKey": "api.demoReset",
                    "messageParams": {},
                })
            except Exception as exc:
                app_log("demo reset failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/demo/exit":
            try:
                from app.demo_data import exit_demo
                result = exit_demo(ROOT)
                clear_runtime_caches()
                return self._json({
                    **result,
                    "messageKey": "api.demoExited",
                    "messageParams": {},
                })
            except Exception as exc:
                app_log("demo exit failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/audit/finding-feedback":
            try:
                from app.audit_workbench import save_finding_feedback

                payload = self._read_json_payload()
                period = normalize_period(payload.get("period") or CURRENT_PERIOD)
                snapshot = build_audit_workbench_for_period(period, persist=False)
                finding_id = str(payload.get("findingId") or "")
                if not any(item.get("findingId") == finding_id for item in snapshot.get("findings", [])):
                    return self._json({"ok": False, "error": "Audit finding not found"}, code=404)
                feedback = save_finding_feedback(
                    STATE_DIR,
                    period=period,
                    finding_id=finding_id,
                    decision=str(payload.get("decision") or "undecided"),
                    note=str(payload.get("note") or ""),
                )
                return self._json({"ok": True, "feedback": feedback})
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, code=400)
            except Exception as exc:
                app_log("audit finding feedback failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/audit/rule-cycle":
            try:
                from app.audit_workbench import create_rule_cycle, stop_rule_cycle

                payload = self._read_json_payload()
                if payload.get("action") == "stop":
                    cycle = stop_rule_cycle(STATE_DIR, str(payload.get("cycleId") or ""))
                    return self._json({"ok": True, "cycle": cycle})
                period = normalize_period(payload.get("period") or CURRENT_PERIOD)
                snapshot = build_audit_workbench_for_period(period, persist=False)
                finding_id = str(payload.get("findingId") or "")
                finding = next(
                    (item for item in snapshot.get("findings", []) if item.get("findingId") == finding_id),
                    None,
                )
                if not finding:
                    return self._json({"ok": False, "error": "Audit finding not found"}, code=404)
                cycle = create_rule_cycle(
                    STATE_DIR,
                    period=period,
                    snapshot=snapshot,
                    finding=finding,
                    rule_text=str(payload.get("ruleText") or ""),
                    operator=str(payload.get("operator") or ""),
                    target_value=payload.get("targetValue"),
                )
                return self._json({"ok": True, "cycle": cycle})
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, code=400)
            except Exception as exc:
                app_log("audit rule cycle failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/audit/roundtrip-note":
            try:
                from app.audit_workbench import ROUND_TRIPS_FILE, save_round_trip_note

                payload = self._read_json_payload()
                period = normalize_period(payload.get("period") or CURRENT_PERIOD)
                snapshot = build_audit_workbench_for_period(period, persist=False)
                round_trip_path = period_state_file(period, ROUND_TRIPS_FILE)
                round_trip_payload = read_json(round_trip_path, {})
                if round_trip_payload.get("snapshotId") != snapshot.get("snapshotId"):
                    build_audit_workbench_for_period(period, persist=True)
                    round_trip_payload = read_json(round_trip_path, {})
                round_trip_id = str(payload.get("roundTripId") or "")
                if not any(item.get("roundTripId") == round_trip_id for item in round_trip_payload.get("items", [])):
                    return self._json({"ok": False, "error": "Trade round trip not found"}, code=404)
                note = save_round_trip_note(
                    STATE_DIR,
                    period=period,
                    round_trip_id=round_trip_id,
                    payload=payload,
                )
                return self._json({"ok": True, "note": note})
            except ValueError as exc:
                return self._json({"ok": False, "error": str(exc)}, code=400)
            except Exception as exc:
                app_log("audit roundtrip note failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/audit/report/generate":
            try:
                if demo_mode_active():
                    period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
                    snapshot = build_audit_workbench_for_period(period, persist=True)
                    report_path, cached, stale = generate_demo_audit_report_for_period(
                        period, snapshot=snapshot
                    )
                    snapshot = build_audit_workbench_for_period(period, persist=True)
                    return self._json({
                        "ok": True,
                        "period": period,
                        "report_path": report_path,
                        "cached": cached,
                        "stale": stale,
                        "demo": True,
                        "synthetic": True,
                        "generationMode": "offline-demo-template",
                        "aiReport": snapshot.get("aiReport", {}),
                    })
                period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
                build_audit_workbench_for_period(period, persist=True)
                report_path, cached, stale = generate_audit_report_for_period(period, force=True)
                snapshot = build_audit_workbench_for_period(period, persist=True)
                return self._json({
                    "ok": True,
                    "period": period,
                    "report_path": report_path,
                    "cached": cached,
                    "stale": stale,
                    "aiReport": snapshot.get("aiReport", {}),
                })
            except Exception as exc:
                app_log("audit report generation failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/refresh":
            acquired_refresh_lock = False
            try:
                if demo_mode_active():
                    return self._json(
                        {
                            "ok": False,
                            "error": "Exit demo mode before refreshing real data.",
                            "messageKey": "api.demoExitRequired",
                            "messageParams": {},
                        },
                        code=409,
                    )
                if REFRESH_STATE["running"]:
                    return self._json({"ok": False, "error": "A refresh is already running; wait for it to finish"}, code=429)
                acquired_refresh_lock = REFRESH_LOCK.acquire(blocking=False)
                if not acquired_refresh_lock:
                    return self._json({"ok": False, "error": "A refresh is already running; wait for it to finish"}, code=429)
                REFRESH_STATE["running"] = True
                REFRESH_STATE["progress"] = 0
                REFRESH_STATE["total"] = 100
                REFRESH_STATE["step"] = "init"
                REFRESH_STATE["message"] = "Refresh will start shortly..."
                REFRESH_STATE["result"] = None
                REFRESH_STATE["error"] = None
                def run_refresh():
                    try:
                        freeze_historical_periods()
                        refresh_data(prefetch=True, archive=True)
                    except Exception as exc:
                        log_exception("refresh thread failed", exc)
                        REFRESH_STATE["error"] = str(exc)
                        REFRESH_STATE["message"] = f"Refresh failed: {exc}"
                        REFRESH_STATE["running"] = False
                    finally:
                        REFRESH_STATE["running"] = False
                        if REFRESH_LOCK.locked():
                            REFRESH_LOCK.release()
                threading.Thread(target=run_refresh, daemon=True).start()
                return self._json({"ok": True, "message": "Refresh started"})
            except Exception as exc:
                if acquired_refresh_lock and REFRESH_LOCK.locked():
                    REFRESH_LOCK.release()
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/upload":
            try:
                if demo_mode_active():
                    return self._json(
                        {
                            "ok": False,
                            "error": "Exit demo mode before uploading real data.",
                            "messageKey": "api.demoExitRequired",
                            "messageParams": {},
                        },
                        code=409,
                    )
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in content_type:
                    return self._json({"ok": False, "error": "Upload files using multipart/form-data"}, code=400)
                boundary = content_type.split("boundary=")[-1].strip().strip('"')
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length)
                parts = parse_multipart(body, boundary)
                results = []
                for part in parts:
                    filename = part.get("filename", "")
                    payload = part.get("payload", b"")
                    if not filename or not payload:
                        continue
                    ok, info = validate_upload_file(filename, payload)
                    if not ok:
                        app_log("upload rejected", level="warning", filename=filename, error=info)
                        results.append({"name": filename, "ok": False, "error": info})
                        continue
                    target, routing = upload_target_for_file(filename, payload)
                    atomic_write_bytes(target, payload)
                    app_log("upload accepted", filename=filename, target=str(target.relative_to(ROOT)), routing=routing, bytes=len(payload))
                    results.append({"name": filename, "ok": True, "info": info, "target": str(target.relative_to(ROOT)), "routing": routing})
                return self._json({"ok": True, "results": results})
            except Exception as exc:
                app_log("upload failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/audit/generate":
            try:
                period = normalize_period(params.get("period", [CURRENT_PERIOD])[0])
                force = params.get("force", ["0"])[0].lower() in {"1", "true", "yes"}
                generate_ai = params.get("generate_ai", ["1"])[0].lower() not in {"0", "false", "no"}
                if demo_mode_active():
                    generate_ai = False
                pipeline = run_audit_pipeline_for_period(period)
                scoring_result = pipeline.get("scoring", {})
                match_result = pipeline.get("match", {})
                attr_result = pipeline.get("attribution", {})

                # Rebuild Trader Memory from latest closed trades for scoring and audit context.
                memory_updated = False
                try:
                    from app.trader_memory import build_memory
                    memory_result = build_memory()
                    if "error" not in memory_result:
                        memory_updated = True
                        app_log("trader_memory_rebuilt", period=period, tagCount=memory_result.get("tagCount", 0))
                except Exception as memory_exc:
                    app_log("trader_memory_rebuild_skipped", level="warning", period=period, error=str(memory_exc))

                workbench = build_audit_workbench_for_period(period, persist=True)

                # Try to generate LLM report, but don't fail the deterministic workbench if it errors.
                report_path = ""
                cached = False
                report_stale = False
                report_error = None
                if generate_ai:
                    try:
                        report_path, cached, report_stale = generate_audit_report_for_period(period, force=force)
                        workbench = build_audit_workbench_for_period(period, persist=True)
                    except Exception as report_exc:
                        report_error = str(report_exc)
                        app_log("audit_generate_report_skipped", level="warning", period=period, error=report_error)

                payload = {
                    "ok": True,
                    "period": period,
                    "periodLabel": period_label(period),
                    "cached": cached,
                    "memory_updated": memory_updated,
                    "closed_trades": match_result.get("closed_trade_count", 0),
                    "themes": attr_result.get("themes", []),
                    "scores": scoring_result.get("scores", {}),
                    "report_path": report_path,
                    "snapshotId": workbench.get("snapshotId", ""),
                    "aiReport": workbench.get("aiReport", {}),
                }
                if report_stale:
                    payload["stale"] = True
                    payload["report_warning"] = "AI audit generation failed; an older cached report is displayed."
                if report_error:
                    payload["report_error"] = report_error
                return self._json(payload)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/performance/refresh":
            try:
                from app.performance_tracker import refresh_performance
                data = refresh_performance()
                if data.get("error"):
                    return self._json({"ok": False, "error": data["error"]}, code=500)
                return self._json({"ok": True, "points": len(data.get("series", []))})
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/trade-plan":
            try:
                content_type = self.headers.get("Content-Type", "")
                if "application/json" not in content_type:
                    return self._json({"ok": False, "error": "Use application/json to save a trade plan"}, code=400)
                content_length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                symbol = params.get("symbol", [payload.get("symbol") or DEFAULT_SYMBOL])[0]
                plan = save_trade_plan(symbol, payload)
                clear_replay_payload_cache()
                app_log("trade plan saved", symbol=plan.get("symbol"), setupType=plan.get("setupType"))
                return self._json({"ok": True, "symbol": plan.get("symbol"), "plan": plan, "setupTypes": SETUP_TYPES})
            except Exception as exc:
                app_log("trade plan save failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/settings":
            try:
                content_type = self.headers.get("Content-Type", "")
                if "application/json" not in content_type:
                    return self._json({"ok": False, "error": "Use application/json Save settings"}, code=400)
                content_length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                settings = save_settings(payload)
                invalidate_market_data_caches()
                record_recent_symbol(settings.get("defaultSymbol") or DEFAULT_SYMBOL)
                build_overview_payload.cache_clear()
                app_log("settings saved", defaultSymbol=settings.get("defaultSymbol"), defaultPeriod=settings.get("defaultPeriod"))
                return self._json({"ok": True, "settings": settings})
            except Exception as exc:
                app_log("settings save failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/watchlist-triggers":
            try:
                content_type = self.headers.get("Content-Type", "")
                if "application/json" not in content_type:
                    return self._json({"ok": False, "error": "Use application/json to save Watchlist triggers"}, code=400)
                content_length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                rows = payload.get("triggers", payload) if isinstance(payload, dict) else payload
                if not isinstance(rows, list):
                    return self._json({"ok": False, "error": "triggers must be an array"}, code=400)
                triggers = save_watchlist_triggers(rows)
                app_log("watchlist triggers saved", count=len(triggers))
                return self._json({"ok": True, "triggers": triggers})
            except Exception as exc:
                app_log("watchlist triggers save failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path.startswith("/api/watchlist/"):
            try:
                payload = self._read_json_payload()
                if parsed.path == "/api/watchlist/groups":
                    group = watchlist_manager.create_group(payload)
                    return self._json({"ok": True, "group": group, **watchlist_state_payload()})
                if parsed.path == "/api/watchlist/items":
                    item = watchlist_manager.create_item(payload)
                    return self._json({"ok": True, "item": item, **watchlist_state_payload()})
                if parsed.path == "/api/watchlist/items/batch":
                    result = watchlist_manager.batch_items(payload)
                    return self._json({**result, **watchlist_state_payload()})
                if parsed.path == "/api/watchlist/import/tradingview":
                    result = watchlist_manager.import_tradingview(payload)
                    return self._json({**result, **watchlist_state_payload()})
                if parsed.path == "/api/watchlist/refresh":
                    symbols = payload.get("symbols") or watchlist_manager.all_symbols()
                    normalized = [normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)]
                    if demo_mode_active():
                        results = [
                            {
                                "symbol": symbol,
                                "ok": True,
                                "cached": True,
                                "messageKey": "api.demoOffline",
                            }
                            for symbol in normalized
                        ]
                        name_results = []
                    else:
                        results = prefetch_symbols_for_refresh(normalized, force_refresh=True)
                        name_results = refresh_watchlist_company_names(normalized)
                    return self._json({"ok": True, "symbols": normalized, "results": results, "nameResults": name_results, **watchlist_state_payload()})
            except Exception as exc:
                app_log("watchlist post failed", level="error", path=parsed.path, error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/trade-setup":
            try:
                content_type = self.headers.get("Content-Type", "")
                if "application/json" not in content_type:
                    return self._json({"ok": False, "error": "Use application/json"}, code=400)
                content_length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                from app.trade_setup_manager import save_setup_for_trade, SETUP_TYPES
                symbol = params.get("symbol", [payload.get("symbol") or DEFAULT_SYMBOL])[0]
                trade_date = payload.get("tradeDate", "") or payload.get("openDate", "")
                trade_time = payload.get("tradeTime", "")
                setup_type = payload.get("setupType", "")
                note = payload.get("note", "")
                side = payload.get("side", "")
                setup = save_setup_for_trade(symbol, trade_date, setup_type, note, trade_time, side)
                app_log("trade setup saved", symbol=symbol, setupType=setup_type, tradeDate=trade_date, tradeTime=trade_time)
                return self._json({"ok": True, "setup": setup, "setupTypes": SETUP_TYPES})
            except Exception as exc:
                app_log("trade setup save failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path == "/api/trade-setup/batch":
            try:
                content_type = self.headers.get("Content-Type", "")
                if "application/json" not in content_type:
                    return self._json({"ok": False, "error": "Use application/json"}, code=400)
                content_length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                from app.trade_setup_manager import save_setup_batch, SETUP_TYPES
                items = payload.get("items", [])
                if not isinstance(items, list):
                    return self._json({"ok": False, "error": "items must be an array"}, code=400)
                result = save_setup_batch(items)
                app_log("trade setup batch saved", count=result.get("saved", 0), errors=len(result.get("errors", [])))
                return self._json({"ok": True, **result, "setupTypes": SETUP_TYPES})
            except Exception as exc:
                app_log("trade setup batch save failed", level="error", error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        return self._json({"error": "not found"}, code=404)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/watchlist/groups/"):
            try:
                group_id = int(parsed.path.rsplit("/", 1)[-1])
                group = watchlist_manager.update_group(group_id, self._read_json_payload())
                return self._json({"ok": True, "group": group, **watchlist_state_payload()})
            except Exception as exc:
                app_log("watchlist group patch failed", level="error", path=parsed.path, error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path.startswith("/api/watchlist/items/"):
            try:
                item_id = int(parsed.path.rsplit("/", 1)[-1])
                item = watchlist_manager.update_item(item_id, self._read_json_payload())
                return self._json({"ok": True, "item": item, **watchlist_state_payload()})
            except Exception as exc:
                app_log("watchlist item patch failed", level="error", path=parsed.path, error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        return self._json({"error": "not found"}, code=404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/watchlist/groups/"):
            try:
                group_id = int(parsed.path.rsplit("/", 1)[-1])
                result = watchlist_manager.delete_group(group_id)
                return self._json({**result, **watchlist_state_payload()})
            except Exception as exc:
                app_log("watchlist group delete failed", level="error", path=parsed.path, error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        if parsed.path.startswith("/api/watchlist/items/"):
            try:
                item_id = int(parsed.path.rsplit("/", 1)[-1])
                result = watchlist_manager.delete_item(item_id)
                return self._json({**result, **watchlist_state_payload()})
            except Exception as exc:
                app_log("watchlist item delete failed", level="error", path=parsed.path, error=str(exc))
                return self._json({"ok": False, "error": str(exc)}, code=500)
        return self._json({"error": "not found"}, code=404)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/static/"):
            file_path = STATIC_DIR / parsed.path[len("/static/"):]
            if file_path.exists() and file_path.is_file() and str(file_path.resolve()).startswith(str(STATIC_DIR.resolve())):
                return self._send(200, "application/octet-stream", b"", include_body=False)
            return self._send(404, "text/plain", b"", include_body=False)
        if parsed.path in (
            "/site-icon.png",
            "/favicon.png",
            "/favicon.ico",
            "/tradecraft-favicon.svg",
            "/tradecraft-mark.svg",
            "/tradecraft-mark-reverse.svg",
            "/apple-touch-icon.png",
            "/apple-touch-icon-precomposed.png",
        ):
            icon_path = STATIC_DIR / {
                "/favicon.png": "favicon.png",
                "/favicon.ico": "favicon.png",
                "/tradecraft-favicon.svg": "tradecraft-favicon.svg",
                "/tradecraft-mark.svg": "tradecraft-mark.svg",
                "/tradecraft-mark-reverse.svg": "tradecraft-mark-reverse.svg",
                "/apple-touch-icon.png": "apple-touch-icon.png",
                "/apple-touch-icon-precomposed.png": "apple-touch-icon.png",
            }.get(parsed.path, "site-icon.png")
            if icon_path.exists():
                content_type = "image/svg+xml" if icon_path.suffix == ".svg" else "image/png"
                return self._send(200, content_type, icon_path.read_bytes(), include_body=False)
            return self._send(404, "text/plain", b"icon not found", include_body=False)
        if parsed.path == "/":
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").encode("utf-8")
            return self._send(200, "text/html; charset=utf-8", html, include_body=False)
        return self._send(404, "application/json", b'{"error":"not found"}', include_body=False)

    def log_message(self, fmt, *args):
        return

    def _read_json_payload(self) -> dict:
        content_type = self.headers.get("Content-Type", "")
        if content_type and "application/json" not in content_type:
            raise ValueError("Use application/json")
        content_length = int(self.headers.get("Content-Length", 0))
        if not content_length:
            return {}
        return json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")

    def _json(self, payload: dict, code: int = 200):
        if isinstance(payload, dict) and "ok" not in payload:
            payload = {"ok": code < 400, **payload}
        if isinstance(payload, dict) and code >= 400 and payload.get("ok") is False:
            payload.setdefault("code", code)
            payload.setdefault(
                "messageKey",
                "api.notFound" if code == 404 else ("api.invalidRequest" if code < 500 else "api.internalError"),
            )
            payload.setdefault("messageParams", {})
        if isinstance(payload, dict) and payload.get("messageKey") and "message" not in payload:
            payload["message"] = translate(
                "en",
                str(payload["messageKey"]),
                str(payload.get("error") or ""),
                **(payload.get("messageParams") or {}),
            )
        return self._send(code, "application/json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _send(self, code: int, content_type: str, body: bytes, include_body: bool = True):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Content-Language", "en")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            if include_body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            app_log(
                "client disconnected during response",
                level="warning",
                path=getattr(self, "path", ""),
                code=code,
                error=str(exc),
            )


def initialize_demo_workspace(force: bool = False) -> dict:
    """Seed and fully build the isolated synthetic first-run workspace."""
    from app.demo_data import ensure_demo_workspace, record_demo_files, reset_demo

    ensure_data_dirs()
    status = reset_demo(ROOT) if force else ensure_demo_workspace(ROOT)
    if not status.get("active"):
        return status

    if not watchlist_manager.DB_PATH.exists():
        group = watchlist_manager.create_group({"name": "Demo Leaders"})
        colors = ("blue", "green", "purple", "red", "gray")
        watchlist_symbols = status.get("watchlistSymbols") or status.get("symbols") or ["QQQ.US"]
        for symbol, color in zip(watchlist_symbols, colors):
            watchlist_manager.create_item({
                "symbol": symbol,
                "groupId": group["id"],
                "color": color,
                "note": "Synthetic demo item",
            })

    demo_periods = [
        normalize_period(period)
        for period in (status.get("periods") or [CURRENT_PERIOD])
        if str(period).upper() != "ALL"
    ]
    for period in demo_periods:
        try:
            summary_path = period_state_file(period, "audit_summary.json")
            if not summary_path.exists():
                run_audit_pipeline_for_period(period)
            snapshot = build_audit_workbench_for_period(period, persist=True)
            report_path = period_state_file(period, "audit_report.md")
            if force or not report_path.exists():
                generate_demo_audit_report_for_period(period, snapshot=snapshot)
        except Exception as exc:
            app_log(
                "demo audit build skipped",
                level="warning",
                period=period,
                error=str(exc),
            )
    clear_runtime_caches()
    return record_demo_files(ROOT)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        prefetch = "--no-prefetch" not in sys.argv[2:]
        payload = refresh_data(prefetch=prefetch, archive=True)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "refresh-history":
        payload = freeze_historical_periods()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    ensure_data_dirs()
    initialize_demo_workspace()
    port = int(os.environ.get("PORT", "8888"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Demo server running on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
