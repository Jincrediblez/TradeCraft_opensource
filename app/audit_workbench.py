#!/usr/bin/env python3
"""Structured trading-audit workbench.

The legacy audit pipeline writes a compact summary and an optional Markdown
report.  This module turns those deterministic outputs into a versioned,
drillable workbench without making the LLM report a source of truth.

The implementation intentionally stays JSON-only.  At the expected personal
scale (five years / roughly twenty thousand executions), period sharding,
bounded API payloads, stable identifiers and process-local caching are enough.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from app.state_store import read_json, write_json
from app.symbols import normalize_symbol
from app.trade_setup_manager import SETUP_TYPES as CONTROLLED_SETUP_TYPES


SCHEMA_VERSION = 3
WORKBENCH_FILE = "audit_workbench.json"
ROUND_TRIPS_FILE = "audit_round_trips.json"
REPORT_META_FILE = "audit_report_meta.json"
REVIEW_STATE_FILE = "audit_review_state.json"
ROUND_TRIP_NOTES_FILE = "audit_roundtrip_notes.json"

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
DECISIONS = {"confirmed", "dismissed", "undecided"}
RULE_OPERATORS = {"<=", ">="}


def _ensure_supported_schema(payload: object, label: str) -> None:
    if not isinstance(payload, dict) or not payload:
        return
    try:
        version = int(payload.get("schemaVersion", 1) or 1)
    except (TypeError, ValueError):
        version = 1
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{label} schemaVersion={version} is incompatible with English-first schemaVersion={SCHEMA_VERSION}. "
            "Rebuild this workspace from the original broker exports. The existing file was not modified."
        )


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _finite(value, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _round(value, digits: int = 2):
    if value is None:
        return None
    return round(_finite(value), digits)


def _date_text(value: str) -> str:
    raw = str(value or "").replace("-", "")
    return raw if len(raw) == 8 and raw.isdigit() else ""


def _iso_date(value: str) -> str:
    raw = _date_text(value)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if raw else ""


def _json_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def enrich_executions(rows: List[dict]) -> List[dict]:
    """Add deterministic IDs while preserving all legacy fields.

    Exact duplicate executions are disambiguated by a deterministic occurrence
    index.  Re-importing the same normalized data therefore produces the same
    identifiers.
    """

    normalized = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["symbol_lb"] = normalize_symbol(str(item.get("symbol_lb") or item.get("symbol") or ""))
        item["side"] = str(item.get("side") or "").upper()
        item["trade_date"] = _date_text(item.get("trade_date", ""))
        item["trade_time"] = str(item.get("trade_time") or "00:00:00")
        item["_sourceIndex"] = index
        normalized.append(item)

    normalized.sort(
        key=lambda row: (
            row.get("trade_date", ""),
            row.get("trade_time", ""),
            row.get("symbol_lb", ""),
            row.get("side", ""),
            _finite(row.get("quantity")),
            _finite(row.get("price")),
            str(row.get("source") or ""),
            row["_sourceIndex"],
        )
    )
    occurrences: Dict[str, int] = defaultdict(int)
    enriched = []
    for item in normalized:
        fingerprint = "|".join(
            [
                item.get("trade_date", ""),
                item.get("trade_time", ""),
                item.get("symbol_lb", ""),
                item.get("side", ""),
                f"{_finite(item.get('quantity')):.8f}",
                f"{_finite(item.get('price')):.8f}",
                str(item.get("currency") or ""),
                str(item.get("exchange") or ""),
                str(item.get("source") or ""),
            ]
        )
        ordinal = occurrences[fingerprint]
        occurrences[fingerprint] += 1
        item["executionId"] = str(item.get("executionId") or _stable_id("exe", fingerprint, ordinal))
        item.pop("_sourceIndex", None)
        enriched.append(item)
    return enriched


def enrich_round_trips(closed_rows: List[dict], executions: List[dict]) -> List[dict]:
    """Add stable IDs and execution references to FIFO closed-lot rows."""

    execution_by_symbol: Dict[str, List[dict]] = defaultdict(list)
    for row in executions:
        execution_by_symbol[normalize_symbol(row.get("symbol_lb") or row.get("symbol") or "")].append(row)

    ordered = []
    for index, raw in enumerate(closed_rows):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["symbol"] = normalize_symbol(str(item.get("symbol") or ""))
        item["open_date"] = _date_text(item.get("open_date", ""))
        item["close_date"] = _date_text(item.get("close_date", ""))
        item["_sourceIndex"] = index
        ordered.append(item)
    ordered.sort(
        key=lambda row: (
            row.get("open_date", ""),
            row.get("close_date", ""),
            row.get("symbol", ""),
            _finite(row.get("quantity")),
            _finite(row.get("avg_entry")),
            _finite(row.get("avg_exit")),
            row["_sourceIndex"],
        )
    )

    occurrences: Dict[str, int] = defaultdict(int)
    out = []
    for item in ordered:
        fingerprint = "|".join(
            [
                item.get("symbol", ""),
                item.get("open_date", ""),
                item.get("close_date", ""),
                f"{_finite(item.get('quantity')):.8f}",
                f"{_finite(item.get('avg_entry')):.8f}",
                f"{_finite(item.get('avg_exit')):.8f}",
                str(item.get("entry_source") or ""),
            ]
        )
        ordinal = occurrences[fingerprint]
        occurrences[fingerprint] += 1
        item["roundTripId"] = str(item.get("roundTripId") or _stable_id("rt", fingerprint, ordinal))

        related = []
        for execution in execution_by_symbol.get(item["symbol"], []):
            day = execution.get("trade_date", "")
            if item["open_date"] <= day <= item["close_date"]:
                related.append(execution["executionId"])
        item["executionIds"] = related
        item["openDate"] = _iso_date(item["open_date"])
        item["closeDate"] = _iso_date(item["close_date"])
        item["realizedPnl"] = _round(item.get("realized_pnl"))
        item["holdingDays"] = int(_finite(item.get("holding_days")))
        item["setupType"] = str(item.get("setup_type") or "Unclassified")
        item["exitSetupType"] = str(item.get("exit_setup_type") or "Unlabeled")
        item["riskStatus"] = str(item.get("risk_status") or "risk_missing")
        item.pop("_sourceIndex", None)
        out.append(item)
    return out


def _aggregate_round_trips(rows: List[dict], field: str) -> List[dict]:
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        key = str(row.get(field) or "Unclassified")
        grouped[key].append(row)

    output = []
    for name, items in grouped.items():
        pnls = [_finite(item.get("realized_pnl")) for item in items]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        notionals = [
            _finite(item.get("quantity")) * _finite(item.get("avg_entry"))
            for item in items
        ]
        risk_known = [item for item in items if item.get("r_multiple") is not None]
        expectancy_pct = (
            sum(pnls) / sum(notionals) * 100
            if sum(notionals) > 0
            else None
        )
        output.append(
            {
                "name": name,
                "closedTradeCount": len(items),
                "winRate": round(len(wins) / len(items) * 100, 1) if items else 0.0,
                "realizedPnl": round(sum(pnls), 2),
                "avgWin": round(gross_profit / len(wins), 2) if wins else None,
                "avgLoss": round(gross_loss / len(losses), 2) if losses else None,
                "profitFactor": round(gross_profit / gross_loss, 2) if gross_loss else None,
                "expectancyPct": round(expectancy_pct, 2) if expectancy_pct is not None else None,
                "riskCoveragePct": round(len(risk_known) / len(items) * 100, 1) if items else 0.0,
                "confidence": "high" if len(items) >= 20 else "medium" if len(items) >= 8 else "low",
            }
        )
    output.sort(key=lambda row: (row["realizedPnl"], row["closedTradeCount"]), reverse=True)
    return output


def _window_stats(executions: List[dict], round_trips: List[dict], dates: List[str]) -> dict:
    date_set = set(dates)
    trades = [row for row in executions if row.get("trade_date") in date_set]
    closes = [row for row in round_trips if row.get("close_date") in date_set]
    active_days = len(date_set)
    notionals = [_finite(row.get("gross_amount")) for row in trades]
    holding = [int(_finite(row.get("holding_days"))) for row in closes if _finite(row.get("holding_days")) > 0]
    sizes = [_finite(row.get("quantity")) * _finite(row.get("avg_entry")) for row in closes]
    pnls = [_finite(row.get("realized_pnl")) for row in closes]
    return {
        "activeTradingDays": active_days,
        "executionCount": len(trades),
        "dailyAverage": round(len(trades) / active_days, 2) if active_days else 0.0,
        "averageNotional": round(sum(notionals) / len(notionals), 2) if notionals else 0.0,
        "medianHoldingDays": _median(holding),
        "averagePositionSize": round(sum(sizes) / len(sizes), 2) if sizes else 0.0,
        "closedTradeCount": len(closes),
        "realizedPnl": round(sum(pnls), 2),
        "winRate": round(sum(1 for value in pnls if value > 0) / len(pnls) * 100, 1) if pnls else 0.0,
    }


def _median(values: Iterable[float]) -> float:
    ordered = sorted(_finite(value) for value in values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def build_behavior_comparison(executions: List[dict], round_trips: List[dict], window: int = 20) -> dict:
    trade_dates = sorted({row.get("trade_date", "") for row in executions if row.get("trade_date")})
    current_dates = trade_dates[-window:]
    previous_dates = trade_dates[-window * 2:-window]
    current = _window_stats(executions, round_trips, current_dates)
    previous = _window_stats(executions, round_trips, previous_dates)

    def change(key: str) -> Optional[float]:
        old = _finite(previous.get(key))
        new = _finite(current.get(key))
        return round((new / old - 1) * 100, 1) if old else None

    comparison = []
    labels = {
        "dailyAverage": "Average daily executions",
        "medianHoldingDays": "Median holding period",
        "averagePositionSize": "Average round-trip size",
        "winRate": "Closed-trade win rate",
    }
    for key, label in labels.items():
        comparison.append(
            {
                "key": key,
                "label": label,
                "current": current.get(key),
                "previous": previous.get(key),
                "changePct": change(key),
            }
        )
    return {
        "windowTradingDays": window,
        "currentStart": _iso_date(current_dates[0]) if current_dates else "",
        "currentEnd": _iso_date(current_dates[-1]) if current_dates else "",
        "previousStart": _iso_date(previous_dates[0]) if previous_dates else "",
        "previousEnd": _iso_date(previous_dates[-1]) if previous_dates else "",
        "current": current,
        "previous": previous,
        "comparison": comparison,
        "tradeDates": trade_dates,
        "sampleSufficient": len(current_dates) == window and len(previous_dates) == window,
    }


def _benchmark_return(
    symbol: str,
    start_date: str,
    end_date: str,
    kline_loader: Callable[[str], List[dict]],
) -> dict:
    rows = kline_loader(symbol) or []
    candidates = []
    for row in rows:
        day = str(row.get("time") or "")[:10]
        close = _finite(row.get("close"), default=0.0)
        if start_date <= day <= end_date and close > 0:
            candidates.append((day, close))
    if len(candidates) < 2:
        return {"symbol": symbol, "returnPct": None, "fresh": False, "reason": "Insufficient market-data coverage"}
    candidates.sort()
    first_day, first_close = candidates[0]
    last_day, last_close = candidates[-1]
    try:
        gap = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(last_day, "%Y-%m-%d")).days
    except ValueError:
        gap = 999
    fresh = gap <= 4
    return {
        "symbol": symbol,
        "returnPct": round((last_close / first_close - 1) * 100, 2),
        "startDate": first_day,
        "endDate": last_day,
        "fresh": fresh,
        "reason": "" if fresh else f"Market data is only available through {last_day}",
    }


def _account_outcome(
    period: str,
    statement: dict,
    settings: dict,
    kline_loader: Callable[[str], List[dict]],
) -> dict:
    account = statement.get("accountSnapshot", {}) if isinstance(statement, dict) else {}
    report_end = str(account.get("reportEndDate") or "")
    year = str(period)[:4] if str(period)[:4].isdigit() else report_end[:4]
    start = f"{year}-01-01" if year else ""
    primary_symbol = normalize_symbol(str(settings.get("auditPrimaryBenchmark") or "QQQ.US"))
    secondary_symbol = normalize_symbol(str(settings.get("auditSecondaryBenchmark") or "SPY.US"))
    twr = account.get("timeWeightedReturnPct")
    primary = _benchmark_return(primary_symbol, start, report_end, kline_loader) if start and report_end else {
        "symbol": primary_symbol,
        "returnPct": None,
        "fresh": False,
        "reason": "Account statement period is missing",
    }
    secondary = _benchmark_return(secondary_symbol, start, report_end, kline_loader) if start and report_end else {
        "symbol": secondary_symbol,
        "returnPct": None,
        "fresh": False,
        "reason": "Account statement period is missing",
    }
    alpha = None
    if twr is not None and primary.get("fresh") and primary.get("returnPct") is not None:
        alpha = round(_finite(twr) - _finite(primary["returnPct"]), 2)
    return {
        "reportEndDate": report_end,
        "timeWeightedReturnPct": _round(twr),
        "startingValue": _round(account.get("startingValue")),
        "endingValue": _round(account.get("endingValue")),
        "primaryBenchmark": primary,
        "secondaryBenchmark": secondary,
        "alphaVsPrimaryPct": alpha,
        "verdict": (
            "Outperformed primary benchmark" if alpha is not None and alpha > 0
            else "Underperformed primary benchmark" if alpha is not None
            else "Benchmark data is insufficient; alpha is not evaluated"
        ),
    }


def _symbols_from_text(text: str) -> List[str]:
    return sorted(set(re.findall(r"\b[A-Z0-9]{1,8}\.(?:US|HK)\b", str(text or ""))))


def _finding_evidence(
    finding_type: str,
    detail: str,
    summary: dict,
    round_trips: List[dict],
    theme_symbols: Dict[str, set],
) -> List[str]:
    refs = []
    if finding_type == "asymmetric_exit":
        for missed in (summary.get("exit_quality", {}) or {}).get("topMissed20d", []):
            symbol = normalize_symbol(str(missed.get("symbol") or ""))
            day = _date_text(missed.get("closeDate", ""))
            match = next(
                (row for row in round_trips if row.get("symbol") == symbol and row.get("close_date") == day),
                None,
            )
            if match:
                refs.append(match["roundTripId"])
    elif finding_type == "churn_friction":
        refs.extend(
            row["roundTripId"]
            for row in sorted(round_trips, key=lambda item: item.get("close_date", ""), reverse=True)
            if int(_finite(row.get("holding_days"))) <= 3
        )
    else:
        symbols = set(_symbols_from_text(detail))
        if finding_type == "narrative_hype":
            symbols.update(theme_symbols.get("optionality", set()))
        refs.extend(
            row["roundTripId"]
            for row in sorted(round_trips, key=lambda item: item.get("close_date", ""), reverse=True)
            if row.get("symbol") in symbols
        )
    return list(dict.fromkeys(refs))


def _risk_metric_key(finding_type: str) -> str:
    return {
        "asymmetric_exit": "process.sellTooEarlyRatePct",
        "churn_friction": "behavior.roundTrips3d",
        "narrative_hype": "process.optionalityLossPct",
        "tail_risk": "process.concentrationRisk",
        "entry_quality": "process.entryRisk",
        "data_quality_risk": "coverage.initialRiskPct",
        "data_quality_plan": "coverage.tradePlanPct",
    }.get(finding_type, "process.overallRisk")


def _build_findings(summary: dict, round_trips: List[dict], plan_coverage: dict) -> List[dict]:
    themes = summary.get("theme_attribution", []) or []
    optionality_symbols = {
        symbol
        for theme in themes
        if theme.get("tier") == "optionality"
        for symbol in (theme.get("symbols") or [])
    }
    theme_symbols = {"optionality": optionality_symbols}
    findings = []

    for alert in summary.get("alerts", []) or []:
        finding_type = str(alert.get("type") or "audit")
        detail = str(alert.get("detail") or "")
        score = _finite(alert.get("score"))
        severity = str(alert.get("severity") or "low")
        refs = _finding_evidence(finding_type, detail, summary, round_trips, theme_symbols)
        evidence_filter = {"roundTripIds": refs}
        if finding_type == "narrative_hype":
            evidence_filter = {"symbols": sorted(optionality_symbols)}
        elif finding_type == "churn_friction":
            evidence_filter = {"holdingDaysMax": 3}
        findings.append(
            {
                "findingId": _stable_id("finding", finding_type, detail),
                "type": finding_type,
                "title": detail or finding_type,
                "detail": detail,
                "severity": severity,
                "status": "confirmed" if severity == "high" else "suspected",
                "confidence": 0.85 if refs else 0.55,
                "score": round(score, 1),
                "metricKey": _risk_metric_key(finding_type),
                "direction": "lower",
                "sampleSize": len(refs),
                "evidenceRefs": refs[:3],
                "evidenceCount": len(refs),
                "evidenceFilter": evidence_filter,
                "allEvidence": list(alert.get("all_evidence") or [])[:8],
                "suggestedRule": _suggest_rule(finding_type),
            }
        )

    risk_missing = sum(
        1 for row in round_trips
        if row.get("risk_status") in {None, "", "risk_missing", "invalid_risk"}
    )
    if risk_missing:
        detail = f"{risk_missing} / {len(round_trips)} closed round trips lack initial risk, so R multiples cannot be evaluated"
        refs = [row["roundTripId"] for row in round_trips if row.get("risk_status") != "planned"]
        findings.append(
            {
                "findingId": _stable_id("finding", "data_quality_risk", detail),
                "type": "data_quality_risk",
                "title": "Insufficient initial-risk coverage",
                "detail": detail,
                "severity": "high",
                "status": "needs_data",
                "confidence": 1.0,
                "score": 100.0,
                "metricKey": "coverage.initialRiskPct",
                "direction": "higher",
                "sampleSize": len(round_trips),
                "evidenceRefs": refs[:3],
                "evidenceCount": len(refs),
                "evidenceFilter": {"riskStatuses": ["risk_missing", "invalid_risk", ""]},
                "allEvidence": [],
                "suggestedRule": "New trades must include an invalidation price before the first buy; otherwise they are excluded from the valid sample.",
            }
        )

    missing_plans = int(plan_coverage.get("missing_plan_count", 0) or 0)
    if missing_plans:
        detail = f"{missing_plans} / {plan_coverage.get('tracked_symbol_count', 0)} tracked symbols lack a pre-trade plan"
        findings.append(
            {
                "findingId": _stable_id("finding", "data_quality_plan", detail),
                "type": "data_quality_plan",
                "title": "Insufficient trade-plan coverage",
                "detail": detail,
                "severity": "high",
                "status": "needs_data",
                "confidence": 1.0,
                "score": 100.0,
                "metricKey": "coverage.tradePlanPct",
                "direction": "higher",
                "sampleSize": int(plan_coverage.get("tracked_symbol_count", 0) or 0),
                "evidenceRefs": [],
                "evidenceCount": 0,
                "evidenceFilter": {},
                "allEvidence": list(plan_coverage.get("missing_symbols") or [])[:20],
                "suggestedRule": "Before the first buy in a new symbol, record the thesis, invalidation price, and target holding period.",
            }
        )

    for item in (summary.get("position_rules", {}) or {}).get("alerts", [])[:5]:
        detail = str(item.get("evidence") or item.get("title") or "")
        finding_type = "position_rule"
        refs = _finding_evidence(finding_type, detail, summary, round_trips, theme_symbols)
        symbols = _symbols_from_text(detail)
        findings.append(
            {
                "findingId": _stable_id("finding", finding_type, item.get("rule"), detail),
                "type": finding_type,
                "title": str(item.get("title") or item.get("rule") or "Position discipline"),
                "detail": detail,
                "severity": str(item.get("severity") or "medium"),
                "status": "confirmed",
                "confidence": 0.85,
                "score": 70.0 if item.get("severity") == "high" else 45.0,
                "metricKey": "process.positionRuleRisk",
                "direction": "lower",
                "sampleSize": len(refs),
                "evidenceRefs": refs[:3],
                "evidenceCount": len(refs),
                "evidenceFilter": {"symbols": symbols} if symbols else {"roundTripIds": refs},
                "allEvidence": [],
                "suggestedRule": "Position size must match the intended role and predefined risk budget.",
            }
        )

    unique = {}
    for item in findings:
        unique.setdefault(item["findingId"], item)
    result = list(unique.values())
    result.sort(
        key=lambda item: (
            SEVERITY_ORDER.get(item.get("severity"), 3),
            0 if item.get("status") == "confirmed" else 1,
            -_finite(item.get("score")),
        )
    )
    return result[:30]


def _suggest_rule(finding_type: str) -> str:
    return {
        "asymmetric_exit": "Record the exit trigger before selling; reduce only according to plan while the trend remains intact.",
        "churn_friction": "Before re-entering the same symbol within three days, record the new information that differs from the prior trade.",
        "narrative_hype": "Non-core positions must respect the allocation cap and define invalidation before entry.",
        "tail_risk": "New positions must not increase single-theme or top-three concentration.",
        "entry_quality": "High-FOMO entries must be delayed by one trading day and the invalidation price reconfirmed.",
    }.get(finding_type, "Record the plan, invalidation condition, and risk budget before the next similar trade.")


def _plan_coverage(summary: dict) -> dict:
    symbols = set((summary.get("ytd_mtm_contribution") or {}).keys())
    for theme in summary.get("theme_attribution", []) or []:
        symbols.update(theme.get("symbols") or [])
    try:
        from app.trade_plans import load_trade_plans

        plans = load_trade_plans()
    except Exception:
        plans = {}
    planned = {symbol for symbol in symbols if isinstance(plans.get(symbol), dict)}
    missing = sorted(symbols - planned)
    return {
        "tracked_symbol_count": len(symbols),
        "planned_symbol_count": len(planned),
        "missing_plan_count": len(missing),
        "missing_symbols": missing,
    }


def _score_risks(summary: dict) -> List[dict]:
    labels = {
        "theme_judgment": "Theme-selection risk",
        "churn_friction": "Trading-friction risk",
        "stock_selection": "Stock-selection risk",
        "narrative_hype": "Narrative risk",
        "entry_quality": "Entry risk",
        "asymmetric_exit": "Exit risk",
        "tail_risk": "Tail risk",
    }
    rows = []
    for key, value in (summary.get("scores") or {}).items():
        raw_score = _finite((value or {}).get("score"))
        risk_score = raw_score
        rows.append(
            {
                "key": key,
                "label": labels.get(key, key),
                "riskScore": round(risk_score, 1),
                "level": "high" if risk_score >= 60 else "medium" if risk_score >= 35 else "low",
            }
        )
    rows.sort(key=lambda row: row["riskScore"], reverse=True)
    return rows


def _ai_report_status(report_path: Path, meta_path: Path, snapshot_id: str) -> dict:
    meta = read_json(meta_path, {})
    exists = report_path.exists()
    content = report_path.read_text(encoding="utf-8", errors="ignore") if exists else ""
    placeholder = bool(re.search(r"\bX\b|\{\{|TODO|TBD", content))
    bound = bool(meta.get("snapshotId")) and meta.get("snapshotId") == snapshot_id
    stale = exists and (not bound or placeholder)
    return {
        "exists": exists,
        "stale": stale,
        "demo": bool(meta.get("demo")),
        "generationMode": str(meta.get("generationMode") or ""),
        "snapshotId": meta.get("snapshotId", ""),
        "generatedAt": meta.get("generatedAt", ""),
        "placeholderDetected": placeholder,
        "message": (
            "AI summary matches the current snapshot" if exists and not stale
            else "AI summary is stale; regenerate it when needed" if exists
            else "AI summary has not been generated"
        ),
    }


def build_workbench(
    *,
    period: str,
    summary: dict,
    trades: List[dict],
    closed_trades: List[dict],
    statement_snapshot: dict,
    settings: dict,
    report_path: Path,
    report_meta_path: Path,
    kline_loader: Callable[[str], List[dict]],
    notes: Optional[dict] = None,
) -> Tuple[dict, List[dict], List[dict]]:
    executions = enrich_executions(trades)
    round_trips = enrich_round_trips(closed_trades, executions)
    notes = notes or {}
    for row in round_trips:
        if isinstance(notes.get(row["roundTripId"]), dict):
            row["userNote"] = notes[row["roundTripId"]]

    behavior = build_behavior_comparison(executions, round_trips)
    setup_rows = _aggregate_round_trips(round_trips, "setup_type")
    exit_setup_rows = _aggregate_round_trips(round_trips, "exit_setup_type")
    plan_coverage = _plan_coverage(summary)
    findings = _build_findings(summary, round_trips, plan_coverage)
    score_risks = _score_risks(summary)
    outcome = _account_outcome(period, statement_snapshot, settings, kline_loader)

    closed_count = len(round_trips)
    risk_known = sum(1 for row in round_trips if row.get("risk_status") == "planned")
    classified = sum(
        1 for row in round_trips
        if str(row.get("setup_type") or "") not in {"", "Unclassified", "Unplanned"}
    )
    opening_missing = len((summary.get("audit_coverage") or {}).get("missing") or [])
    initial_risk_pct = round(risk_known / closed_count * 100, 1) if closed_count else 0.0
    setup_pct = round(classified / closed_count * 100, 1) if closed_count else 0.0
    tracked = int(plan_coverage.get("tracked_symbol_count", 0) or 0)
    plan_pct = round(int(plan_coverage.get("planned_symbol_count", 0) or 0) / tracked * 100, 1) if tracked else 0.0
    benchmark_fresh = bool(outcome.get("primaryBenchmark", {}).get("fresh"))
    confidence_components = [initial_risk_pct, setup_pct, plan_pct, 100.0 if benchmark_fresh else 0.0]
    confidence_score = round(sum(confidence_components) / len(confidence_components), 1)

    exit_quality = summary.get("exit_quality", {}) or {}
    evaluated = int(exit_quality.get("evaluatedCount", 0) or 0)
    early_count = int(exit_quality.get("sellTooEarlyCount20d", 0) or 0)
    early_rate = round(early_count / evaluated * 100, 1) if evaluated else 0.0
    metrics = summary.get("metrics", {}) or {}
    optionality_loss = next(
        (
            _finite(alert.get("score"))
            for alert in summary.get("alerts", []) or []
            if alert.get("type") == "narrative_hype"
        ),
        0.0,
    )
    max_risk = max((_finite(row.get("riskScore")) for row in score_risks), default=0.0)
    metric_catalog = {
        "result.accountTwrPct": outcome.get("timeWeightedReturnPct"),
        "result.alphaVsPrimaryPct": outcome.get("alphaVsPrimaryPct"),
        "behavior.dailyAverage": behavior.get("current", {}).get("dailyAverage"),
        "behavior.roundTrips3d": int(metrics.get("round_trips_3d", 0) or 0),
        "process.sellTooEarlyRatePct": early_rate,
        "process.optionalityLossPct": optionality_loss,
        "process.concentrationRisk": next((row["riskScore"] for row in score_risks if row["key"] == "tail_risk"), 0),
        "process.entryRisk": next((row["riskScore"] for row in score_risks if row["key"] == "entry_quality"), 0),
        "process.positionRuleRisk": 100 if (summary.get("position_rules", {}) or {}).get("alerts") else 0,
        "process.overallRisk": max_risk,
        "coverage.initialRiskPct": initial_risk_pct,
        "coverage.tradePlanPct": plan_pct,
        "coverage.setupPct": setup_pct,
    }

    source_digest = _json_hash(
        {
            "summary": summary,
            "executionIds": [row["executionId"] for row in executions],
            "roundTripIds": [row["roundTripId"] for row in round_trips],
            "settings": {
                "primary": settings.get("auditPrimaryBenchmark"),
                "secondary": settings.get("auditSecondaryBenchmark"),
            },
        }
    )
    snapshot_id = _stable_id("audit", period, source_digest)
    generated_at = datetime.now().isoformat(timespec="seconds")

    advantages = []
    for row in setup_rows:
        if row["closedTradeCount"] >= 5 and row["realizedPnl"] > 0:
            advantages.append(
                {
                    **row,
                    "title": f"{row['name']} is a candidate strength",
                    "proven": row["riskCoveragePct"] >= 70 and row["closedTradeCount"] >= 20,
                    "reason": (
                        "Sample size and risk coverage meet the validation threshold"
                        if row["riskCoveragePct"] >= 70 and row["closedTradeCount"] >= 20
                        else "P&L is positive, but initial-risk or sample coverage is insufficient; treat this only as a candidate"
                    ),
                }
            )

    snapshot = {
        "schemaVersion": SCHEMA_VERSION,
        "snapshotId": snapshot_id,
        "generatedAt": generated_at,
        "sourceDigest": source_digest,
        "period": period,
        "periodLabel": summary.get("periodLabel") or period,
        "dataCutoff": outcome.get("reportEndDate") or behavior.get("currentEnd"),
        "meta": {
            "executionCount": len(executions),
            "roundTripCount": closed_count,
            "allTradeDates": behavior.get("tradeDates", []),
            "riskScoreSemantics": "0=lower risk, 100=higher risk",
        },
        "scorecards": {
            "result": {
                "title": "Outcome quality",
                "value": outcome.get("timeWeightedReturnPct"),
                "unit": "%",
                "verdict": outcome.get("verdict"),
            },
            "process": {
                "title": "Process risk",
                "value": max_risk,
                "unit": "/100",
                "verdict": score_risks[0]["label"] if score_risks else "No evaluable data",
            },
            "behavior": {
                "title": "Recent behavior",
                "value": behavior.get("current", {}).get("dailyAverage"),
                "unit": " fills/trading day",
                "verdict": (
                    "The recent and prior 20-day samples are complete"
                    if behavior.get("sampleSufficient")
                    else "Small sample; interpret the trend cautiously"
                ),
            },
            "confidence": {
                "title": "Data confidence",
                "value": confidence_score,
                "unit": "/100",
                "verdict": "Low" if confidence_score < 40 else "Medium" if confidence_score < 70 else "High",
            },
        },
        "outcome": outcome,
        "riskScores": score_risks,
        "behavior": {key: value for key, value in behavior.items() if key != "tradeDates"},
        "setupPerformance": setup_rows,
        "exitSetupPerformance": exit_setup_rows,
        "exitQuality": {
            **exit_quality,
            "sellTooEarlyRatePct": early_rate,
        },
        "themeAttribution": summary.get("theme_attribution", []) or [],
        "mtmRankings": summary.get("mtm_rankings", {}) or {},
        "advantages": advantages[:5],
        "findings": findings,
        "dataQuality": {
            "confidenceScore": confidence_score,
            "initialRiskCoveragePct": initial_risk_pct,
            "tradePlanCoveragePct": plan_pct,
            "setupCoveragePct": setup_pct,
            "benchmarkFresh": benchmark_fresh,
            "openingCoverageMissingCount": opening_missing,
            "closedTradeCount": closed_count,
            "riskKnownCount": risk_known,
            "plannedSymbolCount": plan_coverage.get("planned_symbol_count", 0),
            "trackedSymbolCount": tracked,
            "missingPlanSymbols": plan_coverage.get("missing_symbols", [])[:20],
        },
        "metricCatalog": metric_catalog,
        "aiReport": {},
    }
    snapshot["aiReport"] = _ai_report_status(report_path, report_meta_path, snapshot_id)
    return snapshot, round_trips, executions


def save_snapshot(
    period_dir: Path,
    snapshot: dict,
    round_trips: List[dict],
    executions: Optional[List[dict]] = None,
) -> None:
    period_dir.mkdir(parents=True, exist_ok=True)
    previous = read_json(period_dir / ROUND_TRIPS_FILE, {})
    _ensure_supported_schema(previous, ROUND_TRIPS_FILE)
    old_items = previous.get("items", []) if isinstance(previous, dict) else []
    new_ids = {item.get("roundTripId") for item in round_trips}
    aliases = dict(previous.get("aliases", {})) if isinstance(previous, dict) else {}

    def alias_key(item: dict) -> tuple:
        return (
            item.get("symbol"),
            item.get("open_date"),
            item.get("close_date"),
            round(_finite(item.get("quantity")), 6),
        )

    new_by_key: Dict[tuple, List[dict]] = defaultdict(list)
    for item in round_trips:
        new_by_key[alias_key(item)].append(item)
    for old in old_items:
        old_id = old.get("roundTripId")
        if not old_id or old_id in new_ids:
            continue
        candidates = new_by_key.get(alias_key(old), [])
        if len(candidates) == 1:
            aliases[old_id] = candidates[0].get("roundTripId")

    write_json(period_dir / WORKBENCH_FILE, snapshot)
    write_json(
        period_dir / ROUND_TRIPS_FILE,
        {
            "schemaVersion": SCHEMA_VERSION,
            "snapshotId": snapshot.get("snapshotId"),
            "generatedAt": snapshot.get("generatedAt"),
            "aliases": aliases,
            "executions": executions or [],
            "items": round_trips,
        },
    )


def load_review_state(state_dir: Path) -> dict:
    payload = read_json(state_dir / REVIEW_STATE_FILE, {})
    _ensure_supported_schema(payload, REVIEW_STATE_FILE)
    if not isinstance(payload, dict):
        payload = {}
    feedback = payload.get("feedback") if isinstance(payload.get("feedback"), dict) else {}
    cycles = payload.get("ruleCycles") if isinstance(payload.get("ruleCycles"), list) else []
    return {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": str(payload.get("updatedAt") or ""),
        "feedback": feedback,
        "ruleCycles": cycles,
    }


def _save_review_state(state_dir: Path, state: dict) -> dict:
    path = state_dir / REVIEW_STATE_FILE
    existing = read_json(path, {})
    _ensure_supported_schema(existing, REVIEW_STATE_FILE)
    state["schemaVersion"] = SCHEMA_VERSION
    state["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    write_json(path, state)
    return state


def save_finding_feedback(
    state_dir: Path,
    *,
    period: str,
    finding_id: str,
    decision: str,
    note: str = "",
) -> dict:
    if decision not in DECISIONS:
        raise ValueError("decision must be confirmed, dismissed, or undecided")
    if not finding_id:
        raise ValueError("findingId must not be empty")
    state = load_review_state(state_dir)
    key = f"{period}:{finding_id}"
    state["feedback"][key] = {
        "period": period,
        "findingId": finding_id,
        "decision": decision,
        "note": str(note or "")[:1000],
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    _save_review_state(state_dir, state)
    return state["feedback"][key]


def _suggest_target(baseline: float, direction: str) -> Tuple[str, float]:
    if direction == "higher":
        return ">=", round(min(100.0, baseline + max(10.0, (100.0 - baseline) * 0.2)), 2)
    return "<=", round(max(0.0, baseline * 0.8), 2)


def create_rule_cycle(
    state_dir: Path,
    *,
    period: str,
    snapshot: dict,
    finding: dict,
    rule_text: str = "",
    operator: str = "",
    target_value=None,
) -> dict:
    state = load_review_state(state_dir)
    active = [cycle for cycle in state["ruleCycles"] if cycle.get("status") == "active"]
    if len(active) >= 3:
        raise ValueError("At most three rules may be tracked at once; complete or stop an active rule first")
    metric_key = str(finding.get("metricKey") or "")
    baseline = snapshot.get("metricCatalog", {}).get(metric_key)
    if baseline is None:
        raise ValueError("This finding has no trackable metric")
    suggested_operator, suggested_target = _suggest_target(_finite(baseline), finding.get("direction", "lower"))
    selected_operator = operator or suggested_operator
    if selected_operator not in RULE_OPERATORS:
        raise ValueError("operator must be <= or >=")
    target = _finite(target_value, suggested_target) if target_value is not None else suggested_target
    trade_dates = snapshot.get("meta", {}).get("allTradeDates", []) or []
    start_date = trade_dates[-1] if trade_dates else ""
    cycle_id = _stable_id("rule", period, finding.get("findingId"), snapshot.get("snapshotId"), len(active))
    cycle = {
        "cycleId": cycle_id,
        "period": period,
        "findingId": finding.get("findingId"),
        "findingTitle": finding.get("title"),
        "ruleText": str(rule_text or finding.get("suggestedRule") or "")[:1000],
        "metricKey": metric_key,
        "baselineValue": _round(baseline),
        "operator": selected_operator,
        "targetValue": _round(target),
        "startTradeDate": start_date,
        "durationTradingDays": 20,
        "observedTradingDays": 0,
        "currentValue": _round(baseline),
        "status": "active",
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "completedAt": "",
    }
    state["ruleCycles"].append(cycle)
    _save_review_state(state_dir, state)
    return cycle


def stop_rule_cycle(state_dir: Path, cycle_id: str) -> dict:
    state = load_review_state(state_dir)
    cycle = next((item for item in state["ruleCycles"] if item.get("cycleId") == cycle_id), None)
    if not cycle:
        raise ValueError("Rule cycle not found")
    if cycle.get("status") == "active":
        cycle["status"] = "stopped"
        cycle["completedAt"] = datetime.now().isoformat(timespec="seconds")
        _save_review_state(state_dir, state)
    return cycle


def evaluate_rule_cycles(state_dir: Path, snapshot: dict, persist: bool = False) -> dict:
    state = load_review_state(state_dir)
    trade_dates = snapshot.get("meta", {}).get("allTradeDates", []) or []
    catalog = snapshot.get("metricCatalog", {}) or {}
    changed = False
    for cycle in state["ruleCycles"]:
        if cycle.get("status") != "active" or cycle.get("period") != snapshot.get("period"):
            continue
        observed = len([day for day in trade_dates if day > str(cycle.get("startTradeDate") or "")])
        current = catalog.get(cycle.get("metricKey"))
        cycle["observedTradingDays"] = observed
        cycle["currentValue"] = _round(current) if current is not None else None
        if observed >= int(cycle.get("durationTradingDays", 20) or 20):
            if current is None:
                cycle["status"] = "insufficient_data"
            elif cycle.get("operator") == "<=":
                cycle["status"] = "met" if _finite(current) <= _finite(cycle.get("targetValue")) else "missed"
            else:
                cycle["status"] = "met" if _finite(current) >= _finite(cycle.get("targetValue")) else "missed"
            cycle["completedAt"] = datetime.now().isoformat(timespec="seconds")
            changed = True
    if persist and changed:
        _save_review_state(state_dir, state)
    return state


def save_round_trip_note(
    state_dir: Path,
    *,
    period: str,
    round_trip_id: str,
    payload: dict,
) -> dict:
    if not round_trip_id:
        raise ValueError("roundTripId must not be empty")
    setup_type = str(payload.get("setupType") or "")
    if setup_type and setup_type not in CONTROLLED_SETUP_TYPES:
        raise ValueError("setupType must use a canonical English value")
    path = state_dir / ROUND_TRIP_NOTES_FILE
    state = read_json(path, {})
    _ensure_supported_schema(state, ROUND_TRIP_NOTES_FILE)
    if not isinstance(state, dict):
        state = {}
    notes = state.get("notes") if isinstance(state.get("notes"), dict) else {}
    invalidation = payload.get("invalidationPrice")
    if invalidation not in (None, "") and _finite(invalidation, -1) < 0:
        raise ValueError("Invalidation price must be non-negative")
    holding = payload.get("targetHoldingDays")
    if holding not in (None, "") and not 1 <= int(_finite(holding)) <= 3650:
        raise ValueError("Target holding period must be between 1 and 3650 days")
    note = {
        "period": period,
        "roundTripId": round_trip_id,
        "setupType": setup_type,
        "invalidationPrice": _round(invalidation) if invalidation not in (None, "") else None,
        "targetHoldingDays": int(_finite(holding)) if holding not in (None, "") else None,
        "exitReason": str(payload.get("exitReason") or "")[:1000],
        "note": str(payload.get("note") or "")[:2000],
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    notes[round_trip_id] = note
    write_json(
        path,
        {
            "schemaVersion": SCHEMA_VERSION,
            "updatedAt": note["updatedAt"],
            "notes": notes,
        },
    )
    return note


def load_round_trip_notes(state_dir: Path) -> dict:
    payload = read_json(state_dir / ROUND_TRIP_NOTES_FILE, {})
    _ensure_supported_schema(payload, ROUND_TRIP_NOTES_FILE)
    return payload.get("notes", {}) if isinstance(payload, dict) else {}


def evidence_page(
    round_trip_payload: dict,
    *,
    finding: Optional[dict] = None,
    page: int = 1,
    page_size: int = 50,
    symbol: str = "",
    setup: str = "",
) -> dict:
    rows = list(round_trip_payload.get("items") or [])
    execution_map = {
        row.get("executionId"): row
        for row in (round_trip_payload.get("executions") or [])
        if isinstance(row, dict) and row.get("executionId")
    }
    if finding:
        evidence_filter = finding.get("evidenceFilter") or {}
        refs = set(evidence_filter.get("roundTripIds") or finding.get("evidenceRefs") or [])
        symbols = set(evidence_filter.get("symbols") or [])
        risk_statuses = set(evidence_filter.get("riskStatuses") or [])
        if evidence_filter.get("holdingDaysMax") is not None:
            rows = [
                row for row in rows
                if int(_finite(row.get("holding_days"))) <= int(evidence_filter["holdingDaysMax"])
            ]
        elif symbols:
            rows = [row for row in rows if row.get("symbol") in symbols]
        elif risk_statuses:
            rows = [row for row in rows if str(row.get("risk_status") or "") in risk_statuses]
        elif refs:
            rows = [row for row in rows if row.get("roundTripId") in refs]
    normalized_symbol = normalize_symbol(symbol) if symbol else ""
    if normalized_symbol:
        rows = [row for row in rows if row.get("symbol") == normalized_symbol]
    if setup:
        rows = [row for row in rows if str(row.get("setup_type") or "") == setup]
    rows.sort(key=lambda row: (row.get("close_date", ""), row.get("symbol", "")), reverse=True)
    page_size = max(1, min(int(page_size or 50), 100))
    page = max(1, int(page or 1))
    start = (page - 1) * page_size
    items = []
    for raw in rows[start:start + page_size]:
        item = dict(raw)
        item["executions"] = [
            execution_map[execution_id]
            for execution_id in item.get("executionIds", [])
            if execution_id in execution_map
        ]
        items.append(item)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "snapshotId": round_trip_payload.get("snapshotId", ""),
        "page": page,
        "pageSize": page_size,
        "total": len(rows),
        "items": items,
    }
