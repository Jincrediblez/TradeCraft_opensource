#!/usr/bin/env python3
"""Trade-level setup manager.

Manages per-trade setup annotations in trade_setups.json.
Closed trades are keyed by symbol + open_date (multiple lots on same day share
the same setup, which is correct because they come from the same entry decision).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.setup_classifier import auto_classify_all
from app.setup_types import CANONICAL_SETUP_TYPES


try:  # normal package import (server, cli, pytest)
    from app.state_store import read_json as _store_read_json, atomic_write_text as _atomic_write
except ImportError:  # standalone script execution puts app/ on sys.path
    from state_store import read_json as _store_read_json, atomic_write_text as _atomic_write

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
TRADE_SETUPS_FILE = STATE_DIR / "trade_setups.json"
CLOSED_TRADES_FILE = STATE_DIR / "closed_trades.json"

SETUP_TYPES = list(CANONICAL_SETUP_TYPES)


def _read_json(path: Path, default):
    return _store_read_json(path, default)


def _write_json(path: Path, payload) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _make_key(symbol: str, trade_date: str, trade_time: str = "") -> str:
    if trade_time:
        return f"{symbol}_{trade_date}_{trade_time}"
    return f"{symbol}_{trade_date}"


def _old_key(symbol: str, open_date: str) -> str:
    return f"{symbol}_{open_date}"


def load_trade_setups() -> Dict[str, dict]:
    payload = _read_json(TRADE_SETUPS_FILE, {})
    if not isinstance(payload, dict):
        return {}
    for setup in payload.values():
        setup_type = str(setup.get("setupType") or "") if isinstance(setup, dict) else ""
        if setup_type and setup_type not in SETUP_TYPES:
            raise ValueError(
                "Legacy or unsupported trade setup value detected. "
                "Rebuild this workspace with the English-first schema."
            )
    return payload


def get_setup_for_trade(symbol: str, trade_date: str, trade_time: str = "") -> Optional[dict]:
    setups = load_trade_setups()
    key = _make_key(symbol, trade_date, trade_time)
    if key in setups:
        return setups[key]
    # Fallback to old key format (symbol_openDate)
    return setups.get(_old_key(symbol, trade_date))


def save_setup_for_trade(symbol: str, trade_date: str, setup_type: str, note: str = "", trade_time: str = "", side: str = "") -> dict:
    """Save or update a single trade setup. Always sets source='manual'."""
    if setup_type and setup_type not in SETUP_TYPES:
        raise ValueError(f"Invalid setup_type: {setup_type}. Must be one of {SETUP_TYPES}")

    setups = load_trade_setups()
    key = _make_key(symbol, trade_date, trade_time) if trade_time else _old_key(symbol, trade_date)

    setups[key] = {
        "symbol": symbol,
        "tradeDate": trade_date,
        "tradeTime": trade_time,
        "side": side,
        "setupType": setup_type or "Unclassified",
        "confidence": "manual",
        "source": "manual",
        "reasons": [],
        "note": note,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(TRADE_SETUPS_FILE, setups)
    return setups[key]


def save_setup_batch(items: List[dict]) -> dict:
    """Save multiple trade setups in one batch. Returns {saved, errors}."""
    setups = load_trade_setups()
    saved = 0
    errors = []
    for item in items:
        symbol = item.get("symbol", "")
        trade_date = item.get("openDate", "") or item.get("tradeDate", "")
        trade_time = item.get("tradeTime", "")
        setup_type = item.get("setupType", "")
        note = item.get("note", "")
        side = item.get("side", "")
        if not symbol or not trade_date:
            errors.append({"symbol": symbol, "error": "missing symbol or tradeDate"})
            continue
        if setup_type and setup_type not in SETUP_TYPES:
            errors.append({"symbol": symbol, "error": f"invalid setup_type: {setup_type}"})
            continue
        key = _make_key(symbol, trade_date, trade_time) if trade_time else _old_key(symbol, trade_date)
        setups[key] = {
            "symbol": symbol,
            "tradeDate": trade_date,
            "tradeTime": trade_time,
            "side": side,
            "setupType": setup_type or "Unclassified",
            "confidence": "manual",
            "source": "manual",
            "reasons": [],
            "note": note,
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
        }
        saved += 1
    if saved > 0:
        _write_json(TRADE_SETUPS_FILE, setups)
    return {"saved": saved, "errors": errors}


def apply_setups_to_closed_trades() -> dict:
    """Apply trade_setups.json setup_type into closed_trades.json.

    Returns summary of applied / missing counts.
    """
    setups = load_trade_setups()
    if not CLOSED_TRADES_FILE.exists():
        return {"error": "No closed trades found"}

    closed_trades = _read_json(CLOSED_TRADES_FILE, [])
    applied = 0
    missing = 0

    for trade in closed_trades:
        symbol = trade.get("symbol", "")
        open_date = trade.get("open_date", "")
        key = _make_key(symbol, open_date)

        if key in setups:
            trade["setup_type"] = setups[key].get("setupType", "")
            applied += 1
        else:
            missing += 1

    _write_json(CLOSED_TRADES_FILE, closed_trades)

    return {
        "applied": applied,
        "missing": missing,
        "total": len(closed_trades),
    }


def run_auto_classify_and_apply(force: bool = False) -> dict:
    """One-shot: auto classify all closed trades, then apply to closed_trades.json."""
    classify_result = auto_classify_all(force=force)
    if "error" in classify_result:
        return classify_result

    apply_result = apply_setups_to_closed_trades()

    return {
        "classify": classify_result,
        "apply": apply_result,
    }


if __name__ == "__main__":
    import sys
    ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(ROOT))
    result = run_auto_classify_and_apply(force=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
