"""One-shot migration: legacy ``TRANSLATIONS`` dict -> JSON catalogs.

Run once during the 4.0 refactor; afterwards the JSON files are the single
source of truth and :mod:`src.localization` is a thin facade over them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.localization_legacy import LANGUAGE_LABELS, TRANSLATIONS  # noqa: E402

CATALOG_DIR = ROOT / "src" / "presentation" / "i18n" / "catalog"
META_FILE = ROOT / "src" / "presentation" / "i18n" / "languages.json"


def main() -> None:
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    for language, strings in TRANSLATIONS.items():
        path = CATALOG_DIR / f"{language}.json"
        path.write_text(
            json.dumps(strings, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path.relative_to(ROOT)} ({len(strings)} keys)")

    meta = {
        "default": "en",
        "rtl": ["ar", "fa", "he", "ur"],
        "languages": {code: label for code, label in sorted(LANGUAGE_LABELS.items())},
    }
    META_FILE.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {META_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
