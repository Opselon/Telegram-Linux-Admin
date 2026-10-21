"""Verifies a built wheel actually contains the catalogs and subpackages.

The Docker image installs a wheel, not the checkout, so a packaging mistake
(missing package-data) only shows up as raw i18n keys in production.  CI runs
this right after ``python -m build`` so that class of bug fails the build.

Usage: python scripts/verify_wheel.py [dist/*.whl]
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PACKAGES = ("domain", "application", "infrastructure", "presentation")
REQUIRED_LANGUAGES = (
    "ar", "de", "en", "es", "fa", "fr", "hi", "it", "ja", "ko", "pt", "ru", "tr", "ur", "zh",
)


def main(argv: list[str]) -> int:
    wheels = [Path(arg) for arg in argv[1:]] or sorted((ROOT / "dist").glob("*.whl"))
    if not wheels:
        print("FAIL: no wheel found (run `python -m build` first)")
        return 1

    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    problems: list[str] = []
    for package in REQUIRED_PACKAGES:
        if f"src/{package}/__init__.py" not in names:
            problems.append(f"missing subpackage src/{package}")

    for language in REQUIRED_LANGUAGES:
        if f"src/presentation/i18n/catalog/{language}.json" not in names:
            problems.append(f"missing catalog {language}.json")

    if "src/presentation/i18n/languages.json" not in names:
        problems.append("missing languages.json")

    if problems:
        print(f"FAIL: {wheel.name} is incomplete")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print(
        f"OK: {wheel.name} contains {len(REQUIRED_PACKAGES)} subpackages "
        f"and {len(REQUIRED_LANGUAGES)} language catalogs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
