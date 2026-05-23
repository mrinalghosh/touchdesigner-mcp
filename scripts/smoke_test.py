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
        # Screenshot path: create a throwaway constantTOP, encode it to JPEG via
        # PIL+numpy (same path screenshot_op uses), assert we get plausible bytes,
        # then clean up. Verifies Pillow+numpy are available in TD's Python and
        # that numpyArray()→encode round-trips without writing to disk.
        (
            "screenshot_op encode path",
            (
                "import io, base64, numpy as np\n"
                "from PIL import Image as _PIL\n"
                "_tmp = op('/project1').create(td.constantTOP, 'mcp_smoke_shot')\n"
                "try:\n"
                "  _tmp.par.resolutionw, _tmp.par.resolutionh = 64, 64\n"
                "  _tmp.par.colorr, _tmp.par.colorg, _tmp.par.colorb = 0.2, 0.5, 0.8\n"
                "  _tmp.cook(force=True)\n"
                "  _arr = _tmp.numpyArray(delayed=False)\n"
                "  _u8 = (np.clip(np.flipud(_arr), 0, 1) * 255 + 0.5).astype('uint8')[:, :, :3]\n"
                "  _buf = io.BytesIO()\n"
                "  _PIL.fromarray(_u8, 'RGB').save(_buf, 'JPEG', quality=75)\n"
                "  _b = _buf.getvalue()\n"
                "  assert _b[:3] == b'\\xff\\xd8\\xff', 'not a JPEG'\n"
                "  _result = {'bytes': len(_b), 'b64_head': base64.b64encode(_b)[:16].decode()}\n"
                "finally:\n"
                "  _tmp.destroy()"
            ),
            "exec",
        ),
        # bind_parameter_expression: verifies the happy path (expr evaluates to
        # the expected value, op_errors stays empty) AND the failure path (a
        # syntax-error expression surfaces either via the direct eval exception
        # or via new entries in op.errors() — TD does one or the other depending
        # on version, so we accept either).
        (
            "bind_parameter_expression happy + sad",
            (
                "_t = op('/project1').create(td.constantCHOP, 'mcp_smoke_expr')\n"
                "try:\n"
                "  _p = _t.par.value0\n"
                "  _before = set((_t.errors(recurse=False) or '').splitlines())\n"
                "  _p.expr = '0.25 + 0.25'\n"
                "  _p.mode = td.ParMode.EXPRESSION\n"
                "  _good = _p.eval()\n"
                "  assert abs(_good - 0.5) < 1e-6, 'expected 0.5, got ' + repr(_good)\n"
                "  _p.expr = 'this is not python'\n"
                "  _p.mode = td.ParMode.EXPRESSION\n"
                "  _raised = None\n"
                "  try: _p.eval()\n"
                "  except Exception as _e: _raised = type(_e).__name__\n"
                "  _new = [ln for ln in (_t.errors(recurse=False) or '').splitlines() if ln not in _before]\n"
                "  assert _raised or _new, 'bad expression produced neither exception nor op error'\n"
                "  _result = {'good': _good, 'bad_raised': _raised, 'bad_op_errors': len(_new)}\n"
                "finally:\n"
                "  _t.destroy()"
            ),
            "exec",
        ),
        # create_from_template: build a Base COMP, inline the same op-creation
        # sequence the server-side templates produce, verify the GLSL TOP
        # actually shows the uniform color (proves Vectors-page binding works),
        # then destroy the Base COMP so the user's project stays clean even if
        # this test bails midway. Inlined rather than imported because smoke_test
        # talks to TD's webserver directly and can't import the MCP server.
        (
            "templates: chop+null and glsl_top vec4 uniform end-to-end",
            (
                "_root = op('/project1')\n"
                "_box = _root.create(td.baseCOMP, 'mcp_smoke_tmpl')\n"
                "try:\n"
                "  # chop_source_with_null\n"
                "  _stype = 'mouseinCHOP'\n"
                "  _cls = getattr(td, _stype)\n"
                "  _src = _box.create(_cls, 'chop_src')\n"
                "  _null = _box.create(td.nullCHOP, 'chop_null')\n"
                "  _src.outputConnectors[0].connect(_null.inputConnectors[0])\n"
                "  assert _null.inputConnectors[0].connections, 'null not wired to src'\n"
                "  # glsl_top_vec4_uniform — minimal end-to-end\n"
                "  _chop = _box.create(td.constantCHOP, 'glsl_uniform')\n"
                "  for _i, _ch in enumerate(['x','y','z','w']):\n"
                "    getattr(_chop.par, 'name' + str(_i)).val = _ch\n"
                "    getattr(_chop.par, 'value' + str(_i)).val = 1.0 if _ch=='w' else 0.5\n"
                "  _dat = _box.create(td.textDAT, 'glsl_shader')\n"
                "  _dat.text = 'out vec4 fragColor;\\nuniform vec4 uColor;\\nvoid main(){fragColor=uColor;}\\n'\n"
                "  _glsl = _box.create(td.glslTOP, 'glsl_top')\n"
                "  _glsl.par.pixeldat = _dat.name\n"
                "  _glsl.par.vec0name = 'uColor'\n"
                "  for _c in ['x','y','z','w']:\n"
                "    _p = getattr(_glsl.par, 'vec0value' + _c)\n"
                "    _p.expr = \"op('\" + _chop.name + \"')['\" + _c + \"']\"\n"
                "    _p.mode = td.ParMode.EXPRESSION\n"
                "  _glsl.cook(force=True)\n"
                "  _errs = _box.errors(recurse=True) or ''\n"
                "  assert 'uniform' not in _errs.lower() or 'not assigned' not in _errs.lower(), \\\n"
                "    'shader uniform not assigned — vectors-page binding broke: ' + _errs[:300]\n"
                "  _arr = _glsl.numpyArray(delayed=False)\n"
                "  assert _arr is not None and abs(float(_arr[0,0,0]) - 0.5) < 0.05, \\\n"
                "    'glsl output not driven by uniform: ' + repr(None if _arr is None else _arr[0,0,:])\n"
                "  _result = {'children': len(_box.children), 'errors': _errs[:200]}\n"
                "finally:\n"
                "  _box.destroy()"
            ),
            "exec",
        ),
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
