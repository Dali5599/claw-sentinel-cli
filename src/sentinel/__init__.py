"""Top-level package for claw-sentinel.

This package exposes the CLI entry point as ``sentinel.main`` and provides
``python -m sentinel`` invocation.

Usage
-----
    claw-sentinel check --tags cpu,memory --format text
    claw-sentinel watch --interval 60 --output monitor.jsonl
    claw-sentinel alert --stdin
"""
from ._cli import VERSION, main

__version__ = VERSION
__all__ = ["main", "VERSION", "__version__"]