#!/usr/bin/env python3
"""CLI entry for the AI audit pipeline.

Usage:
    python app/cli.py match
    python app/cli.py attribution
    python app/cli.py audit [--period ytd|last_20d|...] [--llm] [--export FILE]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure app/ is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def cmd_match(args):
    from app.position_matcher import run_matching
    result = run_matching()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_attribution(args):
    from app.theme_classifier import run_theme_attribution
    result = run_theme_attribution()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_audit(args):
    # Phase 1: matching
    from app.position_matcher import run_matching
    match_result = run_matching()
    print("[Phase 1] Position matching done:")
    print(f"  Closed trades: {match_result['closed_trade_count']}")
    print(f"  Open positions: {match_result['open_position_count']}")
    print(f"  Realized PnL: ${match_result['total_realized_pnl']:,.2f}")

    # Phase 1a: Auto classify setups and apply to closed trades
    from app.trade_setup_manager import run_auto_classify_and_apply
    setup_result = run_auto_classify_and_apply(force=getattr(args, 'force', False))
    print("\n[Phase 1a] Trade setup classification complete:")
    if "error" not in setup_result:
        classify = setup_result.get("classify", {})
        apply = setup_result.get("apply", {})
        print(f"  Classified: {classify.get('classified', 0)}")
        print(f"  Applied to closed trades: {apply.get('applied', 0)}")
        dist = classify.get("distribution", {})
        if dist:
            print(f"  Distribution: {', '.join(f'{k}:{v}' for k,v in dist.items())}")
    else:
        print(f"  Error: {setup_result['error']}")

    # Phase 1.5: attribution
    from app.theme_classifier import run_theme_attribution
    attr_result = run_theme_attribution()
    print("\n[Phase 1.5] Theme attribution done:")
    print(f"  Themes: {', '.join(attr_result['themes'])}")

    # Phase 2: Memory Engine
    from app.trader_memory import build_memory
    memory_result = build_memory()
    print("\n[Phase 2] Memory Engine done:")
    if "error" not in memory_result:
        print(f"  Memory tags: {memory_result['tagCount']}")
    else:
        print(f"  Error: {memory_result['error']}")

    # Phase 3: Style Drift
    from app.style_drift_engine import build_style_drift
    drift_result = build_style_drift()
    print("\n[Phase 3] Style Drift done:")
    if "error" not in drift_result:
        print(f"  Drift Score: {drift_result['driftScore']['score']}/100 ({drift_result['driftScore']['level']})")
        print(f"  Alerts: {len(drift_result['driftAlerts'])}")
    else:
        print(f"  Error: {drift_result['error']}")

    # Phase 4: scoring
    from app.scoring_engine import run_scoring
    scoring_result = run_scoring()
    print("\n[Phase 4] Scoring done:")
    for name, data in scoring_result.get("scores", {}).items():
        print(f"  {name}: {data['score']}/100 ({data['level']})")

    # Phase 5: LLM report
    if args.llm:
        from app.llm_reporter import generate_report
        report_path = generate_report()
        print(f"\n[Phase 5] LLM report generated: {report_path}")

    # Export
    if args.export:
        from app.scoring_engine import AUDIT_SUMMARY_FILE
        import shutil
        shutil.copy(AUDIT_SUMMARY_FILE, args.export)
        print(f"\n[Export] Audit summary saved to: {args.export}")


def main():
    parser = argparse.ArgumentParser(description="IBKR AI Trading Audit CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # match
    p_match = subparsers.add_parser("match", help="Run FIFO position matching")
    p_match.set_defaults(func=cmd_match)

    # attribution
    p_attr = subparsers.add_parser("attribution", help="Run theme classification + attribution")
    p_attr.set_defaults(func=cmd_attribution)

    # audit
    p_audit = subparsers.add_parser("audit", help="Run full audit pipeline")
    p_audit.add_argument("--period", default="ytd", help="Audit period (ytd, last_20d, etc.)")
    p_audit.add_argument("--llm", action="store_true", help="Generate LLM critique report")
    p_audit.add_argument("--export", help="Export audit JSON to file path")
    p_audit.add_argument("--force", action="store_true", help="Reclassify all trade setups (overwrite automatic classifications)")
    p_audit.set_defaults(func=cmd_audit)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
