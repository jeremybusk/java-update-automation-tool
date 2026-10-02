#!/usr/bin/env python3
"""Regenerate Markdown views from Java update JSON artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from java_update_tool.core import render_reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path(".java-update"))
    args = parser.parse_args()
    outputs = render_reports(args.state.resolve())
    print(f"Generated {len(outputs)} Markdown report(s) under {args.state.resolve() / 'reports'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
