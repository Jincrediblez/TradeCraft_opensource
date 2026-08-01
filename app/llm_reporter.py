#!/usr/bin/env python3
"""Kimi LLM report generator for trading audit critique."""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import requests
import tenacity

try:  # normal package import (server, cli, pytest)
    from app.state_store import app_log, atomic_write_text, read_json
except ImportError:  # standalone: `python app/llm_reporter.py` puts app/ on sys.path
    from state_store import app_log, atomic_write_text, read_json


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
PROMPT_DIR = ROOT / "prompts"

AUDIT_SUMMARY_FILE = STATE_DIR / "audit_summary.json"
REPORT_FILE = STATE_DIR / "audit_report.md"

DEFAULT_KIMI_API_URL = "https://api.moonshot.cn/v1/chat/completions"
DEFAULT_MODEL = "moonshot-v1-32k"

# Network/robustness knobs (overridable via env for slow links or debugging).
KIMI_TIMEOUT_SECONDS = int(os.environ.get("KIMI_TIMEOUT_SECONDS", "120"))
KIMI_MAX_ATTEMPTS = int(os.environ.get("KIMI_MAX_ATTEMPTS", "3"))
# Guard against pathologically large prompts blowing past the model context
# window (moonshot-v1-32k ≈ 32k tokens). ~120k chars is a safe ceiling that
# still leaves headroom for the template and the model's own output.
MAX_CONTEXT_CHARS = int(os.environ.get("KIMI_MAX_CONTEXT_CHARS", "120000"))


def load_audit_summary() -> dict:
    return read_json(AUDIT_SUMMARY_FILE, {})


def prompt_file() -> Path:
    return PROMPT_DIR / "critique_audit_en.md"


def load_prompt() -> str:
    path = prompt_file()
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Unable to read audit prompt template {path.name}: {exc}") from exc


def cap_context_json(audit_json_str: str) -> str:
    """Truncate an over-long audit-context JSON string before prompt assembly."""
    if len(audit_json_str) <= MAX_CONTEXT_CHARS:
        return audit_json_str
    app_log(
        "audit context truncated",
        level="warning",
        original_chars=len(audit_json_str),
        max_chars=MAX_CONTEXT_CHARS,
    )
    return audit_json_str[:MAX_CONTEXT_CHARS] + "\n... (context truncated because it was too long)"


def dated_report_file(target_date: Optional[datetime] = None) -> Path:
    day = target_date or datetime.now()
    return STATE_DIR / f"audit_report_{day.strftime('%Y-%m-%d')}.md"


def _read_env_value(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value:
        return value
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return None


def get_api_key() -> Optional[str]:
    return _read_env_value("KIMI_API_KEY") or os.environ.get("kimi-api-key")


def get_kimi_api_url() -> str:
    return _read_env_value("KIMI_API_URL") or DEFAULT_KIMI_API_URL


def get_model_name() -> str:
    env_model = _read_env_value("KIMI_MODEL")
    if env_model:
        return env_model
    try:
        from app.settings import load_settings
        return str(load_settings().get("kimiModel") or DEFAULT_MODEL)
    except Exception:
        return DEFAULT_MODEL


def _level_text(level: str) -> str:
    return {"high": "High", "medium": "Medium", "low": "Low"}.get(level, level or "-")


def _fmt_money(value) -> str:
    try:
        return f"{float(value):+,.2f}"
    except Exception:
        return "+0.00"


def _top_mtm_items(stocks: dict, reverse: bool) -> list:
    rows = list(stocks.values()) if isinstance(stocks, dict) else []
    rows = [row for row in rows if isinstance(row, dict)]
    return sorted(rows, key=lambda x: x.get("mtmTotal", 0), reverse=reverse)[:10]


def _theme_mtm_map(mtm_by_sym: dict, themes: list) -> dict:
    """Aggregate symbol-level MTM to theme-level MTM."""
    result = {}
    for t in themes:
        theme_name = t.get("theme", "")
        symbols = t.get("symbols", [])
        total = sum(
            mtm_by_sym.get(s, {}).get("mtmTotal", 0) if isinstance(mtm_by_sym.get(s), dict)
            else mtm_by_sym.get(s, 0)
            for s in symbols
        )
        result[theme_name] = total
    return result


def _trade_plan_coverage(symbols: list) -> dict:
    try:
        from app.trade_plans import load_trade_plans
        plans = load_trade_plans()
    except Exception:
        plans = {}
    symbol_set = {s for s in symbols if s}
    planned = {s: p for s, p in plans.items() if s in symbol_set and isinstance(p, dict)}
    setup_counts = {}
    missing = sorted(symbol_set - set(planned.keys()))
    for plan in planned.values():
        setup = plan.get("setupType") or "Not provided"
        setup_counts[setup] = setup_counts.get(setup, 0) + 1
    return {
        "tracked_symbol_count": len(symbol_set),
        "planned_symbol_count": len(planned),
        "missing_plan_count": len(missing),
        "missing_symbols": missing[:20],
        "setup_counts": setup_counts,
    }


def build_audit_context(summary: dict) -> dict:
    """Build a compact report context on a unified MTM basis."""
    mtm_raw = summary.get("ytd_mtm_contribution", {})
    # Normalize: keys may be symbol strings or nested dicts
    mtm_by_sym = {}
    for k, v in mtm_raw.items():
        if isinstance(v, dict):
            mtm_by_sym[v.get("symbol", k)] = v.get("mtmTotal", 0)
        else:
            mtm_by_sym[k] = float(v) if v else 0

    evidence = summary.get("evidence", {}) or {}
    themes = summary.get("theme_attribution", []) or []
    theme_mtm_map = _theme_mtm_map(mtm_by_sym, themes)
    symbols_for_plans = sorted(set(mtm_by_sym.keys()) | {s for t in themes for s in (t.get("symbols", []) or [])})

    # Build theme list with MTM replacing realized_pnl
    themes_mtm = []
    for t in themes:
        themes_mtm.append({
            "theme": t.get("theme", ""),
            "tier": t.get("tier", ""),
            "trade_count": t.get("trade_count", 0),
            "total_notional": t.get("total_notional", 0),
            "symbols": t.get("symbols", []),
            "theme_mtm": theme_mtm_map.get(t.get("theme", ""), 0),
            "avg_trader_return_pct": t.get("avg_trader_return_pct"),
            "avg_stock_buyhold_return_pct": t.get("avg_stock_buyhold_return_pct"),
            "avg_theme_return_pct": t.get("avg_theme_return_pct"),
            "avg_selection_alpha_pct": t.get("avg_selection_alpha_pct"),
            "avg_timing_alpha_pct": t.get("avg_timing_alpha_pct"),
            "verdict": t.get("verdict", "Insufficient data"),
        })

    return {
        "generated_at": summary.get("generated_at", ""),
        "period": summary.get("period", "ytd"),
        "scores": summary.get("scores", {}),
        "style_drift": summary.get("style_drift", {}),
        "metrics": summary.get("metrics", {}),
        "behavior_radar": summary.get("behavior_radar", {}),
        "setup_performance": summary.get("setup_performance", []),
        "exit_quality": summary.get("exit_quality", {}),
        "discipline_summary": summary.get("discipline_summary", {}),
        "trade_plan_coverage": _trade_plan_coverage(symbols_for_plans),
        "top_mtm_winners": _top_mtm_items(mtm_raw, True),
        "top_mtm_losers": _top_mtm_items(mtm_raw, False),
        "theme_attribution": themes_mtm,
        "alerts": (summary.get("alerts", []) or [])[:10],
        "top_evidence": {key: values[:5] for key, values in evidence.items() if values},
    }


def generate_fallback_report(summary: dict) -> str:
    """Generate a structured report locally when LLM is unavailable."""
    context = build_audit_context(summary)
    scores = context.get("scores", {})
    alerts = context.get("alerts", [])
    metrics = context.get("metrics", {})
    plan_coverage = context.get("trade_plan_coverage", {})
    setup_performance = context.get("setup_performance", [])
    exit_quality = context.get("exit_quality", {})
    discipline_summary = context.get("discipline_summary", {})
    themes = context.get("theme_attribution", [])
    evidence = context.get("top_evidence", {})

    lines = [
        "# YTD Trading Quality Audit (Local Report)",
        "",
        f"**Generated at**: {context.get('generated_at', '')}",
        f"**Audit period**: {context.get('period', 'ytd')}",
        "",
        "## 1. Overall verdict",
    ]
    style = context.get("style_drift", {})
    strongest_alert = alerts[0]["detail"] if alerts else "No high-priority alerts."
    lines.append(
        f"The style-drift score is **{style.get('score', 0)}/100 "
        f"({_level_text(style.get('level', ''))})**. Highest-priority issue: {strongest_alert}"
    )
    lines.append("All contribution figures use a unified YTD mark-to-market basis.")
    lines.append("")

    lines.append("## 2. YTD return attribution")
    winners = context.get("top_mtm_winners", [])
    losers = context.get("top_mtm_losers", [])
    if winners:
        lines.append("**Largest positive contributors**")
        for item in winners[:5]:
            lines.append(f"- {item.get('symbol', '')}: {_fmt_money(item.get('mtmTotal', 0))}")
    if losers:
        lines.append("")
        lines.append("**Largest negative contributors**")
        for item in losers[:5]:
            lines.append(f"- {item.get('symbol', '')}: {_fmt_money(item.get('mtmTotal', 0))}")
    lines.append("")

    lines.append("## 3. Trading behavior diagnosis")
    lines.append("| Dimension | Score | Level |")
    lines.append("|------|------|------|")
    score_names = {
        "theme_judgment": "Theme selection",
        "churn_friction": "Style drift",
        "stock_selection": "Stock selection",
        "narrative_hype": "Narrative risk",
        "entry_quality": "Entry quality",
        "asymmetric_exit": "Exit quality",
        "tail_risk": "Position sizing",
    }
    for key, name in score_names.items():
        s = scores.get(key, {})
        lines.append(f"| {name} | {s.get('score', 0)}/100 | {_level_text(s.get('level', 'unknown'))} |")
    lines.append("")
    if alerts:
        for alert in alerts[:6]:
            lines.append(f"- **{alert.get('type', '')}**: {alert.get('detail', '')}")
        lines.append("")

    lines.append("## 4. Theme attribution (YTD MTM basis)")
    for item in themes[:8]:
        lines.append(
            f"- **{item.get('theme', '')}**: YTD MTM {_fmt_money(item.get('theme_mtm', 0))}; "
            f"verdict: {item.get('verdict', 'Insufficient data')}"
        )
    lines.append("")

    lines.append("## 5. Trade-plan discipline")
    lines.append(
        f"- Covered symbols: {plan_coverage.get('tracked_symbol_count', 0)}; "
        f"with plans: {plan_coverage.get('planned_symbol_count', 0)}; "
        f"missing plans: {plan_coverage.get('missing_plan_count', 0)}."
    )
    setup_counts = plan_coverage.get("setup_counts", {})
    if setup_counts:
        lines.append("- Setup distribution: " + ", ".join(f"{k} {v}" for k, v in setup_counts.items()))
    else:
        lines.append("- Setup coverage is insufficient to identify a repeatable approach.")
    lines.append("")

    if setup_performance:
        lines.append("### Setup performance")
        for row in setup_performance[:6]:
            avg_r = row.get("avgR")
            avg_r_text = f"{avg_r}R" if avg_r is not None else "R unavailable"
            lines.append(
                f"- {row.get('setupType')}: {row.get('closedTradeCount', 0)} closed trades, "
                f"win rate {row.get('winRate', 0)}%, realized P&L {_fmt_money(row.get('realizedPnl', 0))}, "
                f"average {avg_r_text}, missing initial risk {row.get('riskMissingCount', 0)}."
            )
        lines.append("")

    issues = discipline_summary.get("topIssues", []) if isinstance(discipline_summary, dict) else []
    if issues:
        lines.append("### Discipline check summary")
        for issue in issues[:3]:
            lines.append(
                f"- {issue.get('title')}: {issue.get('evidence')} Rule: {issue.get('rule')}"
            )
        lines.append("")

    lines.append("## 6. Risk structure")
    lines.append(f"- Year-to-date execution count: {metrics.get('ytd_trade_count', 0)}")
    lines.append(f"- Average daily executions: {metrics.get('ytd_daily_avg', 0)}")
    lines.append(f"- Recent 20-day daily average: {metrics.get('recent_daily_avg', 0)}")
    lines.append(f"- Median holding period: {metrics.get('ytd_median_hold_days', 0)} days")
    lines.append(f"- Friction-cost ratio: {metrics.get('friction_ratio_pct', 0)}%")
    lines.append(f"- Round trips within three days: {metrics.get('round_trips_3d', 0)}")
    r_count = metrics.get("r_multiple_count", 0)
    if r_count:
        lines.append(
            f"- R multiples: {r_count} calculable trades, average {metrics.get('avg_r_multiple')}R, "
            f"best {metrics.get('best_r_multiple')}R, worst {metrics.get('worst_r_multiple')}R."
        )
    lines.append(f"- Trades missing initial risk: {metrics.get('risk_missing_count', 0)}.")
    if exit_quality:
        lines.append(
            f"- Post-exit performance: {exit_quality.get('evaluatedCount', 0)} evaluable trades; "
            f"average 20-day post-exit return {exit_quality.get('avgPostExit20dPct')}%; "
            f"sales followed by gains above 10% {exit_quality.get('sellTooEarlyCount20d', 0)}; "
            f"exits before trend failure {exit_quality.get('trendUnbrokenExitCount', 0)}."
        )
    lines.append("")

    lines.append("## 7. Worst trading patterns")
    worst_theme = min(themes, key=lambda x: x.get("theme_mtm", 0), default=None)
    if worst_theme and worst_theme.get("theme_mtm", 0) < 0:
        lines.append(f"- **{worst_theme.get('theme', '')}** is the largest negative theme contributor at YTD MTM {_fmt_money(worst_theme.get('theme_mtm', 0))}; stop or reduce it.")
    lines.append("")

    lines.append("## 8. Best trades")
    best_theme = max(themes, key=lambda x: x.get("theme_mtm", 0), default=None)
    if best_theme and best_theme.get("theme_mtm", 0) > 0:
        lines.append(f"- **{best_theme.get('theme', '')}** is the largest positive theme contributor at YTD MTM {_fmt_money(best_theme.get('theme_mtm', 0))}; continue validating it.")
    lines.append("")

    lines.append("## 9. Rules for next month")
    lines.append(f"1. Stop opening new positions after three new decisions in one day; target average daily executions below 3 from {metrics.get('ytd_daily_avg', 0)}.")
    lines.append("2. Trade optionality symbols only with an explicit exit price and maximum loss; target a lower optionality loss contribution.")
    lines.append("3. Delay entries with a FOMO score above 70 by one trading day; target a lower high-FOMO entry rate.")
    lines.append("4. Do not add when price is below the running position cost; target fewer adds to losing positions.")
    lines.append("5. Do not add to symbols without a trade plan covering setup, invalidation, exit condition, and holding period.")
    lines.append("6. Exclude trades without an invalidation price or initial risk from setup statistics; reduce risk_missing_count.")
    lines.append("")

    lines.append("## 10. One-line diagnosis")
    if evidence.get("asymmetric_exit"):
        lines.append("The priority is not another stock idea; it is turning reactive execution into a reviewable rule system.")
    else:
        lines.append("Trading discipline is the highest-priority issue.")
    lines.append("")
    lines.append("> The locally generated version uses a YTD mark-to-market basis.")

    return "\n".join(lines)




def _kimi_http_error_message(status: int, text: str) -> str:
    try:
        payload = json.loads(text)
        api_message = str(payload.get("error", {}).get("message") or "")
        api_type = str(payload.get("error", {}).get("type") or "")
    except Exception:
        api_message = text
        api_type = ""

    combined = f"{api_type} {api_message}".lower()
    if status == 401:
        return "Kimi API key authentication failed; the credential may have expired or been revoked."
    if status == 429:
        if "insufficient balance" in combined or "quota" in combined or "exceeded_current_quota" in combined:
            return "Kimi account balance or quota is exhausted; check the Moonshot console before retrying."
        return "Kimi API rate limit reached; retry later."
    if status >= 500:
        return f"Kimi API service is temporarily unavailable (HTTP {status}); retry later."
    return f"Kimi API returned HTTP {status}: {api_message[:200] or 'Unknown error'}"


class _KimiRetryableError(Exception):
    """Transient failure (429/5xx/timeout/malformed body) — worth retrying."""

    def __init__(self, message: str):
        super().__init__(message)
        self.user_message = message


class _KimiFatalError(Exception):
    """Permanent failure (auth, quota, 4xx) — retrying will not help."""

    def __init__(self, message: str):
        super().__init__(message)
        self.user_message = message


def _do_kimi_request(payload: dict, headers: dict) -> str:
    """Single Kimi API attempt. Raises _Kimi{Retryable,Fatal}Error on failure."""
    try:
        resp = requests.post(
            get_kimi_api_url(), json=payload, headers=headers, timeout=KIMI_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code
        text = e.response.text or ""
        message = _kimi_http_error_message(status, text)
        combined = text.lower()
        if status == 401:
            raise _KimiFatalError(message)
        if status == 429 and ("insufficient balance" in combined or "exceeded_current_quota" in combined):
            raise _KimiFatalError(message)
        if status == 429 or status >= 500:
            raise _KimiRetryableError(message)
        raise _KimiFatalError(message)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        raise _KimiRetryableError(f"Network error: {type(e).__name__}")

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise _KimiRetryableError(f"Response parsing failed: {e}")

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list) or not isinstance(choices[0], dict):
        raise _KimiRetryableError("API returned empty choices")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not content:
        raise _KimiRetryableError("API returned empty content")
    return content


def call_kimi_api_with_error(prompt: str, api_key: str) -> Tuple[Optional[str], Optional[str]]:
    """Call Kimi API with retry/backoff and return (content, error_message)."""
    payload = {
        "model": get_model_name(),
        "messages": [
            {
                "role": "system",
                "content": "You are a direct but disciplined trading-quality auditor. Every conclusion must cite the supplied data.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    def _log_retry(retry_state: "tenacity.RetryCallState") -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        app_log(
            "kimi api retry",
            level="warning",
            attempt=retry_state.attempt_number,
            error=str(exc) if exc else "",
        )

    retryer = tenacity.Retrying(
        stop=tenacity.stop_after_attempt(KIMI_MAX_ATTEMPTS),
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=8),
        retry=tenacity.retry_if_exception_type(_KimiRetryableError),
        reraise=True,
        before_sleep=_log_retry,
    )
    try:
        return retryer(_do_kimi_request, payload, headers), None
    except _KimiFatalError as e:
        app_log("kimi api fatal", level="error", error=e.user_message)
        return None, e.user_message
    except _KimiRetryableError as e:
        app_log("kimi api exhausted", level="error", error=e.user_message)
        return None, e.user_message
    except Exception as e:  # unexpected — surface but don't crash the request
        app_log("kimi api unexpected", level="error", error=f"{type(e).__name__}: {e}")
        return None, f"{type(e).__name__}: {e}"


def call_kimi_api(prompt: str, api_key: str) -> Optional[str]:
    """Call Kimi API and return content, or None on failure."""
    content, _ = call_kimi_api_with_error(prompt, api_key)
    return content


def generate_report(force: bool = False) -> tuple[str, bool]:
    """Generate report or reuse today's cached report.

    Returns:
        (report_file_path, cached)
    """
    summary = load_audit_summary()
    report_path = dated_report_file()
    current_report_file = REPORT_FILE

    if not force and report_path.exists():
        content = report_path.read_text(encoding="utf-8")
        atomic_write_text(current_report_file, content)
        return str(report_path), True

    api_key = get_api_key()

    if not api_key:
        raise RuntimeError("KIMI_API_KEY is not configured; an AI trading-quality report cannot be generated.")

    prompt_template = load_prompt()
    audit_context = build_audit_context(summary)
    audit_json_str = cap_context_json(json.dumps(audit_context, ensure_ascii=False, indent=2))
    prompt = prompt_template.replace("{{AUDIT_CONTEXT}}", audit_json_str)
    prompt = prompt.replace("{{AUDIT_JSON}}", audit_json_str)
    content = call_kimi_api(prompt, api_key)
    if content is None:
        raise RuntimeError(
            "Kimi API call failed and no AI trading-quality report was generated. "
            "Check the server log, API key, quota, and network connection before retrying."
        )

    atomic_write_text(report_path, content)
    atomic_write_text(current_report_file, content)

    return str(report_path), False


if __name__ == "__main__":
    path, _ = generate_report()
    print(path)
