"""Child process for deterministic stale HALT claim recovery acceptance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.live.halt import HaltController


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--kind", choices=("cycle", "ticket"), required=True)
    parser.add_argument("--sync-root", required=True)
    parser.add_argument("--index", required=True, type=int, choices=(0, 1))
    args = parser.parse_args()
    controller = HaltController(args.root)
    if args.kind == "cycle":
        token = controller.claim_flatten_cycle(args.episode)
    else:
        token = controller.claim_flatten(args.episode, ticket=88)
    sync_root = Path(args.sync_root)
    sync_root.mkdir(parents=True, exist_ok=True)
    (sync_root / f"ready-{args.index}.json").write_text(
        json.dumps({"token": token}), encoding="utf-8"
    )
    deadline = time.monotonic() + 10
    ready = (sync_root / "ready-0.json", sync_root / "ready-1.json")
    while not all(path.is_file() for path in ready) and time.monotonic() < deadline:
        time.sleep(0.01)
    if not all(path.is_file() for path in ready):
        raise SystemExit("timed out waiting for sibling claim probe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
