#!/usr/bin/env python3
"""Data health payload construction."""

from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from app import state_store


def _active_kline_items(kline_state: dict, adjust_mode: str) -> List[tuple]:
    if not isinstance(kline_state, dict):
        return []
    suffix = f"|{adjust_mode}" if adjust_mode else ""
    if not suffix:
        return [(key, entry) for key, entry in kline_state.items() if isinstance(entry, dict)]
    return [
        (key, entry)
        for key, entry in kline_state.items()
        if isinstance(entry, dict) and str(key).endswith(suffix)
    ]


def build_health_payload(
    *,
    root: Path,
    manifest_file: Path,
    state_metadata_file: Path,
    app_log_file: Path,
    kline_state: dict,
    expected_kline_end_date: Callable[[str, str], str],
    adjust_mode: str,
    app_log: Callable[..., None],
    port: int = 8787,
    git_info: Optional[dict] = None,
    runtime_info: Optional[dict] = None,
    cache_issues: Optional[List[dict]] = None,
    ai_cache: Optional[dict] = None,
) -> dict:
    manifest = state_store.read_json(manifest_file, {})
    state_metadata = state_store.read_json(state_metadata_file, {})
    active_kline_items = _active_kline_items(kline_state, adjust_mode)
    kline_entries = [entry for _, entry in active_kline_items]
    stale_entries = [
        entry
        for key, entry in active_kline_items
        if isinstance(entry, dict)
        and entry.get("requestedEnd")
        and entry.get("cacheEnd")
        and entry.get("cacheEnd") < expected_kline_end_date(entry.get("requestedEnd"), key.split("|", 1)[0])
    ]
    last_errors = [
        {
            "symbol": key.split("|", 1)[0],
            "cacheEnd": entry.get("cacheEnd", ""),
            "requestedEnd": entry.get("requestedEnd", ""),
            "error": entry.get("lastError", ""),
        }
        for key, entry in active_kline_items
        if isinstance(entry, dict) and entry.get("lastError")
    ][:20]

    try:
        from app.llm_reporter import get_api_key
        kimi_configured = bool(get_api_key())
    except Exception:
        kimi_configured = False

    try:
        from app.performance_tracker import CACHE_FILE as perf_cache_file, _candidate_xlsx_paths
        xlsx_candidates = [
            {
                "path": (
                    str(path.relative_to(root))
                    if path.is_relative_to(root)
                    else "configured performance workbook"
                ),
                "exists": path.exists(),
            }
            for path in _candidate_xlsx_paths()
        ]
        performance_cache_exists = perf_cache_file.exists()
        performance_cache_mtime = (
            datetime.fromtimestamp(perf_cache_file.stat().st_mtime).isoformat(timespec="seconds")
            if performance_cache_exists else ""
        )
    except Exception as exc:
        xlsx_candidates = []
        performance_cache_exists = False
        performance_cache_mtime = ""
        app_log("performance health failed", level="warning", error=str(exc))

    source_files = manifest.get("files", []) if isinstance(manifest.get("files", []), list) else []
    tlg_count = sum(1 for item in source_files if str(item.get("path", "")).lower().endswith(".tlg"))
    csv_count = sum(1 for item in source_files if str(item.get("path", "")).lower().endswith(".csv"))
    intraday_entries = [
        {
            "symbol": key.split("|", 1)[0],
            "cacheEnd": entry.get("cacheEnd", ""),
            "lastIntradayAttemptAt": entry.get("lastIntradayAttemptAt", ""),
        }
        for key, entry in active_kline_items
        if isinstance(entry, dict) and entry.get("lastIntradayAttemptAt")
    ][:20]

    return {
        "ok": True,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "service": {
            "port": port,
            "root": str(root),
            "cwd": str((runtime_info or {}).get("cwd", "")),
            "python": str((runtime_info or {}).get("python", "")),
            "git": git_info or {},
        },
        "ibkr": {
            "lastRefreshAt": manifest.get("lastRefreshAt") or manifest.get("builtAt", ""),
            "reportEndDate": manifest.get("reportEndDate", ""),
            "tradeCount": manifest.get("tradeCount", 0),
            "symbolCount": manifest.get("symbolCount", 0),
            "files": manifest.get("files", []),
            "archived": manifest.get("archived", []),
            "parsedTlgRows": manifest.get("parsedTlgRows", 0),
            "parsedCsvRows": manifest.get("parsedCsvRows", 0),
            "sourceFileCounts": {"tlg": tlg_count, "csv": csv_count},
        },
        "kline": {
            "adjustMode": adjust_mode,
            "symbolCount": len(kline_entries),
            "staleCount": len(stale_entries),
            "errorCount": len(last_errors),
            "lastErrors": last_errors,
            "intradayCount": len(intraday_entries),
            "intraday": intraday_entries,
            "cacheIssueCount": len(cache_issues or []),
            "cacheIssues": cache_issues or [],
        },
        "ai": {
            "kimiConfigured": kimi_configured,
            "cache": ai_cache or {},
        },
        "performance": {
            "cacheExists": performance_cache_exists,
            "cacheMtime": performance_cache_mtime,
            "xlsxCandidates": xlsx_candidates,
        },
        "state": {
            "metadataFile": str(state_metadata_file.relative_to(root)),
            "metadataCount": len((state_metadata.get("files", {}) if isinstance(state_metadata, dict) else {})),
        },
        "logs": {
            "appLog": str(app_log_file.relative_to(root)),
            "exists": app_log_file.exists(),
            "mtime": datetime.fromtimestamp(app_log_file.stat().st_mtime).isoformat(timespec="seconds") if app_log_file.exists() else "",
        },
    }
