#!/usr/bin/env python3
"""Shared in-process K-line cache.

Several engines (scoring, setup classification, theme attribution
matching) read the same symbol K-lines during one audit run. They each used to
keep their own module-level dict, which meant 4x disk reads, 4x memory, and —
worse — caches that were never invalidated when the underlying data changed.

This module is the single cache. Call :func:`clear` at the start of a
refresh/audit pipeline so a run never serves stale K-lines.
"""

from typing import Dict, List


_CACHE: Dict[str, List[dict]] = {}


def get_kline(symbol_lb: str) -> List[dict]:
    """Return cached K-lines for ``symbol_lb`` (empty list on any failure)."""
    if symbol_lb not in _CACHE:
        try:
            # Lazy import avoids a circular dependency with app.main, which is
            # the canonical on-disk K-line loader.
            from app.main import load_kline_cache as _load
            _CACHE[symbol_lb] = _load(symbol_lb) or []
        except Exception:
            _CACHE[symbol_lb] = []
    return _CACHE[symbol_lb]


def clear() -> None:
    """Drop all cached K-lines (call at pipeline start to avoid staleness)."""
    _CACHE.clear()
