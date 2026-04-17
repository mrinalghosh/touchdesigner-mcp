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

import httpx
from mcp.server.fastmcp import FastMCP


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

mcp = FastMCP("touchdesigner")


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
    name: str, instance: str | None = None
) -> str:
    """Return the `help()` output for a TD Python name.

    `name` can be a bare identifier on the `td` module ('TOP', 'Par') or a
    dotted path resolvable in TD's namespace ('TOP.cook', 'op').
    """
    code = (
        f"import io, contextlib\n"
        f"_name = {_lit(name)}\n"
        f"_obj = getattr(td, _name, None)\n"
        f"if _obj is None:\n"
        f"  try: _obj = eval(_name, {{'td': td, 'op': op, 'project': project, 'app': app, 'ui': ui}})\n"
        f"  except Exception: _obj = _name\n"
        f"_buf = io.StringIO()\n"
        f"with contextlib.redirect_stdout(_buf):\n"
        f"  help(_obj)\n"
        f"_result = _buf.getvalue()"
    )
    return await _td_call(code, instance=instance)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
