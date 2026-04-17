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


def _jsonable(v, _depth=0):
    """Best-effort convert TD objects / arbitrary values to JSON-safe types."""
    if _depth > 6:
        return str(v)
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(val, _depth + 1) for k, val in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        return [_jsonable(x, _depth + 1) for x in v]
    path = getattr(v, "path", None)
    if isinstance(path, str):
        return {
            "__td_op__": path,
            "type": getattr(v, "OPType", type(v).__name__),
            "name": getattr(v, "name", None),
        }
    return str(v)


def _build_namespace():
    # Intentionally NOT exposing `ops` — in a Web Server DAT callback, `ops` is
    # bound to `me.ops` and searches relative to the DAT, which is surprising.
    # Use `op('/abs/path')` or `root.findChildren(...)` instead.
    return {
        "op": op, "td": td,
        "parent": parent, "root": root, "me": me,
        "project": project, "app": app, "ui": ui,
        "_result": None,
    }


def _run_payload(payload):
    code = payload.get("code", "")
    mode = payload.get("mode", "exec")
    ns = _build_namespace()
    try:
        if mode == "eval":
            value = eval(code, ns)
        else:
            exec(code, ns)
            value = ns.get("_result")
        return {"ok": True, "result": _jsonable(value)}
    except Exception as e:
        return {
            "ok": False,
            "error": "{}: {}".format(type(e).__name__, e),
            "traceback": traceback.format_exc(),
        }


def onHTTPRequest(webServerDAT, request, response):
    method = (request.get("method") or "").upper()
    uri = request.get("uri") or ""

    if method != "POST" or not uri.startswith("/mcp"):
        response["statusCode"] = 404
        response["statusReason"] = "Not Found"
        response["data"] = json.dumps({"ok": False, "error": "expected POST /mcp"})
        return response

    raw = request.get("data") or ""
    try:
        payload = json.loads(raw) if raw else {}
    except Exception as e:
        response["statusCode"] = 400
        response["statusReason"] = "Bad Request"
        response["data"] = json.dumps({"ok": False, "error": "bad JSON: {}".format(e)})
        return response

    result = _run_payload(payload)
    response["statusCode"] = 200 if result["ok"] else 500
    response["statusReason"] = "OK" if result["ok"] else "Internal Server Error"
    response["content-type"] = "application/json"
    response["data"] = json.dumps(result)
    return response


def onWebSocketOpen(webServerDAT, client, uri):
    return


def onWebSocketClose(webServerDAT, client):
    return


def onWebSocketReceiveText(webServerDAT, client, data):
    return


def onWebSocketReceiveBinary(webServerDAT, client, data):
    return


def onWebSocketReceivePing(webServerDAT, client, data):
    return


def onServerStart(webServerDAT):
    return


def onServerStop(webServerDAT):
    return
