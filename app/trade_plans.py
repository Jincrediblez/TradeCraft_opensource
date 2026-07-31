#!/usr/bin/env python3
"""Swing-trade plan storage and normalization."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Dict

from app import state_store
from app.symbols import normalize_symbol  # re-exported for backward compatibility
from app.schemas import coerce_holding_days, coerce_invalidation_price


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
TRADE_PLANS_FILE = STATE_DIR / "trade_plans.json"
TRADE_PLAN_HISTORY_FILE = STATE_DIR / "trade_plan_history.json"

SETUP_TYPES = [
    "趋势突破（计划内）",
    "趋势突破（冲动追入）",
    "回调买入",
    "超跌反弹",
    "基本面驱动",
    "主题轮动",
    "FOMO追高",
    "止损",
    "止盈",
    "风控减仓",
    "趋势走弱减仓",
    "调仓换股",
    "被迫平仓",
]




def load_trade_plans() -> Dict[str, dict]:
    payload = state_store.read_json(TRADE_PLANS_FILE, {})
    return payload if isinstance(payload, dict) else {}


def load_trade_plan_history() -> dict:
    payload = state_store.read_json(TRADE_PLAN_HISTORY_FILE, {})
    if not isinstance(payload, dict):
        payload = {}
    versions = payload.get("versions")
    return {
        "schemaVersion": 2,
        "updatedAt": str(payload.get("updatedAt") or ""),
        "versions": versions if isinstance(versions, dict) else {},
    }


def empty_plan(symbol: str) -> dict:
    return {
        "symbol": normalize_symbol(symbol),
        "setupType": "",
        "thesis": "",
        "invalidationPrice": None,
        "targetHoldingDays": None,
        "addCondition": "",
        "reduceCondition": "",
        "stopCondition": "",
        "updatedAt": "",
    }


def normalize_plan(symbol: str, payload: dict) -> dict:
    plan = empty_plan(symbol)
    if isinstance(payload, dict):
        plan.update(
            {
                "setupType": str(payload.get("setupType") or "")[:40],
                "thesis": str(payload.get("thesis") or "")[:2000],
                "addCondition": str(payload.get("addCondition") or "")[:1000],
                "reduceCondition": str(payload.get("reduceCondition") or "")[:1000],
                "stopCondition": str(payload.get("stopCondition") or "")[:1000],
            }
        )
        # Reject inf/NaN/negative values (the old float()/int() casts let
        # those through); invalid input falls back to None.
        plan["invalidationPrice"] = coerce_invalidation_price(payload.get("invalidationPrice"))
        plan["targetHoldingDays"] = coerce_holding_days(payload.get("targetHoldingDays"))
    plan["updatedAt"] = datetime.now().isoformat(timespec="seconds")
    return plan


def get_trade_plan(symbol: str) -> dict:
    symbol_lb = normalize_symbol(symbol)
    return load_trade_plans().get(symbol_lb) or empty_plan(symbol_lb)


def get_trade_plan_at(symbol: str, trade_date: str) -> dict:
    """Return the latest plan version that existed on or before a trade day."""
    symbol_lb = normalize_symbol(symbol)
    target = str(trade_date or "").replace("-", "")[:8]
    versions = load_trade_plan_history().get("versions", {}).get(symbol_lb, [])
    eligible = []
    for version in versions if isinstance(versions, list) else []:
        updated_day = str(version.get("updatedAt") or "")[:10].replace("-", "")
        if target and updated_day and updated_day <= target:
            eligible.append(version)
    if eligible:
        eligible.sort(key=lambda item: item.get("updatedAt", ""))
        return dict(eligible[-1])
    current = get_trade_plan(symbol_lb)
    current_day = str(current.get("updatedAt") or "")[:10].replace("-", "")
    return current if current_day and target and current_day <= target else empty_plan(symbol_lb)


def save_trade_plan(symbol: str, payload: dict) -> dict:
    symbol_lb = normalize_symbol(symbol)
    plans = load_trade_plans()
    plan = normalize_plan(symbol_lb, payload)
    plans[symbol_lb] = plan
    state_store.write_json(TRADE_PLANS_FILE, plans, root=ROOT, metadata_file=STATE_DIR / "_metadata.json")
    history = load_trade_plan_history()
    versions = history["versions"].setdefault(symbol_lb, [])
    comparable = {
        key: plan.get(key)
        for key in (
            "setupType",
            "thesis",
            "invalidationPrice",
            "targetHoldingDays",
            "addCondition",
            "reduceCondition",
            "stopCondition",
        )
    }
    previous = versions[-1] if versions else {}
    previous_comparable = {key: previous.get(key) for key in comparable}
    if comparable != previous_comparable:
        encoded = json.dumps(
            {"symbol": symbol_lb, "updatedAt": plan["updatedAt"], **comparable},
            ensure_ascii=False,
            sort_keys=True,
        )
        versions.append({
            **plan,
            "versionId": f"plan_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]}",
        })
        history["updatedAt"] = plan["updatedAt"]
        state_store.write_json(
            TRADE_PLAN_HISTORY_FILE,
            history,
            root=ROOT,
            metadata_file=STATE_DIR / "_metadata.json",
        )
    return plan
