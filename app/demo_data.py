#!/usr/bin/env python3
"""Randomized, isolated synthetic workspace data for first-run onboarding."""

import json
import math
import random
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


DEMO_SCHEMA_VERSION = 3
DEMO_MARKER = Path("data/state/tradecraft_demo.json")
DEMO_DISABLED = Path("data/state/demo_disabled.json")
BENCHMARKS = ("QQQ.US", "SPY.US")
MARKET_UNIVERSE: Dict[str, Tuple[str, float]] = {
    "AAPL.US": ("Apple", 190),
    "ADBE.US": ("Adobe", 445),
    "AMD.US": ("AMD", 165),
    "AMZN.US": ("Amazon", 205),
    "AVGO.US": ("Broadcom", 225),
    "COST.US": ("Costco", 930),
    "CRM.US": ("Salesforce", 315),
    "CRWD.US": ("CrowdStrike", 395),
    "GOOGL.US": ("Alphabet", 195),
    "INTU.US": ("Intuit", 665),
    "META.US": ("Meta Platforms", 625),
    "MSFT.US": ("Microsoft", 440),
    "NFLX.US": ("Netflix", 980),
    "NOW.US": ("ServiceNow", 1020),
    "NVDA.US": ("NVIDIA", 145),
    "PANW.US": ("Palo Alto Networks", 190),
    "PLTR.US": ("Palantir", 105),
    "QQQ.US": ("Invesco QQQ", 525),
    "SHOP.US": ("Shopify", 115),
    "SPY.US": ("SPDR S&P 500", 600),
    "TSLA.US": ("Tesla", 285),
    "UBER.US": ("Uber", 90),
}


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def render_demo_audit_report(snapshot: dict) -> str:
    """Render an explicitly synthetic, offline audit narrative for demo mode."""
    outcome = snapshot.get("outcome") or {}
    scorecards = snapshot.get("scorecards") or {}
    data_quality = snapshot.get("dataQuality") or {}
    findings = list(snapshot.get("findings") or [])[:3]
    advantages = list(snapshot.get("advantages") or [])[:2]
    account_return = outcome.get("timeWeightedReturnPct")
    alpha = outcome.get("alphaVsPrimaryPct")
    benchmark = (outcome.get("primaryBenchmark") or {}).get("symbol") or "QQQ.US"
    confidence = (scorecards.get("confidence") or {}).get("value")
    process_risk = (scorecards.get("process") or {}).get("value")

    def number(value, suffix=""):
        try:
            return f"{float(value):.2f}{suffix}"
        except (TypeError, ValueError):
            return "—"

    finding_lines = [
        f"- **{item.get('title') or 'Finding to review'}**: {item.get('detail') or item.get('reason') or 'Drill into the synthetic trade evidence.'}"
        for item in findings
    ] or ["- The current synthetic sample produced no high-priority findings."]
    advantage_lines = [
        f"- **{item.get('title') or item.get('name') or 'Candidate edge'}**: {item.get('reason') or 'Treat this only as a candidate, not a proven strategy.'}"
        for item in advantages
    ] or ["- The sample is insufficient to confirm a durable edge; this is an intentional evidence boundary."]
    return "\n".join([
        "# Demo AI Trading Audit",
        "",
        "> TradeCraft generated this report offline from the current randomized synthetic snapshot. No external AI was called, and no real account, trade, or investment advice is included.",
        "",
        "## Executive summary",
        "",
        f"- Synthetic account return: **{number(account_return, '%')}**; versus {benchmark}: **{number(alpha, ' percentage points')}**.",
        f"- Highest process risk: **{number(process_risk, '/100')}**; data confidence: **{number(confidence, '/100')}**.",
        f"- The current snapshot contains **{snapshot.get('meta', {}).get('executionCount', 0)}** synthetic executions and **{snapshot.get('meta', {}).get('roundTripCount', 0)}** synthetic round trips.",
        "",
        "## Priority review",
        "",
        *finding_lines,
        "",
        "## Candidate strengths",
        "",
        *advantage_lines,
        "",
        "## Next actions",
        "",
        "1. Open the highest-priority finding in Evidence and inspect its complete synthetic BUY/SELL round trips.",
        "2. Turn one actionable finding into a 20-trading-day rule cycle.",
        "3. Revalidate everything with real data; do not treat Demo results as a personal trading conclusion.",
        "",
        "## Data boundary",
        "",
        f"- Initial-risk coverage: {number(data_quality.get('initialRiskCoveragePct'), '%')}.",
        f"- Trade-plan coverage: {number(data_quality.get('tradePlanCoveragePct'), '%')}.",
        "- Every symbol, date, quantity, price, return, and audit conclusion comes from this randomized Demo.",
    ]) + "\n"


def _trade(symbol: str, side: str, quantity: int, price: float, when: date, trade_time: str) -> dict:
    commission_rate = random.SystemRandom().uniform(0.003, 0.008)
    return {
        "symbol": symbol.split(".", 1)[0],
        "symbol_lb": symbol,
        "side": side,
        "quantity": quantity,
        "price": round(price, 2),
        "gross_amount": round(quantity * price, 2),
        "commission": round(max(0.25, quantity * commission_rate), 2),
        "currency": "USD",
        "exchange": "DEMO",
        "trade_date": when.strftime("%Y%m%d"),
        "trade_time": trade_time,
        "source": "demo",
        "synthetic": True,
    }


def _weekday_days(start: date, end: date) -> List[date]:
    days: List[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _previous_weekday(value: date) -> date:
    cursor = value
    while cursor.weekday() >= 5:
        cursor -= timedelta(days=1)
    return cursor


def _candles(symbol: str, days: Iterable[date], rng: random.Random) -> List[dict]:
    base = MARKET_UNIVERSE[symbol][1] * rng.uniform(0.72, 1.28)
    rows = []
    close = float(base)
    drift = rng.uniform(-0.00015, 0.00085)
    cycle = rng.uniform(10, 33)
    volatility = rng.uniform(0.009, 0.028)
    for index, day in enumerate(days):
        change = drift + math.sin(index / cycle) * rng.uniform(0.0002, 0.0018)
        change += rng.uniform(-volatility, volatility)
        open_price = close * (1 + rng.uniform(-0.009, 0.009))
        close = max(4, close * (1 + change))
        high = max(open_price, close) * (1 + rng.uniform(0.001, 0.018))
        low = min(open_price, close) * (1 - rng.uniform(0.001, 0.018))
        volume = rng.randint(4_000_000, 115_000_000)
        rows.append({
            "time": day.isoformat(),
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": volume,
            "turnover": round(volume * close, 2),
            "source": "demo",
            "synthetic": True,
        })
    return rows


def _random_time(rng: random.Random) -> str:
    return f"{rng.randint(9, 15):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"


def _price(rows: List[dict], day: date, rng: random.Random) -> float:
    by_day = {row["time"]: row for row in rows}
    candle = by_day[day.isoformat()]
    return max(1, float(candle["close"]) * (1 + rng.uniform(-0.006, 0.006)))


def _closed_rounds(
    symbols: List[str],
    days: List[date],
    candles: Dict[str, List[dict]],
    rng: random.Random,
) -> Tuple[List[dict], Dict[str, float]]:
    trades: List[dict] = []
    pnl: Dict[str, float] = {}
    if len(days) < 8:
        return trades, pnl
    for symbol in symbols:
        for _ in range(rng.randint(1, 3)):
            entry_index = rng.randint(1, max(1, len(days) - 7))
            exit_index = rng.randint(entry_index + 2, min(len(days) - 1, entry_index + rng.randint(4, 38)))
            entry_day = days[entry_index]
            exit_day = days[exit_index]
            entry_price = _price(candles[symbol], entry_day, rng)
            exit_price = _price(candles[symbol], exit_day, rng)
            target_notional = rng.uniform(1_100, 11_500)
            quantity = max(1, int(target_notional / entry_price))
            trades.extend([
                _trade(symbol, "BUY", quantity, entry_price, entry_day, _random_time(rng)),
                _trade(symbol, "SELL", quantity, exit_price, exit_day, _random_time(rng)),
            ])
            pnl[symbol] = pnl.get(symbol, 0.0) + (exit_price - entry_price) * quantity
    return trades, pnl


def _open_positions(
    symbols: List[str],
    days: List[date],
    candles: Dict[str, List[dict]],
    rng: random.Random,
) -> Tuple[List[dict], List[dict], Dict[str, float]]:
    trades: List[dict] = []
    positions: List[dict] = []
    unrealized: Dict[str, float] = {}
    for symbol in symbols:
        entry_index = rng.randint(max(0, len(days) - 55), max(0, len(days) - 4))
        entry_day = days[entry_index]
        entry_price = _price(candles[symbol], entry_day, rng)
        close_price = float(candles[symbol][-1]["close"])
        quantity = max(1, int(rng.uniform(1_000, 7_500) / entry_price))
        value = close_price * quantity
        cost_basis = entry_price * quantity
        open_pnl = value - cost_basis
        trades.append(_trade(symbol, "BUY", quantity, entry_price, entry_day, _random_time(rng)))
        positions.append({
            "symbol": symbol.split(".", 1)[0],
            "symbolLb": symbol,
            "quantity": quantity,
            "costBasis": round(cost_basis, 2),
            "closePrice": round(close_price, 2),
            "value": round(value, 2),
            "unrealizedPl": round(open_pnl, 2),
            "source": "demo",
            "synthetic": True,
        })
        unrealized[symbol] = open_pnl
    return trades, positions, unrealized


def _performance(current_year: int, month_count: int, rng: random.Random) -> dict:
    start = float(rng.randrange(720, 3100) * 100)
    series = []
    value = start
    month_names = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    for month in range(1, max(1, min(12, month_count)) + 1):
        previous = value
        monthly_return = max(-0.115, min(0.135, rng.gauss(0.008, 0.047)))
        value *= 1 + monthly_return
        series.append({
            "time": f"{current_year}-{month:02d}-01",
            "year": str(current_year),
            "month": month_names[month - 1],
            "monthNum": month,
            "value": round(value, 2),
            "valueWan": round(value / 10000, 1),
            "monthlyReturn": round((value / previous - 1) * 100, 2),
            "ytdReturn": round((value / start - 1) * 100, 2),
            "cumulativeReturn": round((value / start - 1) * 100, 2),
            "synthetic": True,
        })
    latest = series[-1]
    return {
        "years": [str(current_year)],
        "currentYear": str(current_year),
        "series": series,
        "summary": {
            "startValue": start,
            "startValueWan": round(start / 10000, 1),
            "latestValue": latest["value"],
            "latestValueWan": latest["valueWan"],
            "totalReturn": latest["cumulativeReturn"],
            "latestYearYtd": latest["ytdReturn"],
            "latestYear": str(current_year),
            "latestMonth": latest["month"],
        },
        "sourcePath": "synthetic/demo",
        "stale": False,
        "warning": "",
        "synthetic": True,
    }


def _statement(
    report_end: date,
    performance: dict,
    positions: List[dict],
) -> dict:
    starting_value = float(performance["summary"]["startValue"])
    ending_value = float(performance["summary"]["latestValue"])
    stock_value = sum(float(row["value"]) for row in positions)
    ending_cash = ending_value - stock_value
    exposure_ratio = stock_value / ending_value if ending_value else 0
    return {
        "accountSnapshot": {
            "reportEndDate": report_end.isoformat(),
            "startingValue": round(starting_value, 2),
            "endingValue": round(ending_value, 2),
            "endingCash": round(ending_cash, 2),
            "endingSettledCash": round(ending_cash, 2),
            "stockValue": round(stock_value, 2),
            "timeWeightedReturnPct": performance["summary"]["latestYearYtd"],
            "source": "demo",
            "synthetic": True,
        },
        "riskSnapshot": {
            "grossPositionValue": round(stock_value, 2),
            "stockExposurePct": round(exposure_ratio, 6),
            "cashPct": round(1 - exposure_ratio, 6),
            "leverage": round(exposure_ratio, 6),
            "synthetic": True,
        },
        "ibkrOpenPositions": positions,
        "synthetic": True,
    }


def _mtm_payload(
    report_end: date,
    symbols: List[str],
    realized: Dict[str, float],
    unrealized: Dict[str, float],
    rng: random.Random,
) -> dict:
    return {
        "source": "synthetic/demo",
        "reportEndDate": report_end.isoformat(),
        "synthetic": True,
        "stocks": {
            symbol: {
                "symbol": symbol,
                "displayName": MARKET_UNIVERSE[symbol][0],
                "mtmTotal": round(realized.get(symbol, 0.0) + unrealized.get(symbol, 0.0), 2),
                "source": "demo",
                "synthetic": True,
            }
            for symbol in symbols
        },
        "otherPnl": [{
            "assetCategory": rng.choice(["Options", "Forex", "Futures"]),
            "symbol": "SYNTHETIC",
            "displayName": "Randomized demo instrument",
            "mtmTotal": round(rng.uniform(-850, 950), 2),
            "source": "demo",
            "synthetic": True,
        }],
    }


def _workspace_has_real_data(root: Path) -> bool:
    if (root / DEMO_DISABLED).exists():
        return True
    inbox = root / "data" / "inbox"
    if inbox.exists() and any(path.is_file() and path.name != ".DS_Store" for path in inbox.iterdir()):
        return True
    for relative in ("data/state/trades.json", "data/state/manifest.json"):
        path = root / relative
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return True
        if isinstance(payload, list) and any(row.get("source") != "demo" for row in payload if isinstance(row, dict)):
            return True
        if isinstance(payload, dict) and not payload.get("synthetic"):
            return True
    return False


def demo_status(root: Path) -> dict:
    marker = root / DEMO_MARKER
    payload = {}
    if marker.exists():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    return {
        "active": bool(payload.get("active")),
        "schemaVersion": payload.get("schemaVersion", DEMO_SCHEMA_VERSION),
        "seedVersion": payload.get("seedVersion", ""),
        "generationId": payload.get("generationId", ""),
        "generatedAt": payload.get("generatedAt", ""),
        "periods": payload.get("periods", []),
        "symbols": payload.get("symbols", []),
        "watchlistSymbols": payload.get("watchlistSymbols", []),
        "fileCount": len(payload.get("files", [])),
        "synthetic": True,
        "randomized": True,
    }


def seed_demo(root: Path, force: bool = False) -> dict:
    marker = root / DEMO_MARKER
    if marker.exists() and not force:
        return demo_status(root)
    if not force and _workspace_has_real_data(root):
        return {"active": False, "synthetic": True, "randomized": True, "reason": "real-data-present"}

    generation_id = secrets.token_hex(12)
    rng = random.Random(secrets.randbits(256))
    current_year = date.today().year
    previous_year = current_year - 1
    ytd_end = _previous_weekday(date.today())
    ytd_days = _weekday_days(date(current_year, 1, 2), ytd_end)
    prior_days = _weekday_days(date(previous_year, 4, 1), date(previous_year, 12, 20))
    all_days = prior_days + ytd_days

    equity_pool = [symbol for symbol in MARKET_UNIVERSE if symbol not in BENCHMARKS]
    active_symbols = rng.sample(equity_pool, rng.randint(8, min(12, len(equity_pool))))
    closed_symbols = rng.sample(active_symbols, rng.randint(5, min(8, len(active_symbols))))
    open_symbols = rng.sample(active_symbols, rng.randint(2, min(4, len(active_symbols))))
    prior_symbols = rng.sample(active_symbols, rng.randint(3, min(6, len(active_symbols))))
    cache_symbols = sorted(set(active_symbols) | set(BENCHMARKS))
    candles = {symbol: _candles(symbol, all_days, rng) for symbol in cache_symbols}

    current_candles = {
        symbol: [row for row in rows if row["time"] >= f"{current_year}-01-01"]
        for symbol, rows in candles.items()
    }
    prior_candles = {
        symbol: [row for row in rows if row["time"].startswith(str(previous_year))]
        for symbol, rows in candles.items()
    }
    closed_trades, realized = _closed_rounds(closed_symbols, ytd_days, current_candles, rng)
    open_trades, positions, unrealized = _open_positions(open_symbols, ytd_days, current_candles, rng)
    ytd_trades = sorted(closed_trades + open_trades, key=lambda row: (row["trade_date"], row["trade_time"]))
    prior_trades, prior_realized = _closed_rounds(prior_symbols, prior_days, prior_candles, rng)
    prior_trades = sorted(prior_trades, key=lambda row: (row["trade_date"], row["trade_time"]))
    all_trades = sorted(prior_trades + ytd_trades, key=lambda row: (row["trade_date"], row["trade_time"]))

    performance = _performance(current_year, ytd_end.month, rng)
    statement = _statement(ytd_end, performance, positions)
    prior_performance = _performance(previous_year, 12, rng)
    prior_statement = _statement(date(previous_year, 12, 20), prior_performance, [])
    current_mtm = _mtm_payload(ytd_end, active_symbols, realized, unrealized, rng)
    prior_mtm = _mtm_payload(date(previous_year, 12, 20), prior_symbols, prior_realized, {}, rng)

    generated_at = datetime.now().isoformat(timespec="seconds")
    manifest = {
        "builtAt": generated_at,
        "lastRefreshAt": generated_at,
        "reportEndDate": ytd_end.isoformat(),
        "files": [{
            "path": "synthetic/demo",
            "sha256": f"synthetic-{generation_id}",
            "size": 0,
            "title": "Randomized TradeCraft Demo",
            "synthetic": True,
        }],
        "tradeCount": len(ytd_trades),
        "symbolCount": len({row["symbol_lb"] for row in ytd_trades}),
        "parsedTlgRows": len(ytd_trades),
        "parsedCsvRows": 0,
        "generationId": generation_id,
        "frozen": True,
        "synthetic": True,
        "randomized": True,
    }
    prior_manifest = {
        **manifest,
        "reportEndDate": f"{previous_year}-12-20",
        "tradeCount": len(prior_trades),
        "symbolCount": len({row["symbol_lb"] for row in prior_trades}),
    }

    created: List[str] = []
    payloads: Dict[str, object] = {
        "data/state/trades.json": ytd_trades,
        "data/state/trades_all.json": all_trades,
        "data/state/mtm.json": current_mtm,
        "data/state/manifest.json": manifest,
        "data/state/statement_snapshot.json": statement,
        "data/state/ibkr_open_positions.json": positions,
        "data/state/performance.json": performance,
        f"data/state/periods/{current_year}YTD/trades.json": ytd_trades,
        f"data/state/periods/{current_year}YTD/mtm.json": current_mtm,
        f"data/state/periods/{current_year}YTD/manifest.json": manifest,
        f"data/state/periods/{current_year}YTD/statement_snapshot.json": statement,
        f"data/state/periods/{current_year}YTD/ibkr_open_positions.json": positions,
        f"data/state/periods/{previous_year}/trades.json": prior_trades,
        f"data/state/periods/{previous_year}/mtm.json": prior_mtm,
        f"data/state/periods/{previous_year}/manifest.json": prior_manifest,
        f"data/state/periods/{previous_year}/statement_snapshot.json": prior_statement,
        f"data/state/periods/{previous_year}/ibkr_open_positions.json": [],
        "data/state/symbol_names.json": {
            symbol: {"name": MARKET_UNIVERSE[symbol][0], "source": "demo", "synthetic": True}
            for symbol in cache_symbols
        },
    }
    for relative, payload in payloads.items():
        _write_json(root / relative, payload)
        created.append(relative)

    for symbol in cache_symbols:
        relative = f"cache/kline/{symbol}_yahoo_day.json"
        _write_json(root / relative, candles[symbol])
        created.append(relative)
    kline_manifest = {
        f"{symbol}|yahoo": {
            "lastAttemptDate": ytd_end.isoformat(),
            "cacheStart": all_days[0].isoformat(),
            "cacheEnd": all_days[-1].isoformat(),
            "requestedEnd": ytd_end.isoformat(),
            "lastError": "",
            "lastSuccessDate": ytd_end.isoformat(),
            "generationId": generation_id,
            "source": "demo",
            "synthetic": True,
        }
        for symbol in cache_symbols
    }
    _write_json(root / "cache/kline/_manifest.json", kline_manifest)
    created.append("cache/kline/_manifest.json")

    watchlist_symbols = rng.sample(active_symbols, min(5, len(active_symbols)))
    marker_payload = {
        "active": True,
        "synthetic": True,
        "randomized": True,
        "schemaVersion": DEMO_SCHEMA_VERSION,
        "seedVersion": f"{DEMO_SCHEMA_VERSION}-{current_year}",
        "generationId": generation_id,
        "generatedAt": generated_at,
        "periods": ["ALL", f"{current_year}YTD", str(previous_year)],
        "symbols": cache_symbols,
        "watchlistSymbols": watchlist_symbols,
        "files": sorted(set(created + [str(DEMO_MARKER)])),
    }
    _write_json(marker, marker_payload)
    return demo_status(root)


def _remove_manifest_files(root: Path) -> None:
    marker = root / DEMO_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {}
    except Exception:
        payload = {}
    for relative in payload.get("files", []):
        target = (root / str(relative)).resolve()
        if not str(target).startswith(str(root.resolve())):
            continue
        if target.is_file():
            target.unlink()


def reset_demo(root: Path) -> dict:
    _remove_manifest_files(root)
    disabled = root / DEMO_DISABLED
    if disabled.exists():
        disabled.unlink()
    return seed_demo(root, force=True)


def exit_demo(root: Path) -> dict:
    _remove_manifest_files(root)
    _write_json(
        root / DEMO_DISABLED,
        {"disabledAt": datetime.now().isoformat(timespec="seconds"), "reason": "user-selected-real-data"},
    )
    return {"active": False, "synthetic": True, "randomized": True}


def record_demo_files(root: Path) -> dict:
    marker = root / DEMO_MARKER
    if not marker.exists():
        return demo_status(root)
    payload = json.loads(marker.read_text(encoding="utf-8"))
    files = set(payload.get("files", []))
    for relative_root in ("data/state", "cache/kline"):
        base = root / relative_root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                files.add(str(path.relative_to(root)))
    payload["files"] = sorted(files)
    _write_json(marker, payload)
    return demo_status(root)


def ensure_demo_workspace(root: Path) -> dict:
    return seed_demo(root, force=False)
