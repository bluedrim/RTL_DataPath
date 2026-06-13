#!/usr/bin/env python3
"""Trace a likely RTL datapath from a hierarchical signal path."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple


MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)")
ENDMODULE_RE = re.compile(r"\bendmodule\b")
PORT_BLOCK_RE = re.compile(r"\bmodule\s+[A-Za-z_][A-Za-z0-9_$]*\s*(?:#\s*\([^;]*?\))?\s*\((.*?)\)\s*;", re.S)
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
COMMENT_LINE_RE = re.compile(r"//.*?$", re.M)
COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.S)
DECL_PORT_RE = re.compile(r"\b(input|output|inout)\b([^;]*);", re.S)
ASSIGN_RE = re.compile(r"^\s*assign\s+(.+?)\s*=\s*(.+?)\s*$", re.S)
PROC_ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_$]*(?:\s*\[[^\]]+\])?)\s*(<=|=)\s*(.+)$", re.S)
IF_RE = re.compile(r"\bif\s*\((.*?)\)", re.S)
CASE_RE = re.compile(r"\b(?:case|casex|casez)\s*\((.*?)\)", re.S)

KEYWORDS = {
    "always",
    "always_comb",
    "always_ff",
    "always_latch",
    "assign",
    "begin",
    "case",
    "casex",
    "casez",
    "default",
    "else",
    "end",
    "endcase",
    "for",
    "function",
    "generate",
    "if",
    "input",
    "inout",
    "integer",
    "logic",
    "module",
    "negedge",
    "or",
    "output",
    "posedge",
    "reg",
    "signed",
    "task",
    "wire",
}

DEPTH_PALETTE = [
    ("#DCEBFA", "#2F5E8E"),
    ("#E5F2D7", "#4B7F35"),
    ("#FDE8C9", "#A45A13"),
    ("#E9E0F7", "#6D4BA3"),
    ("#DDF2EF", "#337A73"),
]

BLUE = "\033[34m"
RESET = "\033[0m"


@dataclass
class Assignment:
    lhs: str
    rhs: str
    lhs_signals: List[str]
    rhs_signals: List[str]
    conditional: bool
    kind: str
    context: str


@dataclass
class Instance:
    module_type: str
    name: str
    connections: Dict[str, str] = field(default_factory=dict)


@dataclass
class Module:
    name: str
    file: Path
    body: str = ""
    ports: List[str] = field(default_factory=list)
    port_directions: Dict[str, str] = field(default_factory=dict)
    instances: List[Instance] = field(default_factory=list)
    assignments: List[Assignment] = field(default_factory=list)
    condition_uses: List[Tuple[str, List[str], str]] = field(default_factory=list)


@dataclass
class Design:
    modules: Dict[str, Module]
    top: str
    top_candidates: List[str] = field(default_factory=list)
    top_is_explicit: bool = False


@dataclass
class HierNode:
    module_name: str
    inst_name: str | None
    path: Tuple[str, ...]
    children: List["HierNode"] = field(default_factory=list)
    parent: "HierNode | None" = None
    parent_instance: Instance | None = None

    def display_path(self) -> str:
        return ".".join(self.path)


@dataclass(frozen=True)
class SignalRef:
    path: Tuple[str, ...]
    module_name: str
    signal: str

    def label(self) -> str:
        return ".".join(self.path + (self.signal,))


@dataclass
class TraceEdge:
    src: SignalRef
    dst: SignalRef
    action: str
    detail: str
    stopped: bool = False
    reason: str = ""


@dataclass
class TraceResult:
    start: SignalRef
    edges: List[TraceEdge]
    terminal_refs: List[SignalRef]
    stop_edges: List[TraceEdge]
    main_path: List[TraceEdge]
    longest_path: List[TraceEdge]
    all_paths: List[List[TraceEdge]]


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


def split_top_level_commas(text: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    paren = bracket = brace = 0
    for char in text:
        if char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        if char == "," and paren == 0 and bracket == 0 and brace == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
    part = "".join(current).strip()
    if part:
        parts.append(part)
    return parts


def split_semicolon_statements(text: str) -> List[str]:
    statements: List[str] = []
    current: List[str] = []
    paren = bracket = brace = 0
    for char in text:
        if char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        if char == ";" and paren == 0 and bracket == 0 and brace == 0:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def base_signal(name: str) -> str:
    match = IDENT_RE.search(name)
    return match.group(0) if match else name.strip()


def expression_signals(expr: str) -> List[str]:
    signals: List[str] = []
    seen: Set[str] = set()
    for token in IDENT_RE.findall(expr):
        if token in KEYWORDS:
            continue
        if token not in seen:
            seen.add(token)
            signals.append(token)
    return signals


def signal_in_expr(signal: str, expr: str) -> bool:
    return signal in expression_signals(expr)


def parse_port_decl_fragment(fragment: str, previous_direction: str | None) -> Tuple[str | None, List[str]]:
    direction_match = re.search(r"\b(input|output|inout)\b", fragment)
    direction = direction_match.group(1) if direction_match else previous_direction
    names = [
        token
        for token in IDENT_RE.findall(fragment)
        if token not in KEYWORDS and token != direction
    ]
    return direction, names[-1:] if direction_match else names


def parse_header_ports(port_block: str) -> Tuple[List[str], Dict[str, str]]:
    ports: List[str] = []
    directions: Dict[str, str] = {}
    previous_direction: str | None = None
    for part in split_top_level_commas(port_block):
        direction, names = parse_port_decl_fragment(part, previous_direction)
        if direction:
            previous_direction = direction
        for name in names:
            if name not in ports:
                ports.append(name)
            if direction:
                directions[name] = direction
    return ports, directions


def parse_body_port_decls(block: str, ports: List[str], directions: Dict[str, str]) -> None:
    for match in DECL_PORT_RE.finditer(block):
        direction = match.group(1)
        tail = match.group(2)
        for name in expression_signals(tail):
            if name not in ports:
                ports.append(name)
            directions[name] = direction


def strip_module_header(block: str) -> str:
    match = PORT_BLOCK_RE.search(block)
    if not match:
        return block
    return block[match.end() :]


def parse_named_connections(conn_text: str) -> Dict[str, str]:
    connections: Dict[str, str] = {}
    positional_index = 0
    for part in split_top_level_commas(conn_text):
        named = re.match(r"\.\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\((.*)\)\s*$", part, re.S)
        if named:
            connections[named.group(1)] = named.group(2).strip()
        else:
            connections[f"__pos{positional_index}"] = part.strip()
            positional_index += 1
    return connections


def parse_instance(statement: str, known_modules: Set[str]) -> Instance | None:
    if re.match(r"^\s*(module|assign|always|always_comb|always_ff|always_latch|if|case|for|while|function|task)\b", statement):
        return None
    match = re.match(
        r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\(.*\)\s*)?([A-Za-z_][A-Za-z0-9_$]*)\s*\((.*)\)\s*$",
        statement,
        re.S,
    )
    if not match:
        return None
    module_type, inst_name, conn_text = match.group(1), match.group(2), match.group(3)
    if module_type not in known_modules:
        return None
    return Instance(module_type=module_type, name=inst_name, connections=parse_named_connections(conn_text))


def statement_is_conditional(statement: str) -> bool:
    return bool(re.search(r"\b(if|else|case|casex|casez)\b", statement) or "?" in statement)


def parse_assignments_and_conditions(body: str) -> Tuple[List[Assignment], List[Tuple[str, List[str], str]]]:
    assignments: List[Assignment] = []
    condition_uses: List[Tuple[str, List[str], str]] = []

    for match in IF_RE.finditer(body):
        expr = match.group(1).strip()
        condition_uses.append(("if", expression_signals(expr), expr))
    for match in CASE_RE.finditer(body):
        expr = match.group(1).strip()
        condition_uses.append(("case", expression_signals(expr), expr))

    for statement in split_semicolon_statements(body):
        stripped = statement.strip()
        if not stripped:
            continue
        is_conditional = statement_is_conditional(stripped)

        assign_match = ASSIGN_RE.match(stripped)
        if assign_match:
            lhs, rhs = assign_match.group(1).strip(), assign_match.group(2).strip()
            assignments.append(
                Assignment(
                    lhs=lhs,
                    rhs=rhs,
                    lhs_signals=expression_signals(lhs),
                    rhs_signals=expression_signals(rhs),
                    conditional=False,
                    kind="assign",
                    context=stripped,
                )
            )
            continue

        proc_match = PROC_ASSIGN_RE.search(stripped)
        if proc_match and not re.search(r"(==|!=|>=|<=\s*[^=])", stripped.split(proc_match.group(2), 1)[0]):
            lhs, rhs = proc_match.group(1).strip(), proc_match.group(3).strip()
            if lhs and rhs and expression_signals(lhs):
                assignments.append(
                    Assignment(
                        lhs=lhs,
                        rhs=rhs,
                        lhs_signals=expression_signals(lhs),
                        rhs_signals=expression_signals(rhs),
                        conditional=is_conditional,
                        kind="procedural",
                        context=stripped,
                    )
                )

    return assignments, condition_uses


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
        ports: List[str] = []
        directions: Dict[str, str] = {}
        port_match = PORT_BLOCK_RE.search(block)
        if port_match:
            ports, directions = parse_header_ports(port_match.group(1))
        body = strip_module_header(block)
        parse_body_port_decls(body, ports, directions)
        assignments, condition_uses = parse_assignments_and_conditions(body)
        modules[name] = Module(
            name=name,
            file=source,
            body=body,
            ports=ports,
            port_directions=directions,
            assignments=assignments,
            condition_uses=condition_uses,
        )

    return modules


def infer_top_candidates(modules: Dict[str, Module]) -> List[str]:
    all_mods = set(modules)
    children = {
        inst.module_type
        for module in modules.values()
        for inst in module.instances
        if inst.module_type in all_mods
    }
    return sorted(all_mods - children)


def reachable_count(modules: Dict[str, Module], top: str) -> int:
    seen = {top}
    queue = deque([top])
    while queue:
        name = queue.popleft()
        for inst in modules[name].instances:
            if inst.module_type in modules and inst.module_type not in seen:
                seen.add(inst.module_type)
                queue.append(inst.module_type)
    return len(seen)


def infer_top(modules: Dict[str, Module]) -> Tuple[str, List[str]]:
    candidates = infer_top_candidates(modules)
    if not candidates:
        fallback = sorted(modules)[0]
        return fallback, []
    return max(candidates, key=lambda name: (reachable_count(modules, name), name)), candidates


def build_design(filelist: Path, explicit_top: str | None) -> Design:
    files = parse_filelist(filelist)
    if not files:
        raise ValueError(f"No Verilog/SystemVerilog files found in filelist: {filelist}")
    modules: Dict[str, Module] = {}
    for path in files:
        modules.update(extract_modules(path.read_text(encoding="utf-8", errors="ignore"), path))
    if not modules:
        raise ValueError("No module declarations found.")
    known_modules = set(modules)
    for module in modules.values():
        for statement in split_semicolon_statements(module.body):
            instance = parse_instance(statement, known_modules)
            if instance:
                module.instances.append(instance)
    top_candidates = infer_top_candidates(modules)
    top = explicit_top
    if not top:
        top, top_candidates = infer_top(modules)
    if top not in modules:
        raise ValueError(f"Top module '{top}' not found in parsed modules.")
    return Design(modules=modules, top=top, top_candidates=top_candidates, top_is_explicit=bool(explicit_top))


def build_hierarchy(design: Design) -> HierNode:
    def expand(module_name: str, inst_name: str | None, path: Tuple[str, ...], parent: HierNode | None, parent_inst: Instance | None, ancestry: Set[str]) -> HierNode:
        node = HierNode(module_name=module_name, inst_name=inst_name, path=path, parent=parent, parent_instance=parent_inst)
        for inst in design.modules[module_name].instances:
            if inst.module_type not in design.modules or inst.module_type in ancestry:
                continue
            child = expand(inst.module_type, inst.name, path + (inst.name,), node, inst, ancestry | {inst.module_type})
            node.children.append(child)
        return node

    return expand(design.top, None, (design.top,), None, None, {design.top})


def iter_hierarchy(node: HierNode) -> Iterable[HierNode]:
    yield node
    for child in node.children:
        yield from iter_hierarchy(child)


def find_child(node: HierNode, token: str) -> HierNode | None:
    exact = [child for child in node.children if child.inst_name == token]
    if len(exact) == 1:
        return exact[0]
    by_module = [child for child in node.children if child.module_name == token]
    if len(by_module) == 1:
        return by_module[0]
    return None


def resolve_start(root: HierNode, signal_path: str) -> SignalRef:
    parts = [part for part in signal_path.split(".") if part]
    if len(parts) < 2:
        raise ValueError("Signal path must include a root/module path and a signal name.")
    signal = parts[-1]
    path_parts = parts[:-1]
    node = root
    if path_parts and path_parts[0] == root.module_name:
        path_parts = path_parts[1:]
    for token in path_parts:
        child = find_child(node, token)
        if not child:
            raise ValueError(f"Cannot resolve hierarchy token '{token}' below {node.display_path()}.")
        node = child
    return SignalRef(path=node.path, module_name=node.module_name, signal=signal)


def node_by_path(root: HierNode, path: Tuple[str, ...]) -> HierNode:
    for node in iter_hierarchy(root):
        if node.path == path:
            return node
    raise KeyError(path)


def connected_expr_signal(expr: str) -> List[str]:
    return expression_signals(expr)


SIMILARITY_IGNORE_TOKENS = {
    "d",
    "i",
    "in",
    "input",
    "nxt",
    "next",
    "o",
    "out",
    "output",
    "q",
    "r",
    "reg",
    "wire",
}


def signal_name_tokens(signal: str) -> Set[str]:
    tokens: Set[str] = set()
    for part in re.split(r"[^A-Za-z0-9]+", base_signal(signal).lower()):
        if not part:
            continue
        for token in re.findall(r"[a-z]+|[0-9]+", part):
            if token and token not in SIMILARITY_IGNORE_TOKENS:
                tokens.add(token)
    return tokens


def signal_name_similarity(left: str, right: str) -> float:
    if base_signal(left) == base_signal(right):
        return 1.0
    left_tokens = signal_name_tokens(left)
    right_tokens = signal_name_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def ref_direction(design: Design, ref: SignalRef) -> str:
    module = design.modules.get(ref.module_name)
    if not module:
        return "unknown"
    return module.port_directions.get(ref.signal, "unknown")


def path_score(design: Design, root: HierNode, start: SignalRef, path: List[TraceEdge]) -> Tuple[int, int, int, int, int]:
    refs = refs_for_path(start, path)
    terminal = refs[-1]
    direction = ref_direction(design, terminal)
    if direction in {"output", "inout"} and terminal.path == root.path:
        output_rank = 3
    elif direction in {"output", "inout"}:
        output_rank = 2
    else:
        output_rank = 0
    final_similarity = int(signal_name_similarity(start.signal, terminal.signal) * 1000)
    best_output_similarity = max(
        (signal_name_similarity(start.signal, ref.signal) for ref in refs if ref_direction(design, ref) in {"output", "inout"}),
        default=0.0,
    )
    clean_rank = 0 if any(edge.stopped for edge in path) else 1
    return (
        output_rank,
        int(best_output_similarity * 1000),
        final_similarity,
        clean_rank,
        len(path),
    )


def trace_datapath(design: Design, root: HierNode, start: SignalRef, max_steps: int = 1000) -> TraceResult:
    queue: deque[Tuple[SignalRef, List[TraceEdge]]] = deque([(start, [])])
    visited: Set[Tuple[Tuple[str, ...], str]] = set()
    edges: List[TraceEdge] = []
    terminal_paths: List[List[TraceEdge]] = []
    stop_edges: List[TraceEdge] = []
    terminal_refs: List[SignalRef] = []
    steps = 0

    while queue and steps < max_steps:
        current, path_edges = queue.popleft()
        key = (current.path, current.signal)
        if key in visited:
            continue
        visited.add(key)
        steps += 1
        node = node_by_path(root, current.path)
        module = design.modules[current.module_name]
        advanced = False

        for kind, signals, expr in module.condition_uses:
            if current.signal in signals:
                stop = TraceEdge(
                    src=current,
                    dst=current,
                    action="condition",
                    detail=f"{kind} ({expr})",
                    stopped=True,
                    reason="signal enters conditional expression",
                )
                stop_edges.append(stop)
                edges.append(stop)
                terminal_paths.append(path_edges + [stop])

        for assignment in module.assignments:
            if current.signal not in assignment.rhs_signals:
                continue
            for lhs_signal in assignment.lhs_signals:
                dst = SignalRef(path=current.path, module_name=current.module_name, signal=lhs_signal)
                detail = f"{assignment.kind}: {assignment.lhs} = {assignment.rhs}"
                if assignment.conditional:
                    stop = TraceEdge(
                        src=current,
                        dst=dst,
                        action="conditional assignment",
                        detail=detail,
                        stopped=True,
                        reason="assignment is under conditional logic",
                    )
                    stop_edges.append(stop)
                    edges.append(stop)
                    terminal_paths.append(path_edges + [stop])
                    continue
                edge = TraceEdge(src=current, dst=dst, action="assign", detail=detail)
                edges.append(edge)
                queue.append((dst, path_edges + [edge]))
                advanced = True

        for child in node.children:
            child_module = design.modules[child.module_name]
            assert child.parent_instance is not None
            for port_name, expr in child.parent_instance.connections.items():
                if port_name.startswith("__pos"):
                    continue
                direction = child_module.port_directions.get(port_name, "unknown")
                if direction == "output":
                    continue
                if not signal_in_expr(current.signal, expr):
                    continue
                dst = SignalRef(path=child.path, module_name=child.module_name, signal=port_name)
                edge = TraceEdge(
                    src=current,
                    dst=dst,
                    action="enter block",
                    detail=f"{child.inst_name}.{port_name} <= {expr}",
                )
                edges.append(edge)
                queue.append((dst, path_edges + [edge]))
                advanced = True

        if node.parent and node.parent_instance:
            direction = module.port_directions.get(current.signal, "unknown")
            if direction in {"output", "inout", "unknown"}:
                expr = node.parent_instance.connections.get(current.signal)
                if expr:
                    for parent_signal in connected_expr_signal(expr):
                        dst = SignalRef(path=node.parent.path, module_name=node.parent.module_name, signal=parent_signal)
                        edge = TraceEdge(
                            src=current,
                            dst=dst,
                            action="leave block",
                            detail=f"{node.inst_name}.{current.signal} => {expr}",
                        )
                        edges.append(edge)
                        queue.append((dst, path_edges + [edge]))
                        advanced = True

        if not advanced:
            terminal_refs.append(current)
            terminal_paths.append(path_edges)

    unique_paths: List[List[TraceEdge]] = []
    seen_path_keys: Set[Tuple[Tuple[str, str, str, str], ...]] = set()
    for path in terminal_paths:
        path_key = tuple((edge.src.label(), edge.dst.label(), edge.action, edge.reason) for edge in path)
        if path_key in seen_path_keys:
            continue
        seen_path_keys.add(path_key)
        unique_paths.append(path)

    main_path = max(unique_paths, key=lambda path: path_score(design, root, start, path)) if unique_paths else []
    longest_path = max(unique_paths, key=len) if unique_paths else []
    return TraceResult(
        start=start,
        edges=edges,
        terminal_refs=terminal_refs,
        stop_edges=stop_edges,
        main_path=main_path,
        longest_path=longest_path,
        all_paths=unique_paths,
    )


def stable_int(*parts: str) -> int:
    digest = hashlib.sha1("::".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def excalidraw_base_element(element_id: str, element_type: str, x: float, y: float) -> Dict[str, Any]:
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
        "seed": stable_int(element_id, "seed") % 2_147_483_647,
        "version": 1,
        "versionNonce": stable_int(element_id, "version") % 2_147_483_647,
        "isDeleted": False,
        "boundElements": None,
        "updated": 1,
        "link": None,
        "locked": False,
    }


def emit_trace_excalidraw(result: TraceResult) -> str:
    path_refs = [result.start]
    for edge in result.main_path:
        path_refs.append(edge.dst)

    elements: List[Dict[str, Any]] = []
    node_width = 260
    node_height = 90
    x_gap = 120
    y = 80
    positions: Dict[int, Tuple[float, float]] = {}

    for index, ref in enumerate(path_refs):
        x = 80 + index * (node_width + x_gap)
        positions[index] = (x, y)
        fill, border = DEPTH_PALETTE[index % len(DEPTH_PALETTE)]
        rect_id = f"trace-node-{index}-{stable_int(ref.label()):08x}"
        text_id = f"trace-text-{index}-{stable_int(ref.label(), 'text'):08x}"
        rect = excalidraw_base_element(rect_id, "rectangle", x, y)
        rect.update(
            {
                "width": node_width,
                "height": node_height,
                "strokeColor": border,
                "backgroundColor": fill,
                "strokeWidth": 2,
                "roundness": {"type": 3},
            }
        )
        elements.append(rect)
        label = f"{'.'.join(ref.path)}\n{ref.signal}"
        text = excalidraw_base_element(text_id, "text", x + 12, y + 18)
        text.update(
            {
                "width": node_width - 24,
                "height": 52,
                "strokeColor": "#1e1e1e",
                "roughness": 0,
                "fontSize": 16,
                "fontFamily": 1,
                "text": label,
                "rawText": label,
                "textAlign": "center",
                "verticalAlign": "middle",
                "baseline": 40,
                "containerId": None,
                "originalText": label,
                "lineHeight": 1.2,
            }
        )
        elements.append(text)

    for index, edge in enumerate(result.main_path):
        src_x, src_y = positions[index]
        dst_x, dst_y = positions[index + 1]
        start_x = src_x + node_width
        start_y = src_y + node_height / 2
        end_x = dst_x
        end_y = dst_y + node_height / 2
        arrow_id = f"trace-arrow-{index}-{stable_int(edge.src.label(), edge.dst.label()):08x}"
        arrow = excalidraw_base_element(arrow_id, "arrow", start_x, start_y)
        arrow.update(
            {
                "width": end_x - start_x,
                "height": end_y - start_y,
                "strokeColor": "#D84315" if edge.action == "assign" else "#2F5E8E",
                "strokeWidth": 2,
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
        rename = f"{edge.src.signal} -> {edge.dst.signal}" if edge.src.signal != edge.dst.signal else edge.action
        label = excalidraw_base_element(f"trace-label-{index}-{stable_int(rename):08x}", "text", start_x + 12, start_y - 36)
        label.update(
            {
                "width": max(110, min(280, len(rename) * 8)),
                "height": 22,
                "strokeColor": "#1e1e1e",
                "backgroundColor": "#ffffff",
                "roughness": 0,
                "fontSize": 14,
                "fontFamily": 1,
                "text": rename,
                "rawText": rename,
                "textAlign": "center",
                "verticalAlign": "middle",
                "baseline": 17,
                "containerId": None,
                "originalText": rename,
                "lineHeight": 1.2,
            }
        )
        elements.append(label)

    scene = {
        "type": "excalidraw",
        "version": 2,
        "source": "RTL_datapath_trace.py",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }
    return json.dumps(scene, indent=2) + "\n"


def refs_for_path(start: SignalRef, path: List[TraceEdge]) -> List[SignalRef]:
    refs = [start]
    for edge in path:
        if edge.dst != refs[-1]:
            refs.append(edge.dst)
    return refs


def path_key(path: List[TraceEdge]) -> Tuple[Tuple[str, str, str, str], ...]:
    return tuple((edge.src.label(), edge.dst.label(), edge.action, edge.reason) for edge in path)


def append_folded_block(lines: List[str], header: str, fold_title: str, body_lines: List[str]) -> None:
    lines.append(header)
    lines.append(f"//{{{{{{ {fold_title}")
    lines.extend(body_lines or ["  <none>"])
    lines.append("//}}}")


def path_summary_lines(result: TraceResult) -> List[str]:
    ordered_paths = [result.main_path] + result.all_paths if result.all_paths else [[]]
    lines: List[str] = []
    seen_paths: Set[Tuple[Tuple[str, str, str, str], ...]] = set()
    path_index = 0
    for path in ordered_paths:
        key = path_key(path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        refs = refs_for_path(result.start, path)
        append_folded_block(
            lines,
            f"[TRACE] [PATH_{path_index}]",
            refs[-1].label(),
            [f"  {ref.label()}" for ref in refs],
        )
        path_index += 1
    return lines


def named_path_summary_lines(result: TraceResult, name: str, path: List[TraceEdge], color: bool = False) -> List[str]:
    label = f"{BLUE}{name}{RESET}" if color else name
    refs = refs_for_path(result.start, path)
    lines: List[str] = []
    append_folded_block(
        lines,
        f"[TRACE] {label}",
        refs[-1].label(),
        [f"  {ref.label()}" for ref in refs],
    )
    return lines


def print_lines(lines: List[str]) -> None:
    for line in lines:
        print(line)


def safe_trace_filename(signal: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.$]+", "_", signal).strip("_")
    if not safe:
        safe = "signal"
    return Path(f"trace_{safe}.txt")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace a datapath from a hierarchical RTL signal.")
    parser.add_argument("filelist", type=Path, help="Path to Verilog/SystemVerilog filelist")
    parser.add_argument("signal", help="Hierarchical signal path, for example tb_top.top.i_data")
    parser.add_argument("--top", type=str, default=None, help="Root module override. Defaults to inferred TOP.")
    parser.add_argument(
        "--excalidraw",
        type=Path,
        default=Path("rtl_datapath_trace.excalidraw"),
        help="Output Excalidraw file for the main trace path",
    )
    parser.add_argument("--max-steps", type=int, default=1000, help="Maximum trace expansion steps")
    args = parser.parse_args()

    design = build_design(args.filelist, args.top)
    root = build_hierarchy(design)
    start = resolve_start(root, args.signal)
    result = trace_datapath(design, root, start, max_steps=args.max_steps)
    text_path = safe_trace_filename(args.signal)
    path_lines = path_summary_lines(result)
    args.excalidraw.write_text(emit_trace_excalidraw(result), encoding="utf-8")
    text_path.write_text(
        "\n".join(path_lines) + "\n",
        encoding="utf-8",
    )
    print_lines(
        path_lines
        + named_path_summary_lines(result, "MAIN", result.main_path, color=True)
        + named_path_summary_lines(result, "LONGEST", result.longest_path, color=True)
    )


if __name__ == "__main__":
    main()
