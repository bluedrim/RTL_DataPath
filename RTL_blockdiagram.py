#!/usr/bin/env python3
"""Generate a nested RTL hierarchy block diagram from a filelist."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from xml.sax.saxutils import escape as xml_escape


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
ENV_VAR_PAREN_RE = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
RTL_FILE_EXTENSIONS = {".v", ".sv", ".vh", ".svh"}
FILELIST_EXTENSIONS = {".f", ".flist", ".lst", ".list"}

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
ROW_GAP = 72
MAX_CHILD_COLUMNS = 4
OUTPUT_DIR = Path("output")
VISIO_PX_PER_INCH = 96
VISIO_PAGE_MARGIN = 60


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


class MultipleTopCandidatesError(ValueError):
    def __init__(self, candidates: List[str]) -> None:
        self.candidates = candidates
        super().__init__("Multiple TOP candidates found.")


def strip_comments(text: str) -> str:
    text = COMMENT_BLOCK_RE.sub("", text)
    return COMMENT_LINE_RE.sub("", text)


def split_filelist_line(raw: str) -> List[str]:
    line = raw.split("//", 1)[0].split("#", 1)[0].strip()
    if not line:
        return []
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def expand_filelist_token(token: str) -> str:
    def replace_make_var(match: re.Match[str]) -> str:
        name = match.group(1)
        return os.environ.get(name, match.group(0))

    expanded = ENV_VAR_PAREN_RE.sub(replace_make_var, token)
    expanded = os.path.expandvars(expanded)
    return os.path.expanduser(expanded)


def resolve_filelist_entry(base: Path, token: str) -> Path:
    candidate = Path(expand_filelist_token(token))
    if not candidate.is_absolute():
        candidate = (base / candidate).resolve()
    return candidate


def parse_filelist(path: Path) -> List[Path]:
    files: List[Path] = []
    seen_files: Set[Path] = set()
    seen_filelists: Set[Path] = set()

    def add_rtl_file(candidate: Path) -> None:
        candidate = candidate.resolve()
        if candidate in seen_files:
            return
        if candidate.suffix.lower() in RTL_FILE_EXTENSIONS and candidate.exists():
            seen_files.add(candidate)
            files.append(candidate)

    def visit_filelist(filelist: Path) -> None:
        filelist = filelist.resolve()
        if filelist in seen_filelists or not filelist.exists():
            return
        seen_filelists.add(filelist)
        base = filelist.parent

        for raw in filelist.read_text(encoding="utf-8").splitlines():
            tokens = split_filelist_line(raw)
            if not tokens:
                continue
            first = tokens[0]
            if first.startswith("+incdir+"):
                continue
            if first == "-y" or first.startswith("-y"):
                continue
            if first == "-v" and len(tokens) >= 2:
                add_rtl_file(resolve_filelist_entry(base, tokens[1]))
                continue
            if first in {"-f", "-F"} and len(tokens) >= 2:
                visit_filelist(resolve_filelist_entry(base, tokens[1]))
                continue
            if first.startswith("-f") and len(first) > 2:
                visit_filelist(resolve_filelist_entry(base, first[2:]))
                continue
            if first.startswith("-F") and len(first) > 2:
                visit_filelist(resolve_filelist_entry(base, first[2:]))
                continue
            if first.startswith("-"):
                continue

            candidate = resolve_filelist_entry(base, first)
            suffix = candidate.suffix.lower()
            if suffix in RTL_FILE_EXTENSIONS:
                add_rtl_file(candidate)
            elif suffix in FILELIST_EXTENSIONS:
                visit_filelist(candidate)

    visit_filelist(resolve_filelist_entry(Path.cwd(), str(path)))
    return files


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


def row_width(row: List[HierarchyNode]) -> float:
    return sum(child.width for child in row) + CHILD_GAP * (len(row) - 1)


def row_height(row: List[HierarchyNode]) -> float:
    return max(child.height for child in row)


def compute_hierarchy_layout(node: HierarchyNode) -> Tuple[float, float]:
    if not node.children:
        node.width = NODE_MIN_WIDTH
        node.height = NODE_MIN_HEIGHT
        return node.width, node.height

    for child in node.children:
        compute_hierarchy_layout(child)

    rows = child_rows(node.children)
    row_widths = [row_width(row) for row in rows]
    row_heights = [row_height(row) for row in rows]
    content_width = max(row_widths) if row_widths else 0
    content_height = sum(row_heights) + ROW_GAP * (len(row_heights) - 1)

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
        width = row_width(row)
        height = row_height(row)
        child_x = x + (node.width - width) / 2
        for child in row:
            place_hierarchy(child, child_x, row_y, positions)
            child_x += child.width + CHILD_GAP
        row_y += height + ROW_GAP


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
        if len(top_candidates) > 1:
            raise MultipleTopCandidatesError(top_candidates)
        top, top_candidates = infer_top(modules)
    if top not in modules:
        raise ValueError(f"Top module '{top}' not found in parsed modules.")

    return Design(modules=modules, top=top, top_candidates=top_candidates, top_is_explicit=bool(explicit_top))


def ensure_parent_dir(path: Path) -> None:
    parent = path.parent
    if parent != Path("."):
        parent.mkdir(parents=True, exist_ok=True)


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


def dot_row_minlen(row: List[HierarchyNode]) -> int:
    return max(3, int(row_height(row) / NODE_MIN_HEIGHT) + 1)


def emit_dot(design: Design, depth_limit: int = 0, show_instances: bool = False) -> str:
    tree = build_hierarchy_tree(design, depth_limit)
    compute_hierarchy_layout(tree)

    lines = [
        "digraph RTL {",
        '  graph [fontname="Helvetica", bgcolor="white", labeljust="l", labelloc="t", ranksep="1.0"];',
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
            minlen = dot_row_minlen(rows[row_index])
            lines.append(
                f'{prefix}  "{dot_row_anchor_id(node, row_index)}" -> '
                f'"{dot_row_anchor_id(node, row_index + 1)}" '
                f'[style=invis, weight=100, minlen={minlen}];'
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


def iter_hierarchy_nodes(node: HierarchyNode) -> List[HierarchyNode]:
    nodes = [node]
    for child in node.children:
        nodes.extend(iter_hierarchy_nodes(child))
    return nodes


def visio_number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text else "0"


def px_to_visio(value: float) -> float:
    return value / VISIO_PX_PER_INCH


def visio_shape_xml(
    shape_id: int,
    node: HierarchyNode,
    x: float,
    y: float,
    page_height: float,
    design: Design,
    show_instances: bool,
) -> str:
    fill, border, penwidth = module_style(design, node.module_name, node.depth)
    label = xml_escape(hierarchy_label(node, show_instances))
    width = px_to_visio(node.width)
    height = px_to_visio(node.height)
    pin_x = px_to_visio(x + node.width / 2)
    pin_y = page_height - px_to_visio(y + node.height / 2)
    line_weight = max(0.01, 0.01 * penwidth)
    font_size = 0.24 if node.depth == 0 else 0.18
    text_height = 0.45 if "\n" not in label else 0.62
    shape_name = xml_escape(f"{node.module_name}_{shape_id}")
    return f"""    <Shape ID="{shape_id}" Name="{xml_escape(node.module_name)}" NameU="{shape_name}" Type="Shape" LineStyle="0" FillStyle="0" TextStyle="0">
      <Cell N="PinX" V="{visio_number(pin_x)}"/>
      <Cell N="PinY" V="{visio_number(pin_y)}"/>
      <Cell N="Width" V="{visio_number(width)}"/>
      <Cell N="Height" V="{visio_number(height)}"/>
      <Cell N="LocPinX" V="{visio_number(width / 2)}"/>
      <Cell N="LocPinY" V="{visio_number(height / 2)}"/>
      <Cell N="FillForegnd" V="{fill}"/>
      <Cell N="FillPattern" V="1"/>
      <Cell N="LineColor" V="{border}"/>
      <Cell N="LineWeight" V="{visio_number(line_weight)}"/>
      <Cell N="Rounding" V="0.05"/>
      <Cell N="TxtWidth" V="{visio_number(max(0.5, width - 0.25))}"/>
      <Cell N="TxtHeight" V="{visio_number(text_height)}"/>
      <Cell N="TxtPinX" V="{visio_number(width / 2)}"/>
      <Cell N="TxtPinY" V="{visio_number(max(0.2, height - text_height / 2 - 0.1))}"/>
      <Cell N="VerticalAlign" V="0"/>
      <Section N="Character">
        <Row IX="0">
          <Cell N="Size" V="{visio_number(font_size)}"/>
        </Row>
      </Section>
      <Section N="Paragraph">
        <Row IX="0">
          <Cell N="HorzAlign" V="0"/>
        </Row>
      </Section>
      <Section N="Geometry" IX="0">
        <Cell N="NoFill" V="0"/>
        <Cell N="NoLine" V="0"/>
        <Cell N="NoShow" V="0"/>
        <Cell N="NoSnap" V="0"/>
        <Row T="MoveTo" IX="1">
          <Cell N="X" V="0"/>
          <Cell N="Y" V="0"/>
        </Row>
        <Row T="LineTo" IX="2">
          <Cell N="X" V="{visio_number(width)}"/>
          <Cell N="Y" V="0"/>
        </Row>
        <Row T="LineTo" IX="3">
          <Cell N="X" V="{visio_number(width)}"/>
          <Cell N="Y" V="{visio_number(height)}"/>
        </Row>
        <Row T="LineTo" IX="4">
          <Cell N="X" V="0"/>
          <Cell N="Y" V="{visio_number(height)}"/>
        </Row>
        <Row T="LineTo" IX="5">
          <Cell N="X" V="0"/>
          <Cell N="Y" V="0"/>
        </Row>
      </Section>
      <Text>{label}</Text>
    </Shape>"""


def visio_page_xml(
    design: Design,
    tree: HierarchyNode,
    positions: Dict[Tuple[str, ...], Tuple[float, float]],
    show_instances: bool,
) -> str:
    nodes = iter_hierarchy_nodes(tree)
    max_x = max(positions[node.path][0] + node.width for node in nodes) + VISIO_PAGE_MARGIN
    max_y = max(positions[node.path][1] + node.height for node in nodes) + VISIO_PAGE_MARGIN
    page_width = max(8.5, px_to_visio(max_x))
    page_height = max(11.0, px_to_visio(max_y))
    shapes = [
        visio_shape_xml(index, node, *positions[node.path], page_height, design, show_instances)
        for index, node in enumerate(nodes, start=1)
    ]
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main" xml:space="preserve">
  <PageSheet LineStyle="0" FillStyle="0" TextStyle="0">
    <Cell N="PageWidth" V="{visio_number(page_width)}"/>
    <Cell N="PageHeight" V="{visio_number(page_height)}"/>
    <Cell N="DrawingScale" V="1"/>
    <Cell N="PageScale" V="1"/>
  </PageSheet>
  <Shapes>
{chr(10).join(shapes)}
  </Shapes>
</PageContents>
"""


def visio_package_parts(page_xml: str) -> Dict[str, str]:
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "[Content_Types].xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
  <Override PartName="/visio/document.xml" ContentType="application/vnd.ms-visio.drawing.main+xml"/>
  <Override PartName="/visio/pages/pages.xml" ContentType="application/vnd.ms-visio.pages+xml"/>
  <Override PartName="/visio/pages/page1.xml" ContentType="application/vnd.ms-visio.page+xml"/>
</Types>
""",
        "_rels/.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/document" Target="visio/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
""",
        "docProps/core.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>RTL DataPath</dc:title>
  <dc:creator>RTL_blockdiagram.py</dc:creator>
  <cp:lastModifiedBy>RTL_blockdiagram.py</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>
""",
        "docProps/app.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>RTL_blockdiagram.py</Application>
  <DocSecurity>0</DocSecurity>
  <ScaleCrop>false</ScaleCrop>
  <HeadingPairs>
    <vt:vector size="2" baseType="variant">
      <vt:variant><vt:lpstr>Pages</vt:lpstr></vt:variant>
      <vt:variant><vt:i4>1</vt:i4></vt:variant>
    </vt:vector>
  </HeadingPairs>
  <TitlesOfParts>
    <vt:vector size="1" baseType="lpstr">
      <vt:lpstr>Page-1</vt:lpstr>
    </vt:vector>
  </TitlesOfParts>
  <LinksUpToDate>false</LinksUpToDate>
  <SharedDoc>false</SharedDoc>
  <HyperlinksChanged>false</HyperlinksChanged>
  <AppVersion>16.0000</AppVersion>
</Properties>
""",
        "visio/document.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<VisioDocument xmlns="http://schemas.microsoft.com/office/visio/2012/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xml:space="preserve">
  <DocumentSettings/>
  <Colors/>
  <FaceNames>
    <FaceName ID="0" Name="Calibri"/>
  </FaceNames>
  <StyleSheets>
    <StyleSheet ID="0" Name="No Style" NameU="No Style" IsCustomName="1" IsCustomNameU="1">
      <Cell N="EnableLineProps" V="1"/>
      <Cell N="EnableFillProps" V="1"/>
      <Cell N="EnableTextProps" V="1"/>
    </StyleSheet>
  </StyleSheets>
  <DocumentSheet LineStyle="0" FillStyle="0" TextStyle="0"/>
</VisioDocument>
""",
        "visio/_rels/document.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/pages" Target="pages/pages.xml"/>
</Relationships>
""",
        "visio/pages/pages.xml": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Pages xmlns="http://schemas.microsoft.com/office/visio/2012/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <Page ID="0" Name="Page-1" NameU="Page-1" IsCustomName="1" IsCustomNameU="1">
    <Rel r:id="rId1"/>
  </Page>
</Pages>
""",
        "visio/pages/_rels/pages.xml.rels": """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.microsoft.com/visio/2010/relationships/page" Target="page1.xml"/>
</Relationships>
""",
        "visio/pages/page1.xml": page_xml,
    }


def write_visio_vsdx(path: Path, design: Design, depth_limit: int = 0, show_instances: bool = False) -> None:
    tree = build_hierarchy_tree(design, depth_limit)
    compute_hierarchy_layout(tree)
    node_positions: Dict[Tuple[str, ...], Tuple[float, float]] = {}
    place_hierarchy(tree, 60, 60, node_positions)
    page_xml = visio_page_xml(design, tree, node_positions, show_instances)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in visio_package_parts(page_xml).items():
            archive.writestr(name, content)


def visio_vdx_shape_xml(
    shape_id: int,
    node: HierarchyNode,
    x: float,
    y: float,
    page_height: float,
    design: Design,
    show_instances: bool,
) -> str:
    fill, border, penwidth = module_style(design, node.module_name, node.depth)
    label = xml_escape(hierarchy_label(node, show_instances))
    width = px_to_visio(node.width)
    height = px_to_visio(node.height)
    pin_x = px_to_visio(x + node.width / 2)
    pin_y = page_height - px_to_visio(y + node.height / 2)
    loc_pin_x = width / 2
    loc_pin_y = height / 2
    line_weight = max(0.01, 0.01 * penwidth)
    font_size = 0.24 if node.depth == 0 else 0.18
    text_height = 0.45 if "\n" not in label else 0.62
    shape_name = xml_escape(f"{node.module_name}_{shape_id}")
    return f"""      <Shape ID="{shape_id}" Name="{xml_escape(node.module_name)}" NameU="{shape_name}" Type="Shape" LineStyle="0" FillStyle="0" TextStyle="0">
        <XForm>
          <PinX>{visio_number(pin_x)}</PinX>
          <PinY>{visio_number(pin_y)}</PinY>
          <Width>{visio_number(width)}</Width>
          <Height>{visio_number(height)}</Height>
          <LocPinX>{visio_number(loc_pin_x)}</LocPinX>
          <LocPinY>{visio_number(loc_pin_y)}</LocPinY>
        </XForm>
        <Line>
          <LineColor>{border}</LineColor>
          <LineWeight>{visio_number(line_weight)}</LineWeight>
        </Line>
        <Fill>
          <FillForegnd>{fill}</FillForegnd>
          <FillPattern>1</FillPattern>
        </Fill>
        <TextBlock>
          <TxtWidth>{visio_number(max(0.5, width - 0.25))}</TxtWidth>
          <TxtHeight>{visio_number(text_height)}</TxtHeight>
          <TxtPinX>{visio_number(width / 2)}</TxtPinX>
          <TxtPinY>{visio_number(max(0.2, height - text_height / 2 - 0.1))}</TxtPinY>
          <VerticalAlign>0</VerticalAlign>
        </TextBlock>
        <Char IX="0">
          <Font>0</Font>
          <Size>{visio_number(font_size)}</Size>
        </Char>
        <Para IX="0">
          <HorzAlign>0</HorzAlign>
        </Para>
        <Geom IX="0">
          <NoFill>0</NoFill>
          <NoLine>0</NoLine>
          <NoShow>0</NoShow>
          <NoSnap>0</NoSnap>
          <MoveTo IX="1">
            <X>0</X>
            <Y>0</Y>
          </MoveTo>
          <LineTo IX="2">
            <X>{visio_number(width)}</X>
            <Y>0</Y>
          </LineTo>
          <LineTo IX="3">
            <X>{visio_number(width)}</X>
            <Y>{visio_number(height)}</Y>
          </LineTo>
          <LineTo IX="4">
            <X>0</X>
            <Y>{visio_number(height)}</Y>
          </LineTo>
          <LineTo IX="5">
            <X>0</X>
            <Y>0</Y>
          </LineTo>
        </Geom>
        <Text>{label}</Text>
      </Shape>"""


def emit_visio_vdx(design: Design, depth_limit: int = 0, show_instances: bool = False) -> str:
    tree = build_hierarchy_tree(design, depth_limit)
    compute_hierarchy_layout(tree)
    node_positions: Dict[Tuple[str, ...], Tuple[float, float]] = {}
    place_hierarchy(tree, 60, 60, node_positions)
    nodes = iter_hierarchy_nodes(tree)
    max_x = max(node_positions[node.path][0] + node.width for node in nodes) + VISIO_PAGE_MARGIN
    max_y = max(node_positions[node.path][1] + node.height for node in nodes) + VISIO_PAGE_MARGIN
    page_width = max(8.5, px_to_visio(max_x))
    page_height = max(11.0, px_to_visio(max_y))
    shapes = [
        visio_vdx_shape_xml(index, node, *node_positions[node.path], page_height, design, show_instances)
        for index, node in enumerate(nodes, start=1)
    ]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<VisioDocument xmlns="http://schemas.microsoft.com/visio/2003/core" start="0" metric="0" DocLangID="1033">
  <DocumentProperties>
    <Title>RTL DataPath</Title>
    <Creator>RTL_blockdiagram.py</Creator>
    <Company/>
  </DocumentProperties>
  <Colors/>
  <FaceNames>
    <FaceName ID="0" Name="Calibri"/>
  </FaceNames>
  <StyleSheets>
    <StyleSheet ID="0" Name="No Style" NameU="No Style" IsCustomName="1" IsCustomNameU="1"/>
  </StyleSheets>
  <DocumentSheet LineStyle="0" FillStyle="0" TextStyle="0"/>
  <Pages>
    <Page ID="0" Name="Page-1" NameU="Page-1" IsCustomName="1" IsCustomNameU="1">
      <PageSheet LineStyle="0" FillStyle="0" TextStyle="0">
        <PageProps>
          <PageWidth>{visio_number(page_width)}</PageWidth>
          <PageHeight>{visio_number(page_height)}</PageHeight>
          <DrawingScale>1</DrawingScale>
          <PageScale>1</PageScale>
          <DrawingScaleType>0</DrawingScaleType>
          <DrawingSizeType>0</DrawingSizeType>
        </PageProps>
      </PageSheet>
      <Shapes>
{chr(10).join(shapes)}
      </Shapes>
    </Page>
  </Pages>
</VisioDocument>
"""


def write_visio_vdx(path: Path, design: Design, depth_limit: int = 0, show_instances: bool = False) -> None:
    path.write_text(emit_visio_vdx(design, depth_limit, show_instances), encoding="utf-8")


def maybe_render_png(dot_file: Path, png_file: Path) -> bool:
    dot_bin = shutil.which("dot")
    if not dot_bin:
        return False
    subprocess.run([dot_bin, "-Tpng", str(dot_file), "-o", str(png_file)], check=True)
    return True


def quote_cli_arg(value: object) -> str:
    return shlex.quote(str(value))


def format_multiple_top_message(prog: str, filelist: Path, depth_limit: int, candidates: List[str]) -> str:
    lines = [f"[ERROR] Multiple TOP candidates found ({len(candidates)}).", "Specify one with --top <module>.", ""]
    lines.append("TOP candidates:")
    lines.extend(f"  - {candidate}" for candidate in candidates)
    lines.extend(["", "Command examples:"])
    for candidate in candidates:
        lines.append(
            "  "
            + " ".join(
                [
                    "python3",
                    quote_cli_arg(prog),
                    quote_cli_arg(filelist),
                    quote_cli_arg(depth_limit),
                    "--top",
                    quote_cli_arg(candidate),
                ]
            )
        )
    return "\n".join(lines)


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
    parser.add_argument("--out", type=Path, default=None, help="Output .dot path")
    parser.add_argument("--png", type=Path, default=None, help="Optional png path")
    parser.add_argument(
        "--excalidraw",
        type=Path,
        default=None,
        help="Output Excalidraw-compatible scene path",
    )
    parser.add_argument(
        "--visio",
        type=Path,
        default=None,
        help="Output Visio XML Drawing .vdx path",
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

    try:
        design = build_design(args.filelist, args.top)
    except MultipleTopCandidatesError as exc:
        parser.exit(2, format_multiple_top_message(parser.prog, args.filelist, depth_limit, exc.candidates) + "\n")
    default_stem = f"{design.top}_blockdiagrm"
    if args.out is None:
        args.out = OUTPUT_DIR / f"{default_stem}.dot"
    if args.png is None:
        args.png = OUTPUT_DIR / f"{default_stem}.png"
    if args.excalidraw is None:
        args.excalidraw = OUTPUT_DIR / f"{default_stem}.excalidraw"
    if args.visio is None:
        args.visio = OUTPUT_DIR / f"{default_stem}.vdx"
    if args.visio.suffix.lower() != ".vdx":
        parser.error("--visio output must use the .vdx extension")

    tree = build_hierarchy_tree(design, depth_limit)
    drawn_block_count = count_hierarchy_nodes(tree)
    dot = emit_dot(design, depth_limit, show_instances=args.show_instances)
    ensure_parent_dir(args.out)
    args.out.write_text(dot, encoding="utf-8")
    excalidraw = emit_excalidraw(design, depth_limit, show_instances=args.show_instances)
    ensure_parent_dir(args.excalidraw)
    args.excalidraw.write_text(excalidraw, encoding="utf-8")
    ensure_parent_dir(args.visio)
    write_visio_vdx(args.visio, design, depth_limit, show_instances=args.show_instances)

    print(f"[OK] DOT generated: {args.out}")
    print(f"[OK] Excalidraw generated: {args.excalidraw}")
    print(f"[OK] Visio generated: {args.visio}")
    root_source = "explicit" if design.top_is_explicit else "inferred"
    print(f"[INFO] diagram root: {design.top} ({root_source})")
    depth_text = "all" if depth_limit == 0 else str(depth_limit)
    print(f"[INFO] depth: {depth_text}")
    label_mode = "module + instance" if args.show_instances else "module only"
    print(f"[INFO] label mode: {label_mode}")
    print(f"[INFO] parsed module count: {len(design.modules)}")
    print(f"[INFO] drawn block count: {drawn_block_count}")

    try:
        ensure_parent_dir(args.png)
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
