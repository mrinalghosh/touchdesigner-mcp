# Changelog

Reverse-chronological log of notable changes. One or two lines per entry.

## Unreleased

- **Three more templates: `audio_in_with_analyze`, `feedback_loop_top`, `render_pipeline`.** Audio template wires Audio Device In → Null and Audio Spectrum → Null so both time- and frequency-domain endpoints are stable. Feedback template wires the canonical Source → Composite[0] / Feedback → Composite[1] / Feedback.top → Composite trailing-frames pattern. Render template lays down Camera + Light + (empty) Geo + Render TOP with par.camera/lights/geometry pre-set.
- **MCPB bundle.** `scripts/build_mcpb.sh` now also ships `td_component/touchdesigner_mcp.tox` + callbacks inside the .mcpb so both halves install from one file. Manifest `command` switched from `python` to `python3` for the common macOS case where `python` isn't on PATH.
- **Fix screenshot_op and ParMode against real TD.** TD's bundled Python has no Pillow → replaced PIL with a pure-numpy PNG encoder (vectorized row-filter). TD's `ParMode` enum isn't on the `td` module → grab the class off an existing parameter via `type(par.mode).EXPRESSION`. Smoke test also now surfaces the HTTP 500 body so TD-side tracebacks are visible.
- **create_from_template + list_templates tools** — multi-op recipes that encode CLAUDE.md gotchas as one-call primitives. Ships with `chop_source_with_null` (source CHOP + Null) and `glsl_top_vec4_uniform` (GLSL TOP + Constant CHOP + Text DAT wired via the Vectors page).
- **bind_parameter_expression tool** — set a parameter to Expression mode with verification. Returns the evaluated value plus any direct exception *and* any new op-level error TD logged, so silently-broken expressions can't slip through.
- **screenshot_op tool** — capture a TOP's current frame as an inline MCP `Image` (default 512px JPEG, `full_resolution=True` for native PNG). Encodes in-memory on TD's main thread; nothing hits disk. Handles mono/RG/RGB/RGBA TOPs and old + new Pillow Resampling APIs.
- **Smoke test for the screenshot encode path** — exercises `numpyArray → PIL → JPEG` against a throwaway `constantTOP` to catch missing Pillow/numpy in TD's Python.

## Prior history

- **Bundled `.tox` install** ([ef85ed8], [3b4fd22], [ffaca2e]) — TD-side bridge ships as a drag-and-drop component, documented as the primary install path.
- **CLAUDE.md gotchas** ([7f48c55]) — lowercase param names, GLSL Vectors-page uniforms, Null-CHOP-before-reference rule.
- **Structured bridge errors** ([8361052]) — distinguish "TD unreachable" vs. "TD timed out cooking" vs. "TD raised", each with its own remedy.
- **Webserver textport logging** ([43c5c22]) — every callback request/response prints to TD's textport for debugging.
- **README setup + usage** ([5511c08]).
- **TD Python API introspection tools** ([7ba134a]) — `get_td_info`, `get_td_classes`, `get_td_class_details`, `get_module_help` so the agent discovers the API at runtime instead of guessing.
- **Initial MCP server** ([8234382]) — node lifecycle, parameters, wiring, query, arbitrary `exec_python` / `eval_python` over an HTTP bridge to the Web Server DAT.

[ef85ed8]: https://github.com/mrinalghosh/touchdesigner-mcp/commit/ef85ed8
[3b4fd22]: https://github.com/mrinalghosh/touchdesigner-mcp/commit/3b4fd22
[ffaca2e]: https://github.com/mrinalghosh/touchdesigner-mcp/commit/ffaca2e
[7f48c55]: https://github.com/mrinalghosh/touchdesigner-mcp/commit/7f48c55
[8361052]: https://github.com/mrinalghosh/touchdesigner-mcp/commit/8361052
[43c5c22]: https://github.com/mrinalghosh/touchdesigner-mcp/commit/43c5c22
[5511c08]: https://github.com/mrinalghosh/touchdesigner-mcp/commit/5511c08
[7ba134a]: https://github.com/mrinalghosh/touchdesigner-mcp/commit/7ba134a
[8234382]: https://github.com/mrinalghosh/touchdesigner-mcp/commit/8234382
