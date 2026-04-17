"""Verify the TD Web Server DAT bridge without involving MCP.

Run with TouchDesigner open and the mcp_webserver set up:
    uv run python scripts/smoke_test.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

HOST = os.environ.get("TD_HOST", "127.0.0.1")
PORT = int(os.environ.get("TD_PORT", "9980"))
URL = f"http://{HOST}:{PORT}/mcp"


def call(code: str, mode: str = "exec") -> dict:
    req = urllib.request.Request(
        URL,
        data=json.dumps({"code": code, "mode": mode}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def main() -> int:
    checks = [
        ("ping", "_result = {'app': app.product, 'version': app.version}", "exec"),
        ("eval 1+1", "1 + 1", "eval"),
        ("list /project1", "_result = [c.name for c in op('/project1').children]", "exec"),
    ]
    failed = 0
    for label, code, mode in checks:
        try:
            resp = call(code, mode)
            ok = resp.get("ok")
            print(f"[{'OK ' if ok else 'ERR'}] {label}: {resp}")
            if not ok:
                failed += 1
        except Exception as e:
            print(f"[ERR] {label}: {e}")
            failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
