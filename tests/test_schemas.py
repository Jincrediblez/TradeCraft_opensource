#!/usr/bin/env python3
"""Phase 3: pydantic validation at data-load boundaries."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas import (
    coerce_holding_days,
    coerce_invalidation_price,
    trade_from_row,
    trades_from_rows,
)
from app.models import Trade


def _good_row(**over):
    row = {
        "symbol": "NVDA", "symbol_lb": "NVDA.US", "side": "BUY",
        "quantity": 10, "price": 100.0, "gross_amount": 1000.0,
        "commission": 1.0, "currency": "USD", "exchange": "NASDAQ",
        "trade_date": "2026-05-01", "trade_time": "10:00:00", "source": "tlg",
    }
    row.update(over)
    return row


def test_valid_row_becomes_trade():
    t = trade_from_row(_good_row())
    assert isinstance(t, Trade)
    assert t.symbol_lb == "NVDA.US" and t.quantity == 10.0


def test_extra_fields_ignored():
    t = trade_from_row(_good_row(junk="ignored", another=123))
    assert isinstance(t, Trade)
    assert t.price == 100.0


def test_missing_required_field_returns_none():
    assert trade_from_row({"symbol": "X"}) is None


def test_non_finite_numbers_rejected():
    assert trade_from_row(_good_row(price=float("nan"))) is None
    assert trade_from_row(_good_row(quantity=float("inf"))) is None


def test_string_numbers_coerced():
    t = trade_from_row(_good_row(quantity="10", price="100.5"))
    assert t is not None and t.quantity == 10.0 and t.price == 100.5


def test_trades_from_rows_drops_bad_and_keeps_good():
    rows = [_good_row(), {"symbol": "X"}, "not a dict", _good_row(symbol="AMD", symbol_lb="AMD.US")]
    out = trades_from_rows(rows)
    assert len(out) == 2
    assert {t.symbol_lb for t in out} == {"NVDA.US", "AMD.US"}


def test_coerce_invalidation_price():
    assert coerce_invalidation_price("") is None
    assert coerce_invalidation_price(None) is None
    assert coerce_invalidation_price(-5) is None      # negative rejected
    assert coerce_invalidation_price(float("inf")) is None
    assert coerce_invalidation_price(float("nan")) is None
    assert coerce_invalidation_price("12.5") == 12.5
    assert coerce_invalidation_price(0) == 0.0


def test_coerce_holding_days():
    assert coerce_holding_days("") is None
    assert coerce_holding_days(-3) is None
    assert coerce_holding_days(float("inf")) is None  # must not raise OverflowError
    assert coerce_holding_days("10") == 10
    assert coerce_holding_days(7.9) == 7


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
