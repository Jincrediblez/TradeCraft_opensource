#!/usr/bin/env python3
"""User-configurable workspace settings."""

from pathlib import Path
from typing import Dict

from app import state_store
from app.symbols import normalize_symbol  # re-exported for backward compatibility


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
SETTINGS_FILE = STATE_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "locale": "auto",
    "defaultPeriod": "",
    "defaultSymbol": "INTC.US",
    "yahooRefreshSeconds": 300,
    "longbridgeRefreshSeconds": 300,
    "kimiModel": "moonshot-v1-32k",
    "themeBenchmark": "SMH.US",
    "auditPrimaryBenchmark": "QQQ.US",
    "auditSecondaryBenchmark": "SPY.US",
}


def load_settings() -> Dict[str, object]:
    payload = state_store.read_json(SETTINGS_FILE, {})
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(payload, dict):
        settings.update(payload)
    settings["defaultSymbol"] = normalize_symbol(str(settings.get("defaultSymbol") or DEFAULT_SETTINGS["defaultSymbol"]))
    try:
        refresh_seconds = int(
            settings.get("yahooRefreshSeconds")
            or settings.get("longbridgeRefreshSeconds")
            or DEFAULT_SETTINGS["yahooRefreshSeconds"]
        )
    except Exception:
        refresh_seconds = DEFAULT_SETTINGS["yahooRefreshSeconds"]
    settings["yahooRefreshSeconds"] = max(60, min(refresh_seconds, 3600))
    settings["longbridgeRefreshSeconds"] = settings["yahooRefreshSeconds"]
    settings["kimiModel"] = str(settings.get("kimiModel") or DEFAULT_SETTINGS["kimiModel"])[:80]
    settings["themeBenchmark"] = normalize_symbol(str(settings.get("themeBenchmark") or DEFAULT_SETTINGS["themeBenchmark"]))
    settings["auditPrimaryBenchmark"] = normalize_symbol(
        str(settings.get("auditPrimaryBenchmark") or DEFAULT_SETTINGS["auditPrimaryBenchmark"])
    )
    settings["auditSecondaryBenchmark"] = normalize_symbol(
        str(settings.get("auditSecondaryBenchmark") or DEFAULT_SETTINGS["auditSecondaryBenchmark"])
    )
    settings["defaultPeriod"] = str(settings.get("defaultPeriod") or "")
    locale = str(settings.get("locale") or "auto").strip()
    settings["locale"] = locale if locale in {"auto", "en", "zh-CN"} else "auto"
    return settings


def save_settings(payload: dict) -> Dict[str, object]:
    settings = load_settings()
    if isinstance(payload, dict):
        for key in DEFAULT_SETTINGS:
            if key in payload:
                settings[key] = payload[key]
    settings["defaultSymbol"] = normalize_symbol(str(settings.get("defaultSymbol") or DEFAULT_SETTINGS["defaultSymbol"]))
    settings["themeBenchmark"] = normalize_symbol(str(settings.get("themeBenchmark") or DEFAULT_SETTINGS["themeBenchmark"]))
    settings["auditPrimaryBenchmark"] = normalize_symbol(
        str(settings.get("auditPrimaryBenchmark") or DEFAULT_SETTINGS["auditPrimaryBenchmark"])
    )
    settings["auditSecondaryBenchmark"] = normalize_symbol(
        str(settings.get("auditSecondaryBenchmark") or DEFAULT_SETTINGS["auditSecondaryBenchmark"])
    )
    settings["kimiModel"] = str(settings.get("kimiModel") or DEFAULT_SETTINGS["kimiModel"])[:80]
    try:
        refresh_seconds = int(
            settings.get("yahooRefreshSeconds")
            or settings.get("longbridgeRefreshSeconds")
            or DEFAULT_SETTINGS["yahooRefreshSeconds"]
        )
    except Exception:
        refresh_seconds = DEFAULT_SETTINGS["yahooRefreshSeconds"]
    settings["yahooRefreshSeconds"] = max(60, min(refresh_seconds, 3600))
    settings["longbridgeRefreshSeconds"] = settings["yahooRefreshSeconds"]
    settings["defaultPeriod"] = str(settings.get("defaultPeriod") or "")
    locale = str(settings.get("locale") or "auto").strip()
    settings["locale"] = locale if locale in {"auto", "en", "zh-CN"} else "auto"
    state_store.write_json(SETTINGS_FILE, settings, root=ROOT, metadata_file=STATE_DIR / "_metadata.json")
    return settings
