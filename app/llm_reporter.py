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


def prompt_file(locale: str = "en") -> Path:
    from app.i18n import normalize_locale
    return PROMPT_DIR / f"critique_audit_{normalize_locale(locale)}.md"


def load_prompt(locale: str = "en") -> str:
    path = prompt_file(locale)
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
    return audit_json_str[:MAX_CONTEXT_CHARS] + "\n…（上下文过长已截断）"


def dated_report_file(target_date: Optional[datetime] = None, locale: str = "en") -> Path:
    from app.i18n import normalize_locale
    day = target_date or datetime.now()
    return STATE_DIR / f"audit_report_{normalize_locale(locale)}_{day.strftime('%Y-%m-%d')}.md"


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
    return {"high": "高", "medium": "中", "low": "低"}.get(level, level or "-")


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
        setup = plan.get("setupType") or "未填写"
        setup_counts[setup] = setup_counts.get(setup, 0) + 1
    return {
        "tracked_symbol_count": len(symbol_set),
        "planned_symbol_count": len(planned),
        "missing_plan_count": len(missing),
        "missing_symbols": missing[:20],
        "setup_counts": setup_counts,
    }


def build_audit_context(summary: dict) -> dict:
    """Build a compact report context — unified MTM口径, no realized/opening split."""
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
            "verdict": t.get("verdict", "数据不足"),
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
    coverage = context.get("audit_coverage", {})
    plan_coverage = context.get("trade_plan_coverage", {})
    setup_performance = context.get("setup_performance", [])
    exit_quality = context.get("exit_quality", {})
    discipline_summary = context.get("discipline_summary", {})
    themes = context.get("theme_attribution", [])
    evidence = context.get("top_evidence", {})

    lines = []
    lines.append("# YTD 交易质量测评（本地生成）")
    lines.append("")
    lines.append(f"**生成时间**: {context.get('generated_at', '')}")
    lines.append(f"**审计周期**: {context.get('period', 'ytd')}")
    lines.append("")

    lines.append("## 1. 总体裁决")
    style = context.get("style_drift", {})
    strongest_alert = alerts[0]["detail"] if alerts else "暂无高优先级预警"
    lines.append(
        f"风格漂移分数为 **{style.get('score', 0)}/100（{_level_text(style.get('level', ''))}）**。"
        f"当前最需要处理的问题是：{strongest_alert}。"
    )
    lines.append("口径统一：所有数字为 YTD Mark-to-Market，不区分期初持仓与年内交易。")
    lines.append("")

    lines.append("## 2. YTD 收益归因")
    winners = context.get("top_mtm_winners", [])
    losers = context.get("top_mtm_losers", [])
    if winners:
        lines.append("**主要正贡献**")
        for item in winners[:5]:
            lines.append(f"- {item.get('symbol', '')}: {_fmt_money(item.get('mtmTotal', 0))}")
    if losers:
        lines.append("")
        lines.append("**主要负贡献**")
        for item in losers[:5]:
            lines.append(f"- {item.get('symbol', '')}: {_fmt_money(item.get('mtmTotal', 0))}")
    lines.append("")

    lines.append("## 3. 交易行为诊断")
    lines.append("| 维度 | 分数 | 级别 |")
    lines.append("|------|------|------|")
    score_names = {
        "theme_judgment": "主线选择",
        "churn_friction": "风格偏移",
        "stock_selection": "选股水平",
        "narrative_hype": "叙事上头",
        "entry_quality": "入场质量",
        "asymmetric_exit": "出场质量",
        "tail_risk": "仓位控制",
    }
    for key, name in score_names.items():
        s = scores.get(key, {})
        lines.append(f"| {name} | {s.get('score', 0)}/100 | {_level_text(s.get('level', 'unknown'))} |")
    lines.append("")
    if alerts:
        for alert in alerts[:6]:
            lines.append(f"- **{alert.get('type', '')}**：{alert.get('detail', '')}")
        lines.append("")

    lines.append("## 4. 主题归因（YTD MTM口径）")
    for item in themes[:8]:
        lines.append(
            f"- **{item.get('theme', '')}**：YTD MTM {_fmt_money(item.get('theme_mtm', 0))}，"
            f"判定：{item.get('verdict', '数据不足')}"
        )
    lines.append("")

    lines.append("## 5. 交易计划纪律")
    lines.append(
        f"- 覆盖标的：{plan_coverage.get('tracked_symbol_count', 0)}；"
        f"已有计划：{plan_coverage.get('planned_symbol_count', 0)}；"
        f"缺少计划：{plan_coverage.get('missing_plan_count', 0)}。"
    )
    setup_counts = plan_coverage.get("setup_counts", {})
    if setup_counts:
        lines.append("- 逻辑分布：" + "，".join(f"{k} {v}" for k, v in setup_counts.items()))
    else:
        lines.append("- 买卖逻辑标签不足，无法判断哪类打法可复制。")
    lines.append("")

    if setup_performance:
        lines.append("### 逻辑表现")
        for row in setup_performance[:6]:
            avg_r = row.get("avgR")
            avg_r_text = f"{avg_r}R" if avg_r is not None else "R缺失"
            lines.append(
                f"- {row.get('setupType')}: {row.get('closedTradeCount', 0)} 笔，"
                f"胜率 {row.get('winRate', 0)}%，实现盈亏 {_fmt_money(row.get('realizedPnl', 0))}，"
                f"平均 {avg_r_text}，缺初始风险 {row.get('riskMissingCount', 0)} 笔。"
            )
        lines.append("")

    issues = discipline_summary.get("topIssues", []) if isinstance(discipline_summary, dict) else []
    if issues:
        lines.append("### 纪律检查摘要")
        for issue in issues[:3]:
            lines.append(
                f"- {issue.get('title')}: {issue.get('evidence')} 规则：{issue.get('rule')}"
            )
        lines.append("")

    lines.append("## 6. 风险结构")
    lines.append(f"- 年初至今交易笔数：{metrics.get('ytd_trade_count', 0)}")
    lines.append(f"- 日均交易次数：{metrics.get('ytd_daily_avg', 0)}")
    lines.append(f"- 近20日日均：{metrics.get('recent_daily_avg', 0)}")
    lines.append(f"- 持仓周期中位数：{metrics.get('ytd_median_hold_days', 0)} 天")
    lines.append(f"- 摩擦成本占比：{metrics.get('friction_ratio_pct', 0)}%")
    lines.append(f"- 3日内反复交易：{metrics.get('round_trips_3d', 0)} 次")
    r_count = metrics.get("r_multiple_count", 0)
    if r_count:
        lines.append(
            f"- R 倍数：可计算 {r_count} 笔，平均 {metrics.get('avg_r_multiple')}R，"
            f"最好 {metrics.get('best_r_multiple')}R，最差 {metrics.get('worst_r_multiple')}R。"
        )
    lines.append(f"- 缺少初始风险定义：{metrics.get('risk_missing_count', 0)} 笔。")
    if exit_quality:
        lines.append(
            f"- 退出后表现：可评估 {exit_quality.get('evaluatedCount', 0)} 笔，"
            f"退出后20日平均 {exit_quality.get('avgPostExit20dPct')}%，"
            f"20日后继续涨超10%的卖出 {exit_quality.get('sellTooEarlyCount20d', 0)} 笔，"
            f"趋势未破退出 {exit_quality.get('trendUnbrokenExitCount', 0)} 笔。"
        )
    lines.append("")

    lines.append("## 7. 最坏的交易模式")
    worst_theme = min(themes, key=lambda x: x.get("theme_mtm", 0), default=None)
    if worst_theme and worst_theme.get("theme_mtm", 0) < 0:
        lines.append(f"- **{worst_theme.get('theme', '')}** 是当前最大负贡献主题，YTD MTM {_fmt_money(worst_theme.get('theme_mtm', 0))}，需要停止或收缩。")
    lines.append("")

    lines.append("## 8. 最好的交易")
    best_theme = max(themes, key=lambda x: x.get("theme_mtm", 0), default=None)
    if best_theme and best_theme.get("theme_mtm", 0) > 0:
        lines.append(f"- **{best_theme.get('theme', '')}** 是当前最大正贡献主题，YTD MTM {_fmt_money(best_theme.get('theme_mtm', 0))}，值得坚持。")
    lines.append("")

    lines.append("## 9. 下月铁律")
    lines.append(f"1. 单日新增交易决策达到 3 次后停止开新仓；复盘指标：日均交易次数从 {metrics.get('ytd_daily_avg', 0)} 降到 3 以下。")
    lines.append("2. Optionality 标的只允许在有明确退出价和最大亏损时参与；复盘指标：optionality 亏损贡献下降。")
    lines.append("3. FOMO score 高于 70 的买入延后一个交易日；复盘指标：高 FOMO 买入占比下降。")
    lines.append("4. 当前价低于运行持仓均价时禁止追加同一标的；复盘指标：亏损加仓次数下降。")
    lines.append("5. 缺少交易计划的标的禁止新增仓位，直到补齐买卖逻辑、无效价、止损条件和目标持有周期。")
    lines.append("6. 没有无效价 / 初始风险的交易不得计入有效逻辑统计；复盘指标：risk_missing_count 下降。")
    lines.append("")

    lines.append("## 10. 一句话诊断")
    if evidence.get("asymmetric_exit"):
        lines.append("你当前最需要处理的不是选股灵感，而是把交易执行从冲动反应改成可复盘的规则系统。")
    else:
        lines.append("当前最需要处理的是交易纪律问题。")
    lines.append("")
    lines.append("> 本地生成版本使用 YTD MTM 口径。")

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
        return "Kimi API key 认证失败，可能已过期或被撤销。"
    if status == 429:
        if "insufficient balance" in combined or "quota" in combined or "exceeded_current_quota" in combined:
            return "Kimi 账户余额不足或额度已用完，请在 Moonshot 控制台充值或检查套餐后重试。"
        return "Kimi API 触发速率限制，请稍后重试。"
    if status >= 500:
        return f"Kimi API 服务端暂时不可用（HTTP {status}），请稍后重试。"
    return f"Kimi API 返回 HTTP {status}：{api_message[:200] or '未知错误'}"


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
        raise _KimiRetryableError(f"网络错误：{type(e).__name__}")

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise _KimiRetryableError(f"响应解析失败: {e}")

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices or not isinstance(choices, list) or not isinstance(choices[0], dict):
        raise _KimiRetryableError("API 返回空 choices")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not content:
        raise _KimiRetryableError("API 返回空 content")
    return content


def call_kimi_api_with_error(prompt: str, api_key: str, locale: str = "en") -> Tuple[Optional[str], Optional[str]]:
    """Call Kimi API with retry/backoff and return (content, error_message)."""
    payload = {
        "model": get_model_name(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一位锋利但克制的交易质量审计员，所有结论必须绑定数据。"
                    if str(locale).lower().startswith("zh")
                    else "You are a direct but disciplined trading-quality auditor. Every conclusion must cite the supplied data."
                ),
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


def call_kimi_api(prompt: str, api_key: str, locale: str = "en") -> Optional[str]:
    """Call Kimi API and return content, or None on failure."""
    content, _ = call_kimi_api_with_error(prompt, api_key, locale)
    return content


def generate_report(force: bool = False, locale: str = "en") -> tuple[str, bool]:
    """Generate report or reuse today's cached report.

    Returns:
        (report_file_path, cached)
    """
    summary = load_audit_summary()
    from app.i18n import normalize_locale
    locale = normalize_locale(locale)
    report_path = dated_report_file(locale=locale)
    current_report_file = STATE_DIR / f"audit_report.{locale}.md"

    if not force and report_path.exists():
        content = report_path.read_text(encoding="utf-8")
        atomic_write_text(current_report_file, content)
        return str(report_path), True

    api_key = get_api_key()

    if not api_key:
        raise RuntimeError("Kimi API key 未配置，无法生成 AI 交易质量评测。请设置 KIMI_API_KEY。")

    prompt_template = load_prompt(locale)
    audit_context = build_audit_context(summary)
    audit_json_str = cap_context_json(json.dumps(audit_context, ensure_ascii=False, indent=2))
    prompt = prompt_template.replace("{{AUDIT_CONTEXT}}", audit_json_str)
    prompt = prompt.replace("{{AUDIT_JSON}}", audit_json_str)
    content = call_kimi_api(prompt, api_key, locale)
    if content is None:
        raise RuntimeError(
            "Kimi API 调用失败，未生成 AI 交易质量评测。"
            "可能原因：网络超时、API 服务端暂时不可用、或 API key 无效。"
            "请检查服务器日志获取详细错误信息，稍后重试。"
        )

    atomic_write_text(report_path, content)
    atomic_write_text(current_report_file, content)

    return str(report_path), False


if __name__ == "__main__":
    path, _ = generate_report()
    print(path)
