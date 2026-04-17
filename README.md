# touchdesigner-mcp

Model Context Protocol (MCP) server that lets Claude drive a running TouchDesigner instance — create operators, wire them together, set parameters, run arbitrary Python, and introspect the `td` API.

## Architecture

```
Claude (MCP client)  ──stdio──►  touchdesigner-mcp (Python)  ──HTTP POST──►  Web Server DAT  ──►  td.run()  ──►  main-thread eval/exec
```

The MCP server is a thin stdio bridge. All TD mutation happens on TouchDesigner's main thread via a Web Server DAT callback that `exec`s or `eval`s the code you send.

## Prerequisites

- TouchDesigner (any recent 2022+ build — tools introspect the live API)
- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) or plain `pip`

## Install

```bash
git clone https://github.com/mrinalghosh/touchdesigner-mcp
cd touchdesigner-mcp
uv venv
source .venv/bin/activate
uv pip install -e .
```

This exposes a `touchdesigner-mcp` console script inside `.venv/bin/`.

## TouchDesigner-side setup (one-time, per .toe)

1. Open your project in TouchDesigner.
2. Inside `/project1` (or any persistent COMP) create a **Web Server DAT**. Suggested name: `mcp_webserver`.
3. On that DAT:
   - **Port** = `9980`
   - **Active** = On
4. The DAT auto-creates a Callbacks Text DAT. Replace its contents with [td_component/webserver_callbacks.py](td_component/webserver_callbacks.py).
5. Save the .toe.

Verify from a terminal:

```bash
curl -s -X POST http://127.0.0.1:9980/mcp \
  -H 'content-type: application/json' \
  -d '{"code": "_result = app.version", "mode": "exec"}'
# → {"ok": true, "result": "2023.xxxxx"}
```

Or run the packaged smoke test with TD open:

```bash
uv run python scripts/smoke_test.py
```

## MCP client configuration

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) — single-instance form:

```json
{
  "mcpServers": {
    "touchdesigner": {
      "command": "/absolute/path/to/touchdesigner-mcp/.venv/bin/touchdesigner-mcp",
      "env": {
        "TD_HOST": "127.0.0.1",
        "TD_PORT": "9980"
      }
    }
  }
}
```

Multi-instance form (target several TD processes from one Claude session):

```json
{
  "mcpServers": {
    "touchdesigner": {
      "command": "/absolute/path/to/touchdesigner-mcp/.venv/bin/touchdesigner-mcp",
      "env": {
        "TD_INSTANCES": "main=127.0.0.1:9980,fx=127.0.0.1:9981,stage=192.168.1.40:9980",
        "TD_DEFAULT_INSTANCE": "main"
      }
    }
  }
}
```

Restart Claude Desktop after editing.

### Claude Code CLI

```bash
claude mcp add touchdesigner /absolute/path/to/touchdesigner-mcp/.venv/bin/touchdesigner-mcp \
  --env TD_INSTANCES=main=127.0.0.1:9980,fx=127.0.0.1:9981 \
  --env TD_DEFAULT_INSTANCE=main
```

See [claude_desktop_config.example.json](claude_desktop_config.example.json) for both forms in one place.

## Environment variables

| Var                   | Default      | Purpose                                                                              |
| --------------------- | ------------ | ------------------------------------------------------------------------------------ |
| `TD_HOST`             | `127.0.0.1`  | Single-instance host                                                                 |
| `TD_PORT`             | `9980`       | Single-instance port                                                                 |
| `TD_PATH`             | `/mcp`       | HTTP path the Web Server DAT answers on                                              |
| `TD_INSTANCES`        | —            | Multi-instance map: `name=host:port[/path],...` (overrides the single-instance vars) |
| `TD_DEFAULT_INSTANCE` | first in map | Which instance unqualified tool calls target                                         |
| `TD_TIMEOUT`          | `10.0`       | HTTP timeout in seconds                                                              |

Per-instance path override: `TD_INSTANCES="main=127.0.0.1:9980/mcp,dev=127.0.0.1:9981/mcp-dev"`.

## Tools

Every tool accepts an optional `instance` argument to target a specific TD process. Omit it to hit `TD_DEFAULT_INSTANCE`.

**Meta**

- `list_instances` — show configured TD processes
- `ping` / `ping_all` — health check

**Arbitrary code**

- `exec_python(code)` — runs on TD's main thread; assign to `_result` to return a value
- `eval_python(expression)` — single-expression eval

**Node lifecycle**

- `create_operator(parent_path, op_type, name)`
- `delete_operator(path)`
- `rename_operator(path, new_name)`
- `move_operator(path, x, y)`

**Parameters**

- `set_parameter(path, param, value)`
- `get_parameter(path, param)`
- `list_parameters(path)`
- `pulse_parameter(path, param)`

**Wiring**

- `connect_operators(source_path, target_path, source_output=0, target_input=0)`
- `disconnect_input(path, input_index=0)`

**Query**

- `list_children(comp_path)`
- `find_operators(root_path='/', op_type=None, name_pattern=None, depth=4)`
- `get_errors(path='/', recurse=True)`

**Introspection** (lets the model discover the live `td` API rather than guessing)

- `get_td_info` — version, project, Python, platform
- `get_td_classes(name_contains=None)`
- `get_td_class_details(class_name)`
- `get_module_help(name)`

## Example prompts

> "Under `/project1`, create a Noise TOP called `n1` and a Level TOP called `lvl1`, wire n1 → lvl1, then set `lvl1.Brightness` to 0.6."

> "List every TOP under `/project1` and report which ones have errors."

> "Show me `td.noiseTOP`'s parameters so I know what I can tweak."

## Security

The Web Server DAT callback executes **arbitrary Python** against your live project. Bind it to `127.0.0.1` only, never expose the port to the internet, and don't run untrusted prompts against a TD instance with valuable state open.
