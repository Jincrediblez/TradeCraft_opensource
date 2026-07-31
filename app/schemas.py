#!/usr/bin/env python3
"""Pydantic validation at data-load boundaries.

Raw rows read from disk are validated/coerced here so a malformed record is
skipped (and logged) rather than crashing an entire pipeline. The canonical
runtime objects remain the dataclasses in :mod:`app.models`; these models only
guard the ingest edge.
"""

import math
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, field_validator

try:  # normal package import (server, cli, pytest)
    from app.models import Trade
    from app.state_store import app_log
except ImportError:  # standalone script execution puts app/ on sys.path
    from models import Trade
    from state_store import app_log


class TradeRow(BaseModel):
    """Validates one raw trade dict from trades.json / trades_all.json."""

    model_config = ConfigDict(extra="ignore")

    symbol: str
    symbol_lb: str
    side: str
    quantity: float
    price: float
    gross_amount: float = 0.0
    commission: float = 0.0
    currency: str = ""
    exchange: str = ""
    trade_date: str = ""
    trade_time: str = ""
    source: str = ""

    @field_validator("quantity", "price", "gross_amount", "commission")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("must be a finite number")
        return v

    def to_trade(self) -> Trade:
        return Trade(
            symbol=self.symbol,
            symbol_lb=self.symbol_lb,
            side=self.side,
            quantity=self.quantity,
            price=self.price,
            gross_amount=self.gross_amount,
            commission=self.commission,
            currency=self.currency,
            exchange=self.exchange,
            trade_date=self.trade_date,
            trade_time=self.trade_time,
            source=self.source,
        )


def trade_from_row(row: dict) -> Optional[Trade]:
    """Validate one raw row into a Trade, or return None (+log) if malformed."""
    try:
        return TradeRow.model_validate(row).to_trade()
    except Exception as exc:
        app_log("invalid trade row skipped", level="warning", error=str(exc))
        return None


def trades_from_rows(rows: List[dict]) -> List[Trade]:
    """Validate a list of raw rows, dropping (and logging) any malformed ones."""
    out: List[Trade] = []
    for row in rows:
        if not isinstance(row, dict):
            app_log("invalid trade row skipped", level="warning", error="row is not a dict")
            continue
        trade = trade_from_row(row)
        if trade is not None:
            out.append(trade)
    return out


def coerce_invalidation_price(value) -> Optional[float]:
    """Trade-plan invalidation price: non-negative finite float, else None."""
    if value in ("", None):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0:
        return None
    return f


def coerce_holding_days(value) -> Optional[int]:
    """Trade-plan target holding days: non-negative int, else None."""
    if value in ("", None):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0:
        return None
    return int(f)
