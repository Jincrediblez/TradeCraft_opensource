#!/usr/bin/env python3
"""State-store helpers: JSON IO, atomic writes, metadata, and app logging."""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = ROOT / "data"
STATE_DIR = DATA_ROOT / "state"
LOG_DIR = ROOT / "logs"
APP_LOG_FILE = LOG_DIR / "app.log"
STATE_METADATA_FILE = STATE_DIR / "_metadata.json"
LOGGER_NAME = "tradecraft.opensource"


def configure_logging(log_file: Path = APP_LOG_FILE) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False


def app_log(message: str, level: str = "info", **fields) -> None:
    try:
        configure_logging()
        payload = {"message": message, **fields}
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        logger = logging.getLogger(LOGGER_NAME)
        getattr(logger, level if level in {"info", "warning", "error"} else "info")(text)
    except Exception:
        return


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        # A corrupted or unreadable state file must not crash callers — log it
        # loudly and fall back to the caller's default so the rest of the app
        # keeps working (and the bad file shows up in the structured log).
        app_log("read_json failed", level="error", path=str(path), error=str(exc))
        return default


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_bytes(content)
    os.replace(tmp, path)


def payload_record_count(payload) -> Optional[int]:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("stocks", "otherPnl", "files", "prefetch"):
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                return len(value)
    return None


def state_metadata_entry(path: Path, payload, root: Path = ROOT) -> Optional[dict]:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    if not (rel.parts and rel.parts[0] in {"data", "cache"}):
        return None
    entry = {
        "schemaVersion": 1,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "path": str(rel),
        "payloadType": type(payload).__name__,
    }
    count = payload_record_count(payload)
    if count is not None:
        entry["recordCount"] = count
    if isinstance(payload, dict):
        for key in ("period", "reportEndDate", "marketDataEndDate", "source", "lastRefreshAt", "builtAt"):
            if payload.get(key):
                entry[key] = payload.get(key)
        files = payload.get("files")
        if isinstance(files, list):
            entry["sourceFiles"] = [
                {"path": item.get("path", ""), "sha256": item.get("sha256", ""), "period": item.get("period", "")}
                for item in files
                if isinstance(item, dict)
            ]
    return entry


def update_state_metadata(path: Path, payload, root: Path = ROOT, metadata_file: Path = STATE_METADATA_FILE) -> None:
    if path == metadata_file:
        return
    entry = state_metadata_entry(path, payload, root=root)
    if not entry:
        return
    try:
        metadata = read_json(metadata_file, {})
        files = metadata.get("files", {}) if isinstance(metadata, dict) else {}
        files[entry["path"]] = entry
        atomic_write_text(
            metadata_file,
            json.dumps(
                {
                    "schemaVersion": 1,
                    "generatedAt": datetime.now().isoformat(timespec="seconds"),
                    "files": files,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
    except Exception as exc:
        app_log("state metadata update failed", level="warning", path=str(path), error=str(exc))


def write_json(path: Path, payload, root: Path = ROOT, metadata_file: Path = STATE_METADATA_FILE) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    update_state_metadata(path, payload, root=root, metadata_file=metadata_file)
