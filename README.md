# RTL DataPath

Generate editable RTL hierarchy block diagrams from a Verilog/SystemVerilog filelist.

`RTL_blockdiagram.py` reads an `rte`-style `.f` filelist, infers the TOP module, and draws a nested containment diagram. TOP is the outermost block, direct child instances are placed inside it, and each deeper hierarchy level gets a slightly different color. A numeric depth controls how many instance levels are shown.

## Usage

```bash
python3 RTL_blockdiagram.py ./rte/filelist.f 3
```

Depth rules:

- `3`: draw modules up to 3 instance levels below TOP.
- `0`: draw every parsed module reachable from TOP.
- If `--top <module>` is not set, the diagram starts from the inferred full-design TOP.
- If `--top <module>` is set, the diagram starts from that module instead.

Example with an explicit root module:

```bash
python3 RTL_blockdiagram.py ./rte/filelist.f 2 --top subsystem_top
```

Label rules:

- By default, blocks show module names only.
- Use `--show-instances` to show instance names above module names.

Layout rules:

- Child blocks are wrapped after 4 modules per row, then continue on the next row.
- Each next row starts below the tallest block in the previous row, with additional vertical spacing.

Outputs:

- `rtl_datapath.dot`: Graphviz DOT source using nested clusters.
- `rtl_datapath.png`: generated when Graphviz `dot` is installed.
- `rtl_datapath.excalidraw`: editable Excalidraw-compatible block diagram.
- `rtl_datapath.vsdx`: Visio-compatible block diagram.

Output paths can be overridden:

```bash
python3 RTL_blockdiagram.py ./rte/filelist.f 3 \
  --out hierarchy.dot \
  --png hierarchy.png \
  --excalidraw hierarchy.excalidraw \
  --visio hierarchy.vsdx
```

## Filelist Support

- Verilog/SystemVerilog file paths: `.v`, `.sv`, `.vh`, `.svh`
- `+incdir+...` entries are accepted and ignored during parsing.
- `-v <file>`
- Comments and blank lines

Complex macro-heavy or `generate`-heavy RTL may not be parsed perfectly because the tool uses a lightweight static parser.

## DataPath Trace

`RTL_datapath_trace.py` traces a likely datapath from a hierarchical signal path.

```bash
python3 RTL_datapath_trace.py ./rte/filelist.f tb_top.top.i_data
```

Trace behavior:

- Follows named instance port connections, continuous `assign`, and simple procedural assignments.
- Continues through renames such as `assign stage_data = i_data`.
- Stops and prints a stop record when the signal enters conditional logic such as `if`, `case`, or a conditional procedural assignment.
- Selects `MAIN` by preferring paths that reach output ports with signal names similar to the input.
- Prints path summaries as `[PATH_0]`, `[PATH_1]`, and so on; console output also prints `MAIN` and `LONGEST` at the end.
- Wraps each path block with `//{{{ <final_signal>` and `//}}}` fold markers.
- Writes the path-only summary to `trace_<input_signal>.txt`.
- Writes `rtl_datapath_trace.excalidraw`, showing the main path with arrows and signal rename labels.

Options:

- `--top <module>`: override the inferred root module.
- `--excalidraw <path>`: choose the Excalidraw output path.
- `--max-steps <N>`: cap trace expansion.

The tracer currently focuses on named port connections and lightweight expression matching.
