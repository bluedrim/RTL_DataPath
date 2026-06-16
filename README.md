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
- If multiple TOP candidates are found, the tool stops and prints candidate names with `--top` command examples.
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
- TOP labels use a larger horizontally centered font; intermediate labels are horizontally centered, and leaf labels are centered horizontally and vertically.

Outputs:

- `output/<TOPNAME>_blockdiagrm.dot`: Graphviz DOT source using nested clusters.
- `output/<TOPNAME>_blockdiagrm.png`: generated when Graphviz `dot` is installed.
- `output/<TOPNAME>_blockdiagrm.excalidraw`: editable Excalidraw-compatible block diagram.
- `output/<TOPNAME>_blockdiagrm.vdx`: Visio XML Drawing block diagram.

Output paths can be overridden:

```bash
python3 RTL_blockdiagram.py ./rte/filelist.f 3 \
  --out hierarchy.dot \
  --png hierarchy.png \
  --excalidraw hierarchy.excalidraw \
  --visio hierarchy.vdx
```

## Filelist Support

- Verilog/SystemVerilog file paths: `.v`, `.sv`, `.vh`, `.svh`
- Nested filelists through `-f <filelist>`, `-F <filelist>`, `-ffilelist`, `-Ffilelist`, or plain `.f`, `.flist`, `.lst`, `.list` entries.
- Relative paths are resolved from the filelist that contains each entry.
- Environment variables in paths are expanded: `$VAR`, `${VAR}`, `$(VAR)`, and `~`.
- Repeated filelists and repeated RTL files are processed only once.
- `+incdir+...` entries are accepted and ignored during parsing.
- `-v <file>`
- Comments and blank lines

Complex macro-heavy or `generate`-heavy RTL may not be parsed perfectly because the tool uses a lightweight static parser.

## DataPath Trace

`RTL_datapath_trace.py` traces a likely datapath from a hierarchical signal path.

```bash
python3 RTL_datapath_trace.py ./rte/filelist.f tb_top.top.i_data
```

You can also pass a signal file with one signal per line:

```bash
python3 RTL_datapath_trace.py ./rte/filelist.f --signal signals.txt --output traces.txt
```

Trace behavior:

- Follows named instance port connections, continuous `assign`, and simple procedural assignments.
- Continues through renames such as `assign stage_data = i_data`.
- Supports output-port tracing. If the start signal is an `output`, `auto` mode traces backward from output to its source signals.
- Reverse output tracing continues through chained assignments such as `a <= b`, `b <= c`, and `c <= d`.
- Stops and prints a stop record when the signal enters conditional logic such as `if`, `case`, or a conditional procedural assignment.
- Selects `MAIN` by preferring paths that reach output ports for forward traces or input ports for reverse traces.
- Prints path summaries as `[PATH_0]`, `[PATH_1]`, and so on; console output also prints `MAIN` and `LONGEST` at the end.
- Wraps the whole trace with a title fold marker: `//{{{<start_signal>` through the final `//}}}`.
- Wraps each path block with `//{{{ <final_signal>` and `//}}}` fold markers.
- Writes the path-only summary to `--output` or `trace_<signal>.txt`.
- If `--signal <file>` is used, traces every listed signal in order and writes all results to `--output` or `trace_<signal_file_stem>.txt`.
- Missing signals are reported as `[ERROR] <signal>: ...` and the command exits non-zero after writing the output file.
- Writes `rtl_datapath_trace.excalidraw`, showing the main path with arrows and signal rename labels.

Options:

- `--top <module>`: trace from the specified root module. If omitted, the tracer uses the first signal-path token when it matches a parsed module, otherwise the inferred root.
- `--signal <path>`: read one signal per line from a file instead of using a positional single signal.
- `--excalidraw <path>`: choose the Excalidraw output path.
- `--output <path>` or `-o <path>`: choose the text output file.
- `--direction auto|forward|reverse`: choose trace direction. `auto` traces output ports backward and other signals forward.
- `--max-steps <N>`: cap trace expansion.

The tracer currently focuses on named port connections and lightweight expression matching.
