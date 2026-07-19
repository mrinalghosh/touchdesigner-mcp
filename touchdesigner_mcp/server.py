"""TouchDesigner MCP server.

Bridges Claude (over MCP stdio) to one or more running TouchDesigner instances
whose Web Server DATs expose an HTTP endpoint for executing Python on TD's main
thread. See td_component/webserver_callbacks.py for the TD-side half.

## Configuration

Single instance (simple form, backward compatible):
    TD_HOST=127.0.0.1   TD_PORT=9980   TD_PATH=/mcp

Multi-instance:
    TD_INSTANCES="main=127.0.0.1:9980,fx=127.0.0.1:9981,stage=192.168.1.40:9980"
    TD_DEFAULT_INSTANCE="main"     # which one unqualified tool calls target

Per-instance override for path:
    TD_INSTANCES="main=127.0.0.1:9980/mcp,dev=127.0.0.1:9981/mcp-dev"

Timeout:
    TD_TIMEOUT=10.0
"""
from __future__ import annotations

import os
from typing import Any

import base64

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image


def _parse_instances() -> dict[str, tuple[str, int, str]]:
    """Return {name: (host, port, path)} from TD_INSTANCES, or a single 'default'."""
    raw = os.environ.get("TD_INSTANCES", "").strip()
    if not raw:
        return {
            "default": (
                os.environ.get("TD_HOST", "127.0.0.1"),
                int(os.environ.get("TD_PORT", "9980")),
                os.environ.get("TD_PATH", "/mcp"),
            )
        }
    out: dict[str, tuple[str, int, str]] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(f"TD_INSTANCES entry missing '=': {entry!r}")
        name, endpoint = (s.strip() for s in entry.split("=", 1))
        path = "/mcp"
        hostport = endpoint
        if "/" in endpoint:
            hostport, rest = endpoint.split("/", 1)
            path = "/" + rest
        if ":" not in hostport:
            raise ValueError(f"TD_INSTANCES entry {name!r} missing port: {endpoint!r}")
        host, port_s = hostport.rsplit(":", 1)
        out[name] = (host, int(port_s), path)
    if not out:
        raise ValueError("TD_INSTANCES parsed to empty mapping")
    return out


INSTANCES = _parse_instances()
DEFAULT_INSTANCE = os.environ.get("TD_DEFAULT_INSTANCE") or next(iter(INSTANCES))
if DEFAULT_INSTANCE not in INSTANCES:
    raise ValueError(
        f"TD_DEFAULT_INSTANCE={DEFAULT_INSTANCE!r} not in configured instances "
        f"{sorted(INSTANCES)}"
    )
TD_TIMEOUT = float(os.environ.get("TD_TIMEOUT", "10.0"))


# Sent to every client at connection time (MCP `instructions`), so this travels
# with the server to any project/directory — unlike a repo CLAUDE.md or a local
# skill. Kept deliberately tight: it lives in context for the whole session, so
# bloating it would be self-defeating.
_INSTRUCTIONS = """\
Drive a running TouchDesigner instance from its Web Server DAT. Every tool call
is a full model turn plus ~one TD frame of latency, and large results stay in
context and slow every later turn — so be economical.

EFFICIENCY
- Batch. To build or modify more than ~2 ops, use build_network (declarative
  ops + connections + params) or a single exec_python that assigns _result —
  not a stream of create_operator / connect_operators / set_parameter calls.
  create_from_template covers common wired recipes.
- Introspect sparingly, cheapest-first. For an op's params use list_parameters
  on the actual op. For the Python API, climb the ladder: get_module_help with
  grep='x' to find a member (~hundreds of tok), then member='x' to read one
  (~150 tok); get_td_class_details for a structured whole-class summary (~3k);
  bare get_module_help is the ~10k-token full dump — last resort. Filter
  get_td_classes with name_contains. TD's API is stable within a session —
  don't re-introspect the same class/op.
- Verify with get_errors or the `errors` field build_network returns, not
  screenshot_op, unless you genuinely need to see pixels.

TD GOTCHAS (each fails silently if ignored)
- Parameter names are the LOWERCASE internal names from list_parameters
  (e.g. 'brightness1', 'translatex'), NOT the TitleCase tooltip label.
- After a source CHOP (mouseIn, audioIn, midiIn, oscIn, ...), insert a Null
  CHOP and reference the Null — sources rename/prefix channels unpredictably.
- GLSL TOP runtime uniforms belong on the Vectors page (declare a vec4, set
  vec0name), not the Constants page (compile-time only, assigns nothing).
- TD's Python has numpy but not Pillow; screenshot_op already encodes PNG —
  don't reach for PIL.
"""

mcp = FastMCP("touchdesigner", instructions=_INSTRUCTIONS)


def _resolve(instance: str | None) -> tuple[str, str]:
    """Return (instance_name, url) for a given name (None → default)."""
    name = instance or DEFAULT_INSTANCE
    if name not in INSTANCES:
        raise ValueError(
            f"Unknown TD instance {name!r}. Configured: {sorted(INSTANCES)}"
        )
    host, port, path = INSTANCES[name]
    return name, f"http://{host}:{port}{path}"


async def _td_call(code: str, mode: str = "exec", instance: str | None = None) -> Any:
    """POST Python to the selected TD instance; raise on structured failure.

    Error classification surfaces the distinction between "can't reach TD at
    all" (process down, wrong port, DAT inactive), "TD accepted the request
    but the main thread didn't respond in time" (busy cooking, blocking
    script), and "TD ran the code and it raised" — because the fix is
    different in each case.
    """
    name, url = _resolve(instance)
    try:
        async with httpx.AsyncClient(timeout=TD_TIMEOUT) as client:
            r = await client.post(url, json={"code": code, "mode": mode})
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"[{name}] cannot reach TouchDesigner at {url}: {e}. "
            f"Check that TD is open, the Web Server DAT exists and is Active, "
            f"and that its Port matches (default 9980)."
        ) from None
    except httpx.ReadTimeout as e:
        raise RuntimeError(
            f"[{name}] TouchDesigner did not respond within {TD_TIMEOUT}s at {url}: {e}. "
            f"TD's main thread may be busy cooking or blocked by a long script. "
            f"Raise TD_TIMEOUT or simplify the call."
        ) from None
    except httpx.HTTPError as e:
        raise RuntimeError(f"[{name}] HTTP error talking to {url}: {e}") from None

    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(
            f"[{name}] non-JSON response from {url} (HTTP {r.status_code}). "
            f"The Web Server DAT callback may be missing or out of date — "
            f"re-paste td_component/webserver_callbacks.py. "
            f"Body: {r.text[:200]!r}"
        ) from None

    if not data.get("ok"):
        err = data.get("error") or "TD returned ok=false with no detail"
        tb = data.get("traceback")
        raise RuntimeError(f"[{name}] {err}" + (f"\n{tb}" if tb else ""))
    return data.get("result")


def _lit(value: Any) -> str:
    """Render a Python literal for safe embedding in generated TD code."""
    return repr(value)


# ─── meta tools ──────────────────────────────────────────────────────────────

@mcp.tool()
async def list_instances() -> dict:
    """Show configured TouchDesigner instances.

    Use any other tool's `instance` parameter to target a specific one.
    Omit `instance` (or pass null) to hit the default.
    """
    return {
        "default": DEFAULT_INSTANCE,
        "instances": {
            name: {"host": h, "port": p, "path": pa}
            for name, (h, p, pa) in INSTANCES.items()
        },
    }


@mcp.tool()
async def ping(instance: str | None = None) -> dict:
    """Health check for a TD instance. Returns app/version/project info."""
    code = (
        "_result = {"
        "'app': getattr(app, 'product', None), "
        "'version': getattr(app, 'version', None), "
        "'build': getattr(app, 'build', None), "
        "'project': getattr(project, 'name', None), "
        "'folder': getattr(project, 'folder', None)"
        "}"
    )
    result = await _td_call(code, instance=instance)
    name, _ = _resolve(instance)
    return {"instance": name, **(result or {})}


@mcp.tool()
async def ping_all() -> dict:
    """Ping every configured instance; returns {name: status}."""
    out: dict[str, Any] = {}
    for name in INSTANCES:
        try:
            out[name] = await ping(instance=name)
        except Exception as e:
            out[name] = {"error": str(e)}
    return out


# ─── arbitrary code ──────────────────────────────────────────────────────────

@mcp.tool()
async def exec_python(code: str, instance: str | None = None) -> Any:
    """Execute arbitrary Python inside TouchDesigner.

    Runs on TD's main thread with `op`, `td`, `parent`, `root`, `project`, `app`,
    `ui`, `me` bound. Assign your return value to `_result` to receive it back.
    Example:
        _result = [c.path for c in op('/project1').children]
    """
    return await _td_call(code, mode="exec", instance=instance)


@mcp.tool()
async def eval_python(expression: str, instance: str | None = None) -> Any:
    """Evaluate a single Python expression in TD and return its value."""
    return await _td_call(expression, mode="eval", instance=instance)


# ─── node lifecycle ──────────────────────────────────────────────────────────

@mcp.tool()
async def create_operator(
    parent_path: str, op_type: str, name: str, instance: str | None = None
) -> str:
    """Create a new operator under a parent COMP.

    Args:
        parent_path: e.g. '/project1'
        op_type:     TD class name like 'noiseTOP', 'waveCHOP', 'boxSOP', 'textDAT'
        name:        identifier for the new op (must be unique in the parent)
    Returns the created op's full path.
    """
    code = (
        f"_cls = getattr(td, {_lit(op_type)}, None)\n"
        f"if _cls is None: raise ValueError('Unknown op type: ' + {_lit(op_type)})\n"
        f"_result = op({_lit(parent_path)}).create(_cls, {_lit(name)}).path"
    )
    return await _td_call(code, instance=instance)


@mcp.tool()
async def delete_operator(path: str, instance: str | None = None) -> str:
    """Destroy the operator at `path`."""
    code = f"op({_lit(path)}).destroy()\n_result = {_lit(path)}"
    return await _td_call(code, instance=instance)


@mcp.tool()
async def rename_operator(
    path: str, new_name: str, instance: str | None = None
) -> str:
    """Rename an operator. Returns its new full path."""
    code = (
        f"_o = op({_lit(path)})\n"
        f"_o.name = {_lit(new_name)}\n"
        f"_result = _o.path"
    )
    return await _td_call(code, instance=instance)


@mcp.tool()
async def move_operator(
    path: str, x: float, y: float, instance: str | None = None
) -> str:
    """Move an operator's tile in the network editor."""
    code = (
        f"_o = op({_lit(path)})\n"
        f"_o.nodeX = {float(x)}\n_o.nodeY = {float(y)}\n"
        f"_result = _o.path"
    )
    return await _td_call(code, instance=instance)


# ─── parameters ──────────────────────────────────────────────────────────────

@mcp.tool()
async def set_parameter(
    path: str, param: str, value: Any, instance: str | None = None
) -> dict:
    """Set a parameter on an operator.

    `param` is the TD parameter name as it appears on `.par` — usually the
    tooltip name in TitleCase (e.g. 'Period', 'Amp', 'Translatex').
    Works for numeric, string, and menu parameters.
    """
    code = (
        f"_p = getattr(op({_lit(path)}).par, {_lit(param)})\n"
        f"_p.val = {_lit(value)}\n"
        f"_result = {{'path': {_lit(path)}, 'param': {_lit(param)}, 'value': _p.eval()}}"
    )
    return await _td_call(code, instance=instance)


@mcp.tool()
async def get_parameter(
    path: str, param: str, instance: str | None = None
) -> Any:
    """Read the evaluated value of a parameter."""
    code = f"_result = getattr(op({_lit(path)}).par, {_lit(param)}).eval()"
    return await _td_call(code, instance=instance)


@mcp.tool()
async def list_parameters(path: str, instance: str | None = None) -> list[dict]:
    """List every parameter on an operator with current value and style."""
    code = (
        f"_o = op({_lit(path)})\n"
        f"def _sv(p):\n"
        f"    try: return p.eval()\n"
        f"    except Exception: return None\n"
        f"_result = [{{'name': p.name, 'label': p.label, 'style': p.style, "
        f"'value': _sv(p), 'default': p.default}} for p in _o.pars()]"
    )
    return await _td_call(code, instance=instance)


@mcp.tool()
async def bind_parameter_expression(
    path: str,
    param: str,
    expression: str,
    instance: str | None = None,
) -> dict:
    """Set a parameter to evaluate a Python expression at cook time.

    Switches the parameter to Expression mode, writes the expression string,
    forces a single evaluation, and reports back the value plus any error
    TD raised — both the direct eval exception and any new entries in the
    owning op's error list.

    Use this instead of writing expressions through `exec_python`: that path
    silently no-ops on syntax errors and you only notice when nothing moves.

    Example:
        bind_parameter_expression('/project1/transform1', 'tx',
                                  "op('null1')['tx']")
    """
    code = (
        f"_path, _name, _src = {_lit(path)}, {_lit(param)}, {_lit(expression)}\n"
        f"_o = op(_path)\n"
        f"if _o is None: raise ValueError('No op at ' + _path)\n"
        f"_p = getattr(_o.par, _name, None)\n"
        f"if _p is None: raise AttributeError(_path + ' has no parameter ' + _name)\n"
        f"_before = set((_o.errors(recurse=False) or '').splitlines())\n"
        f"_p.expr = _src\n"
        # ParMode isn't on the td module — grab the enum class off an actual
        # par instance so we work across TD builds.
        f"_p.mode = type(_p.mode).EXPRESSION\n"
        f"_err = None\n"
        f"_val = None\n"
        f"try: _val = _p.eval()\n"
        f"except Exception as _e: _err = type(_e).__name__ + ': ' + str(_e)\n"
        f"_new = [ln for ln in (_o.errors(recurse=False) or '').splitlines() if ln not in _before]\n"
        f"_result = {{'path': _path, 'param': _name, 'expression': _src, "
        f"'mode': str(_p.mode), 'value': _val, 'error': _err, 'op_errors': _new}}"
    )
    return await _td_call(code, instance=instance)


@mcp.tool()
async def pulse_parameter(
    path: str, param: str, instance: str | None = None
) -> str:
    """Pulse a pulse-style parameter (e.g. a Reset or Trigger button)."""
    code = f"getattr(op({_lit(path)}).par, {_lit(param)}).pulse()"
    await _td_call(code, instance=instance)
    return f"pulsed {path}.{param}"


# ─── wiring ──────────────────────────────────────────────────────────────────

@mcp.tool()
async def connect_operators(
    source_path: str,
    target_path: str,
    source_output: int = 0,
    target_input: int = 0,
    instance: str | None = None,
) -> str:
    """Wire source_path's output N into target_path's input M."""
    code = (
        f"_src = op({_lit(source_path)})\n"
        f"_dst = op({_lit(target_path)})\n"
        f"_src.outputConnectors[{int(source_output)}].connect("
        f"_dst.inputConnectors[{int(target_input)}])\n"
        f"_result = _src.path + ' -> ' + _dst.path"
    )
    suffix = f" (out {int(source_output)}, in {int(target_input)})"
    result = await _td_call(code, instance=instance)
    return f"{result}{suffix}"


@mcp.tool()
async def disconnect_input(
    path: str, input_index: int = 0, instance: str | None = None
) -> str:
    """Disconnect whatever feeds input `input_index` on `path`."""
    code = (
        f"_c = op({_lit(path)}).inputConnectors[{int(input_index)}]\n"
        f"for _con in list(_c.connections): _con.disconnect()\n"
        f"_result = {_lit(path)}"
    )
    return await _td_call(code, instance=instance)


# ─── query ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_children(
    comp_path: str, instance: str | None = None
) -> list[dict]:
    """List direct children of a COMP."""
    code = (
        f"_result = [{{'name': c.name, 'path': c.path, 'type': c.OPType, "
        f"'family': c.family}} for c in op({_lit(comp_path)}).children]"
    )
    return await _td_call(code, instance=instance)


@mcp.tool()
async def find_operators(
    root_path: str = "/",
    op_type: str | None = None,
    name_pattern: str | None = None,
    depth: int = 4,
    instance: str | None = None,
) -> list[dict]:
    """Search for operators under a root by type and/or glob name pattern."""
    kwargs: list[str] = []
    if op_type is not None:
        kwargs.append(f"type=getattr(td, {_lit(op_type)})")
    if name_pattern is not None:
        kwargs.append(f"name={_lit(name_pattern)}")
    kwargs.append(f"depth={int(depth)}")
    code = (
        f"_result = [{{'name': c.name, 'path': c.path, 'type': c.OPType}} "
        f"for c in op({_lit(root_path)}).findChildren({', '.join(kwargs)})]"
    )
    return await _td_call(code, instance=instance)


@mcp.tool()
async def get_errors(
    path: str = "/", recurse: bool = True, instance: str | None = None
) -> dict:
    """Return errors and warnings at `path`."""
    code = (
        f"_o = op({_lit(path)})\n"
        f"_result = {{'errors': _o.errors(recurse={bool(recurse)}), "
        f"'warnings': _o.warnings(recurse={bool(recurse)})}}"
    )
    return await _td_call(code, instance=instance)


# ─── introspection ───────────────────────────────────────────────────────────
# Let the model discover TD's Python API at runtime instead of relying on
# trained knowledge. TD's API drifts between versions; introspection reflects
# whatever is actually installed.

@mcp.tool()
async def get_td_info(instance: str | None = None) -> dict:
    """Describe the TouchDesigner runtime: version, project, Python, platform.

    Call this first when API compatibility matters — the other introspection
    tools return data specific to this version.
    """
    code = (
        "import sys, platform\n"
        "_result = {\n"
        "  'td': {\n"
        "    'product': getattr(app, 'product', None),\n"
        "    'version': getattr(app, 'version', None),\n"
        "    'build': getattr(app, 'build', None),\n"
        "    'architecture': getattr(app, 'architecture', None),\n"
        "  },\n"
        "  'project': {\n"
        "    'name': getattr(project, 'name', None),\n"
        "    'folder': getattr(project, 'folder', None),\n"
        "    'saveVersion': getattr(project, 'saveVersion', None),\n"
        "    'cookRate': getattr(project, 'cookRate', None),\n"
        "  },\n"
        "  'python': {'version': sys.version, 'executable': sys.executable},\n"
        "  'platform': {'system': platform.system(), 'release': platform.release(), 'machine': platform.machine()},\n"
        "  'root_children': [c.name for c in op('/').children],\n"
        "}"
    )
    result = await _td_call(code, instance=instance)
    name, _ = _resolve(instance)
    return {"instance": name, **(result or {})}


@mcp.tool()
async def get_td_classes(
    name_contains: str | None = None, instance: str | None = None
) -> list[str]:
    """List public classes exposed by the `td` module.

    Use this to discover what op types and helper classes are available before
    calling `create_operator` or `get_td_class_details`. Optional case-insensitive
    substring filter on `name_contains` (e.g. 'TOP', 'CHOP', 'noise').
    """
    filt = (
        f"_f = {_lit(name_contains)}\n"
        f"_match = lambda n: (_f.lower() in n.lower())\n"
        if name_contains is not None
        else "_match = lambda n: True\n"
    )
    code = (
        f"{filt}"
        f"_result = sorted(\n"
        f"  n for n in dir(td)\n"
        f"  if not n.startswith('_')\n"
        f"  and isinstance(getattr(td, n, None), type)\n"
        f"  and _match(n)\n"
        f")"
    )
    return await _td_call(code, instance=instance)


@mcp.tool()
async def get_td_class_details(
    class_name: str, instance: str | None = None
) -> dict:
    """Describe a TD Python class: docstring, base classes, methods, attributes.

    `class_name` must be a top-level name in the `td` module (e.g. 'TOP',
    'noiseTOP', 'OP', 'Par'). Use `get_td_classes` to find candidates.
    Method signatures come from `inspect.signature`; some C-extension methods
    will fall back to '(...)'.
    """
    code = (
        f"import inspect\n"
        f"_name = {_lit(class_name)}\n"
        f"_cls = getattr(td, _name, None)\n"
        f"if not isinstance(_cls, type):\n"
        f"  raise ValueError('Not a class on td module: ' + _name)\n"
        f"def _firstline(s):\n"
        f"  if not s: return None\n"
        f"  for _ln in str(s).splitlines():\n"
        f"    _ln = _ln.strip()\n"
        f"    if _ln: return _ln\n"
        f"  return None\n"
        f"_methods, _attrs = [], []\n"
        f"for _n in sorted(dir(_cls)):\n"
        f"  if _n.startswith('_'): continue\n"
        f"  try: _v = getattr(_cls, _n)\n"
        f"  except Exception: continue\n"
        f"  if callable(_v):\n"
        f"    try: _sig = str(inspect.signature(_v))\n"
        f"    except (TypeError, ValueError): _sig = '(...)'\n"
        f"    _methods.append({{'name': _n, 'signature': _sig, 'doc': _firstline(getattr(_v, '__doc__', None))}})\n"
        f"  else:\n"
        f"    _attrs.append({{'name': _n, 'type': type(_v).__name__}})\n"
        f"_result = {{\n"
        f"  'name': _cls.__name__,\n"
        f"  'doc': (_cls.__doc__ or '').strip() or None,\n"
        f"  'bases': [b.__name__ for b in _cls.__bases__],\n"
        f"  'methods': _methods,\n"
        f"  'attributes': _attrs,\n"
        f"}}"
    )
    return await _td_call(code, instance=instance)


@mcp.tool()
async def get_module_help(
    name: str,
    member: str | None = None,
    grep: str | None = None,
    instance: str | None = None,
) -> str | dict:
    """Look up TD Python API docs. Prefer the scalpel modes — the full dump is ~10k tokens.

    `name` is a bare identifier on the `td` module ('TOP', 'Par') or a dotted
    path resolvable in TD's namespace ('op', 'project'). Climb cheapest-first:

    - grep='pattern'  — SEARCH: members of `name` whose name or docstring match
      (case-insensitive). Returns [{name, signature, doc}] rows, ~hundreds of
      tokens. Use when you don't know the exact member. (grep like the shell.)
    - member='cook'   — ZOOM: help() for just that one member, ~150 tokens.
      Use when you know which method/attr you want.
    - neither         — FULL help() text for the whole object, ~10k tokens for a
      big class. Last resort. Consider get_td_class_details for a structured,
      3x-smaller summary of the whole surface instead.

    `grep` wins if both are given.
    """
    code = (
        f"import io, contextlib, inspect\n"
        f"_name = {_lit(name)}\n"
        f"_member = {_lit(member)}\n"
        f"_grep = {_lit(grep)}\n"
        f"_obj = getattr(td, _name, None)\n"
        f"if _obj is None:\n"
        f"  try: _obj = eval(_name, {{'td': td, 'op': op, 'project': project, 'app': app, 'ui': ui}})\n"
        f"  except Exception: _obj = _name\n"
        f"if _grep is not None:\n"
        f"  _q = _grep.lower()\n"
        f"  _rows = []\n"
        f"  for _n in sorted(dir(_obj)):\n"
        f"    if _n.startswith('_'): continue\n"
        f"    try: _v = getattr(_obj, _n)\n"
        f"    except Exception: continue\n"
        f"    _doc = getattr(_v, '__doc__', '') or ''\n"
        f"    if _q in _n.lower() or _q in _doc.lower():\n"
        f"      if callable(_v):\n"
        f"        try: _sig = str(inspect.signature(_v))\n"
        f"        except (TypeError, ValueError): _sig = '(...)'\n"
        f"      else: _sig = None\n"
        f"      _first = next((_l.strip() for _l in _doc.splitlines() if _l.strip()), None)\n"
        f"      _rows.append({{'name': _n, 'signature': _sig, 'doc': _first[:100] if _first else None}})\n"
        f"  _result = {{'name': _name, 'grep': _grep, 'matches': _rows}}\n"
        f"elif _member is not None:\n"
        f"  _target = getattr(_obj, _member, None)\n"
        f"  if _target is None:\n"
        f"    raise ValueError('No member ' + repr(_member) + ' on ' + _name)\n"
        f"  _buf = io.StringIO()\n"
        f"  with contextlib.redirect_stdout(_buf):\n"
        f"    help(_target)\n"
        f"  _result = _buf.getvalue()\n"
        f"else:\n"
        f"  _buf = io.StringIO()\n"
        f"  with contextlib.redirect_stdout(_buf):\n"
        f"    help(_obj)\n"
        f"  _result = _buf.getvalue()"
    )
    return await _td_call(code, instance=instance)


# ─── templates ───────────────────────────────────────────────────────────────
# Multi-op recipes that encode CLAUDE.md gotchas as one-call primitives, so
# the agent can't fumble the wiring. Each builder returns a TD code block that
# creates the ops, wires them, lays them out, and assigns `_result`.

def _tmpl_chop_source_with_null(parent_path: str, prefix: str, opts: dict) -> str:
    """Source CHOP → Null CHOP. Reference the Null, never the source."""
    source_type = str(opts.get("source_type", "mouseinCHOP"))
    return (
        f"_parent_path = {_lit(parent_path)}\n"
        f"_prefix = {_lit(prefix)}\n"
        f"_stype = {_lit(source_type)}\n"
        "_parent = op(_parent_path)\n"
        "if _parent is None: raise ValueError('No parent COMP at ' + _parent_path)\n"
        "_cls = getattr(td, _stype, None)\n"
        "if not isinstance(_cls, type): raise ValueError('Unknown CHOP type: ' + _stype)\n"
        "_src = _parent.create(_cls, _prefix + '_src')\n"
        "_null = _parent.create(td.nullCHOP, _prefix + '_null')\n"
        "_src.outputConnectors[0].connect(_null.inputConnectors[0])\n"
        "_null.nodeX = _src.nodeX + 200\n"
        "_null.nodeY = _src.nodeY\n"
        "_result = {'source': _src.path, 'null': _null.path, "
        "'reference_via': _null.path, 'created': [_src.path, _null.path]}"
    )


def _tmpl_glsl_top_vec4_uniform(parent_path: str, prefix: str, opts: dict) -> str:
    """GLSL TOP + Constant CHOP + Text DAT, wired via the Vectors page."""
    uniform_name = str(opts.get("uniform_name", "uColor"))
    if not uniform_name.isidentifier():
        raise ValueError(f"uniform_name {uniform_name!r} is not a valid GLSL identifier")
    shader = (
        "out vec4 fragColor;\n"
        f"uniform vec4 {uniform_name};\n"
        "void main() {\n"
        f"    fragColor = {uniform_name};\n"
        "}\n"
    )
    return (
        f"_parent_path = {_lit(parent_path)}\n"
        f"_prefix = {_lit(prefix)}\n"
        f"_uname = {_lit(uniform_name)}\n"
        f"_shader_src = {_lit(shader)}\n"
        "_parent = op(_parent_path)\n"
        "if _parent is None: raise ValueError('No parent COMP at ' + _parent_path)\n"
        # Constant CHOP — four named channels, default to opaque mid-grey.
        "_chop = _parent.create(td.constantCHOP, _prefix + '_uniform')\n"
        "for _i, _ch in enumerate(['x', 'y', 'z', 'w']):\n"
        "  getattr(_chop.par, 'name' + str(_i)).val = _ch\n"
        "  getattr(_chop.par, 'value' + str(_i)).val = 1.0 if _ch == 'w' else 0.5\n"
        # Text DAT with a starter shader that uses the uniform.
        "_dat = _parent.create(td.textDAT, _prefix + '_shader')\n"
        "_dat.text = _shader_src\n"
        # GLSL TOP wired to both.
        "_glsl = _parent.create(td.glslTOP, _prefix + '_glsl')\n"
        "_glsl.par.pixeldat = _dat.name\n"
        "_glsl.par.vec0name = _uname\n"
        "for _comp in ['x', 'y', 'z', 'w']:\n"
        "  _p = getattr(_glsl.par, 'vec0value' + _comp)\n"
        "  _p.expr = \"op('\" + _chop.name + \"')['\" + _comp + \"']\"\n"
        "  _p.mode = type(_p.mode).EXPRESSION\n"
        # Layout for legibility.
        "_chop.nodeX, _chop.nodeY = 0, 200\n"
        "_dat.nodeX,  _dat.nodeY  = 0, 50\n"
        "_glsl.nodeX, _glsl.nodeY = 300, 125\n"
        "_result = {'chop': _chop.path, 'shader_dat': _dat.path, "
        "'glsl_top': _glsl.path, 'uniform_name': _uname, "
        "'created': [_chop.path, _dat.path, _glsl.path]}"
    )


def _tmpl_audio_in_with_analyze(parent_path: str, prefix: str, opts: dict) -> str:
    """Audio Device In → Null, plus Audio Spectrum → Null. Reference the Nulls."""
    return (
        f"_parent_path = {_lit(parent_path)}\n"
        f"_prefix = {_lit(prefix)}\n"
        "_parent = op(_parent_path)\n"
        "if _parent is None: raise ValueError('No parent COMP at ' + _parent_path)\n"
        "_audio = _parent.create(td.audiodeviceinCHOP, _prefix + '_audio_src')\n"
        "_audio_null = _parent.create(td.nullCHOP, _prefix + '_audio_null')\n"
        "_audio.outputConnectors[0].connect(_audio_null.inputConnectors[0])\n"
        "_spec = _parent.create(td.audiospectrumCHOP, _prefix + '_spectrum')\n"
        "_audio_null.outputConnectors[0].connect(_spec.inputConnectors[0])\n"
        "_spec_null = _parent.create(td.nullCHOP, _prefix + '_spectrum_null')\n"
        "_spec.outputConnectors[0].connect(_spec_null.inputConnectors[0])\n"
        "_audio.nodeX, _audio.nodeY = 0, 0\n"
        "_audio_null.nodeX, _audio_null.nodeY = 200, 0\n"
        "_spec.nodeX, _spec.nodeY = 400, 0\n"
        "_spec_null.nodeX, _spec_null.nodeY = 600, 0\n"
        "_result = {'audio_in': _audio.path, 'audio_null': _audio_null.path, "
        "'spectrum': _spec.path, 'spectrum_null': _spec_null.path, "
        "'reference_time_via': _audio_null.path, "
        "'reference_freq_via': _spec_null.path, "
        "'created': [_audio.path, _audio_null.path, _spec.path, _spec_null.path]}"
    )


def _tmpl_feedback_loop_top(parent_path: str, prefix: str, opts: dict) -> str:
    """Source TOP → Composite TOP[0]; Feedback TOP → Composite TOP[1]; Feedback.top → Composite.

    Canonical trailing-frames pattern. The Feedback TOP samples the Composite's
    output from the previous frame, so each new source frame composites over a
    decayed history of itself.
    """
    source_type = str(opts.get("source_type", "noiseTOP"))
    return (
        f"_parent_path = {_lit(parent_path)}\n"
        f"_prefix = {_lit(prefix)}\n"
        f"_stype = {_lit(source_type)}\n"
        "_parent = op(_parent_path)\n"
        "if _parent is None: raise ValueError('No parent COMP at ' + _parent_path)\n"
        "_cls = getattr(td, _stype, None)\n"
        "if not isinstance(_cls, type): raise ValueError('Unknown TOP type: ' + _stype)\n"
        "_src = _parent.create(_cls, _prefix + '_src')\n"
        "_fb = _parent.create(td.feedbackTOP, _prefix + '_feedback')\n"
        "_comp = _parent.create(td.compositeTOP, _prefix + '_composite')\n"
        "_src.outputConnectors[0].connect(_comp.inputConnectors[0])\n"
        "_fb.outputConnectors[0].connect(_comp.inputConnectors[1])\n"
        # Feedback TOP samples the Composite's previous-frame output.
        "_fb.par.top = _comp.name\n"
        "_src.nodeX, _src.nodeY = 0, 100\n"
        "_fb.nodeX, _fb.nodeY = 0, -100\n"
        "_comp.nodeX, _comp.nodeY = 250, 0\n"
        "_result = {'source': _src.path, 'feedback': _fb.path, "
        "'composite': _comp.path, 'output': _comp.path, "
        "'created': [_src.path, _fb.path, _comp.path]}"
    )


def _tmpl_render_pipeline(parent_path: str, prefix: str, opts: dict) -> str:
    """Camera + Light + Geo (empty) + Render TOP. Minimal 3D scene."""
    return (
        f"_parent_path = {_lit(parent_path)}\n"
        f"_prefix = {_lit(prefix)}\n"
        "_parent = op(_parent_path)\n"
        "if _parent is None: raise ValueError('No parent COMP at ' + _parent_path)\n"
        "_cam = _parent.create(td.cameraCOMP, _prefix + '_camera')\n"
        "_light = _parent.create(td.lightCOMP, _prefix + '_light')\n"
        "_geo = _parent.create(td.geometryCOMP, _prefix + '_geo')\n"
        "_render = _parent.create(td.renderTOP, _prefix + '_render')\n"
        # Render TOP references its scene ops by path string.
        "_render.par.camera = _cam.path\n"
        "_render.par.lights = _light.path\n"
        "_render.par.geometry = _geo.path\n"
        "_cam.nodeX, _cam.nodeY = 0, 200\n"
        "_light.nodeX, _light.nodeY = 0, 0\n"
        "_geo.nodeX, _geo.nodeY = 0, -200\n"
        "_render.nodeX, _render.nodeY = 300, 0\n"
        "_result = {'camera': _cam.path, 'light': _light.path, "
        "'geo': _geo.path, 'render': _render.path, "
        "'note': 'Add SOPs inside the Geo COMP to make it visible.', "
        "'created': [_cam.path, _light.path, _geo.path, _render.path]}"
    )


TEMPLATES: dict[str, dict] = {
    "chop_source_with_null": {
        "description": (
            "Source CHOP followed by a Null CHOP. Reference the Null for "
            "stable channel names — source CHOPs (mouseIn, keyboardIn, audioIn, "
            "etc.) rename/prefix channels in non-obvious ways."
        ),
        "options": {
            "source_type": "TD class name of the source CHOP. Default 'mouseinCHOP'.",
        },
        "build": _tmpl_chop_source_with_null,
    },
    "glsl_top_vec4_uniform": {
        "description": (
            "GLSL TOP with a runtime-updatable vec4 uniform bound to a "
            "Constant CHOP via the Vectors page (NOT the Constants page, which "
            "is compile-time only and fails silently). Ships a starter "
            "fragment shader that outputs the uniform as a flat color."
        ),
        "options": {
            "uniform_name": "Shader uniform identifier (must be a valid GLSL name). Default 'uColor'.",
        },
        "build": _tmpl_glsl_top_vec4_uniform,
    },
    "audio_in_with_analyze": {
        "description": (
            "Audio Device In CHOP → Null, then Audio Spectrum CHOP → Null. "
            "Both Nulls give stable, inspectable endpoints — reference them "
            "instead of the source/spectrum directly. The audio Null is "
            "time-domain; the spectrum Null is frequency-domain."
        ),
        "options": {},
        "build": _tmpl_audio_in_with_analyze,
    },
    "feedback_loop_top": {
        "description": (
            "Canonical trailing-frames pattern: Source TOP → Composite[0], "
            "Feedback TOP → Composite[1], Feedback.top points back at the "
            "Composite. Each frame composites the new source over a decayed "
            "history of itself. Use the Composite as the visible output."
        ),
        "options": {
            "source_type": "TD class name of the source TOP. Default 'noiseTOP'.",
        },
        "build": _tmpl_feedback_loop_top,
    },
    "render_pipeline": {
        "description": (
            "Minimal 3D scene: Camera COMP + Light COMP + (empty) Geometry "
            "COMP + Render TOP wired together. The Geo COMP starts empty — "
            "drop SOPs inside it (or wire a SOP via its `Render` flag) to "
            "make something visible."
        ),
        "options": {},
        "build": _tmpl_render_pipeline,
    },
}


@mcp.tool()
async def list_templates() -> list[dict]:
    """List multi-op templates that can be instantiated with create_from_template.

    Each template bundles several ops wired correctly per the project's known
    gotchas, so the agent doesn't have to reconstruct them from scratch.
    """
    return [
        {"name": n, "description": t["description"], "options": t["options"]}
        for n, t in TEMPLATES.items()
    ]


@mcp.tool()
async def create_from_template(
    template: str,
    parent_path: str,
    name_prefix: str,
    options: dict | None = None,
    instance: str | None = None,
) -> dict:
    """Instantiate a named template under `parent_path`.

    Args:
        template:    Template name from `list_templates`.
        parent_path: COMP to create the ops in (e.g. '/project1').
        name_prefix: Prefix applied to every op created (e.g. 'fx1' →
                     'fx1_src', 'fx1_null'). Must be unique within the parent.
        options:     Template-specific options. See `list_templates`.
    Returns a dict with `created` (list of paths) plus per-template fields.
    """
    if template not in TEMPLATES:
        raise ValueError(
            f"Unknown template {template!r}. Available: {sorted(TEMPLATES)}"
        )
    if not name_prefix or not name_prefix.replace("_", "").isalnum():
        raise ValueError(
            f"name_prefix {name_prefix!r} must be alphanumeric (underscores ok) "
            "and non-empty — it's used as a TD op name component."
        )
    code = TEMPLATES[template]["build"](parent_path, name_prefix, options or {})
    return await _td_call(code, instance=instance)


@mcp.tool()
async def build_network(
    parent_path: str,
    operators: list[dict],
    connections: list[dict] | None = None,
    instance: str | None = None,
) -> dict:
    """Create, wire, parameterize, and lay out many operators in ONE call.

    Collapses what would otherwise be dozens of create_operator /
    connect_operators / set_parameter / move_operator round-trips into a single
    main-thread exec. Every MCP round-trip costs ~one TD frame *plus* a full
    model turn, so prefer this (or create_from_template) whenever you're
    building more than two or three ops at once.

    Args:
        parent_path: COMP to build in, e.g. '/project1'.
        operators: list of dicts, each:
            {'name': str,              # unique within the parent
             'type': str,              # TD class, e.g. 'noiseTOP', 'transformTOP'
             'params': {name: value},  # optional; LOWERCASE internal names as
                                        #   returned by list_parameters (e.g.
                                        #   'brightness1', NOT 'Brightness1')
             'x': float, 'y': float}    # optional tile position
        connections: list of dicts, each:
            {'from': str,   # op name in THIS batch, or a full path
             'to': str,     # op name in THIS batch, or a full path
             'out': int,    # source output index (default 0)
             'in': int}     # target input index (default 0)

    Ops are created first (so connections can reference them by name), then
    params are set, then wiring. Failures are collected per-item instead of
    aborting the whole batch. Returns
    {'created': [paths], 'connected': [...], 'errors': [...]} — always check
    `errors`.
    """
    code = (
        f"_parent = op({_lit(parent_path)})\n"
        f"if _parent is None: raise ValueError('No parent COMP at ' + {_lit(parent_path)})\n"
        f"_ops_spec = {_lit(operators)}\n"
        f"_conns_spec = {_lit(connections or [])}\n"
        "_made, _created, _errors = {}, [], []\n"
        "for _s in _ops_spec:\n"
        "  try:\n"
        "    _nm, _ty = _s['name'], _s['type']\n"
        "    _cls = getattr(td, _ty, None)\n"
        "    if not isinstance(_cls, type): raise ValueError('unknown op type ' + str(_ty))\n"
        "    _o = _parent.create(_cls, _nm)\n"
        "    _made[_nm] = _o; _created.append(_o.path)\n"
        "    for _pk, _pv in (_s.get('params') or {}).items():\n"
        "      try: getattr(_o.par, _pk).val = _pv\n"
        "      except Exception as _pe: _errors.append('param ' + _nm + '.' + str(_pk) + ': ' + str(_pe))\n"
        "    if 'x' in _s: _o.nodeX = float(_s['x'])\n"
        "    if 'y' in _s: _o.nodeY = float(_s['y'])\n"
        "  except Exception as _e:\n"
        "    _errors.append('create ' + str(_s.get('name')) + ': ' + str(_e))\n"
        "def _resolve(_r):\n"
        "  return _made[_r] if _r in _made else op(_r)\n"
        "_connected = []\n"
        "for _c in _conns_spec:\n"
        "  try:\n"
        "    _src, _dst = _resolve(_c['from']), _resolve(_c['to'])\n"
        "    if _src is None or _dst is None: raise ValueError('unresolved endpoint')\n"
        "    _src.outputConnectors[int(_c.get('out', 0))].connect(_dst.inputConnectors[int(_c.get('in', 0))])\n"
        "    _connected.append(_src.path + ' -> ' + _dst.path)\n"
        "  except Exception as _e:\n"
        "    _errors.append('connect ' + str(_c.get('from')) + '->' + str(_c.get('to')) + ': ' + str(_e))\n"
        "_result = {'created': _created, 'connected': _connected, 'errors': _errors}"
    )
    return await _td_call(code, instance=instance)


# ─── visual capture ──────────────────────────────────────────────────────────

@mcp.tool()
async def screenshot_op(
    path: str,
    max_edge: int = 512,
    instance: str | None = None,
) -> Image:
    """Grab the current frame of a TOP and return it as an inline PNG.

    Nothing is written to disk — the bytes are encoded on TD's main thread
    with a tiny pure-numpy PNG writer (TD's bundled Python has numpy but
    NOT Pillow) and returned in the MCP response.

    Args:
        path:     full path to a TOP (e.g. '/project1/render1').
        max_edge: long-edge cap in pixels for nearest-neighbor decimation.
                  Pass 0 for native resolution (may be large).
    """
    cap = max(0, int(max_edge))
    code = (
        f"_path = {_lit(path)}\n"
        f"_cap = {cap}\n"
        "import base64, struct, zlib\n"
        "import numpy as np\n"
        "_o = op(_path)\n"
        "if _o is None: raise ValueError('No op at ' + _path)\n"
        "if not hasattr(_o, 'numpyArray'):\n"
        "  raise TypeError(_path + ' is not a TOP (no numpyArray)')\n"
        "_arr = _o.numpyArray(delayed=False)\n"
        "if _arr is None: raise RuntimeError('TOP returned no pixels (not cooked yet?)')\n"
        "if _arr.ndim == 2: _arr = _arr[:, :, None]\n"
        "_arr = np.flipud(_arr)\n"
        "_h, _w = _arr.shape[:2]\n"
        "if _cap > 0 and max(_h, _w) > _cap:\n"
        "  _stride = max(1, int(round(max(_h, _w) / float(_cap))))\n"
        "  _arr = _arr[::_stride, ::_stride]\n"
        "  _h, _w = _arr.shape[:2]\n"
        "_u8 = (np.clip(_arr, 0.0, 1.0) * 255.0 + 0.5).astype('uint8')\n"
        "_ch = _u8.shape[2]\n"
        # PNG color type: 0=gray, 2=rgb, 4=gray+alpha, 6=rgba.
        "if _ch == 1: _ct = 0; _data = _u8\n"
        "elif _ch == 2: _ct = 4; _data = _u8\n"
        "elif _ch == 3: _ct = 2; _data = _u8\n"
        "else: _ct = 6; _data = np.ascontiguousarray(_u8[:, :, :4])\n"
        # Prefix each row with a filter byte (0 = None) — vectorized to avoid
        # per-row Python overhead on large images.
        "_rows = _data.reshape(_h, -1)\n"
        "_filtered = np.concatenate([np.zeros((_h, 1), dtype='uint8'), _rows], axis=1)\n"
        "_raw = _filtered.tobytes()\n"
        "def _chunk(t, d):\n"
        "  return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)\n"
        "_ihdr = struct.pack('>IIBBBBB', _w, _h, 8, _ct, 0, 0, 0)\n"
        "_idat = zlib.compress(_raw, 6)\n"
        "_png = b'\\x89PNG\\r\\n\\x1a\\n' + _chunk(b'IHDR', _ihdr) + _chunk(b'IDAT', _idat) + _chunk(b'IEND', b'')\n"
        "_result = {'b64': base64.b64encode(_png).decode('ascii'), "
        "'format': 'png', 'width': _w, 'height': _h}\n"
        # The base64 PNG is an intentional large payload — opt out of the bridge
        # size cap, which would truncate it and corrupt the image (see
        # webserver_callbacks._jsonable). A 512px screenshot is ~240KB of base64.
        "_no_clip = True"
    )
    payload = await _td_call(code, instance=instance)
    return Image(data=base64.b64decode(payload["b64"]), format=payload["format"])


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
