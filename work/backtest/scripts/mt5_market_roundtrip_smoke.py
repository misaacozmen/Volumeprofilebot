"""Retired compatibility entry point for the old direct MT5 smoke script.

All smoke execution is now owned by the Super1 ProductionOrderFlow
coordinator. Keeping this command as an explicit fail-closed stub prevents a
second order-send path from being used accidentally.
"""

from __future__ import annotations


def main() -> None:
    raise RuntimeError(
        "Direct MT5 round-trip smoke is retired; use the Super1 production "
        "coordinator with an authorized demo-only smoke approval."
    )


if __name__ == "__main__":
    main()
