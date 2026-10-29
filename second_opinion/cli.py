"""Command-line entry point.

    python3 -m second_opinion.cli seeds
    python3 -m second_opinion.cli audit "Fontaines D.C."
    python3 -m second_opinion.cli audit "RÜFÜS DU SOL"
    python3 -m second_opinion.cli audit "Fontaines D.C." --candidate "Little Simz" --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .adapters import AdapterError, EntityResolutionError, FixtureAdapter, LiveAdapter, describe_environment
from .agent import SecondOpinionAgent
from .catalog import seed_list
from .config import DEFAULT_CONFIG, AuditConfig
from .env import load_env_file


def _adapter(mode: str):
    if mode == "live":
        return LiveAdapter()
    return FixtureAdapter()


def _default_mode() -> str:
    mode = (os.environ.get("SO_MODE") or "live").strip().lower()
    return mode if mode in ("live", "fixture") else "live"


def cmd_seeds(args) -> int:
    for seed in seed_list():
        print(f"{seed['name']:20s} {seed['note']}")
    env = describe_environment()
    print(f"\nmode={args.mode}  live_ready={env['live_ready']}  host={env['host']}")
    if args.mode == "live" and not env["live_ready"]:
        print("live mode needs QLOO_API_KEY in the environment; audits will refuse until it is set")
    return 0


def cmd_audit(args) -> int:
    adapter = _adapter(args.mode)
    config = AuditConfig(
        strong_threshold=args.threshold,
        min_dimensions_corroborated=args.min_corroborated,
    )
    agent = SecondOpinionAgent(adapter, config=config)
    try:
        report = agent.audit(args.seed)
    except AdapterError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        return 2

    if args.candidate:
        wanted = args.candidate.lower()
        report = dict(report)
        report["candidates"] = [
            c for c in report["candidates"] if c["candidate"].lower() == wanted
        ]
        if not report["candidates"]:
            print(f"no candidate named {args.candidate!r} for this seed", file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    print(f"seed: {args.seed}  mode={report['mode']}")
    if report["refusal"]:
        print(f"\nREFUSED ({report['refusal']['code']})")
        print(f"  {report['refusal']['detail']}")
        for key, value in (report["refusal"].get("facts") or {}).items():
            print(f"  {key}: {value}")
        print("\ntrace:")
        for step in report["trace"]:
            print(f"  [{step['status']:8s}] {step['step']}: {step['detail']}")
        return 1

    for cand in report["candidates"]:
        print(f"\n{'=' * 74}")
        aff = f"{cand['affinity']:.4f}" if cand["affinity"] is not None else "none"
        print(f"{cand['candidate']}   affinity={aff}   STATE={cand['state']}")
        print(f"  {cand['reason']}")
        for name, dim in cand["dimensions"].items():
            ov = f"{dim['overlap']:.2f}" if dim["overlap"] is not None else " -- "
            print(f"  {name:9s} {dim['status']:13s} overlap={ov}  {dim['reason']}")
            if dim["shared_terms"]:
                print(f"            shared: {', '.join(dim['shared_terms'])}")
        print(f"  receipts_verified={cand['receipts_verified']}")
    print("\ntrace:")
    for step in report["trace"]:
        print(f"  [{step['status']:8s}] {step['step']}: {step['detail']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="second-opinion", description=__doc__)
    parser.add_argument("--mode", choices=["fixture", "live"], default=_default_mode(),
                        help="live (default) calls Qloo; fixture reads local captures")
    sub = parser.add_subparsers(dest="command", required=True)

    seeds = sub.add_parser("seeds", help="list available seed entities")
    seeds.set_defaults(func=cmd_seeds)

    audit = sub.add_parser("audit", help="audit the evidence behind one seed's recommendations")
    audit.add_argument("seed")
    audit.add_argument("--candidate", help="restrict output to one candidate name")
    audit.add_argument("--json", action="store_true", help="emit the full report as JSON")
    audit.add_argument("--threshold", type=float, default=DEFAULT_CONFIG.strong_threshold,
                       help="overlap needed for a dimension to corroborate (default 0.30)")
    audit.add_argument("--min-corroborated", type=int, default=DEFAULT_CONFIG.min_dimensions_corroborated,
                       help="corroborated dimensions needed for CORROBORATED (default 2)")
    audit.set_defaults(func=cmd_audit)
    return parser


def main(argv=None) -> int:
    # Same precedence as the server: the project-root `.env` fills gaps,
    # but anything already exported into the environment is left untouched.
    load_env_file()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except EntityResolutionError as exc:
        print(f"entity resolution failed: {exc}", file=sys.stderr)
        return 2
    except AdapterError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
