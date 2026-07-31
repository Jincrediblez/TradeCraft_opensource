#!/usr/bin/env python3
"""Local watchlist storage for TradeCraft."""

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app import state_store
from app.symbols import normalize_symbol


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
DB_PATH = STATE_DIR / "watchlist.db"
DEFAULT_GROUP = "Unsorted"
DEFAULT_SUBGROUP = "Watch"
VALID_COLORS = {"none", "red", "green", "blue", "yellow", "purple", "gray"}

# TradingView exchange prefix -> CODE.MARKET suffix used across this app.
TV_EXCHANGE_MARKETS = {
    "NASDAQ": "US",
    "NYSE": "US",
    "AMEX": "US",
    "NYSEARCA": "US",
    "BATS": "US",
    "OTC": "US",
    "CBOE": "US",
    "US": "US",
    "HKEX": "HK",
    "HKG": "HK",
    "SSE": "SH",
    "SZSE": "SZ",
}


def now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value)))
    except Exception:
        return default


def safe_float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace(",", ""))
    except Exception:
        return None


def as_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def parse_json_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return as_list(value)
    if not value:
        return []
    try:
        return as_list(json.loads(str(value)))
    except Exception:
        return as_list(value)


def validate_color(value: Any) -> str:
    color = str(value or "none").strip().lower()
    if color not in VALID_COLORS:
        raise ValueError(f"invalid color: {color}")
    return color


def parse_symbol_token(token: Any) -> str:
    """Turn one raw symbol token into ``CODE.MARKET``, or ``""`` if unsupported.

    Accepts plain forms (``NVDA``, ``700.HK``) and TradingView's
    ``EXCHANGE:CODE`` form for exchanges this app can chart (US/HK/SH/SZ).
    Ratio spreads (``SP:SPX/AMEX:RSP``), futures (``CME_MINI:ES1!``) and
    data-feed prefixes (``TVC:``, ``FRED:``, ``ECONOMICS:`` ...) are rejected.
    """
    raw = str(token or "").strip()
    if not raw or "/" in raw or "!" in raw:
        return ""
    if ":" in raw:
        exchange, code = raw.split(":", 1)
        market = TV_EXCHANGE_MARKETS.get(exchange.strip().upper())
        code = code.strip().upper()
        if not market or not code:
            return ""
        return f"{code}.{market}"
    return normalize_symbol(raw)


def parse_tradingview_text(text: str, default_group: str = "") -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse a TradingView watchlist export into ordered entries.

    The export is comma/newline separated; ``###Name`` tokens open a section
    which becomes the group for the symbols that follow. Returns
    ``(entries, skipped)`` where each entry is ``{"symbol", "group"}`` and
    ``skipped`` lists tokens this app cannot chart (unsupported exchanges,
    ratios, futures, macro data feeds).
    """
    entries: List[Dict[str, Any]] = []
    skipped: List[str] = []
    section = ""
    seen = set()
    for token in re.split(r"[,\r\n]+", str(text or "")):
        token = token.strip()
        if not token:
            continue
        if token.startswith("###"):
            section = token.lstrip("#").strip()
            continue
        symbol = parse_symbol_token(token)
        if not symbol:
            skipped.append(token)
            continue
        group = section or default_group or DEFAULT_GROUP
        key = (symbol, group)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"symbol": symbol, "group": group})
    return entries, skipped


def init_db() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS groups (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              description TEXT DEFAULT '',
              sort_order INTEGER DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subgroups (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              group_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              description TEXT DEFAULT '',
              sort_order INTEGER DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(group_id, name),
              FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS symbols (
              symbol TEXT PRIMARY KEY,
              name TEXT DEFAULT '',
              market TEXT DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              symbol TEXT NOT NULL,
              name TEXT DEFAULT '',
              group_id INTEGER,
              subgroup_id INTEGER,
              status TEXT DEFAULT 'active',
              priority INTEGER DEFAULT 3,
              color TEXT DEFAULT 'none',
              tags TEXT DEFAULT '[]',
              thesis TEXT DEFAULT '',
              trigger_price REAL,
              stop_price REAL,
              target_price REAL,
              note TEXT DEFAULT '',
              archived INTEGER DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(symbol, group_id, subgroup_id),
              FOREIGN KEY(symbol) REFERENCES symbols(symbol) ON DELETE CASCADE,
              FOREIGN KEY(group_id) REFERENCES groups(id) ON DELETE SET NULL,
              FOREIGN KEY(subgroup_id) REFERENCES subgroups(id) ON DELETE SET NULL
            );
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
        migrations = {
            "name": "ALTER TABLE items ADD COLUMN name TEXT DEFAULT ''",
            "color": "ALTER TABLE items ADD COLUMN color TEXT DEFAULT 'none'",
            "note": "ALTER TABLE items ADD COLUMN note TEXT DEFAULT ''",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)
        ensure_default_group(conn)


def db() -> sqlite3.Connection:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def ensure_default_group(conn: sqlite3.Connection) -> Dict[str, int]:
    group_id = get_or_create_group(conn, DEFAULT_GROUP)
    subgroup_id = get_or_create_subgroup(conn, group_id, DEFAULT_SUBGROUP)
    return {"groupId": group_id, "subgroupId": subgroup_id}


def upsert_symbol(conn: sqlite3.Connection, symbol: str, name: str = "") -> None:
    symbol = normalize_symbol(symbol)
    if not symbol:
        raise ValueError("symbol is required")
    market = symbol.split(".", 1)[1] if "." in symbol else ""
    ts = now_text()
    conn.execute(
        """
        INSERT INTO symbols(symbol, name, market, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
          name=COALESCE(NULLIF(excluded.name, ''), symbols.name),
          market=excluded.market,
          updated_at=excluded.updated_at
        """,
        (symbol, name or "", market, ts, ts),
    )


def get_or_create_group(conn: sqlite3.Connection, name: str) -> int:
    clean = str(name or "").strip() or DEFAULT_GROUP
    ts = now_text()
    conn.execute(
        "INSERT OR IGNORE INTO groups(name, created_at, updated_at) VALUES (?, ?, ?)",
        (clean, ts, ts),
    )
    row = conn.execute("SELECT id FROM groups WHERE name=?", (clean,)).fetchone()
    return int(row["id"])


def get_or_create_subgroup(conn: sqlite3.Connection, group_id: int, name: str) -> int:
    clean = str(name or "").strip() or DEFAULT_SUBGROUP
    ts = now_text()
    conn.execute(
        "INSERT OR IGNORE INTO subgroups(group_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (group_id, clean, ts, ts),
    )
    row = conn.execute(
        "SELECT id FROM subgroups WHERE group_id=? AND name=?",
        (group_id, clean),
    ).fetchone()
    return int(row["id"])


def normalize_item_payload(payload: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    symbol = normalize_symbol(str(payload.get("symbol") or ""))
    if not symbol:
        raise ValueError("symbol is required")
    group_id = payload.get("groupId", payload.get("group_id"))
    subgroup_id = payload.get("subgroupId", payload.get("subgroup_id"))
    if not group_id:
        defaults = ensure_default_group(conn)
        group_id = defaults["groupId"]
        subgroup_id = defaults["subgroupId"]
    elif not subgroup_id:
        subgroup_id = get_or_create_subgroup(conn, safe_int(group_id), DEFAULT_SUBGROUP)
    tags = payload.get("tags") or []
    return {
        "symbol": symbol,
        "name": str(payload.get("name") or "")[:120],
        "group_id": safe_int(group_id),
        "subgroup_id": safe_int(subgroup_id),
        "status": str(payload.get("status") or "active")[:40],
        "priority": safe_int(payload.get("priority"), 3),
        "color": validate_color(payload.get("color")),
        "tags": json.dumps(as_list(tags), ensure_ascii=False),
        "thesis": str(payload.get("thesis") or "")[:1000],
        "trigger_price": safe_float_or_none(payload.get("triggerPrice", payload.get("trigger_price"))),
        "stop_price": safe_float_or_none(payload.get("stopPrice", payload.get("stop_price"))),
        "target_price": safe_float_or_none(payload.get("targetPrice", payload.get("target_price"))),
        "note": str(payload.get("note") or "")[:1000],
        "archived": 1 if payload.get("archived") else 0,
    }


def list_groups() -> List[Dict[str, Any]]:
    with db() as conn:
        groups = [row_to_dict(row) for row in conn.execute("SELECT * FROM groups ORDER BY sort_order, name")]
        for group in groups:
            subgroups = conn.execute(
                "SELECT * FROM subgroups WHERE group_id=? ORDER BY sort_order, name",
                (group["id"],),
            ).fetchall()
            group["subgroups"] = [row_to_dict(row) for row in subgroups]
            group["itemCount"] = conn.execute(
                "SELECT COUNT(*) AS n FROM items WHERE group_id=? AND archived=0",
                (group["id"],),
            ).fetchone()["n"]
    return groups


def create_group(payload: Dict[str, Any]) -> Dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    with db() as conn:
        group_id = get_or_create_group(conn, name)
        if "description" in payload:
            conn.execute(
                "UPDATE groups SET description=?, updated_at=? WHERE id=?",
                (str(payload.get("description") or ""), now_text(), group_id),
            )
        subgroup_name = str(payload.get("subgroup") or payload.get("subgroupName") or DEFAULT_SUBGROUP)
        get_or_create_subgroup(conn, group_id, subgroup_name)
        return row_to_dict(conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone())


def update_group(group_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = []
    values: List[Any] = []
    for incoming, column in (("name", "name"), ("description", "description"), ("sortOrder", "sort_order")):
        if incoming in payload:
            value = str(payload[incoming]).strip() if incoming == "name" else payload[incoming]
            if incoming == "name" and not value:
                raise ValueError("name is required")
            fields.append(f"{column}=?")
            values.append(value)
    if not fields:
        raise ValueError("no fields to update")
    fields.append("updated_at=?")
    values.append(now_text())
    values.append(group_id)
    with db() as conn:
        conn.execute(f"UPDATE groups SET {', '.join(fields)} WHERE id=?", values)
        row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not row:
            raise ValueError("group not found")
        return row_to_dict(row)


def delete_group(group_id: int) -> Dict[str, Any]:
    with db() as conn:
        defaults = ensure_default_group(conn)
        if group_id == defaults["groupId"]:
            raise ValueError("default group cannot be deleted")
        conn.execute(
            "UPDATE items SET group_id=?, subgroup_id=?, updated_at=? WHERE group_id=?",
            (defaults["groupId"], defaults["subgroupId"], now_text(), group_id),
        )
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
    return {"ok": True, "deleted": group_id}


def create_item(payload: Dict[str, Any]) -> Dict[str, Any]:
    ts = now_text()
    with db() as conn:
        item = normalize_item_payload(payload, conn)
        upsert_symbol(conn, item["symbol"], item["name"])
        conn.execute(
            """
            INSERT INTO items(symbol, name, group_id, subgroup_id, status, priority, color, tags, thesis,
                              trigger_price, stop_price, target_price, note, archived, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, group_id, subgroup_id) DO UPDATE SET
              name=COALESCE(NULLIF(excluded.name, ''), items.name),
              status=excluded.status,
              priority=excluded.priority,
              color=excluded.color,
              tags=excluded.tags,
              thesis=excluded.thesis,
              trigger_price=excluded.trigger_price,
              stop_price=excluded.stop_price,
              target_price=excluded.target_price,
              note=excluded.note,
              archived=excluded.archived,
              updated_at=excluded.updated_at
            """,
            (
                item["symbol"], item["name"], item["group_id"], item["subgroup_id"], item["status"],
                item["priority"], item["color"], item["tags"], item["thesis"], item["trigger_price"],
                item["stop_price"], item["target_price"], item["note"], item["archived"], ts, ts,
            ),
        )
        row = conn.execute(
            """
            SELECT * FROM items
            WHERE symbol=? AND group_id=? AND subgroup_id=?
            """,
            (item["symbol"], item["group_id"], item["subgroup_id"]),
        ).fetchone()
        return item_to_api(row_to_dict(row))


def update_item(item_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    if "groupId" in payload and not payload.get("subgroupId"):
        payload = dict(payload)
        with db() as conn:
            payload["subgroupId"] = get_or_create_subgroup(conn, safe_int(payload.get("groupId")), DEFAULT_SUBGROUP)
    allowed = {
        "name": "name",
        "status": "status",
        "priority": "priority",
        "color": "color",
        "tags": "tags",
        "thesis": "thesis",
        "triggerPrice": "trigger_price",
        "stopPrice": "stop_price",
        "targetPrice": "target_price",
        "note": "note",
        "archived": "archived",
        "groupId": "group_id",
        "subgroupId": "subgroup_id",
    }
    fields = []
    values: List[Any] = []
    for incoming, column in allowed.items():
        if incoming not in payload:
            continue
        value = payload[incoming]
        if incoming == "color":
            value = validate_color(value)
        elif incoming == "tags":
            value = json.dumps(as_list(value), ensure_ascii=False)
        elif incoming == "archived":
            value = 1 if value else 0
        elif incoming in {"triggerPrice", "stopPrice", "targetPrice"}:
            value = safe_float_or_none(value)
        elif incoming in {"groupId", "subgroupId", "priority"}:
            value = safe_int(value)
        fields.append(f"{column}=?")
        values.append(value)
    if not fields:
        raise ValueError("no fields to update")
    fields.append("updated_at=?")
    values.append(now_text())
    values.append(item_id)
    with db() as conn:
        conn.execute(f"UPDATE items SET {', '.join(fields)} WHERE id=?", values)
        row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise ValueError("item not found")
        return item_to_api(row_to_dict(row))


def delete_item(item_id: int) -> Dict[str, Any]:
    with db() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    return {"ok": True, "deleted": item_id}


def item_to_api(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id"),
        "symbol": item.get("symbol", ""),
        "name": item.get("name", ""),
        "groupId": item.get("group_id"),
        "subgroupId": item.get("subgroup_id"),
        "groupName": item.get("group_name", ""),
        "subgroupName": item.get("subgroup_name", ""),
        "status": item.get("status", "active"),
        "priority": item.get("priority", 3),
        "color": item.get("color", "none"),
        "tags": parse_json_list(item.get("tags")),
        "thesis": item.get("thesis", ""),
        "triggerPrice": item.get("trigger_price"),
        "stopPrice": item.get("stop_price"),
        "targetPrice": item.get("target_price"),
        "note": item.get("note", ""),
        "archived": bool(item.get("archived")),
        "createdAt": item.get("created_at", ""),
        "updatedAt": item.get("updated_at", ""),
    }


def list_items(include_archived: bool = False) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT i.*, g.name AS group_name, sg.name AS subgroup_name
            FROM items i
            LEFT JOIN groups g ON g.id=i.group_id
            LEFT JOIN subgroups sg ON sg.id=i.subgroup_id
            WHERE (? OR i.archived=0)
            ORDER BY i.archived, COALESCE(g.sort_order, 0), g.name, COALESCE(sg.sort_order, 0), sg.name, i.priority, i.symbol
            """,
            (1 if include_archived else 0,),
        ).fetchall()
    return [item_to_api(row_to_dict(row)) for row in rows]


def all_symbols() -> List[str]:
    return sorted({item["symbol"] for item in list_items(False) if item.get("symbol")})


def get_symbol_items(symbol: str) -> List[Dict[str, Any]]:
    target = normalize_symbol(symbol)
    return [item for item in list_items(True) if item["symbol"] == target]


def batch_items(payload: Dict[str, Any]) -> Dict[str, Any]:
    action = str(payload.get("action") or "add").strip().lower()
    items = payload.get("items") or []
    ids = [safe_int(item) for item in payload.get("ids") or [] if safe_int(item)]
    result: Dict[str, Any] = {"ok": True, "action": action, "saved": 0, "deleted": 0, "updated": 0, "errors": []}
    if action == "add":
        rows = []
        if isinstance(items, str):
            rows = [{"symbol": part.strip()} for part in items.replace(",", "\n").splitlines() if part.strip()]
        elif isinstance(items, list):
            rows = [item if isinstance(item, dict) else {"symbol": item} for item in items]
        defaults = {k: payload[k] for k in ("groupId", "subgroupId", "color", "status", "priority") if k in payload}
        for row in rows:
            try:
                symbol = parse_symbol_token(row.get("symbol", ""))
                if not symbol:
                    raise ValueError("unsupported symbol")
                create_item({**defaults, **row, "symbol": symbol})
                result["saved"] += 1
            except Exception as exc:
                result["errors"].append({"symbol": row.get("symbol", ""), "error": str(exc)})
    elif action == "delete":
        for item_id in ids:
            delete_item(item_id)
            result["deleted"] += 1
    elif action in {"move", "color"}:
        patch: Dict[str, Any] = {}
        if action == "move":
            patch["groupId"] = payload.get("groupId")
            if payload.get("subgroupId"):
                patch["subgroupId"] = payload.get("subgroupId")
        if action == "color":
            patch["color"] = payload.get("color", "none")
        for item_id in ids:
            update_item(item_id, patch)
            result["updated"] += 1
    else:
        raise ValueError("unsupported batch action")
    result["items"] = list_items()
    result["groups"] = list_groups()
    return result


def import_tradingview(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Import a TradingView watchlist export (text of the .txt file)."""
    text = str(payload.get("text") or "")
    if not text.strip():
        raise ValueError("text is required")
    # TradingView names exports "{list}_{hash}.txt"; use the list name as the
    # group for symbols that appear before any ###section marker.
    list_name = str(payload.get("name") or "").strip()
    list_name = re.sub(r"\.txt$", "", list_name, flags=re.IGNORECASE)
    list_name = re.sub(r"_[0-9a-z]{4,}$", "", list_name, flags=re.IGNORECASE).strip()
    entries, skipped = parse_tradingview_text(text, default_group=list_name)
    imported = 0
    errors: List[Dict[str, Any]] = []
    group_ids: Dict[str, int] = {}
    with db() as conn:
        for entry in entries:
            if entry["group"] not in group_ids:
                group_id = get_or_create_group(conn, entry["group"])
                get_or_create_subgroup(conn, group_id, DEFAULT_SUBGROUP)
                group_ids[entry["group"]] = group_id
    for entry in entries:
        try:
            create_item({"symbol": entry["symbol"], "groupId": group_ids[entry["group"]]})
            imported += 1
        except Exception as exc:
            errors.append({"symbol": entry["symbol"], "error": str(exc)})
    state_store.app_log(
        "watchlist tradingview import completed",
        name=list_name, imported=imported, skipped=len(skipped), errors=len(errors),
    )
    return {
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "groups": list_groups(),
        "items": list_items(),
    }


def app_state() -> Dict[str, Any]:
    items = list_items()
    groups = list_groups()
    colors: Dict[str, int] = {}
    positioned = 0
    for item in items:
        colors[item["color"]] = colors.get(item["color"], 0) + 1
        if item.get("hasPosition"):
            positioned += 1
    return {
        "ok": True,
        "groups": groups,
        "items": items,
        "summary": {
            "itemCount": len(items),
            "groupCount": len(groups),
            "colors": colors,
            "positioned": positioned,
        },
        "defaultSymbol": items[0]["symbol"] if items else "",
        "validColors": sorted(VALID_COLORS),
    }
