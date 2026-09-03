"""Run the next immutable local Super1 acceptance evidence transaction."""

from __future__ import annotations

from pathlib import Path
import sys

import run_super1_run008_evidence as base


WORKSPACE = Path(__file__).resolve().parents[3]
base.RUN_ID = "run-009"
base.DEFAULT_OUTPUT = WORKSPACE / "outputs" / "super1_readiness_20260831" / "run-009"
base.OLD_OUTPUT = WORKSPACE / "outputs" / "super1_readiness_20260831" / "run-008"


if __name__ == "__main__":
    raise SystemExit(base.main())
