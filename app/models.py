#!/usr/bin/env python3
"""Shared data models for IBKR Trades services."""

from dataclasses import dataclass
from typing import List


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
class SymbolSnapshot:
    symbol: str
    display_name: str
    amount: float
    trade_count: int
    mtm_total: float
    has_trades: bool
    has_mtm: bool


@dataclass
class OtherPnlSnapshot:
    asset_category: str
    symbol: str
    display_name: str
    mtm_total: float


@dataclass
class KlineResult:
    rows: List[dict]
    freshness: dict
