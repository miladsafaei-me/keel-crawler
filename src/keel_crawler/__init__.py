"""Package version, derived rather than restated.

This file held a hand-written string, and on 2026-09-03 it was reporting 0.26.0
from an installation of 0.27.0 - the CI version guard compares the git tag with
`pyproject.toml` and never looks here, so a bump that forgot this line failed
nothing and the wrong number was what every log line and every report printed.
The installed metadata is the same value `pyproject.toml` produced, so reading it
cannot disagree with it; a source checkout with no metadata falls back to the one
file the guard does check.
"""
from __future__ import annotations


def _resolve_version() -> str:
    try:
        from importlib.metadata import version

        return version("keel-crawler")
    except Exception:  # noqa: BLE001 - not installed, e.g. PYTHONPATH=src
        pass
    import re
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        found = re.search(r'^version\s*=\s*"([^"]+)"',
                          pyproject.read_text(encoding="utf-8"), re.M)
    except OSError:
        return "unknown"
    return found.group(1) if found else "unknown"


__version__ = _resolve_version()
