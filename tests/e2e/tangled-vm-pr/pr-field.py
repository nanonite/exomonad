#!/usr/bin/env python3
import json
import sys

path, key = sys.argv[1:3]
with open(path, encoding="utf-8") as handle:
    registry = json.load(handle)
prs = registry.get("prs") or {}
if not prs:
    raise SystemExit(1)
entry = prs[sorted(prs.keys(), key=lambda item: int(item))[0]]
value = entry.get(key)
if value in (None, ""):
    raise SystemExit(1)
print(value)
