#!/usr/bin/env python3
"""Phase 4: ibkr_parser — TLG/CSV trade parsing and helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ibkr_parser
from app.ibkr_parser import normalize_symbol, parse_csv_transactions, parse_tlg, safe_float


def test_safe_float_handles_commas_and_junk():
    assert safe_float("1,234.5") == 1234.5
    assert safe_float("") == 0.0
    assert safe_float(None) == 0.0
    assert safe_float("abc") == 0.0
    assert safe_float(42) == 42.0


def test_parser_normalize_symbol_is_canonical():
    # ibkr_parser re-exports the single canonical normalize_symbol
    from app.symbols import normalize_symbol as canonical
    assert normalize_symbol is canonical
    assert normalize_symbol("nvda.us") == "NVDA.US"
    assert normalize_symbol("aapl") == "AAPL.US"
    assert normalize_symbol("700") == "700.HK"
    assert normalize_symbol("") == ""


def test_parse_tlg_reads_stk_trades(tmp_path):
    # 16 pipe-delimited fields: idx2=symbol, 4=exchange, 5=action, 7=date,
    # 8=time, 9=ccy, 10=qty, 12=price, 13=gross, 14=commission
    lines = [
        "HEADER|junk",
        "STK_TRD|x|NVDA|x|NASDAQ|BUY|x|20260501|10:00:00|USD|100|x|150.5|15050|1.5|x",
        "STK_TRD|x|700|x|SEHK|SELL|x|20260502|11:00:00|HKD|200|x|50.0|10000|2.0|x",
        "STK_TRD|x|BAD|x|EX|HOLD|x|20260503|12:00:00|USD|10|x|1|10|0|x",  # not BUY/SELL
        "STK_TRD|too|few|fields",  # < 16 parts -> skipped
    ]
    p = tmp_path / "sample.tlg"
    p.write_text("\n".join(lines), encoding="utf-8")

    trades = parse_tlg(p)
    assert len(trades) == 2

    nvda = trades[0]
    assert nvda.symbol_lb == "NVDA.US" and nvda.side == "BUY"
    assert nvda.quantity == 100.0 and nvda.price == 150.5
    assert nvda.commission == 1.5 and nvda.source == "tlg"

    hk = trades[1]
    assert hk.symbol_lb == "700.HK" and hk.side == "SELL" and hk.currency == "HKD"


def test_parse_tlg_takes_absolute_quantity(tmp_path):
    p = tmp_path / "neg.tlg"
    p.write_text(
        "STK_TRD|x|AMD|x|NASDAQ|SELL|x|20260501|10:00:00|USD|-50|x|100|5000|1|x",
        encoding="utf-8",
    )
    trades = parse_tlg(p)
    assert len(trades) == 1 and trades[0].quantity == 50.0


def test_parse_csv_transactions(tmp_path):
    # Transaction History rows: 0=tag,1=Data,2=date,5=type,6=symbol,7=qty,
    # 8=price,9=ccy,10=gross,11=commission (>=13 cols)
    rows = [
        "Transaction History,Data,2026-05-01,x,x,Buy,NVDA,100,150.5,USD,15050,1.5,x",
        "Transaction History,Data,2026-05-02,x,x,Sell,AMD,50,90.0,USD,4500,1.0,x",
        "Transaction History,Data,2026-05-03,x,x,Dividend,NVDA,0,0,USD,0,0,x",  # non-trade
        "Other,Data,2026-05-04,x,x,Buy,X,1,1,USD,1,0,x",  # wrong tag
    ]
    p = tmp_path / "tx.csv"
    p.write_text("\n".join(rows), encoding="utf-8")

    trades = parse_csv_transactions(p)
    assert len(trades) == 2
    assert trades[0].symbol_lb == "NVDA.US" and trades[0].side == "BUY"
    assert trades[0].trade_date == "20260501" and trades[0].source == "csv"
    assert trades[1].side == "SELL" and trades[1].quantity == 50.0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
