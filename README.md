# RTL DataPath

Generate editable RTL hierarchy block diagrams from a Verilog/SystemVerilog filelist.

`rtl_datapath_visualizer.py` reads an `rte`-style `.f` filelist, infers the TOP module, and draws the module hierarchy below TOP. A numeric depth controls how many instance levels are shown.

## Usage

```bash
python3 rtl_datapath_visualizer.py ./rte/filelist.f 3
```

Depth rules:

- `3`: draw modules up to 3 instance levels below TOP.
- `0`: draw every parsed module reachable from TOP.
- `--top <module>`: override automatic TOP detection.

Outputs:

- `rtl_datapath.dot`: Graphviz DOT source.
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
