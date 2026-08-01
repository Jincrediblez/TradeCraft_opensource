#!/usr/bin/env python3
"""Browser UI locale helpers.

The HTTP API and all persisted state are English-only.  The server exposes
stable message keys so the browser may render Simplified Chinese locally.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "static" / "locales"
SUPPORTED_LOCALES = ("en", "zh-CN")
DEFAULT_LOCALE = "en"


def normalize_locale(value: Optional[str]) -> str:
    raw = str(value or "").strip().replace("_", "-")
    if raw.lower() == "zh-cn":
        return "zh-CN"
    return DEFAULT_LOCALE


@lru_cache(maxsize=4)
def load_catalog(locale: str) -> Dict[str, Any]:
    normalized = normalize_locale(locale)
    path = LOCALES_DIR / f"{normalized}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        if normalized != DEFAULT_LOCALE:
            return load_catalog(DEFAULT_LOCALE)
        return {}


def _lookup(payload: Dict[str, Any], key: str) -> Any:
    current: Any = payload
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def translate(locale: str, key: str, default: str = "", **params: Any) -> str:
    value = _lookup(load_catalog(locale), key)
    text = str(value if isinstance(value, str) else default or key)
    for name, replacement in params.items():
        text = text.replace("{" + name + "}", str(replacement))
    return text


def message(locale: str, key: str, default: str = "", **params: Any) -> dict:
    return {
        "message": translate(locale, key, default, **params),
        "messageKey": key,
        "messageParams": params,
    }
