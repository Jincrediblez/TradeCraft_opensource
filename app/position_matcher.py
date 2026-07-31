#!/usr/bin/env python3
"""FIFO position matcher for IBKR trades.

Reads trades.json, matches BUY/SELL per symbol using FIFO,
and outputs closed_trades.json + open_positions.json.
"""

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


try:  # normal package import (server, cli, pytest)
    from app.state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
    from app.symbols import normalize_symbol
    from app.schemas import trades_from_rows
except ImportError:  # standalone: `python app/position_matcher.py` puts app/ on sys.path
    from state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
    from symbols import normalize_symbol
    from schemas import trades_from_rows

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
TRADES_FILE = STATE_DIR / "trades.json"
MTM_FILE = STATE_DIR / "mtm.json"
OPENING_POSITIONS_FILE = STATE_DIR / "opening_positions.json"
CLOSED_TRADES_FILE = STATE_DIR / "closed_trades.json"
OPEN_POSITIONS_FILE = STATE_DIR / "open_positions.json"
AUDIT_COVERAGE_FILE = STATE_DIR / "audit_coverage.json"

ENTRY_ACTUAL = "actual_trade"
ENTRY_OPENING = "opening_rebased"
ENTRY_MIXED = "mixed"


@dataclass
class Trade:
    symbol: str
    symbol_lb: str
    side: str
    quantity: float
    price: float
    gross_amount: float
    commission: float
    currency: str
    exchange: str
    trade_date: str
    trade_time: str
    source: str


@dataclass
class Lot:
    """A partially or fully unmatched BUY lot."""
    symbol_lb: str
    date: str
    quantity: float
    price: float
    commission: float
    entry_source: str = ENTRY_ACTUAL
    setup_type: str = ""  # per-trade setup tag


@dataclass
class ClosedTrade:
    symbol: str
    open_date: str
    close_date: str
    holding_days: int
    quantity: float
    avg_entry: float
    avg_exit: float
    realized_pnl: float
    commissions: float
    entry_source: str
    initial_risk: Optional[float] = None
    r_multiple: Optional[float] = None
    risk_status: str = "risk_missing"
    setup_type: str = ""       # entry setup tag from BUY lot
    exit_setup_type: str = ""  # exit setup tag from SELL trade


@dataclass
class OpenPosition:
    symbol: str
    quantity: float
    avg_cost: float
    total_commissions: float
    first_buy_date: str
    last_buy_date: str
    unrealized_pnl: Optional[float]
    current_drawdown_pct: Optional[float]
    days_held: int
    entry_source: str


def read_json(path: Path, default):
    return _store_read_json(path, default)


def write_json(path: Path, payload) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def parse_date(date_str: str) -> datetime:
    """Parse YYYYMMDD or YYYY-MM-DD."""
    if len(date_str) == 8:
        return datetime.strptime(date_str, "%Y%m%d")
    return datetime.strptime(date_str, "%Y-%m-%d")


def load_trades() -> List[Trade]:
    return trades_from_rows(read_json(TRADES_FILE, []))


def group_trades_by_symbol(trades: List[Trade]) -> Dict[str, List[Trade]]:
    grouped = defaultdict(list)
    for t in trades:
        grouped[t.symbol_lb].append(t)
    # Sort by date then time
    for symbol in grouped:
        grouped[symbol].sort(key=lambda t: (t.trade_date, t.trade_time))
    return dict(grouped)


def audit_year_from_trades(trades: List[Trade]) -> int:
    dates = [t.trade_date for t in trades if t.trade_date]
    if dates:
        first = min(dates)
        return parse_date(first).year
    return datetime.now().year


def trade_plan_for_symbol(symbol_lb: str, trade_date: str = "") -> dict:
    try:
        from app.trade_plans import get_trade_plan, get_trade_plan_at
        return get_trade_plan_at(symbol_lb, trade_date) if trade_date else get_trade_plan(symbol_lb)
    except Exception:
        return {}


def setup_type_for_symbol(symbol_lb: str) -> str:
    plan = trade_plan_for_symbol(symbol_lb)
    return str(plan.get("setupType") or "")


def setup_type_for_trade(symbol_lb: str, trade_date: str, trade_time: str = "") -> str:
    """Prefer trade-level setup from trade_setups.json, fallback to symbol-level plan."""
    try:
        from app.trade_setup_manager import get_setup_for_trade
        ts = get_setup_for_trade(symbol_lb, trade_date, trade_time)
        if ts and ts.get("setupType"):
            return ts.get("setupType")
    except Exception:
        pass
    return setup_type_for_symbol(symbol_lb)


def risk_metrics_for_closed_trade(
    symbol_lb: str,
    entry_price: float,
    quantity: float,
    realized_pnl: float,
    open_date: str = "",
) -> Tuple[Optional[float], Optional[float], str]:
    try:
        plan = trade_plan_for_symbol(symbol_lb, open_date)
    except TypeError:
        # Backward-compatible for older integrations/tests that replace this
        # helper with a one-argument callable.
        plan = trade_plan_for_symbol(symbol_lb)
    invalidation = plan.get("invalidationPrice")
    try:
        invalidation_price = float(invalidation)
    except (TypeError, ValueError):
        return None, None, "risk_missing"
    risk_per_share = entry_price - invalidation_price
    if risk_per_share <= 0 or quantity <= 0:
        return None, None, "invalid_risk"
    initial_risk = risk_per_share * quantity
    return round(initial_risk, 6), round(realized_pnl / initial_risk, 6), "planned"


def load_opening_position_quantities() -> Dict[str, float]:
    """Load YTD opening quantities from an audit-only state file.

    Supported formats:
      {"positions": [{"symbol": "INTC.US", "quantity": 100}]}
      {"INTC.US": 100, "AMD.US": {"quantity": 50}}
    """
    payload = read_json(OPENING_POSITIONS_FILE, {})
    if isinstance(payload, dict) and isinstance(payload.get("positions"), list):
        rows = payload["positions"]
    elif isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = []
        for symbol, value in payload.items():
            if symbol in {"source", "asOf", "year"}:
                continue
            qty = value.get("quantity") if isinstance(value, dict) else value
            rows.append({"symbol": symbol, "quantity": qty})
    else:
        rows = []

    quantities: Dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol_lb") or row.get("symbol") or row.get("code")
        if not symbol:
            continue
        try:
            qty = float(row.get("quantity", 0))
        except Exception:
            qty = 0.0
        if abs(qty) > 1e-9:
            quantities[normalize_symbol(str(symbol))] = qty
    return quantities


def load_mtm_symbols() -> set:
    payload = read_json(MTM_FILE, {})
    stocks = payload.get("stocks", {}) if isinstance(payload, dict) else {}
    return set(stocks.keys()) if isinstance(stocks, dict) else set()


def rebase_price_on_or_before(symbol_lb: str, audit_year: int) -> Optional[Tuple[str, float]]:
    from app.main import load_kline_cache  # lazy import to avoid circular
    target = f"{audit_year - 1}1231"
    best: Optional[Tuple[str, float]] = None
    for row in load_kline_cache(symbol_lb):
        day = str(row.get("time", ""))[:10].replace("-", "")
        if day <= target:
            try:
                best = (day, float(row.get("close", 0)))
            except Exception:
                pass
        else:
            break
    if best and best[1] > 0:
        return best
    return None


def build_opening_lots(audit_year: int, quantities: Dict[str, float]) -> Tuple[Dict[str, List[Lot]], List[dict]]:
    lots: Dict[str, List[Lot]] = {}
    missing: List[dict] = []
    for symbol, qty in quantities.items():
        if qty <= 0:
            missing.append({
                "symbol": symbol,
                "reason": "short_or_zero_opening_position_not_supported",
                "quantity": qty,
            })
            continue
        rebased = rebase_price_on_or_before(symbol, audit_year)
        if not rebased:
            missing.append({
                "symbol": symbol,
                "reason": "opening_rebase_price_missing",
                "quantity": qty,
            })
            continue
        rebase_date, price = rebased
        lots[symbol] = [Lot(
            symbol_lb=symbol,
            date=rebase_date,
            quantity=qty,
            price=price,
            commission=0.0,
            entry_source=ENTRY_OPENING,
        )]
    return lots, missing


def match_fifo(
    symbol_lb: str,
    trades: List[Trade],
    opening_lots: Optional[List[Lot]] = None,
) -> Tuple[List[ClosedTrade], List[Lot], List[dict]]:
    """FIFO match BUY/SELL for a single symbol.

    Returns (closed_trades, remaining_open_lots, missing_coverage).
    """
    buy_queue: List[Lot] = list(opening_lots or [])
    closed: List[ClosedTrade] = []
    missing: List[dict] = []

    for trade in trades:
        qty = abs(trade.quantity)
        if qty == 0:
            continue

        if trade.side == "BUY":
            buy_setup = setup_type_for_trade(symbol_lb, trade.trade_date, trade.trade_time)
            buy_queue.append(Lot(
                symbol_lb=symbol_lb,
                date=trade.trade_date,
                quantity=qty,
                price=trade.price,
                commission=trade.commission,
                entry_source=ENTRY_ACTUAL,
                setup_type=buy_setup,
            ))
        elif trade.side == "SELL":
            remaining_sell = qty
            sell_commission = trade.commission
            sell_price = trade.price
            sell_date = trade.trade_date

            # Distribute sell commission across matched lots proportionally
            # First, figure out how much we'll actually match
            total_matched = 0.0
            temp_queue = list(buy_queue)
            temp_remaining = remaining_sell
            lots_to_close = []
            for lot in temp_queue:
                if temp_remaining <= 0:
                    break
                match_qty = min(lot.quantity, temp_remaining)
                lots_to_close.append((lot, match_qty))
                total_matched += match_qty
                temp_remaining -= match_qty

            if total_matched == 0:
                missing.append({
                    "symbol": symbol_lb,
                    "reason": "opening_position_missing",
                    "date": sell_date,
                    "unmatched_quantity": remaining_sell,
                })
                continue

            # Allocate commission proportionally
            for lot, match_qty in lots_to_close:
                lot_comm_share = sell_commission * (match_qty / qty) if qty > 0 else 0
                entry_cost = match_qty * lot.price
                exit_proceeds = match_qty * sell_price
                gross_pnl = exit_proceeds - entry_cost
                total_comm = lot_comm_share + (lot.commission * (match_qty / lot.quantity) if lot.quantity > 0 else 0)
                realized_pnl = gross_pnl - total_comm

                holding_days = (parse_date(sell_date) - parse_date(lot.date)).days
                initial_risk, r_multiple, risk_status = risk_metrics_for_closed_trade(
                    symbol_lb,
                    lot.price,
                    match_qty,
                    realized_pnl,
                    lot.date,
                )
                # SELL setup: per-trade exit tag
                exit_setup = setup_type_for_trade(symbol_lb, sell_date, trade.trade_time)

                closed.append(ClosedTrade(
                    symbol=symbol_lb,
                    open_date=lot.date,
                    close_date=sell_date,
                    holding_days=max(holding_days, 0),
                    quantity=match_qty,
                    avg_entry=lot.price,
                    avg_exit=sell_price,
                    realized_pnl=realized_pnl,
                    commissions=total_comm,
                    entry_source=lot.entry_source,
                    initial_risk=initial_risk,
                    r_multiple=r_multiple,
                    risk_status=risk_status,
                    setup_type=lot.setup_type or setup_type_for_trade(symbol_lb, lot.date),
                    exit_setup_type=exit_setup,
                ))

                # Update queue
                remaining_sell -= match_qty
                original_lot_qty = lot.quantity
                lot.quantity -= match_qty
                # Commission on the lot is consumed proportionally
                if original_lot_qty > 0:
                    lot.commission = lot.commission * (lot.quantity / original_lot_qty)

            # Remove exhausted lots
            buy_queue = [lot for lot in buy_queue if lot.quantity > 1e-9]

            if remaining_sell > 1e-9:
                missing.append({
                    "symbol": symbol_lb,
                    "reason": "opening_position_missing",
                    "date": sell_date,
                    "unmatched_quantity": remaining_sell,
                })

    return closed, buy_queue, missing


def compute_open_positions(
    open_lots_by_symbol: Dict[str, List[Lot]],
    kline_prices: Optional[Dict[str, float]] = None,
) -> List[OpenPosition]:
    """Compute current open positions from remaining lots."""
    positions: List[OpenPosition] = []
    today = datetime.now().date()

    for symbol, lots in open_lots_by_symbol.items():
        if not lots:
            continue
        total_qty = sum(l.quantity for l in lots)
        total_cost = sum(l.quantity * l.price for l in lots)
        total_comm = sum(l.commission for l in lots)
        avg_cost = total_cost / total_qty if total_qty > 0 else 0

        dates = [l.date for l in lots]
        sources = sorted(set(l.entry_source for l in lots))
        entry_source = sources[0] if len(sources) == 1 else ENTRY_MIXED
        first_buy = min(dates)
        last_buy = max(dates)
        days_held = (today - parse_date(last_buy).date()).days

        unrealized = None
        drawdown = None
        if kline_prices and symbol in kline_prices:
            current_price = kline_prices[symbol]
            unrealized = (current_price - avg_cost) * total_qty - total_comm
            if avg_cost > 0:
                drawdown = (current_price - avg_cost) / avg_cost

        positions.append(OpenPosition(
            symbol=symbol,
            quantity=total_qty,
            avg_cost=avg_cost,
            total_commissions=total_comm,
            first_buy_date=first_buy,
            last_buy_date=last_buy,
            unrealized_pnl=unrealized,
            current_drawdown_pct=drawdown,
            days_held=days_held,
            entry_source=entry_source,
        ))

    return positions


def load_kline_latest_prices(symbol_list: List[str]) -> Dict[str, float]:
    """Load latest close price from kline cache for each symbol."""
    from app.main import load_kline_cache  # lazy import to avoid circular
    prices = {}
    for symbol in symbol_list:
        rows = load_kline_cache(symbol)
        if rows:
            try:
                prices[symbol] = float(rows[-1].get("close", 0))
            except Exception:
                pass
    return prices


def run_matching() -> dict:
    """Main entry: load trades, run FIFO matching, save outputs."""
    trades = load_trades()
    grouped = group_trades_by_symbol(trades)
    audit_year = audit_year_from_trades(trades)
    opening_quantities = load_opening_position_quantities()
    opening_lots_by_symbol, opening_missing = build_opening_lots(audit_year, opening_quantities)
    mtm_symbols = load_mtm_symbols()

    all_closed: List[ClosedTrade] = []
    all_open_lots: Dict[str, List[Lot]] = {}
    missing_coverage: List[dict] = list(opening_missing)

    symbol_universe = set(grouped) | set(opening_lots_by_symbol)
    for symbol in sorted(symbol_universe):
        symbol_trades = grouped.get(symbol, [])
        closed, open_lots, missing = match_fifo(symbol, symbol_trades, opening_lots_by_symbol.get(symbol))
        all_closed.extend(closed)
        missing_coverage.extend(missing)
        if open_lots:
            all_open_lots[symbol] = open_lots

    for symbol in sorted(mtm_symbols - set(grouped) - set(opening_lots_by_symbol)):
        missing_coverage.append({
            "symbol": symbol,
            "reason": "mtm_without_opening_position",
        })

    # Sort closed trades by close_date, then symbol
    all_closed.sort(key=lambda c: (c.close_date, c.symbol))

    # Load latest prices for open positions
    symbols_with_open = list(all_open_lots.keys())
    latest_prices = load_kline_latest_prices(symbols_with_open)
    open_positions = compute_open_positions(all_open_lots, latest_prices)

    # Save
    write_json(CLOSED_TRADES_FILE, [asdict(c) for c in all_closed])
    write_json(OPEN_POSITIONS_FILE, [asdict(p) for p in open_positions])
    write_json(AUDIT_COVERAGE_FILE, {
        "auditYear": audit_year,
        "openingPositionSource": str(OPENING_POSITIONS_FILE) if OPENING_POSITIONS_FILE.exists() else None,
        "openingPositionCount": len(opening_quantities),
        "openingRebasedCount": sum(len(v) for v in opening_lots_by_symbol.values()),
        "missing": missing_coverage,
    })

    return {
        "closed_trade_count": len(all_closed),
        "open_position_count": len(open_positions),
        "total_realized_pnl": round(sum(c.realized_pnl for c in all_closed), 2),
        "total_unrealized_pnl": round(sum(p.unrealized_pnl or 0 for p in open_positions), 2),
        "symbols_with_open": symbols_with_open,
        "opening_rebased_count": sum(len(v) for v in opening_lots_by_symbol.values()),
        "missing_coverage_count": len(missing_coverage),
    }


if __name__ == "__main__":
    result = run_matching()
    print(json.dumps(result, ensure_ascii=False, indent=2))
