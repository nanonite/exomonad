#!/usr/bin/env python3
"""Check relative Markdown links in the user-facing documentation."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HOSTLIKE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/|$)")


def main() -> int:
    errors: list[str] = []
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            target = match.group(1).strip()
            if target.startswith("<") and ">" in target:
                target = target[1 : target.index(">")]
            else:
                target = target.split(maxsplit=1)[0]
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            # Treat protocol-less host links as external, too. They occur in
            # imported research Markdown and are not repository paths.
            if HOSTLIKE.match(target):
                continue
            target = unquote(target.split("#", 1)[0].split("?", 1)[0])
            candidate = (document.parent / target).resolve()
            try:
                candidate.relative_to(ROOT)
            except ValueError:
                # Some workspace documentation intentionally links to the
                # sibling open-science checkout. Validate it when present,
                # while still reporting unresolved links.
                if not candidate.exists():
                    errors.append(
                        f"{document.relative_to(ROOT)}: missing external link target: {target}"
                    )
                continue
            if not candidate.exists():
                errors.append(f"{document.relative_to(ROOT)}: missing link target: {target}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"documentation links passed ({len(documents)} Markdown files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
