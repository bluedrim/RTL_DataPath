#!/usr/bin/env python3
"""Trace a likely RTL datapath from a hierarchical signal path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
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
ENV_VAR_PAREN_RE = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
RTL_FILE_EXTENSIONS = {".v", ".sv", ".vh", ".svh"}
FILELIST_EXTENSIONS = {".f", ".flist", ".lst", ".list"}
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
    direction: str
    edges: List[TraceEdge]
    terminal_refs: List[SignalRef]
    stop_edges: List[TraceEdge]
    main_path: List[TraceEdge]
    longest_path: List[TraceEdge]
    all_paths: List[List[TraceEdge]]


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


def signal_matches(left: str, right: str) -> bool:
    return left == right or base_signal(left) == base_signal(right)


def signal_in_signals(signal: str, signals: List[str]) -> bool:
    return any(signal_matches(signal, candidate) for candidate in signals)


def signal_in_expr(signal: str, expr: str) -> bool:
    return signal_in_signals(signal, expression_signals(expr))


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


def signal_root_module(signal_path: str, modules: Dict[str, Module]) -> str | None:
    parts = [part for part in signal_path.split(".") if part]
    if len(parts) < 2:
        return None
    return parts[0] if parts[0] in modules else None


def apply_signal_root(design: Design, signal_path: str) -> Design:
    implied_top = signal_root_module(signal_path, design.modules)
    if not implied_top or implied_top == design.top:
        return design
    return Design(
        modules=design.modules,
        top=implied_top,
        top_candidates=design.top_candidates,
        top_is_explicit=design.top_is_explicit,
    )


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


def declaration_signals(body: str) -> List[str]:
    signals: List[str] = []
    seen: Set[str] = set()
    for match in re.finditer(r"\b(?:wire|reg|logic|bit)\b([^;]*);", body, re.S):
        for signal in expression_signals(match.group(1)):
            if signal not in seen:
                seen.add(signal)
                signals.append(signal)
    return signals


def module_known_signals(design: Design, module_name: str) -> List[str]:
    module = design.modules[module_name]
    signals: List[str] = []
    seen: Set[str] = set()

    def add(signal: str) -> None:
        if signal not in seen:
            seen.add(signal)
            signals.append(signal)

    for signal in module.ports:
        add(signal)
    for signal in module.port_directions:
        add(signal)
    for signal in declaration_signals(module.body):
        add(signal)
    for assignment in module.assignments:
        for signal in assignment.lhs_signals + assignment.rhs_signals:
            add(signal)
    for _, condition_signals, _ in module.condition_uses:
        for signal in condition_signals:
            add(signal)
    for instance in module.instances:
        for port_name, expr in instance.connections.items():
            if port_name.startswith("__pos"):
                continue
            for signal in expression_signals(expr):
                add(signal)
    return signals


def validate_start_signal(design: Design, start: SignalRef) -> None:
    known_signals = module_known_signals(design, start.module_name)
    if not signal_in_signals(start.signal, known_signals):
        raise ValueError(f"Signal '{start.signal}' not found in module '{start.module_name}'.")


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
    return module.port_directions.get(ref.signal, module.port_directions.get(base_signal(ref.signal), "unknown"))


def port_connection(instance: Instance, signal: str) -> str | None:
    if signal in instance.connections:
        return instance.connections[signal]
    signal_base = base_signal(signal)
    return instance.connections.get(signal_base)


def path_score(
    design: Design,
    root: HierNode,
    start: SignalRef,
    path: List[TraceEdge],
    trace_direction: str,
) -> Tuple[int, int, int, int, int]:
    refs = refs_for_path(start, path)
    terminal = refs[-1]
    direction = ref_direction(design, terminal)
    target_directions = {"input", "inout"} if trace_direction == "reverse" else {"output", "inout"}
    if direction in target_directions and terminal.path == root.path:
        terminal_rank = 3
    elif direction in target_directions:
        terminal_rank = 2
    else:
        terminal_rank = 0
    final_similarity = int(signal_name_similarity(start.signal, terminal.signal) * 1000)
    best_target_similarity = max(
        (
            signal_name_similarity(start.signal, ref.signal)
            for ref in refs
            if ref_direction(design, ref) in target_directions
        ),
        default=0.0,
    )
    clean_rank = 0 if any(edge.stopped for edge in path) else 1
    return (
        terminal_rank,
        int(best_target_similarity * 1000),
        final_similarity,
        clean_rank,
        len(path),
    )


def resolve_trace_direction(design: Design, start: SignalRef, requested_direction: str = "auto") -> str:
    if requested_direction in {"forward", "reverse"}:
        return requested_direction
    start_direction = ref_direction(design, start)
    return "reverse" if start_direction == "output" else "forward"


def trace_datapath(
    design: Design,
    root: HierNode,
    start: SignalRef,
    max_steps: int = 1000,
    requested_direction: str = "auto",
) -> TraceResult:
    trace_direction = resolve_trace_direction(design, start, requested_direction)
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
        stopped_here = False

        for kind, signals, expr in module.condition_uses:
            if signal_in_signals(current.signal, signals):
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
                stopped_here = True

        for assignment in module.assignments:
            detail = f"{assignment.kind}: {assignment.lhs} = {assignment.rhs}"
            if trace_direction == "forward":
                if not signal_in_signals(current.signal, assignment.rhs_signals):
                    continue
                next_signals = assignment.lhs_signals
            else:
                if not signal_in_signals(current.signal, assignment.lhs_signals):
                    continue
                next_signals = assignment.rhs_signals

            if assignment.conditional:
                stop = TraceEdge(
                    src=current,
                    dst=current,
                    action="conditional assignment",
                    detail=detail,
                    stopped=True,
                    reason="assignment is under conditional logic",
                )
                stop_edges.append(stop)
                edges.append(stop)
                terminal_paths.append(path_edges + [stop])
                stopped_here = True
                continue

            for next_signal in next_signals:
                dst = SignalRef(path=current.path, module_name=current.module_name, signal=next_signal)
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
                if trace_direction == "forward" and direction == "output":
                    continue
                if trace_direction == "reverse" and direction == "input":
                    continue
                if not signal_in_expr(current.signal, expr):
                    continue
                dst = SignalRef(path=child.path, module_name=child.module_name, signal=port_name)
                detail = (
                    f"{child.inst_name}.{port_name} <= {expr}"
                    if trace_direction == "forward"
                    else f"{expr} <= {child.inst_name}.{port_name}"
                )
                edge = TraceEdge(
                    src=current,
                    dst=dst,
                    action="enter block",
                    detail=detail,
                )
                edges.append(edge)
                queue.append((dst, path_edges + [edge]))
                advanced = True

        if node.parent and node.parent_instance:
            direction = module.port_directions.get(
                current.signal,
                module.port_directions.get(base_signal(current.signal), "unknown"),
            )
            if trace_direction == "forward":
                can_cross_to_parent = direction in {"output", "inout", "unknown"}
            else:
                can_cross_to_parent = direction in {"input", "inout", "unknown"}
            if can_cross_to_parent:
                expr = port_connection(node.parent_instance, current.signal)
                if expr:
                    for parent_signal in connected_expr_signal(expr):
                        dst = SignalRef(path=node.parent.path, module_name=node.parent.module_name, signal=parent_signal)
                        detail = (
                            f"{node.inst_name}.{current.signal} => {expr}"
                            if trace_direction == "forward"
                            else f"{node.inst_name}.{current.signal} <= {expr}"
                        )
                        edge = TraceEdge(
                            src=current,
                            dst=dst,
                            action="leave block",
                            detail=detail,
                        )
                        edges.append(edge)
                        queue.append((dst, path_edges + [edge]))
                        advanced = True

        if not advanced and not stopped_here:
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

    main_path = (
        max(unique_paths, key=lambda path: path_score(design, root, start, path, trace_direction))
        if unique_paths
        else []
    )
    longest_path = max(unique_paths, key=len) if unique_paths else []
    return TraceResult(
        start=start,
        direction=trace_direction,
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


def wrap_text_chunks(text: str, max_chars: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    return [text[index : index + max_chars] for index in range(0, len(text), max_chars)]


def wrap_hierarchy_label(text: str, max_chars: int) -> List[str]:
    lines: List[str] = []
    current = ""
    for part in text.split("."):
        if not part:
            continue
        if len(part) > max_chars:
            if current:
                lines.append(current)
                current = ""
            lines.extend(wrap_text_chunks(part, max_chars))
            continue
        candidate = part if not current else f"{current}.{part}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = part
    if current:
        lines.append(current)
    return lines or [text]


def trace_node_label(ref: SignalRef, max_chars: int) -> str:
    lines = wrap_hierarchy_label(".".join(ref.path), max_chars)
    lines.extend(wrap_hierarchy_label(ref.signal, max_chars))
    return "\n".join(lines)


def trace_edge_label(text: str, max_chars: int) -> str:
    lines: List[str] = []
    for token in text.split():
        if not lines:
            lines.extend(wrap_text_chunks(token, max_chars))
            continue
        candidate = f"{lines[-1]} {token}"
        if len(candidate) <= max_chars:
            lines[-1] = candidate
        else:
            lines.extend(wrap_text_chunks(token, max_chars))
    return "\n".join(lines or [text])


def emit_trace_excalidraw(result: TraceResult) -> str:
    path_refs = [result.start]
    for edge in result.main_path:
        path_refs.append(edge.dst)

    elements: List[Dict[str, Any]] = []
    node_width = 260
    x_gap = 120
    y = 80
    node_font_size = 16
    node_line_height = 1.2
    node_text_pad_x = 12
    node_text_pad_y = 12
    node_label_chars = 26
    node_labels = [trace_node_label(ref, node_label_chars) for ref in path_refs]
    max_node_lines = max((label.count("\n") + 1 for label in node_labels), default=2)
    node_text_height = max(52, max_node_lines * node_font_size * node_line_height)
    node_height = max(90, node_text_height + node_text_pad_y * 2)
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
        label = node_labels[index]
        text = excalidraw_base_element(text_id, "text", x + node_text_pad_x, y + node_text_pad_y)
        text.update(
            {
                "width": node_width - node_text_pad_x * 2,
                "height": node_height - node_text_pad_y * 2,
                "strokeColor": "#1e1e1e",
                "roughness": 0,
                "fontSize": node_font_size,
                "fontFamily": 1,
                "text": label,
                "rawText": label,
                "textAlign": "center",
                "verticalAlign": "middle",
                "baseline": node_font_size * node_line_height * (label.count("\n") + 1),
                "containerId": None,
                "originalText": label,
                "lineHeight": node_line_height,
            }
        )
        elements.append(text)

    for index, edge in enumerate(result.main_path):
        src_x, src_y = positions[index]
        dst_x, dst_y = positions[index + 1]
        if result.direction == "reverse":
            start_x = dst_x
            end_x = src_x + node_width
        else:
            start_x = src_x + node_width
            end_x = dst_x
        start_y = src_y + node_height / 2
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
        rename_label = trace_edge_label(rename, 14)
        rename_lines = rename_label.count("\n") + 1
        label_width = 108
        label_height = max(22, rename_lines * 14 * 1.2 + 6)
        label_x = min(start_x, end_x) + (abs(end_x - start_x) - label_width) / 2
        label_y = y + node_height + 14
        label = excalidraw_base_element(
            f"trace-label-{index}-{stable_int(rename):08x}",
            "text",
            label_x,
            label_y,
        )
        label.update(
            {
                "width": label_width,
                "height": label_height,
                "strokeColor": "#1e1e1e",
                "backgroundColor": "#ffffff",
                "roughness": 0,
                "fontSize": 14,
                "fontFamily": 1,
                "text": rename_label,
                "rawText": rename_label,
                "textAlign": "center",
                "verticalAlign": "middle",
                "baseline": 14 * 1.2 * rename_lines,
                "containerId": None,
                "originalText": rename_label,
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


def titled_trace_lines(result: TraceResult, lines: List[str]) -> List[str]:
    return ["//{{{" + result.start.label()] + lines + ["//}}}"]


def print_lines(lines: List[str]) -> None:
    for line in lines:
        print(line)


def safe_trace_filename(signal: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.$]+", "_", signal).strip("_")
    if not safe:
        safe = "signal"
    return Path(f"trace_{safe}.txt")


def safe_signal_suffix(signal: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.$]+", "_", signal).strip("_")
    return safe or "signal"


def ensure_parent_dir(path: Path) -> None:
    parent = path.parent
    if parent != Path("."):
        parent.mkdir(parents=True, exist_ok=True)


def strip_signal_line(raw: str) -> str:
    return raw.split("//", 1)[0].split("#", 1)[0].strip()


def read_signal_file(path: Path) -> List[str]:
    signals: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        signal = strip_signal_line(raw)
        if signal:
            signals.append(signal)
    return signals


def excalidraw_path_for_signal(base_path: Path, signal: str, multiple_signals: bool) -> Path:
    if not multiple_signals:
        return base_path
    suffix = safe_signal_suffix(signal)
    return base_path.with_name(f"{base_path.stem}_{suffix}{base_path.suffix or '.excalidraw'}")


def trace_lines_for_signal(
    base_design: Design,
    signal: str,
    explicit_top: str | None,
    max_steps: int,
    direction: str,
    include_named_paths: bool,
) -> Tuple[TraceResult, List[str]]:
    design = base_design if explicit_top else apply_signal_root(base_design, signal)
    root = build_hierarchy(design)
    start = resolve_start(root, signal)
    validate_start_signal(design, start)
    result = trace_datapath(design, root, start, max_steps=max_steps, requested_direction=direction)
    lines = path_summary_lines(result)
    if include_named_paths:
        lines += named_path_summary_lines(result, "MAIN", result.main_path, color=True)
        lines += named_path_summary_lines(result, "LONGEST", result.longest_path, color=True)
    return result, titled_trace_lines(result, lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace a datapath from a hierarchical RTL signal.")
    parser.add_argument("filelist", type=Path, help="Path to Verilog/SystemVerilog filelist")
    parser.add_argument("signal", nargs="?", help="Single hierarchical signal path, for example tb_top.top.i_data")
    parser.add_argument(
        "--signal",
        dest="signal_file",
        type=Path,
        default=None,
        help="File containing one hierarchical signal path per line.",
    )
    parser.add_argument("--top", type=str, default=None, help="Root module override. Defaults to inferred TOP.")
    parser.add_argument(
        "--excalidraw",
        type=Path,
        default=Path("rtl_datapath_trace.excalidraw"),
        help="Output Excalidraw file for the main trace path",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Text output file. For signal-file input, all traces are written here in order.",
    )
    parser.add_argument(
        "--direction",
        choices=["auto", "forward", "reverse"],
        default="auto",
        help="Trace direction. auto traces output ports backward and other signals forward.",
    )
    parser.add_argument("--max-steps", type=int, default=1000, help="Maximum trace expansion steps")
    args = parser.parse_args()

    if bool(args.signal) == bool(args.signal_file):
        parser.error("provide either a single positional signal or --signal <signal_file>")

    base_design = build_design(args.filelist, args.top)
    signal_file = Path(expand_filelist_token(str(args.signal_file))).resolve() if args.signal_file else None
    signals = read_signal_file(signal_file) if signal_file else [str(args.signal)]
    if not signals:
        parser.error(f"no signals found in signal file: {signal_file}")

    multiple_signals = signal_file is not None
    if args.output:
        text_path = args.output
    elif multiple_signals:
        assert signal_file is not None
        text_path = Path(f"trace_{signal_file.stem}.txt")
    else:
        text_path = safe_trace_filename(args.signal)

    file_lines: List[str] = []
    errors: List[str] = []

    for index, signal in enumerate(signals):
        if index:
            file_lines.append("")
            print()
        try:
            result, trace_file_lines = trace_lines_for_signal(
                base_design=base_design,
                signal=signal,
                explicit_top=args.top,
                max_steps=args.max_steps,
                direction=args.direction,
                include_named_paths=False,
            )
            console_body_lines = path_summary_lines(result)
            console_body_lines += named_path_summary_lines(result, "MAIN", result.main_path, color=True)
            console_body_lines += named_path_summary_lines(result, "LONGEST", result.longest_path, color=True)
            trace_console_lines = titled_trace_lines(result, console_body_lines)
            excalidraw_path = excalidraw_path_for_signal(args.excalidraw, signal, multiple_signals)
            ensure_parent_dir(excalidraw_path)
            excalidraw_path.write_text(emit_trace_excalidraw(result), encoding="utf-8")
            file_lines.extend(trace_file_lines)
            print_lines(trace_console_lines)
        except ValueError as exc:
            error_line = f"[ERROR] {signal}: {exc}"
            errors.append(error_line)
            error_block = ["//{{{" + signal, error_line, "//}}}"]
            file_lines.extend(error_block)
            print_lines(error_block)

    ensure_parent_dir(text_path)
    text_path.write_text("\n".join(file_lines) + "\n", encoding="utf-8")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
