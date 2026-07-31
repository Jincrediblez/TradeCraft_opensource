#!/usr/bin/env python3
"""Unit tests for FIFO position matcher."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.position_matcher as matcher
from app.position_matcher import ENTRY_OPENING, Lot, Trade, match_fifo
from app.scoring_engine import compute_period_metrics


def make_trade(symbol, side, qty, price, date, commission=1.0):
    return Trade(
        symbol=symbol,
        symbol_lb=f"{symbol}.US",
        side=side,
        quantity=qty,
        price=price,
        gross_amount=qty * price,
        commission=commission,
        currency="USD",
        exchange="TEST",
        trade_date=date,
        trade_time="10:00:00",
        source="test",
    )


def test_basic_fifo():
    trades = [
        make_trade("AAPL", "BUY", 100, 100.0, "20260101"),
        make_trade("AAPL", "SELL", 100, 110.0, "20260110"),
    ]
    closed, open_lots, missing = match_fifo("AAPL.US", trades)
    assert len(closed) == 1
    assert closed[0].quantity == 100
    assert closed[0].realized_pnl == (110 - 100) * 100 - 2.0  # gross - commissions
    assert closed[0].holding_days == 9
    assert closed[0].entry_source == "actual_trade"
    assert len(open_lots) == 0
    assert missing == []


def test_partial_sell():
    trades = [
        make_trade("AAPL", "BUY", 100, 100.0, "20260101"),
        make_trade("AAPL", "SELL", 40, 110.0, "20260110"),
    ]
    closed, open_lots, missing = match_fifo("AAPL.US", trades)
    assert len(closed) == 1
    assert closed[0].quantity == 40
    assert len(open_lots) == 1
    assert open_lots[0].quantity == 60
    assert missing == []


def test_multiple_lots():
    trades = [
        make_trade("AAPL", "BUY", 50, 100.0, "20260101"),
        make_trade("AAPL", "BUY", 50, 110.0, "20260102"),
        make_trade("AAPL", "SELL", 60, 120.0, "20260110"),
    ]
    closed, open_lots, missing = match_fifo("AAPL.US", trades)
    assert len(closed) == 2
    assert closed[0].quantity == 50  # First lot fully closed
    assert closed[1].quantity == 10  # Second lot partially closed
    assert len(open_lots) == 1
    assert open_lots[0].quantity == 40
    assert missing == []


def test_day_trade():
    trades = [
        make_trade("AAPL", "BUY", 100, 100.0, "20260101"),
        make_trade("AAPL", "SELL", 100, 101.0, "20260101"),
    ]
    closed, open_lots, missing = match_fifo("AAPL.US", trades)
    assert len(closed) == 1
    assert closed[0].holding_days == 0
    assert missing == []


def test_opening_rebased_lot_matches_first_sell():
    trades = [
        make_trade("AAPL", "SELL", 40, 110.0, "20260110"),
    ]
    opening = [Lot("AAPL.US", "20251231", 100, 100.0, 0.0, ENTRY_OPENING)]
    closed, open_lots, missing = match_fifo("AAPL.US", trades, opening)
    assert len(closed) == 1
    assert closed[0].quantity == 40
    assert closed[0].realized_pnl == (110 - 100) * 40 - 1.0
    assert closed[0].entry_source == ENTRY_OPENING
    assert len(open_lots) == 1
    assert open_lots[0].quantity == 60
    assert open_lots[0].entry_source == ENTRY_OPENING
    assert missing == []


def test_missing_opening_position_is_reported_without_reversing():
    trades = [
        make_trade("AAPL", "SELL", 40, 110.0, "20260110"),
    ]
    closed, open_lots, missing = match_fifo("AAPL.US", trades)
    assert closed == []
    assert open_lots == []
    assert len(missing) == 1
    assert missing[0]["reason"] == "opening_position_missing"


def test_r_multiple_uses_trade_plan_invalidation():
    original = matcher.trade_plan_for_symbol
    try:
        matcher.trade_plan_for_symbol = lambda symbol: {"invalidationPrice": 90.0}
        trades = [
            make_trade("AAPL", "BUY", 100, 100.0, "20260101"),
            make_trade("AAPL", "SELL", 100, 110.0, "20260110"),
        ]
        closed, _, _ = match_fifo("AAPL.US", trades)
        assert closed[0].risk_status == "planned"
        assert closed[0].setup_type == ""
        assert closed[0].initial_risk == 1000
        assert round(closed[0].r_multiple, 3) == round(closed[0].realized_pnl / 1000, 3)
    finally:
        matcher.trade_plan_for_symbol = original


def test_r_multiple_flows_into_scoring_metrics():
    closed_trades = [
        {
            "symbol": "AAPL.US",
            "open_date": "20260101",
            "close_date": "20260110",
            "holding_days": 9,
            "quantity": 100,
            "realized_pnl": 998,
            "r_multiple": 1.0,
            "risk_status": "planned",
        },
        {
            "symbol": "MSFT.US",
            "open_date": "20260102",
            "close_date": "20260112",
            "holding_days": 10,
            "quantity": 50,
            "realized_pnl": -200,
            "r_multiple": -0.5,
            "risk_status": "planned",
        },
        {
            "symbol": "NVDA.US",
            "open_date": "20260103",
            "close_date": "20260113",
            "holding_days": 10,
            "quantity": 25,
            "realized_pnl": 100,
            "r_multiple": None,
            "risk_status": "risk_missing",
        },
    ]
    trades = [
        {
            "symbol_lb": row["symbol"],
            "side": "BUY",
            "trade_date": row["open_date"],
            "trade_time": "10:00:00",
            "commission": 1,
        }
        for row in closed_trades
    ]
    metrics = compute_period_metrics(trades, closed_trades)
    assert metrics["r_multiple_count"] == 2
    assert metrics["avg_r_multiple"] == 0.25
    assert metrics["best_r_multiple"] == 1.0
    assert metrics["worst_r_multiple"] == -0.5
    assert metrics["risk_missing_count"] == 1


if __name__ == "__main__":
    test_basic_fifo()
    print("✓ test_basic_fifo")
    test_partial_sell()
    print("✓ test_partial_sell")
    test_multiple_lots()
    print("✓ test_multiple_lots")
    test_day_trade()
    print("✓ test_day_trade")
    test_opening_rebased_lot_matches_first_sell()
    print("✓ test_opening_rebased_lot_matches_first_sell")
    test_missing_opening_position_is_reported_without_reversing()
    print("✓ test_missing_opening_position_is_reported_without_reversing")
    test_r_multiple_uses_trade_plan_invalidation()
    print("✓ test_r_multiple_uses_trade_plan_invalidation")
    test_r_multiple_flows_into_scoring_metrics()
    print("✓ test_r_multiple_flows_into_scoring_metrics")
    print("\nAll tests passed!")
