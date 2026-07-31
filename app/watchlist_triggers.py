#!/usr/bin/env python3
"""Watchlist trigger storage for the local workstation."""

from datetime import datetime
from pathlib import Path
from typing import Dict, List

from app import state_store
from app.settings import normalize_symbol


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
WATCHLIST_TRIGGERS_FILE = STATE_DIR / "watchlist_triggers.json"


def load_triggers() -> List[dict]:
    payload = state_store.read_json(WATCHLIST_TRIGGERS_FILE, [])
    return payload if isinstance(payload, list) else []


def normalize_trigger(payload: Dict[str, object]) -> dict:
    symbol = normalize_symbol(str(payload.get("symbol") or ""))
    return {
        "symbol": symbol,
        "watchPrice": str(payload.get("watchPrice") or "")[:40],
        "breakoutPrice": str(payload.get("breakoutPrice") or "")[:40],
        "pullbackZone": str(payload.get("pullbackZone") or "")[:80],
        "earningsDate": str(payload.get("earningsDate") or "")[:40],
        "note": str(payload.get("note") or "")[:500],
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }


def save_triggers(rows: List[dict]) -> List[dict]:
    normalized = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = normalize_trigger(row)
        symbol = item.get("symbol")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(item)
    state_store.write_json(WATCHLIST_TRIGGERS_FILE, normalized, root=ROOT, metadata_file=STATE_DIR / "_metadata.json")
    return normalized
