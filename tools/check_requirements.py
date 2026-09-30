"""Keep `.venv` in step with `requirements.txt` — called by Start ArtDrop.bat.

Exit code 0 = up to date (or nothing installed yet), 1 = outdated, so the
launcher knows whether pip has to run. Run with `write` to record the current
hash after a successful install.
"""
from __future__ import annotations

import hashlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
MARKER = ROOT / ".venv" / ".requirements-hash"


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    if not MARKER.parent.exists():
        return 0  # no environment yet: the launcher's first-run path installs everything
    if not REQUIREMENTS.exists():
        print("requirements.txt is missing")
        return 1

    current = digest(REQUIREMENTS)
    if argv[:1] == ["write"]:
        MARKER.write_text(current, encoding="utf-8")
        return 0

    stored = MARKER.read_text(encoding="utf-8").strip() if MARKER.exists() else ""
    return 0 if stored == current else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
