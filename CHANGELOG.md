# Changelog

Reverse-chronological log of notable changes. One or two lines per entry.

## Unreleased

## 0.2.0 — 2026-07-19

Context-and-round-trip reduction pass (branch `perf/reduce-mcp-context-and-roundtrips`). The bridge is frame-locked (~17ms/call); the real cost was many sequential tool calls and large results bloating context. This release attacks both, plus fixes three latent serialization bugs.

- **Re-saved bundled `.tox`.** The embedded Web Server DAT callbacks now match `webserver_callbacks.py` — the size cap, the screenshot cap-exemption, and the numpy fix all ship live in the drag-and-drop component instead of requiring a manual re-paste.
- **Serialize numpy scalars/arrays as numbers, not `str()`.** `np.int64`/`np.float32`/`np.bool_` aren't Python `int`/`float`/`bool`, so they fell through to the `str(v)` fallback and serialized as an opaque `'5'` instead of `5` — silent type corruption for the common case of returning a channel value or a `numpyArray()` element. Now duck-typed via `tolist()` (+`dtype`), so scalars → Python scalars and arrays → nested lists, with the depth/count caps still in force.
- **Emit valid Python for non-finite floats in `_lit`.** `repr(float('inf'))` is the bare name `'inf'`, a `NameError` when the generated code is `exec`'d on TD's main thread. `_lit` now rewrites `inf`/`-inf`/`nan` to `float(...)` calls and recurses through dict/list/tuple, so a non-finite value anywhere in a `build_network` payload is safe.
- **Exempt screenshots from the bridge size cap.** The string cap would clip a base64 PNG mid-payload and break client-side `base64.b64decode`. A tool returning an intentional large payload now sets `_no_clip = True` in the exec namespace; `_run_payload` serializes it with `_clip=False`. Truncation markers are ASCII so a clipped string never breaks an ASCII/base64 consumer.
- **`get_module_help` scalpel modes.** New `grep=` and `member=` params slice a class's help (a single member is ~70x smaller than the full dump, a grep ~14x) instead of always dumping ~40KB into context.
- **Ship the efficiency + gotcha doctrine as MCP server `instructions`.** The batch-don't-chatter guidance and TD gotchas now travel to every `.mcpb` user in any client, where repo-scoped CLAUDE.md and skills can't reach.
- **`build_network` tool** — batch create/wire/param/layout in a single main-thread exec, collapsing a many-POST network build (e.g. 6 ops + 5 wires + 3 params = 14 round-trips) into one. Per-item error collection; returns `{created, connected, errors}`.
- **Cap bridge result serialization size** — 500 items/level, 100KB/string, depth 6, each with an explicit truncation marker. Keeps an accidental `op('/').children` or dumped DAT table from landing megabytes in the model's context. The depth guard also doubles as a cycle/recursion safety.
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
