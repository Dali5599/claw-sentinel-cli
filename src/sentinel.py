#!/usr/bin/env python3
"""claw-sentinel entry point.

Run directly:

    python src/sentinel.py check --tags cpu --format text

Or through the package/console script:

    claw-sentinel check --tags cpu
    python -m sentinel check --tags cpu

Usage
-----
The CLI provides three subcommands:

* ``check``  - collect one hardware snapshot, evaluate alert rules, emit JSON/text.
* ``watch``  - continuous monitoring at a fixed interval with alert dispatch.
* ``alert``  - read an alert payload (JSON/JSONL) and dispatch it.

See ``claw-sentinel --help`` for the full option list.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)

# Make the sibling package importable when this file is executed directly
# (``python src/sentinel.py``). The normal ``python -m sentinel`` and console
# script paths load this same file as a package and need no adjustment.
if _PARENT not in sys.path and _PARENT != os.curdir:
    sys.path.insert(0, _PARENT)

try:
    from sentinel._cli import main
except ImportError:
    from _cli import main

if __name__ == "__main__":
    raise SystemExit(main())