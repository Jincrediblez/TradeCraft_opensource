#!/usr/bin/env python3
"""Shared locale resolution and message catalog access."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "static" / "locales"
SUPPORTED_LOCALES = ("en", "zh-CN")
DEFAULT_LOCALE = "en"


def normalize_locale(value: Optional[str], *, allow_auto: bool = False) -> str:
    raw = str(value or "").strip().replace("_", "-")
    if allow_auto and raw.lower() == "auto":
        return "auto"
    lowered = raw.lower()
    if lowered.startswith("zh"):
        return "zh-CN"
    if lowered.startswith("en"):
        return "en"
    return DEFAULT_LOCALE


def resolve_locale(
    explicit: Optional[str] = None,
    accept_language: Optional[str] = None,
    saved: Optional[str] = None,
) -> str:
    if explicit:
        return normalize_locale(explicit)
    saved_locale = normalize_locale(saved, allow_auto=True)
    if saved_locale != "auto":
        return saved_locale
    first = str(accept_language or "").split(",", 1)[0].strip()
    return normalize_locale(first)


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


def translate_text(locale: str, value: str) -> str:
    """Translate display text at the API boundary without mutating stored state."""
    if normalize_locale(locale) == "zh-CN" or not value:
        return value
    catalog = load_catalog(locale)
    exact = catalog.get("legacy", {})
    if value in exact:
        return str(exact[value])
    translated = value
    fragments = catalog.get("fragments", {})
    for source in sorted(fragments, key=len, reverse=True):
        translated = translated.replace(source, str(fragments[source]))
    return translated


def localize_payload(payload: Any, locale: str) -> Any:
    """Create a localized response copy while leaving persisted JSON unchanged."""
    if isinstance(payload, dict):
        return {key: localize_payload(value, locale) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [localize_payload(value, locale) for value in payload]
    if isinstance(payload, str):
        return translate_text(locale, payload)
    return payload
