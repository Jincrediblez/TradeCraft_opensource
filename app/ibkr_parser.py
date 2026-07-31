#!/usr/bin/env python3
"""IBKR statement and trade-log parsers."""

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.models import OtherPnlSnapshot, Trade
from app.symbols import normalize_symbol  # re-exported for backward compatibility


def safe_float(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except Exception:
        return 0.0


def parse_tlg(path: Path) -> List[Trade]:
    trades: List[Trade] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith("STK_TRD|"):
            continue
        parts = line.split("|")
        if len(parts) < 16:
            continue
        symbol = parts[2].strip().upper()
        action = parts[5].upper()
        side = "BUY" if "BUY" in action else "SELL" if "SELL" in action else ""
        if side not in {"BUY", "SELL"}:
            continue
        trades.append(
            Trade(
                symbol=symbol,
                symbol_lb=normalize_symbol(symbol),
                side=side,
                quantity=abs(safe_float(parts[10])),
                price=safe_float(parts[12]),
                gross_amount=abs(safe_float(parts[13])),
                commission=abs(safe_float(parts[14])),
                currency=parts[9].strip(),
                exchange=parts[4].strip(),
                trade_date=parts[7].strip(),
                trade_time=parts[8].strip(),
                source="tlg",
            )
        )
    return trades


def parse_csv_transactions(path: Path) -> List[Trade]:
    trades: List[Trade] = []
    with path.open(newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 13:
                continue
            tag = row[0].lstrip("\ufeff")
            if tag != "Transaction History" or row[1] != "Data":
                continue
            tx_type = row[5].strip()
            if tx_type not in {"Buy", "Sell"}:
                continue
            symbol = row[6].strip().upper()
            if not symbol or symbol == "-":
                continue
            dt = datetime.strptime(row[2].strip(), "%Y-%m-%d")
            trades.append(
                Trade(
                    symbol=symbol,
                    symbol_lb=normalize_symbol(symbol),
                    side="BUY" if tx_type == "Buy" else "SELL",
                    quantity=abs(safe_float(row[7])),
                    price=safe_float(row[8]),
                    gross_amount=abs(safe_float(row[10])),
                    commission=abs(safe_float(row[11])),
                    currency=row[9].strip(),
                    exchange="CSV",
                    trade_date=dt.strftime("%Y%m%d"),
                    trade_time="00:00:00",
                    source="csv",
                )
            )
    return trades


def parse_activity_statement_snapshot(path: Path) -> dict:
    """Parse account-level snapshot from an IBKR Activity Statement CSV."""
    account: Dict[str, Any] = {}
    risk: Dict[str, Any] = {}
    positions: List[dict] = []
    syep_positions: List[dict] = []
    syep_fees: List[dict] = []
    fin_info: Dict[str, dict] = {}
    shares_lent_map: Dict[str, float] = {}
    syep_collateral = 0.0
    syep_fee_earned = 0.0

    # local helpers
    def parse_period_end(text: str) -> str:
        # "January 1, 2026 - June 5, 2026"
        if "-" in text:
            right = text.split("-", 1)[1].strip()
            try:
                dt = datetime.strptime(right, "%B %d, %Y")
                return dt.strftime("%Y-%m-%d")
            except Exception:
                pass
        return ""

    with path.open(newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            section = row[0].lstrip("\ufeff")
            row_type = row[1].strip()

            if section == "Statement" and row_type == "Data":
                field = row[2].strip() if len(row) > 2 else ""
                value = row[3].strip() if len(row) > 3 else ""
                if field == "Period":
                    account["reportEndDate"] = parse_period_end(value)
                continue

            if section == "Net Asset Value" and row_type == "Data":
                if len(row) < 7:
                    continue
                asset_class = row[2].strip()
                if asset_class == "Cash":
                    account["endingCash"] = safe_float(row[6])
                elif asset_class == "Stock":
                    account["stockValue"] = safe_float(row[6])
                elif asset_class == "Total":
                    account["endingValue"] = safe_float(row[6])
                    account["startingValue"] = safe_float(row[3])
                elif asset_class == "Collateral Value":
                    syep_collateral = safe_float(row[6])
                continue

            if section == "Net Asset Value" and row_type == "Header" and len(row) > 2:
                if "Time Weighted Rate of Return" in row[2]:
                    # Next data row should have the TWR value
                    for r2 in reader:
                        if len(r2) < 3:
                            continue
                        if r2[0].lstrip("\ufeff") == "Net Asset Value" and r2[1].strip() == "Data":
                            twr_text = r2[2].strip().replace("%", "")
                            account["timeWeightedReturnPct"] = safe_float(twr_text)
                        break
                continue

            if section == "Change in NAV" and row_type == "Data":
                if len(row) < 4:
                    continue
                field = row[2].strip()
                value = safe_float(row[3])
                if field == "Starting Value":
                    account["startingValue"] = value
                elif field == "Ending Value":
                    account["endingValue"] = value
                elif field == "Dividends":
                    account["dividends"] = value
                elif field == "Withholding Tax":
                    account["withholdingTax"] = value
                elif field == "Interest":
                    account["interest"] = value
                elif field == "Commissions":
                    account["commissions"] = value
                elif field == "Transaction Fees":
                    account["transactionFees"] = value
                elif field == "Other FX Translations":
                    account["otherFxTranslations"] = value
                continue

            if section == "Cash Report" and row_type == "Data":
                if len(row) < 5:
                    continue
                label = row[2].strip()
                currency = row[3].strip()
                if currency == "Base Currency Summary":
                    if label == "Ending Cash":
                        account["endingCash"] = safe_float(row[4])
                    elif label == "Ending Settled Cash":
                        account["endingSettledCash"] = safe_float(row[4])
                    elif label == "Net Cash Balance":
                        account["netCashBalance"] = safe_float(row[4])
                continue

            if section == "Open Positions" and row_type == "Data":
                if len(row) < 12:
                    continue
                discriminator = row[2].strip()
                asset_category = row[3].strip()
                if discriminator == "Summary" and asset_category == "Stocks":
                    raw_symbol = row[5].strip().upper()
                    symbol_lb = normalize_symbol(raw_symbol)
                    qty = safe_float(row[6])
                    cost_basis = safe_float(row[9]) if len(row) > 9 else 0.0
                    close_price = safe_float(row[10]) if len(row) > 10 else 0.0
                    value = safe_float(row[11]) if len(row) > 11 else 0.0
                    unrealized = safe_float(row[12]) if len(row) > 12 else 0.0
                    positions.append({
                        "symbol": raw_symbol,
                        "symbolLb": symbol_lb,
                        "quantity": qty,
                        "costBasis": cost_basis,
                        "closePrice": close_price,
                        "value": value,
                        "unrealizedPl": unrealized,
                    })
                continue

            if section == "Net Stock Position Summary" and row_type == "Data":
                if len(row) < 10:
                    continue
                asset_category = row[2].strip()
                if asset_category != "Stocks":
                    continue
                raw_symbol = row[4].strip().upper()
                symbol_lb = normalize_symbol(raw_symbol)
                shares_lent = safe_float(row[8]) if len(row) > 8 else 0.0
                shares_lent_map[symbol_lb] = shares_lent
                continue

            if section == "Stock Yield Enhancement Program Securities Lent" and row_type == "Data":
                if len(row) < 9:
                    continue
                raw_symbol = row[4].strip().upper()
                if not raw_symbol:
                    # Total row
                    syep_collateral = safe_float(row[8]) if len(row) > 8 else syep_collateral
                    continue
                symbol_lb = normalize_symbol(raw_symbol)
                qty = safe_float(row[6]) if len(row) > 6 else 0.0
                collateral = safe_float(row[8]) if len(row) > 8 else 0.0
                syep_positions.append({
                    "symbolLb": symbol_lb,
                    "quantity": qty,
                    "collateral": collateral,
                })
                continue

            if section == "Stock Yield Enhancement Program Securities Lent Fee Earned Details" and row_type == "Data":
                if len(row) < 11:
                    continue
                raw_symbol = row[4].strip().upper()
                if not raw_symbol:
                    # Total row
                    syep_fee_earned = safe_float(row[10]) if len(row) > 10 else syep_fee_earned
                    continue
                symbol_lb = normalize_symbol(raw_symbol)
                fee = safe_float(row[10]) if len(row) > 10 else 0.0
                syep_fees.append({"symbolLb": symbol_lb, "fee": fee})
                continue

            if section == "Financial Instrument Information" and row_type == "Data":
                if len(row) < 5:
                    continue
                asset_category = row[2].strip()
                raw_symbol = row[3].strip().upper()
                if not raw_symbol:
                    continue
                symbol_lb = normalize_symbol(raw_symbol)
                description = row[4].strip() if len(row) > 4 else ""
                fin_info[symbol_lb] = {
                    "symbol": raw_symbol,
                    "symbolLb": symbol_lb,
                    "assetCategory": asset_category,
                    "description": description,
                }
                continue

    # Enrich positions with shares lent
    for pos in positions:
        pos["sharesLent"] = shares_lent_map.get(pos["symbolLb"], 0.0)

    # Compute risk metrics
    ending_value = account.get("endingValue") or 0.0
    stock_value = account.get("stockValue") or 0.0
    ending_cash = account.get("endingCash") or 0.0
    fees = abs(account.get("commissions") or 0.0) + abs(account.get("transactionFees") or 0.0)
    net_dividends = (account.get("dividends") or 0.0) + (account.get("withholdingTax") or 0.0)

    risk["stockExposurePct"] = round(stock_value / ending_value, 6) if ending_value else 0.0
    risk["cashPct"] = round(ending_cash / ending_value, 6) if ending_value else 0.0
    risk["leverage"] = round(stock_value / ending_value, 6) if ending_value else 0.0
    risk["fees"] = round(fees, 6)
    risk["financingInterest"] = round(account.get("interest") or 0.0, 6)
    risk["netDividends"] = round(net_dividends, 6)
    risk["syepCollateral"] = round(syep_collateral, 6)
    risk["syepFeeEarned"] = round(syep_fee_earned, 6)

    return {
        "accountSnapshot": account,
        "riskSnapshot": risk,
        "ibkrOpenPositions": positions,
        "syepPositions": syep_positions,
        "syepFees": syep_fees,
        "financialInstrumentInfo": fin_info,
    }


def parse_mtm_file(path: Path) -> Tuple[Dict[str, dict], List[OtherPnlSnapshot]]:
    mtm: Dict[str, dict] = {}
    other: List[OtherPnlSnapshot] = []
    with path.open(newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 13:
                continue
            tag = row[0].lstrip("\ufeff")
            if tag != "Mark-to-Market Performance Summary" or row[1] != "Data":
                continue
            asset_category = row[2].strip()
            raw_symbol = row[3].strip().upper()
            if asset_category == "Stocks" and raw_symbol:
                symbol_lb = normalize_symbol(raw_symbol)
                mtm[symbol_lb] = {
                    "symbol": symbol_lb,
                    "displayName": symbol_lb,
                    "mtmTotal": round(safe_float(row[12]), 6),
                    "tradeCount": 0,
                    "amount": 0.0,
                    "hasTrades": False,
                    "hasMtm": True,
                }
                continue
            if asset_category in {"Stocks", "Total", "Total (All Assets)"}:
                continue
            display_name = raw_symbol or asset_category
            other.append(
                OtherPnlSnapshot(
                    asset_category=asset_category,
                    symbol=raw_symbol,
                    display_name=display_name,
                    mtm_total=round(safe_float(row[12]), 6),
                )
            )
    return mtm, other
