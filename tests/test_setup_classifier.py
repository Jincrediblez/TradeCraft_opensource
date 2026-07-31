#!/usr/bin/env python3
"""Phase 4: setup_classifier.classify_setup rule-based classification."""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.setup_classifier as sc
from app.setup_classifier import SETUP_TYPES, classify_setup


def _rows(closes, vols=None, start=date(2026, 1, 1)):
    out = []
    for i, c in enumerate(closes):
        d = (start + timedelta(days=i)).isoformat()
        out.append({"time": d, "close": c, "high": c, "low": c,
                    "turnover": (vols[i] if vols else 1_000_000)})
    return out


def _closed(rows, avg_entry):
    return {"symbol": "NVDA.US", "open_date": rows[-1]["time"], "avg_entry": avg_entry}


def test_no_kline_data_is_unclassified(monkeypatch):
    monkeypatch.setattr(sc, "load_kline", lambda s: [])
    setup, conf, reasons = classify_setup({"symbol": "X.US", "open_date": "2026-05-01", "avg_entry": 10})
    assert setup == "未分类" and conf == "low"


def test_insufficient_history_is_unclassified(monkeypatch):
    rows = _rows([100.0] * 10)  # < 25 days
    monkeypatch.setattr(sc, "load_kline", lambda s: rows)
    setup, conf, _ = classify_setup(_closed(rows, 100.0))
    assert setup == "未分类" and conf == "low"


def test_parabolic_entry_classified_as_fomo(monkeypatch):
    closes = [round(100 * (1.03 ** i), 2) for i in range(30)]
    vols = [1_000_000] * 30
    vols[-1] = 5_000_000
    rows = _rows(closes, vols)
    monkeypatch.setattr(sc, "load_kline", lambda s: rows)
    setup, conf, reasons = classify_setup(_closed(rows, closes[-1]))
    assert setup == "FOMO追高" and conf == "high"


def test_result_is_always_a_known_setup_type(monkeypatch):
    closes = [round(100 * (1.01 ** i), 2) for i in range(40)]
    rows = _rows(closes)
    monkeypatch.setattr(sc, "load_kline", lambda s: rows)
    setup, conf, reasons = classify_setup(_closed(rows, closes[-1]))
    assert setup in SETUP_TYPES
    assert conf in {"high", "medium", "low"}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
