#!/usr/bin/env python3
"""Generate a nested RTL hierarchy block diagram from a filelist."""

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

DEPTH_PALETTE = [
    ("#DCEBFA", "#2F5E8E"),
    ("#E5F2D7", "#4B7F35"),
    ("#FDE8C9", "#A45A13"),
    ("#E9E0F7", "#6D4BA3"),
    ("#DDF2EF", "#337A73"),
    ("#F7DDE7", "#9B4164"),
    ("#E7E9F0", "#5B6476"),
]

NODE_MIN_WIDTH = 240
NODE_MIN_HEIGHT = 84
HEADER_HEIGHT = 54
CONTAINER_PAD = 22
CHILD_GAP = 18
MAX_CHILD_COLUMNS = 4


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
    top_is_explicit: bool = False


@dataclass
class HierarchyNode:
    module_name: str
    inst_name: str | None
    depth: int
    path: Tuple[str, ...]
    children: List["HierarchyNode"] = field(default_factory=list)
    width: float = 0
    height: float = 0


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


def module_style(design: Design, name: str, depth: int) -> Tuple[str, str, int]:
    mod = design.modules[name]
    fill, border = DEPTH_PALETTE[depth % len(DEPTH_PALETTE)]
    if name == design.top:
        return fill, "#174A7C", 3
    if is_datapath_name(name) or any(is_datapath_name(p) for p in mod.ports):
        return fill, border, 2
    return fill, border, 1


def build_hierarchy_tree(design: Design, depth_limit: int = 0) -> HierarchyNode:
    if depth_limit < 0:
        raise ValueError("depth_limit must be 0 or greater")

    unlimited = depth_limit == 0

    def expand(
        module_name: str,
        inst_name: str | None,
        depth: int,
        path: Tuple[str, ...],
        ancestry: Set[str],
    ) -> HierarchyNode:
        node = HierarchyNode(module_name=module_name, inst_name=inst_name, depth=depth, path=path)
        if not unlimited and depth >= depth_limit:
            return node

        for index, (child_module, child_inst) in enumerate(design.modules[module_name].instances):
            if child_module not in design.modules or child_module in ancestry:
                continue
            child_path = path + (f"{index}:{child_inst}:{child_module}",)
            node.children.append(
                expand(child_module, child_inst, depth + 1, child_path, ancestry | {child_module})
            )

        return node

    return expand(design.top, None, 0, (design.top,), {design.top})


def count_hierarchy_nodes(node: HierarchyNode) -> int:
    return 1 + sum(count_hierarchy_nodes(child) for child in node.children)


def hierarchy_label(node: HierarchyNode, show_instances: bool = False) -> str:
    if show_instances and node.inst_name:
        return f"{node.inst_name}\n{node.module_name}"
    return node.module_name


def child_rows(children: List[HierarchyNode]) -> List[List[HierarchyNode]]:
    if not children:
        return []
    columns = min(MAX_CHILD_COLUMNS, len(children))
    return [children[index : index + columns] for index in range(0, len(children), columns)]


def compute_hierarchy_layout(node: HierarchyNode) -> Tuple[float, float]:
    if not node.children:
        node.width = NODE_MIN_WIDTH
        node.height = NODE_MIN_HEIGHT
        return node.width, node.height

    for child in node.children:
        compute_hierarchy_layout(child)

    rows = child_rows(node.children)
    row_widths = [
        sum(child.width for child in row) + CHILD_GAP * (len(row) - 1)
        for row in rows
    ]
    row_heights = [max(child.height for child in row) for row in rows]
    content_width = max(row_widths) if row_widths else 0
    content_height = sum(row_heights) + CHILD_GAP * (len(row_heights) - 1)

    node.width = max(NODE_MIN_WIDTH, content_width + CONTAINER_PAD * 2)
    node.height = max(
        NODE_MIN_HEIGHT,
        HEADER_HEIGHT + CONTAINER_PAD + content_height + CONTAINER_PAD,
    )
    return node.width, node.height


def place_hierarchy(node: HierarchyNode, x: float, y: float, positions: Dict[Tuple[str, ...], Tuple[float, float]]) -> None:
    positions[node.path] = (x, y)
    if not node.children:
        return

    rows = child_rows(node.children)
    row_y = y + HEADER_HEIGHT + CONTAINER_PAD
    for row in rows:
        row_width = sum(child.width for child in row) + CHILD_GAP * (len(row) - 1)
        row_height = max(child.height for child in row)
        child_x = x + (node.width - row_width) / 2
        for child in row:
            place_hierarchy(child, child_x, row_y, positions)
            child_x += child.width + CHILD_GAP
        row_y += row_height + CHILD_GAP


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

    return Design(modules=modules, top=top, top_candidates=top_candidates, top_is_explicit=bool(explicit_top))


def dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def dot_anchor_id(node: HierarchyNode) -> str:
    return f"anchor_{stable_int(*node.path, 'anchor'):08x}"


def dot_cluster_id(node: HierarchyNode) -> str:
    return f"cluster_{stable_int(*node.path, 'cluster'):08x}"


def dot_row_cluster_id(node: HierarchyNode, row_index: int) -> str:
    return f"cluster_row_{stable_int(*node.path, str(row_index), 'cluster'):08x}"


def dot_row_anchor_id(node: HierarchyNode, row_index: int) -> str:
    return f"row_anchor_{stable_int(*node.path, str(row_index), 'anchor'):08x}"


def emit_dot(design: Design, depth_limit: int = 0, show_instances: bool = False) -> str:
    tree = build_hierarchy_tree(design, depth_limit)

    lines = [
        "digraph RTL {",
        '  graph [fontname="Helvetica", bgcolor="white", labeljust="l", labelloc="t"];',
        '  node [shape=point, style=invis, width=0.01, height=0.01, label=""];',
    ]

    def add_cluster(node: HierarchyNode, indent: int) -> None:
        prefix = "  " * indent
        cluster_id = dot_cluster_id(node)
        anchor_id = dot_anchor_id(node)
        fill, border, penwidth = module_style(design, node.module_name, node.depth)
        label = dot_escape(hierarchy_label(node, show_instances))

        lines.append(f'{prefix}subgraph "{cluster_id}" {{')
        lines.append(f'{prefix}  label="{label}";')
        lines.append(f'{prefix}  style="rounded,filled";')
        lines.append(f'{prefix}  fillcolor="{fill}";')
        lines.append(f'{prefix}  color="{border}";')
        lines.append(f'{prefix}  penwidth={penwidth};')
        lines.append(f'{prefix}  fontname="Helvetica";')
        lines.append(f'{prefix}  "{anchor_id}";')
        rows = child_rows(node.children)
        for row_index, row in enumerate(rows):
            row_cluster_id = dot_row_cluster_id(node, row_index)
            row_anchor_id = dot_row_anchor_id(node, row_index)
            lines.append(f'{prefix}  subgraph "{row_cluster_id}" {{')
            lines.append(f'{prefix}    label="";')
            lines.append(f'{prefix}    color="white";')
            lines.append(f'{prefix}    penwidth=0;')
            lines.append(f'{prefix}    margin=0;')
            lines.append(f'{prefix}    "{row_anchor_id}";')
            for child in row:
                add_cluster(child, indent + 2)
            lines.append(f"{prefix}  }}")
        for row_index in range(len(rows) - 1):
            lines.append(
                f'{prefix}  "{dot_row_anchor_id(node, row_index)}" -> '
                f'"{dot_row_anchor_id(node, row_index + 1)}" '
                '[style=invis, weight=100];'
            )
        lines.append(f"{prefix}}}")

    add_cluster(tree, 1)

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


def emit_excalidraw(design: Design, depth_limit: int = 0, show_instances: bool = False) -> str:
    tree = build_hierarchy_tree(design, depth_limit)
    compute_hierarchy_layout(tree)
    node_positions: Dict[Tuple[str, ...], Tuple[float, float]] = {}
    place_hierarchy(tree, 60, 60, node_positions)
    elements: List[Dict[str, Any]] = []

    def append_node(node: HierarchyNode) -> None:
        x, y = node_positions[node.path]
        fill, border, penwidth = module_style(design, node.module_name, node.depth)
        rect_id = f"module-{stable_int(*node.path, 'rect'):08x}"
        text_id = f"text-{stable_int(*node.path, 'text'):08x}"
        font_size = 20 if node.depth == 0 else 16
        label = hierarchy_label(node, show_instances)

        rect = excalidraw_base_element(rect_id, "rectangle", x, y)
        rect.update(
            {
                "width": node.width,
                "height": node.height,
                "strokeColor": border,
                "backgroundColor": fill,
                "strokeWidth": penwidth,
                "roundness": {"type": 3},
                "boundElements": [],
            }
        )
        elements.append(rect)

        text = excalidraw_base_element(text_id, "text", x + 14, y + 14)
        text.update(
            {
                "width": max(120, node.width - 28),
                "height": 32 if "\n" not in label else 44,
                "strokeColor": "#1e1e1e",
                "roughness": 0,
                "fontSize": font_size,
                "fontFamily": 1,
                "text": label,
                "rawText": label,
                "textAlign": "left",
                "verticalAlign": "top",
                "baseline": font_size + 4,
                "containerId": None,
                "originalText": label,
                "lineHeight": 1.2,
            }
        )
        elements.append(text)

        for child in node.children:
            append_node(child)

    append_node(tree)

    scene = {
        "type": "excalidraw",
        "version": 2,
        "source": "RTL_blockdiagram.py",
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
        description="Read an RTL filelist and generate a nested module hierarchy block diagram"
    )
    parser.add_argument("filelist", type=Path, help="Path to rte/filelist.f style file list")
    parser.add_argument(
        "depth",
        nargs="?",
        type=int,
        default=None,
        help="Optional hierarchy depth below top. 0 draws all modules reachable from top.",
    )
    parser.add_argument(
        "--top",
        type=str,
        default=None,
        help="Module to use as the diagram root. Defaults to the inferred full-design TOP.",
    )
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
    parser.add_argument(
        "--show-instances",
        action="store_true",
        help="Show instance names above module names inside hierarchy blocks.",
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
    tree = build_hierarchy_tree(design, depth_limit)
    drawn_block_count = count_hierarchy_nodes(tree)
    dot = emit_dot(design, depth_limit, show_instances=args.show_instances)
    args.out.write_text(dot, encoding="utf-8")
    excalidraw = emit_excalidraw(design, depth_limit, show_instances=args.show_instances)
    args.excalidraw.write_text(excalidraw, encoding="utf-8")

    print(f"[OK] DOT generated: {args.out}")
    print(f"[OK] Excalidraw generated: {args.excalidraw}")
    root_source = "explicit" if design.top_is_explicit else "inferred"
    print(f"[INFO] diagram root: {design.top} ({root_source})")
    if not args.top and len(design.top_candidates) > 1:
        print(f"[INFO] top candidates: {', '.join(design.top_candidates)}")
    depth_text = "all" if depth_limit == 0 else str(depth_limit)
    print(f"[INFO] depth: {depth_text}")
    label_mode = "module + instance" if args.show_instances else "module only"
    print(f"[INFO] label mode: {label_mode}")
    print(f"[INFO] parsed module count: {len(design.modules)}")
    print(f"[INFO] drawn block count: {drawn_block_count}")

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
