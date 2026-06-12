#!/usr/bin/env python3
"""Generate an RTL module hierarchy + datapath-highlight block diagram from a filelist."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


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
    top_candidates: List[str] = field(default_factory=list)


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


def reachable_module_depths(modules: Dict[str, Module], top: str, depth_limit: int = 0) -> Dict[str, int]:
    """Return known modules reachable from top with their minimum hierarchy depth.

    depth_limit == 0 means unlimited. A positive depth includes modules down to
    that many instance levels below the top module.
    """
    if depth_limit < 0:
        raise ValueError("depth_limit must be 0 or greater")
    if top not in modules:
        raise ValueError(f"Top module '{top}' not found in parsed modules.")

    unlimited = depth_limit == 0
    depths = {top: 0}
    queue = deque([(top, 0)])

    while queue:
        parent, depth = queue.popleft()
        if not unlimited and depth >= depth_limit:
            continue

        for child, _ in modules[parent].instances:
            if child not in modules:
                continue
            next_depth = depth + 1
            if child in depths and depths[child] <= next_depth:
                continue
            depths[child] = next_depth
            queue.append((child, next_depth))

    return depths


def infer_top_candidates(modules: Dict[str, Module]) -> List[str]:
    all_mods = set(modules)
    children = {child for m in modules.values() for child, _ in m.instances if child in all_mods}
    return sorted(all_mods - children)


def infer_top(modules: Dict[str, Module]) -> Tuple[str, List[str]]:
    candidates = infer_top_candidates(modules)
    if not candidates:
        fallback = sorted(modules)[0]
        return fallback, []

    def score(module_name: str) -> Tuple[int, str]:
        return (len(reachable_module_depths(modules, module_name, depth_limit=0)), module_name)

    return max(candidates, key=score), candidates


def is_datapath_name(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in DATAPATH_KEYWORDS)


def module_style(design: Design, name: str) -> Tuple[str, str, str]:
    mod = design.modules[name]
    if name == design.top:
        return "#BBDEFB", "#1565C0", "2"
    if is_datapath_name(name) or any(is_datapath_name(p) for p in mod.ports):
        return "#FFE0B2", "#EF6C00", "2"
    return "#ECEFF1", "#607D8B", "1"


def visible_module_names(visible_depths: Dict[str, int]) -> List[str]:
    return sorted(visible_depths, key=lambda n: (visible_depths[n], n))


def visible_edges(design: Design, visible_depths: Dict[str, int]) -> List[Tuple[str, str, str]]:
    visible_modules = set(visible_depths)
    edges: List[Tuple[str, str, str]] = []

    for parent in visible_module_names(visible_depths):
        mod = design.modules[parent]
        for child, inst in mod.instances:
            if child in visible_modules:
                edges.append((parent, child, inst))

    return edges


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

    top_candidates = infer_top_candidates(modules)
    top = explicit_top
    if not top:
        top, top_candidates = infer_top(modules)
    if top not in modules:
        raise ValueError(f"Top module '{top}' not found in parsed modules.")

    return Design(modules=modules, top=top, top_candidates=top_candidates)


def dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def emit_dot(design: Design, depth_limit: int = 0) -> str:
    visible_depths = reachable_module_depths(design.modules, design.top, depth_limit)

    lines = [
        "digraph RTL {",
        '  rankdir="LR";',
        '  graph [fontname="Helvetica"];',
        '  node [shape=box, style="rounded,filled", fillcolor="#ECEFF1", color="#607D8B", fontname="Helvetica"];',
        '  edge [color="#546E7A", fontname="Helvetica", fontsize=10];',
    ]

    for name in visible_module_names(visible_depths):
        mod = design.modules[name]
        fill, border, penwidth = module_style(design, name)

        label = dot_escape(f"{name}\n({mod.file.name})")
        lines.append(
            f'  "{name}" [label="{label}", fillcolor="{fill}", color="{border}", penwidth={penwidth}];'
        )

    for parent, child, inst in visible_edges(design, visible_depths):
        edge_color = "#546E7A"
        penwidth = "1"
        if is_datapath_name(parent) or is_datapath_name(child) or is_datapath_name(inst):
            edge_color = "#D84315"
            penwidth = "2"
        lines.append(
            f'  "{parent}" -> "{child}" [label="{dot_escape(inst)}", color="{edge_color}", penwidth={penwidth}];'
        )

    lines.append("}")
    return "\n".join(lines) + "\n"


def stable_int(*parts: str) -> int:
    digest = hashlib.sha1("::".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def excalidraw_base_element(element_id: str, element_type: str, x: float, y: float) -> Dict[str, Any]:
    seed = stable_int(element_id, "seed") % 2_147_483_647
    version_nonce = stable_int(element_id, "version") % 2_147_483_647
    return {
        "id": element_id,
        "type": element_type,
        "x": x,
        "y": y,
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": seed,
        "version": 1,
        "versionNonce": version_nonce,
        "isDeleted": False,
        "boundElements": None,
        "updated": 1,
        "link": None,
        "locked": False,
    }


def emit_excalidraw(design: Design, depth_limit: int = 0) -> str:
    visible_depths = reachable_module_depths(design.modules, design.top, depth_limit)
    modules_by_depth: Dict[int, List[str]] = {}
    for name, depth in visible_depths.items():
        modules_by_depth.setdefault(depth, []).append(name)

    node_width = 220
    node_height = 76
    x_gap = 330
    y_gap = 130
    x_origin = 60
    y_origin = 60

    positions: Dict[str, Tuple[float, float]] = {}
    elements: List[Dict[str, Any]] = []

    for depth in sorted(modules_by_depth):
        names = sorted(modules_by_depth[depth])
        column_height = (len(names) - 1) * y_gap
        y_offset = -column_height / 2 if depth == 0 else 0
        for index, name in enumerate(names):
            mod = design.modules[name]
            x = x_origin + depth * x_gap
            y = y_origin + y_offset + index * y_gap
            positions[name] = (x, y)
            fill, border, penwidth = module_style(design, name)
            rect_id = f"module-{stable_int(name, 'rect'):08x}"
            text_id = f"text-{stable_int(name, 'text'):08x}"

            rect = excalidraw_base_element(rect_id, "rectangle", x, y)
            rect.update(
                {
                    "width": node_width,
                    "height": node_height,
                    "strokeColor": border,
                    "backgroundColor": fill,
                    "strokeWidth": int(penwidth),
                    "roundness": {"type": 3},
                    "boundElements": [],
                }
            )
            elements.append(rect)

            label = f"{name}\n({mod.file.name})"
            text = excalidraw_base_element(text_id, "text", x + 10, y + 14)
            text.update(
                {
                    "width": node_width - 20,
                    "height": 48,
                    "strokeColor": "#1e1e1e",
                    "roughness": 0,
                    "fontSize": 18,
                    "fontFamily": 1,
                    "text": label,
                    "rawText": label,
                    "textAlign": "center",
                    "verticalAlign": "middle",
                    "baseline": 41,
                    "containerId": None,
                    "originalText": label,
                    "lineHeight": 1.25,
                }
            )
            elements.append(text)

    for index, (parent, child, inst) in enumerate(visible_edges(design, visible_depths)):
        parent_x, parent_y = positions[parent]
        child_x, child_y = positions[child]
        start_x = parent_x + node_width
        start_y = parent_y + node_height / 2
        end_x = child_x
        end_y = child_y + node_height / 2
        arrow_id = f"edge-{stable_int(parent, child, inst, str(index)):08x}"
        edge_color = "#546E7A"
        stroke_width = 1
        if is_datapath_name(parent) or is_datapath_name(child) or is_datapath_name(inst):
            edge_color = "#D84315"
            stroke_width = 2

        arrow = excalidraw_base_element(arrow_id, "arrow", start_x, start_y)
        arrow.update(
            {
                "width": end_x - start_x,
                "height": end_y - start_y,
                "strokeColor": edge_color,
                "strokeWidth": stroke_width,
                "roundness": {"type": 2},
                "points": [[0, 0], [end_x - start_x, end_y - start_y]],
                "lastCommittedPoint": None,
                "startBinding": None,
                "endBinding": None,
                "startArrowhead": None,
                "endArrowhead": "arrow",
            }
        )
        elements.append(arrow)

        if inst:
            label_x = start_x + (end_x - start_x) / 2 - 45
            label_y = start_y + (end_y - start_y) / 2 - 26
            label_id = f"edge-label-{stable_int(parent, child, inst, str(index)):08x}"
            label = excalidraw_base_element(label_id, "text", label_x, label_y)
            label.update(
                {
                    "width": 90,
                    "height": 22,
                    "strokeColor": edge_color,
                    "backgroundColor": "#ffffff",
                    "roughness": 0,
                    "fontSize": 14,
                    "fontFamily": 1,
                    "text": inst,
                    "rawText": inst,
                    "textAlign": "center",
                    "verticalAlign": "middle",
                    "baseline": 17,
                    "containerId": None,
                    "originalText": inst,
                    "lineHeight": 1.25,
                }
            )
            elements.append(label)

    scene = {
        "type": "excalidraw",
        "version": 2,
        "source": "rtl_datapath_visualizer.py",
        "elements": elements,
        "appState": {
            "gridSize": None,
            "viewBackgroundColor": "#ffffff",
        },
        "files": {},
    }
    return json.dumps(scene, indent=2) + "\n"


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
    parser.add_argument(
        "depth",
        nargs="?",
        type=int,
        default=None,
        help="Optional hierarchy depth below top. 0 draws all modules reachable from top.",
    )
    parser.add_argument("--top", type=str, default=None, help="Top module name (optional)")
    parser.add_argument(
        "--depth",
        dest="depth_option",
        metavar="DEPTH",
        type=int,
        default=None,
        help="Hierarchy depth below top. 0 draws all modules reachable from top.",
    )
    parser.add_argument("--out", type=Path, default=Path("rtl_datapath.dot"), help="Output .dot path")
    parser.add_argument("--png", type=Path, default=Path("rtl_datapath.png"), help="Optional png path")
    parser.add_argument(
        "--excalidraw",
        type=Path,
        default=Path("rtl_datapath.excalidraw"),
        help="Output Excalidraw-compatible scene path",
    )
    args = parser.parse_args()

    if args.depth is not None and args.depth_option is not None:
        parser.error("use either positional depth or --depth, not both")

    depth_limit = args.depth_option if args.depth_option is not None else args.depth
    if depth_limit is None:
        depth_limit = 0
    if depth_limit < 0:
        parser.error("depth must be 0 or greater")

    design = build_design(args.filelist, args.top)
    visible_depths = reachable_module_depths(design.modules, design.top, depth_limit)
    dot = emit_dot(design, depth_limit)
    args.out.write_text(dot, encoding="utf-8")
    excalidraw = emit_excalidraw(design, depth_limit)
    args.excalidraw.write_text(excalidraw, encoding="utf-8")

    print(f"[OK] DOT generated: {args.out}")
    print(f"[OK] Excalidraw generated: {args.excalidraw}")
    print(f"[INFO] top module: {design.top}")
    if not args.top and len(design.top_candidates) > 1:
        print(f"[INFO] top candidates: {', '.join(design.top_candidates)}")
    depth_text = "all" if depth_limit == 0 else str(depth_limit)
    print(f"[INFO] depth: {depth_text}")
    print(f"[INFO] parsed module count: {len(design.modules)}")
    print(f"[INFO] drawn module count: {len(visible_depths)}")

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
