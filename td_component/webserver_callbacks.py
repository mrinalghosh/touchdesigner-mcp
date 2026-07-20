# TouchDesigner Web Server DAT callbacks — MCP bridge.
#
# ONE-TIME SETUP (GUI):
#   1. In TouchDesigner, open the project you want Claude to control.
#   2. In /project1 (or any persistent COMP), add a Web Server DAT.
#      Suggested name: mcp_webserver
#   3. On the Web Server DAT parameters:
#          Port     = 9980
#          Active   = On
#   4. Its "Callbacks DAT" parameter points at a Text DAT (auto-created).
#      Replace that Text DAT's contents with this file.
#   5. Save the .toe. The server is now listening on http://127.0.0.1:9980/mcp
#
# VERIFY FROM A TERMINAL:
#   curl -s -X POST http://127.0.0.1:9980/mcp \
#        -H 'content-type: application/json' \
#        -d '{"code": "_result = app.version", "mode": "exec"}'
#   Expected: {"ok": true, "result": "2023.xxxxx"}
#
# PROTOCOL:
#   POST /mcp   body: {"code": "<python>", "mode": "exec"|"eval"}
#     exec: runs code; assign to `_result` to return a value.
#     eval: evaluates the expression; its value is returned.
#   Response JSON: {"ok": bool, "result": <any>, "error": str, "traceback": str}
#
# SECURITY: this endpoint executes arbitrary Python against your live project.
# Only bind to 127.0.0.1 and never expose the port to the open internet.

import json
import traceback

# Serialization caps. The bridge is fast (~one TD frame per call), but a naive
# `_result = op('/').children` or a dumped DAT table can return megabytes that
# land straight in the model's context window and slow every later turn. Cap
# collection *counts* aggressively and string *length* generously (so an
# intentional large string like get_module_help's 42KB still passes), and
# always leave an explicit truncation marker so the caller knows it's partial.
_MAX_DEPTH = 6
_MAX_ITEMS = 500        # elements per list/dict before truncation
_MAX_STR = 100_000      # chars per string before truncation


def _clip_str(s):
    if len(s) > _MAX_STR:
        # ASCII marker only: a non-ASCII byte here would break any consumer that
        # treats a truncated string as ASCII/base64 (e.g. base64.b64decode).
        return s[:_MAX_STR] + "...[+{} chars truncated]".format(len(s) - _MAX_STR)
    return s


def _jsonable(v, _depth=0, _clip=True):
    """Best-effort convert TD objects / arbitrary values to JSON-safe types.

    With `_clip` True (default), collection counts and string lengths are capped
    so an accidental megabyte dump can't land in the model's context. A tool that
    returns an *intentional* large payload — e.g. screenshot_op's base64 PNG,
    which truncation would corrupt — sets `_no_clip = True` in the exec namespace;
    _run_payload then calls this with `_clip=False` to pass it through intact.
    The depth guard always applies (it prevents runaway recursion on cycles).
    """
    if _depth > _MAX_DEPTH:
        return _clip_str(str(v)) if _clip else str(v)
    if v is None or isinstance(v, (int, float, bool)):
        return v
    if isinstance(v, str):
        return _clip_str(v) if _clip else v
    if isinstance(v, dict):
        out = {}
        for i, (k, val) in enumerate(v.items()):
            if _clip and i >= _MAX_ITEMS:
                out["__truncated__"] = "dict had {} keys; showing {}".format(
                    len(v), _MAX_ITEMS
                )
                break
            out[str(k)] = _jsonable(val, _depth + 1, _clip)
        return out
    if isinstance(v, (list, tuple, set, frozenset)):
        items = list(v)
        shown = items[:_MAX_ITEMS] if _clip else items
        out = [_jsonable(x, _depth + 1, _clip) for x in shown]
        if _clip and len(items) > _MAX_ITEMS:
            out.append("...[+{} items truncated]".format(len(items) - _MAX_ITEMS))
        return out
    # numpy scalars/arrays (TD ships numpy): np.int64 / np.float32 / np.bool_ are
    # NOT Python int/float/bool, so without this they'd hit the str(v) fallback
    # below and serialize as an opaque '5' instead of the number 5 — a silent
    # type corruption for the very common case of returning a channel value or a
    # numpyArray() element. tolist() maps a scalar -> Python scalar and an
    # ndarray -> nested list; recursing keeps the depth/count caps in force.
    # (np.float64 already passes as a Python float subclass and never reaches here.)
    _tolist = getattr(v, "tolist", None)
    if callable(_tolist) and hasattr(v, "dtype"):
        try:
            return _jsonable(_tolist(), _depth, _clip)
        except Exception:
            pass
    path = getattr(v, "path", None)
    if isinstance(path, str):
        return {
            "__td_op__": path,
            "type": getattr(v, "OPType", type(v).__name__),
            "name": getattr(v, "name", None),
        }
    return _clip_str(str(v)) if _clip else str(v)


def _build_namespace():
    # Intentionally NOT exposing `ops` — in a Web Server DAT callback, `ops` is
    # bound to `me.ops` and searches relative to the DAT, which is surprising.
    # Use `op('/abs/path')` or `root.findChildren(...)` instead.
    return {
        "op": op, "td": td,
        "parent": parent, "root": root, "me": me,
        "project": project, "app": app, "ui": ui,
        "_result": None,
        # Set to True in exec code to return _result uncapped (see _jsonable).
        # Only for intentional large payloads like a base64 image.
        "_no_clip": False,
    }


def _run_payload(payload):
    code = payload.get("code", "")
    mode = payload.get("mode", "exec")
    ns = _build_namespace()
    try:
        if mode == "eval":
            value = eval(code, ns)
            no_clip = False  # eval is a bare expression; no way to signal opt-out
        else:
            exec(code, ns)
            value = ns.get("_result")
            no_clip = bool(ns.get("_no_clip"))
        return {"ok": True, "result": _jsonable(value, _clip=not no_clip)}
    except Exception as e:
        return {
            "ok": False,
            "error": "{}: {}".format(type(e).__name__, e),
            "traceback": traceback.format_exc(),
        }


def _log(tag, msg=""):
    print("[mcp-webserver] {} {}".format(tag, msg).rstrip())


def onHTTPRequest(webServerDAT, request, response):
    method = (request.get("method") or "").upper()
    uri = request.get("uri") or ""
    _log("HTTP", "{} {}".format(method, uri))

    if method != "POST" or not uri.startswith("/mcp"):
        _log("HTTP 404", "{} {} (expected POST /mcp)".format(method, uri))
        response["statusCode"] = 404
        response["statusReason"] = "Not Found"
        response["data"] = json.dumps({"ok": False, "error": "expected POST /mcp"})
        return response

    raw = request.get("data") or ""
    try:
        payload = json.loads(raw) if raw else {}
    except Exception as e:
        _log("HTTP 400", "bad JSON: {}".format(e))
        response["statusCode"] = 400
        response["statusReason"] = "Bad Request"
        response["data"] = json.dumps({"ok": False, "error": "bad JSON: {}".format(e)})
        return response

    mode = payload.get("mode", "exec")
    code = payload.get("code", "")
    snippet = code if len(code) <= 200 else code[:200] + "... [+{} chars]".format(len(code) - 200)
    _log("RUN", "mode={} code={!r}".format(mode, snippet))

    result = _run_payload(payload)
    if result["ok"]:
        _log("OK", "result={!r}".format(result.get("result")))
    else:
        _log("ERR", result.get("error", ""))

    response["statusCode"] = 200 if result["ok"] else 500
    response["statusReason"] = "OK" if result["ok"] else "Internal Server Error"
    response["content-type"] = "application/json"
    response["data"] = json.dumps(result)
    return response


def onWebSocketOpen(webServerDAT, client, uri):
    _log("WS open", "{} uri={}".format(client, uri))
    return


def onWebSocketClose(webServerDAT, client):
    _log("WS close", str(client))
    return


def onWebSocketReceiveText(webServerDAT, client, data):
    _log("WS text", "{} {!r}".format(client, data))
    return


def onWebSocketReceiveBinary(webServerDAT, client, data):
    _log("WS binary", "{} {} bytes".format(client, len(data) if data is not None else 0))
    return


def onWebSocketReceivePing(webServerDAT, client, data):
    _log("WS ping", str(client))
    return


def onServerStart(webServerDAT):
    _log("server START", "listening (DAT={})".format(getattr(webServerDAT, "path", "?")))
    return


def onServerStop(webServerDAT):
    _log("server STOP", "DAT={}".format(getattr(webServerDAT, "path", "?")))
    return
