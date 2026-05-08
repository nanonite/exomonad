#!/usr/bin/env python3
"""Format spindle workflow JSON log lines for human reading."""
import sys, json

for line in sys.stdin:
    try:
        e = json.loads(line)
        kind = e.get("kind", "")
        content = e.get("content", "")
        status = e.get("step_status", "")
        if kind == "control":
            print(f"  [{status}] {content}")
        elif kind == "data":
            print(f"       {content[:120]}")
    except Exception:
        pass
