#!/usr/bin/env python3
"""Validate every GitHub Actions workflow file.

PURPOSE / OWNER / CONSUMES / PROVIDES / INVARIANTS / EXTEND
PURPOSE: Catch the workflow mistakes that GitHub either rejects late or
    silently misinterprets, before they reach a push.
OWNER:   ci lane (feat/3.2-ci).
CONSUMES: ``.github/workflows/*.yml`` and ``*.yaml``.
PROVIDES: exit 0 when every workflow is structurally sound, exit 1 with one
    ``ERROR <path>: ...`` line per problem otherwise.
INVARIANTS:
  - stdlib only: PyYAML is used when importable, otherwise the file must be
    JSON (a real fallback, not a silent skip).
  - Flags the three historical defects in this repo: a workflow stored as a
    JSON document, an unquoted emoji in YAML, and a step that sets both
    ``uses`` and ``run`` (GitHub ignores ``run`` in that case).
  - Never needs the network, never writes files.
EXTEND: add a new ``_check_*`` helper and call it from ``_check_workflow``.
"""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

try:  # PyYAML is a test-time nicety; CI installs it, the gate does not need it.
    import yaml
except ImportError:  # pragma: no cover - exercised only on a bare interpreter
    yaml = None


ERRORS: list[str] = []


def _error(path: Path, message: str) -> None:
    """Record one failure so a single run reports every problem at once."""
    ERRORS.append(f"ERROR {path}: {message}")


def _load(path: Path) -> tuple[Any, bool]:
    """Return (document, was_json).

    Parsing must succeed with a *real* parser. The JSON check is independent
    of which parser is available: a workflow stored as a JSON document is the
    exact defect that shipped once already, and PyYAML happily accepts JSON
    because JSON is a YAML subset. So we test for a JSON document first, then
    parse as YAML.
    """
    text = path.read_text(encoding="utf-8")
    was_json = False
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            json.loads(stripped)
            was_json = True
        except json.JSONDecodeError:
            was_json = False

    if yaml is not None:
        try:
            return yaml.safe_load(text), was_json
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML: {exc}") from exc
    # Fallback: without PyYAML only a JSON document can be validated. Refuse
    # anything else rather than pretend it passed.
    try:
        return json.loads(text), True
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid YAML and PyYAML is unavailable (pip install pyyaml): {exc}"
        ) from exc


def _check_emoji_quoting(path: Path, text: str) -> None:
    """Flag emoji that are not quoted or inside a comment.

    An unquoted emoji at the start of a value is a YAML parse risk and the
    historical cause of a broken release workflow. Emoji inside quotes,
    block scalars, or ``#`` comments are fine.
    """
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        for char in line:
            if unicodedata.category(char) != "So":
                continue
            if char in {'"', "'"}:
                continue
            # Quoted already? Count quotes before the emoji.
            index = line.index(char)
            before = line[:index]
            if before.count('"') % 2 == 1 or before.count("'") % 2 == 1:
                continue
            _error(path, f"line {lineno}: unquoted emoji {char!r} - wrap the value in quotes")
            break


def _check_workflow(path: Path, doc: Any, was_json: bool) -> None:
    """Assert the structural shape GitHub needs to schedule any job."""
    if was_json:
        _error(path, "workflow is stored as a JSON document, not YAML")

    if not isinstance(doc, dict):
        _error(path, f"top level must be a mapping, got {type(doc).__name__}")
        return

    # `on` is parsed as the boolean True by YAML 1.1 parsers; accept both so a
    # correct file never fails on that quirk.
    if "on" not in doc and True not in doc:
        _error(path, "missing required key 'on'")
    if "jobs" not in doc:
        _error(path, "missing required key 'jobs'")
        return

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        _error(path, "'jobs' must be a non-empty mapping")
        return

    steps_seen = 0
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            _error(path, f"job '{job_id}' must be a mapping")
            continue
        if "uses" in job:
            # Reusable-workflow call: no steps and no runs-on by design.
            steps_seen += 1
            continue
        if "runs-on" not in job:
            _error(path, f"job '{job_id}' has no 'runs-on'")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            _error(path, f"job '{job_id}' has no steps")
            continue
        for index, step in enumerate(steps):
            steps_seen += 1
            label = f"job '{job_id}' step {index + 1}"
            if not isinstance(step, dict):
                _error(path, f"{label} must be a mapping")
                continue
            if "uses" in step and "run" in step:
                _error(path, f"{label} sets both 'uses' and 'run'; GitHub ignores 'run'")
            if "uses" not in step and "run" not in step:
                _error(path, f"{label} has neither 'uses' nor 'run'")

    if steps_seen == 0:
        _error(path, "no job defines any step")


def main(argv: list[str]) -> int:
    """Validate the given directory (or ``.github/workflows``) and report."""
    target = Path(argv[1]) if len(argv) > 1 else Path(".github/workflows")
    if not target.is_dir():
        print(f"ERROR {target}: not a directory")
        return 1

    paths = sorted([*target.glob("*.yml"), *target.glob("*.yaml")])
    if not paths:
        print(f"ERROR {target}: no workflow files found")
        return 1

    print(
        f"Validating {len(paths)} workflow file(s) in {target} (parser: {'PyYAML' if yaml else 'json'})"
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        _check_emoji_quoting(path, text)
        try:
            doc, was_json = _load(path)
        except ValueError as exc:
            _error(path, str(exc))
            continue
        _check_workflow(path, doc, was_json)
        if not any(e.startswith(f"ERROR {path}:") for e in ERRORS):
            print(f"OK    {path}")

    if ERRORS:
        print()
        for line in ERRORS:
            print(line)
        print(f"\nFAILED with {len(ERRORS)} error(s)")
        return 1
    print(f"\nPASSED: {len(paths)} workflow(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
