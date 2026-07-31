#!/usr/bin/env python3
"""Canonical symbol normalization shared across the app.

A "symbol_lb" is the Longbridge-style ``CODE.MARKET`` form (e.g. ``NVDA.US``,
``700.HK``). This is the single source of truth for turning user/raw input
into that form; callers (main, position_matcher, settings, trade_plans,
ibkr_parser) used to keep their own copies of this exact logic.
"""


def normalize_symbol(symbol: str) -> str:
    """Normalize a raw symbol to ``CODE.MARKET`` form.

    - Empty/None -> ``""``
    - Already has a market suffix -> uppercased ``CODE.MARKET``
    - All-digits (e.g. ``700``) -> ``.HK``
    - Otherwise -> ``.US``
    """
    raw = (symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        code, market = raw.split(".", 1)
        return f"{code.strip().upper()}.{market.strip().upper()}"
    if raw.isdigit():
        return f"{raw}.HK"
    return f"{raw}.US"
