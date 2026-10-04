#!/usr/bin/env python3
"""Regenerate Markdown views from Java update JSON artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from java_update_tool.core import PortfolioError, render_reports
from java_update_tool.runs import run_directory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".java-update"))
    parser.add_argument("--run-id", default="latest")
    args = parser.parse_args()
    state = args.state.resolve()
    try:
        root = run_directory(state, args.run_id)
        outputs = render_reports(root)
    except (PortfolioError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Generated {len(outputs)} Markdown report(s) under {root / 'reports'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
