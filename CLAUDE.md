# TouchDesigner MCP — Agent Guidance

Rules for Claude sessions working against this repo's MCP bridge. These are hard-won gotchas; ignore them and things break silently.

## Parameter names are lowercase internal names

`mcp__touchdesigner__set_parameter` requires the **lowercase internal** param name as returned by `list_parameters` (e.g. `brightness1`, `gamma1`, `translatex`). **Do not** use the TitleCase form (`Brightness1`) — the tool docstring is misleading.

- The server does `getattr(op.par, param)` directly and it is case-sensitive.
- `Brightness1` → `tdAttributeError: 'td.ParCollection' object has no attribute 'Brightness1'`.
- If unsure, call `list_parameters` first and copy the `name` field verbatim.
- If the user says "set Brightness to 0.6", translate to `brightness1` before calling.

## GLSL TOP uniforms: use the Vectors page, not Constants

For runtime-updatable uniforms in a GLSL TOP, use the **Vectors** page — the Constants page is compile-time `#define`-style only.

- Constants page symptom: `Warning: Uniform 'X' is not assigned. Please assign it on the Colors or Vectors page.` Shader compiles but the uniform is never supplied.
- Correct setup: declare `uniform vec4 uName;` in the shader, set `vec0name='uName'`, read as `uName.x` (or `.xy`, `.xyz`, `.xyzw`).
- Bind `vec0valuex` (etc.) via expression to the CHOP channel.
- Scalars aren't supported as runtime uniforms — wrap them in a vec4.
- Colors page works the same way for vec4 color values.

## Insert a Null CHOP after source CHOPs

When wiring CHOP references into expressions, parameters, or other ops, place a **Null CHOP** between the source CHOP and whatever consumes its channels. Reference the Null, not the source.

- Source CHOPs like `mouseIn`, `keyboardIn`, `audioIn`, `oscIn`, `midiIn` prefix/rename channels in non-obvious ways, which breaks naive references like `op('mouseIn1')['tx']`.
- The MCP cannot diagnose these failures from the source CHOP alone — the Null gives a stable, inspectable endpoint.
- After creating the Null, verify channel names (e.g. `list_children` / inspect it) before writing the downstream expression.

## Docked helper ops don't follow programmatic moves

Some ops auto-spawn **docked** companion ops when created. A GLSL TOP spawns `<name>_pixel` (your fragment shader), `<name>_info`, and `<name>_compute` DATs, docked directly beneath it. TD only drags docked ops along when you move the host **in the network editor UI** — setting `nodeX`/`nodeY` from Python moves the host alone and strands the companions at their spawn coordinates, leaving a long dock-tether line reaching back across the network.

- `move_operator` and `build_network` already re-fan a host's docked ops into a clean row beneath it after placing it — prefer them over raw `nodeX`/`nodeY` writes.
- `tidy_docked(path, recursive=...)` re-gathers stranded companions in a network that's already sprawled — pass a single op or a COMP to sweep.
- If you set positions yourself inside an `exec_python` batch, fan the docked ops too: for each `d in host.docked`, set `d.nodeX`/`d.nodeY` relative to the host (put the `_pixel` shader directly below). Otherwise you reintroduce the sprawl.
