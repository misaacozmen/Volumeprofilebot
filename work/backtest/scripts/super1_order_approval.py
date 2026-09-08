"""Local-only Super1 approval CLI: list, show, approve, reject."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backtest.live.approval import ApprovalStore, canonical_store_path
from run_capital_forward import write_fatal_latch
from super1_runtime_guard import current_process_sid, load_lease, load_runtime_config


APP_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = APP_ROOT.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="super1_order_approval")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=RUNTIME_ROOT / "state",
        help="mutable state root containing orders/idempotency.sqlite3",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list")
    list_parser.add_argument("scope", nargs="?", choices=["staged", "approvals"], default="staged")
    sub.add_parser("staged")
    show = sub.add_parser("show")
    show.add_argument("proposal_id")
    approve = sub.add_parser("approve")
    approve.add_argument("proposal_id")
    reject = sub.add_parser("reject")
    reject.add_argument("proposal_id")
    reject.add_argument("--reason", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = ApprovalStore(
        canonical_store_path(args.output_root),
        halt=lambda reason, **details: write_fatal_latch(
            args.output_root, reason, reason, **details
        ),
    )
    if args.command in {"list", "staged"}:
        if args.command == "staged" or args.scope == "staged":
            value = store.staged()
        else:
            value = [{field: getattr(record, field) for field in record.__slots__} for record in store.list()]
    elif args.command == "show":
        record = store.show_for_proposal(args.proposal_id)
        value = {field: getattr(record, field) for field in record.__slots__}
    elif args.command == "approve":
        config, _, _ = load_runtime_config(APP_ROOT)
        sid = current_process_sid()
        if sid != str(config["authorized_operator_sid"]):
            raise RuntimeError("Current interactive SID is not the signed authorized operator SID.")
        lease = load_lease(
            RUNTIME_ROOT,
            config,
            for_order=True,
            principal_sid=sid,
            principal_role="operator",
        )
        candidate_path = APP_ROOT / str(config["candidate_path"])
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        record = store.approve(args.proposal_id, lease=lease, operator_sid=sid, release_id=str(lease["release_id"]), candidate_hash=str(candidate.get("artifact_sha256") or candidate.get("candidate_artifact_sha256") or candidate.get("candidate_hash") or ""))
        value = {field: getattr(record, field) for field in record.__slots__}
    else:
        record = store.reject(args.proposal_id, reason=args.reason)
        value = {field: getattr(record, field) for field in record.__slots__}
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
