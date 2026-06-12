# RTL DataPath

Generate editable RTL hierarchy block diagrams from a Verilog/SystemVerilog filelist.

`rtl_datapath_visualizer.py` reads an `rte`-style `.f` filelist, infers the TOP module, and draws a nested containment diagram. TOP is the outermost block, direct child instances are placed inside it, and each deeper hierarchy level gets a slightly different color. A numeric depth controls how many instance levels are shown.

## Usage

```bash
python3 rtl_datapath_visualizer.py ./rte/filelist.f 3
```

Depth rules:

- `3`: draw modules up to 3 instance levels below TOP.
- `0`: draw every parsed module reachable from TOP.
- If `--top <module>` is not set, the diagram starts from the inferred full-design TOP.
- If `--top <module>` is set, the diagram starts from that module instead.

Example with an explicit root module:

```bash
python3 rtl_datapath_visualizer.py ./rte/filelist.f 2 --top subsystem_top
```

Label rules:

- By default, blocks show module names only.
- Use `--show-instances` to show instance names above module names.

Outputs:

- `rtl_datapath.dot`: Graphviz DOT source using nested clusters.
- `rtl_datapath.png`: generated when Graphviz `dot` is installed.
- `rtl_datapath.excalidraw`: editable Excalidraw-compatible block diagram.

Output paths can be overridden:

```bash
python3 rtl_datapath_visualizer.py ./rte/filelist.f 3 \
  --out hierarchy.dot \
  --png hierarchy.png \
  --excalidraw hierarchy.excalidraw
```

## Filelist Support

- Verilog/SystemVerilog file paths: `.v`, `.sv`, `.vh`, `.svh`
- `+incdir+...` entries are accepted and ignored during parsing.
- `-v <file>`
- Comments and blank lines

Complex macro-heavy or `generate`-heavy RTL may not be parsed perfectly because the tool uses a lightweight static parser.
