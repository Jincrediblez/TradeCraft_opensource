import json
from pathlib import Path

import pytest

from app.audit_workbench import (
    build_behavior_comparison,
    build_workbench,
    create_rule_cycle,
    enrich_executions,
    enrich_round_trips,
    evaluate_rule_cycles,
    evidence_page,
    load_review_state,
    save_finding_feedback,
    save_round_trip_note,
    save_snapshot,
    stop_rule_cycle,
)


def trade(day, time, symbol="AMD.US", side="BUY", quantity=10, price=100):
    return {
        "symbol": symbol.split(".")[0],
        "symbol_lb": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "gross_amount": quantity * price,
        "commission": 1,
        "currency": "USD",
        "exchange": "NASDAQ",
        "trade_date": day,
        "trade_time": time,
        "source": "tlg",
    }


def closed(symbol="AMD.US", open_date="20260102", close_date="20260105", pnl=100):
    return {
        "symbol": symbol,
        "open_date": open_date,
        "close_date": close_date,
        "holding_days": 3,
        "quantity": 10,
        "avg_entry": 100,
        "avg_exit": 111,
        "realized_pnl": pnl,
        "commissions": 2,
        "entry_source": "actual_trade",
        "initial_risk": None,
        "r_multiple": None,
        "risk_status": "risk_missing",
        "setup_type": "主题轮动",
        "exit_setup_type": "止盈",
    }


def base_summary():
    return {
        "period": "2026YTD",
        "periodLabel": "2026YTD",
        "generated_at": "2026-07-29T10:00:00",
        "scores": {
            "narrative_hype": {"score": 75, "level": "high"},
        },
        "metrics": {"round_trips_3d": 1},
        "alerts": [
            {
                "type": "narrative_hype",
                "severity": "high",
                "score": 75,
                "detail": "AMD.US 非主线交易过多",
                "all_evidence": ["AMD.US evidence"],
            }
        ],
        "audit_coverage": {"missing": []},
        "theme_attribution": [
            {
                "theme": "ai_semiconductors",
                "tier": "beta",
                "symbols": ["AMD.US"],
            }
        ],
        "ytd_mtm_contribution": {"AMD.US": {"mtmTotal": 100}},
        "exit_quality": {
            "evaluatedCount": 1,
            "sellTooEarlyCount20d": 0,
            "trendUnbrokenExitCount": 0,
        },
        "position_rules": {"alerts": []},
    }


def test_execution_and_round_trip_ids_are_stable_across_reimport_order():
    rows = [
        trade("20260102", "10:00:00"),
        trade("20260105", "10:00:00", side="SELL", price=111),
    ]
    first = enrich_executions(rows)
    second = enrich_executions(list(reversed(rows)))
    assert [row["executionId"] for row in first] == [row["executionId"] for row in second]

    first_round = enrich_round_trips([closed()], first)
    second_round = enrich_round_trips([closed()], second)
    assert first_round[0]["roundTripId"] == second_round[0]["roundTripId"]
    assert len(first_round[0]["executionIds"]) == 2


def test_behavior_uses_recent_20_vs_previous_20_active_trading_days():
    executions = []
    closed_rows = []
    for index in range(40):
        day = f"202602{index + 1:02d}" if index < 28 else f"202603{index - 27:02d}"
        executions.append(trade(day, "10:00:00"))
        closed_rows.append(closed(open_date=day, close_date=day))
    result = build_behavior_comparison(enrich_executions(executions), enrich_round_trips(closed_rows, []))
    assert result["sampleSufficient"] is True
    assert result["current"]["activeTradingDays"] == 20
    assert result["previous"]["activeTradingDays"] == 20
    assert result["current"]["dailyAverage"] == 1


def test_workbench_separates_result_process_behavior_and_confidence(tmp_path, monkeypatch):
    monkeypatch.setattr("app.trade_plans.load_trade_plans", lambda: {})
    trades = [
        trade("20260102", "10:00:00"),
        trade("20260105", "10:00:00", side="SELL", price=111),
    ]
    statement = {
        "accountSnapshot": {
            "reportEndDate": "2026-01-05",
            "timeWeightedReturnPct": 10,
            "startingValue": 100000,
            "endingValue": 110000,
        }
    }

    def kline(symbol):
        return [
            {"time": "2026-01-02", "close": 100},
            {"time": "2026-01-05", "close": 105},
        ]

    report = tmp_path / "audit_report.md"
    report.write_text("旧报告，累计交易 X 次", encoding="utf-8")
    snapshot, round_trips, executions = build_workbench(
        period="2026YTD",
        summary=base_summary(),
        trades=trades,
        closed_trades=[closed()],
        statement_snapshot=statement,
        settings={"auditPrimaryBenchmark": "QQQ.US", "auditSecondaryBenchmark": "SPY.US"},
        report_path=report,
        report_meta_path=tmp_path / "audit_report_meta.json",
        kline_loader=kline,
    )
    assert set(snapshot["scorecards"]) == {"result", "process", "behavior", "confidence"}
    assert snapshot["riskScores"][0]["key"] == "narrative_hype"
    assert [row["key"] for row in snapshot["riskScores"]] == ["narrative_hype"]
    assert snapshot["aiReport"]["stale"] is True
    assert snapshot["aiReport"]["placeholderDetected"] is True
    assert snapshot["dataQuality"]["initialRiskCoveragePct"] == 0
    assert round_trips[0]["roundTripId"]
    assert len(executions) == 2
    risk_finding = next(item for item in snapshot["findings"] if item["type"] == "data_quality_risk")
    page = evidence_page(
        {"snapshotId": snapshot["snapshotId"], "executions": executions, "items": round_trips},
        finding=risk_finding,
    )
    assert page["total"] == 1


def test_review_feedback_rule_cycle_and_round_trip_note_are_persisted(tmp_path):
    snapshot = {
        "snapshotId": "audit_test",
        "period": "2026YTD",
        "meta": {"allTradeDates": ["20260102"]},
        "metricCatalog": {"behavior.roundTrips3d": 10},
    }
    finding = {
        "findingId": "finding_test",
        "title": "反复交易",
        "metricKey": "behavior.roundTrips3d",
        "direction": "lower",
        "suggestedRule": "三日内不重复进入",
    }
    feedback = save_finding_feedback(
        tmp_path,
        period="2026YTD",
        finding_id="finding_test",
        decision="confirmed",
        note="属实",
    )
    assert feedback["decision"] == "confirmed"

    cycle = create_rule_cycle(
        tmp_path,
        period="2026YTD",
        snapshot=snapshot,
        finding=finding,
    )
    assert cycle["baselineValue"] == 10
    assert cycle["targetValue"] == 8
    assert cycle["durationTradingDays"] == 20

    snapshot["meta"]["allTradeDates"] += [f"202602{day:02d}" for day in range(1, 21)]
    snapshot["metricCatalog"]["behavior.roundTrips3d"] = 7
    state = evaluate_rule_cycles(tmp_path, snapshot, persist=True)
    assert state["ruleCycles"][0]["status"] == "met"

    note = save_round_trip_note(
        tmp_path,
        period="2026YTD",
        round_trip_id="rt_test",
        payload={"setupType": "主题轮动", "invalidationPrice": 90, "targetHoldingDays": 20},
    )
    assert note["invalidationPrice"] == 90
    assert load_review_state(tmp_path)["feedback"]["2026YTD:finding_test"]["note"] == "属实"

    stopped = create_rule_cycle(
        tmp_path,
        period="2026YTD",
        snapshot=snapshot,
        finding={**finding, "findingId": "finding_stop"},
    )
    assert stop_rule_cycle(tmp_path, stopped["cycleId"])["status"] == "stopped"


def test_snapshot_aliases_changed_round_trip_ids_and_evidence_is_paginated(tmp_path):
    old_payload = {
        "schemaVersion": 2,
        "items": [{**closed(), "roundTripId": "rt_old"}],
    }
    (tmp_path / "audit_round_trips.json").write_text(json.dumps(old_payload), encoding="utf-8")
    snapshot = {"snapshotId": "audit_new", "generatedAt": "2026-07-29"}
    new_item = {**closed(), "roundTripId": "rt_new"}
    save_snapshot(tmp_path, snapshot, [new_item])
    stored = json.loads((tmp_path / "audit_round_trips.json").read_text())
    assert stored["aliases"]["rt_old"] == "rt_new"

    result = evidence_page(
        {"snapshotId": "audit_new", "items": [new_item] * 120},
        page=2,
        page_size=50,
    )
    assert result["total"] == 120
    assert len(result["items"]) == 50


def test_invalid_feedback_and_rule_limit_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        save_finding_feedback(
            tmp_path,
            period="2026YTD",
            finding_id="finding",
            decision="maybe",
        )

    snapshot = {
        "snapshotId": "audit_test",
        "period": "2026YTD",
        "meta": {"allTradeDates": ["20260102"]},
        "metricCatalog": {"metric": 10},
    }
    for index in range(3):
        create_rule_cycle(
            tmp_path,
            period="2026YTD",
            snapshot=snapshot,
            finding={
                "findingId": f"finding_{index}",
                "title": "test",
                "metricKey": "metric",
                "direction": "lower",
            },
        )
    with pytest.raises(ValueError):
        create_rule_cycle(
            tmp_path,
            period="2026YTD",
            snapshot=snapshot,
            finding={
                "findingId": "finding_4",
                "title": "test",
                "metricKey": "metric",
                "direction": "lower",
            },
        )


def test_trade_plan_history_is_versioned_and_not_applied_retroactively(tmp_path, monkeypatch):
    from app import trade_plans

    monkeypatch.setattr(trade_plans, "TRADE_PLANS_FILE", tmp_path / "trade_plans.json")
    monkeypatch.setattr(trade_plans, "TRADE_PLAN_HISTORY_FILE", tmp_path / "trade_plan_history.json")
    monkeypatch.setattr(trade_plans, "STATE_DIR", tmp_path)

    saved = trade_plans.save_trade_plan(
        "AMD.US",
        {
            "setupType": "主题轮动",
            "thesis": "test",
            "invalidationPrice": 90,
            "targetHoldingDays": 20,
        },
    )
    history = trade_plans.load_trade_plan_history()
    assert history["versions"]["AMD.US"][0]["versionId"].startswith("plan_")
    assert trade_plans.get_trade_plan_at("AMD.US", "20200101")["invalidationPrice"] is None
    today = saved["updatedAt"][:10].replace("-", "")
    assert trade_plans.get_trade_plan_at("AMD.US", today)["invalidationPrice"] == 90


def test_unknown_future_review_schema_is_read_only_protected(tmp_path):
    (tmp_path / "audit_review_state.json").write_text(
        json.dumps({"schemaVersion": 99, "feedback": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="高于当前支持版本"):
        load_review_state(tmp_path)


def test_twenty_thousand_execution_capacity_keeps_ids_unique_and_pages_bounded():
    rows = []
    for index in range(20_000):
        rows.append(
            trade(
                f"2026{index // 1000 + 1:02d}{index % 28 + 1:02d}",
                f"{index % 24:02d}:{index % 60:02d}:{index % 59:02d}",
                symbol=f"S{index % 100}.US",
                quantity=index % 50 + 1,
                price=100 + (index % 500) / 10,
            )
        )
    enriched = enrich_executions(rows)
    assert len(enriched) == 20_000
    assert len({row["executionId"] for row in enriched}) == 20_000
    page = evidence_page(
        {
            "snapshotId": "capacity",
            "executions": enriched,
            "items": [
                {
                    **closed(symbol=f"S{index % 100}.US"),
                    "roundTripId": f"rt_{index}",
                    "executionIds": [enriched[index]["executionId"]],
                }
                for index in range(500)
            ],
        },
        page=1,
        page_size=500,
    )
    assert page["pageSize"] == 100
    assert len(page["items"]) == 100


def test_frontend_exposes_layered_workbench_and_separates_ai_generation():
    html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'window.location.protocol === "file:"' in html
    assert 'window.location.replace("http://127.0.0.1:8787/")' in html
    for pane in ("Overview", "Outcome", "Process", "Behavior", "Evidence", "Improvement", "Ai"):
        assert f'id="auditPane{pane}"' in html
    assert 'id="auditRefreshData"' in html
    assert "/api/audit/workbench" in html
    assert "/api/audit/evidence" in html
    assert "/api/audit/finding-feedback" in html
    assert "/api/audit/rule-cycle" in html
    assert "/api/audit/roundtrip-note" in html
    assert "/api/audit/report/generate" in html
    assert "generate_ai=0" in html
    assert "data-audit-evidence-page" in html
