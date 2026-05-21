#!/usr/bin/env python3
"""Generate an RTL module hierarchy + datapath-highlight diagram from a filelist."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple


MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)")
ENDMODULE_RE = re.compile(r"\bendmodule\b")
INSTANCE_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\([^;]*?\))?\s+([A-Za-z_][A-Za-z0-9_$]*)\s*\(",
    re.S,
)
PORT_BLOCK_RE = re.compile(r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*\s*(?:#\s*\([^;]*?\))?\s*\((.*?)\)\s*;", re.S)
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
COMMENT_LINE_RE = re.compile(r"//.*?$", re.M)
COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.S)

DATAPATH_KEYWORDS = {
    "data",
    "datapath",
    "alu",
    "mac",
    "mul",
    "adder",
    "sum",
    "acc",
    "regfile",
    "fifo",
    "pipe",
    "execute",
    "decode",
    "memory",
    "mem",
    "vector",
    "lane",
}


@dataclass
class Module:
    name: str
    file: Path
    instances: List[Tuple[str, str]] = field(default_factory=list)  # (child_module, inst_name)
    ports: Set[str] = field(default_factory=set)


@dataclass
class Design:
    modules: Dict[str, Module]
    top: str


def strip_comments(text: str) -> str:
    text = COMMENT_BLOCK_RE.sub("", text)
    return COMMENT_LINE_RE.sub("", text)


def parse_filelist(path: Path) -> List[Path]:
    files: List[Path] = []
    base = path.parent

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if line.startswith("+incdir+"):
            continue
        if line.startswith("-y") or line.startswith("-v"):
            tokens = line.split(maxsplit=1)
            if len(tokens) == 2 and tokens[0] == "-v":
                candidate = (base / tokens[1]).resolve()
                if candidate.exists():
                    files.append(candidate)
            continue

        candidate = Path(line)
        if not candidate.is_absolute():
            candidate = (base / candidate).resolve()
        if candidate.suffix.lower() in {".v", ".sv", ".vh", ".svh"} and candidate.exists():
            files.append(candidate)

    return sorted(set(files))


def extract_modules(verilog: str, source: Path) -> Dict[str, Module]:
    modules: Dict[str, Module] = {}
    cleaned = strip_comments(verilog)

    starts = [m for m in MODULE_RE.finditer(cleaned)]
    ends = [m for m in ENDMODULE_RE.finditer(cleaned)]
    end_idx = 0

    for start in starts:
        while end_idx < len(ends) and ends[end_idx].start() < start.start():
            end_idx += 1
        if end_idx >= len(ends):
            break

        end = ends[end_idx]
        end_idx += 1
        block = cleaned[start.start() : end.end()]
        name = start.group(1)
        mod = Module(name=name, file=source)

        port_match = PORT_BLOCK_RE.search(block)
        if port_match:
            mod.ports = set(IDENT_RE.findall(port_match.group(1)))

        for inst in INSTANCE_RE.finditer(block):
            child, inst_name = inst.group(1), inst.group(2)
            if child in {"if", "for", "while", "case", "assign", "always", "module", "function", "task"}:
                continue
            mod.instances.append((child, inst_name))

        modules[name] = mod

    return modules


def infer_top(modules: Dict[str, Module]) -> str:
    all_mods = set(modules)
    children = {child for m in modules.values() for child, _ in m.instances if child in all_mods}
    tops = sorted(all_mods - children)
    if not tops:
        return sorted(all_mods)[0]
    return tops[0]


def is_datapath_name(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in DATAPATH_KEYWORDS)


def build_design(filelist: Path, explicit_top: str | None) -> Design:
    files = parse_filelist(filelist)
    if not files:
        raise ValueError(f"No Verilog/SystemVerilog files found in filelist: {filelist}")

    modules: Dict[str, Module] = {}
    for f in files:
        extracted = extract_modules(f.read_text(encoding="utf-8", errors="ignore"), f)
        modules.update(extracted)

    if not modules:
        raise ValueError("No module declarations found.")

    top = explicit_top or infer_top(modules)
    if top not in modules:
        raise ValueError(f"Top module '{top}' not found in parsed modules.")

    return Design(modules=modules, top=top)


def emit_dot(design: Design) -> str:
    lines = [
        "digraph RTL {",
        '  rankdir="LR";',
        '  graph [fontname="Helvetica"];',
        '  node [shape=box, style="rounded,filled", fillcolor="#ECEFF1", color="#607D8B", fontname="Helvetica"];',
        '  edge [color="#546E7A", fontname="Helvetica", fontsize=10];',
    ]

    for name, mod in sorted(design.modules.items()):
        if name == design.top:
            fill = "#BBDEFB"
            border = "#1565C0"
            penwidth = "2"
        elif is_datapath_name(name) or any(is_datapath_name(p) for p in mod.ports):
            fill = "#FFE0B2"
            border = "#EF6C00"
            penwidth = "2"
        else:
            fill = "#ECEFF1"
            border = "#607D8B"
            penwidth = "1"

        label = f"{name}\\n({mod.file.name})"
        lines.append(
            f'  "{name}" [label="{label}", fillcolor="{fill}", color="{border}", penwidth={penwidth}];'
        )

    known = set(design.modules)
    for parent, mod in sorted(design.modules.items()):
        for child, inst in mod.instances:
            if child not in known:
                continue
            edge_color = "#546E7A"
            penwidth = "1"
            if is_datapath_name(parent) or is_datapath_name(child) or is_datapath_name(inst):
                edge_color = "#D84315"
                penwidth = "2"
            lines.append(
                f'  "{parent}" -> "{child}" [label="{inst}", color="{edge_color}", penwidth={penwidth}];'
            )

    lines.append("}")
    return "\n".join(lines) + "\n"


def maybe_render_png(dot_file: Path, png_file: Path) -> bool:
    dot_bin = shutil.which("dot")
    if not dot_bin:
        return False
    subprocess.run([dot_bin, "-Tpng", str(dot_file), "-o", str(png_file)], check=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read an RTL filelist and generate module hierarchy/datapath-highlight diagram"
    )
    parser.add_argument("filelist", type=Path, help="Path to rte/filelist.f style file list")
    parser.add_argument("--top", type=str, default=None, help="Top module name (optional)")
    parser.add_argument("--out", type=Path, default=Path("rtl_datapath.dot"), help="Output .dot path")
    parser.add_argument("--png", type=Path, default=Path("rtl_datapath.png"), help="Optional png path")
    args = parser.parse_args()

    design = build_design(args.filelist, args.top)
    dot = emit_dot(design)
    args.out.write_text(dot, encoding="utf-8")

    print(f"[OK] DOT generated: {args.out}")
    print(f"[INFO] top module: {design.top}")
    print(f"[INFO] module count: {len(design.modules)}")

    try:
        rendered = maybe_render_png(args.out, args.png)
    except subprocess.CalledProcessError as exc:
        print(f"[WARN] dot rendering failed: {exc}")
        rendered = False

    if rendered:
        print(f"[OK] PNG generated: {args.png}")
    else:
        print("[WARN] graphviz 'dot' not available (or rendering failed). DOT file is ready.")


if __name__ == "__main__":
    main()
