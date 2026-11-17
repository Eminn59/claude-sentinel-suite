#!/usr/bin/env python3
"""
spiderweb: interactive dependency map for any codebase.

Scans Python, JavaScript/TypeScript, Go and Rust sources, resolves their
imports into a file-level dependency graph (with lines-of-code and token
weight per file), detects import cycles, and writes one self-contained,
dark-mode HTML page with a D3.js force-directed graph: search, zoom, drag,
hover tooltips, language filters and cycle highlighting.

    python spiderweb.py --target . --open
    python spiderweb.py --target ./src --output deps.html --json deps.json

Zero dependencies. Python 3.10+. The generated page loads D3 v7 from cdnjs.
"""

from __future__ import annotations

import argparse
import ast
import datetime as _dt
import fnmatch
import json
import os
import re
import sys
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__version__ = "1.0.0"

LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
}

COLORS = {
    "python": "#5b9cf6",
    "javascript": "#f5c542",
    "typescript": "#a78bfa",
    "go": "#34d399",
    "rust": "#fb923c",
    "stdlib": "#64748b",
    "package": "#e879f9",
}

EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".claude", ".idea", ".vscode", ".cache",
    "node_modules", "bower_components", "vendor", "third_party",
    "__pycache__", ".venv", "venv", "env", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
    "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit",
    "coverage", ".nightshift",
})
EXCLUDE_GLOBS = ("*.min.js", "*.min.mjs", "*.bundle.js", "*.chunk.js", "*_pb2.py", "*.pb.go")
MAX_FILE_BYTES = 1_000_000
JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")

NODE_BUILTINS = frozenset("""
assert async_hooks buffer child_process cluster console constants crypto dgram diagnostics_channel dns
domain events fs http http2 https inspector module net os path perf_hooks process punycode querystring
readline repl stream string_decoder sys timers tls trace_events tty url util v8 vm wasi worker_threads zlib
""".split())
RUST_STD = frozenset({"std", "core", "alloc", "proc_macro", "test"})
PY_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


@dataclass
class Node:
    id: str
    label: str
    lang: str
    loc: int = 0
    bytes: int = 0
    tokens: int = 0
    external: bool = False
    group: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id, "label": self.label, "lang": self.lang, "loc": self.loc,
            "bytes": self.bytes, "tokens": self.tokens, "external": self.external, "group": self.group,
        }


@dataclass
class Graph:
    root: Path
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: set[tuple[str, str]] = field(default_factory=set)

    def add_edge(self, source: str, target: str) -> None:
        if source != target:
            self.edges.add((source, target))

    def external(self, name: str, kind: str) -> str:
        node_id = f"ext:{name}"
        if node_id not in self.nodes:
            self.nodes[node_id] = Node(id=node_id, label=name, lang=kind, external=True, group=kind)
        return node_id


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------

def iter_source_files(root: Path, exclude_globs: tuple[str, ...] = ()) -> list[Path]:
    globs = EXCLUDE_GLOBS + tuple(exclude_globs)
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDE_DIRS and not d.endswith(".egg-info")
            and not any(fnmatch.fnmatch(d, g) for g in exclude_globs)
        )
        for name in sorted(filenames):
            path = Path(dirpath, name)
            if path.suffix.lower() not in LANGUAGES:
                continue
            rel = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g) for g in globs):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(path)
    return files


def _common_prefix_len(a: str, b: str) -> int:
    pa, pb = PurePosixPath(a).parent.parts, PurePosixPath(b).parent.parts
    n = 0
    for x, y in zip(pa, pb):
        if x != y:
            break
        n += 1
    return n


def _closest(importer: str, candidates: list[str]) -> str:
    return max(candidates, key=lambda c: (_common_prefix_len(importer, c), -len(c)))


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------

class PythonResolver:
    def __init__(self, files: list[str]) -> None:
        self.exact: dict[str, str] = {}
        self.suffix: dict[str, list[str]] = {}
        for rel in files:
            if not rel.endswith((".py", ".pyi")):
                continue
            parts = list(PurePosixPath(rel).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            self.exact[".".join(parts)] = rel
            for k in range(len(parts)):
                self.suffix.setdefault(".".join(parts[k:]), []).append(rel)

    def lookup(self, importer: str, dotted: str) -> str | None:
        if dotted in self.exact:
            return self.exact[dotted]
        if dotted.split(".")[0] in PY_STDLIB:
            return None
        candidates = self.suffix.get(dotted)
        return _closest(importer, candidates) if candidates else None

    def resolve(self, importer: str, module: str | None, level: int, names: list[str]) -> tuple[list[str], str | None]:
        """Return (internal targets, external top-level name or None)."""
        if level:
            base = list(PurePosixPath(importer).parent.parts)
            if level > 1:
                base = base[: len(base) - (level - 1)] if level - 1 <= len(base) else []
            prefix = ".".join(base + (module.split(".") if module else []))
            targets = []
            for name in names:
                hit = self.exact.get(f"{prefix}.{name}" if prefix else name)
                if hit:
                    targets.append(hit)
            if not targets and prefix in self.exact:
                targets.append(self.exact[prefix])
            return targets, None
        assert module is not None
        targets = []
        for name in names:
            hit = self.lookup(importer, f"{module}.{name}")
            if hit:
                targets.append(hit)
        if not targets:
            parts = module.split(".")
            for k in range(len(parts), 0, -1):
                hit = self.lookup(importer, ".".join(parts[:k]))
                if hit:
                    targets.append(hit)
                    break
        if targets:
            return targets, None
        return [], module.split(".")[0]


def python_imports(source: str) -> list[tuple[str | None, int, list[str]]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        found = []
        for match in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", source, re.M):
            found.append((match.group(1) or match.group(2), 0, []))
        return found
    found: list[tuple[str | None, int, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, 0, []))
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module, node.level, [a.name for a in node.names if a.name != "*"]))
    return found


# --------------------------------------------------------------------------
# JavaScript / TypeScript
# --------------------------------------------------------------------------

_JS_IMPORT = re.compile(
    r"""(?:^|[\s;}])(?:import|export)\s+(?:type\s+)?(?:[\w*{}\s,$]+?\s+from\s+)?["']([^"'\n]+)["']"""
    r"""|(?:\brequire|\bimport)\s*\(\s*["']([^"'\n]+)["']\s*\)""",
    re.MULTILINE,
)


def js_imports(source: str) -> list[str]:
    return [m.group(1) or m.group(2) for m in _JS_IMPORT.finditer(source)]


def resolve_js(importer: str, spec: str, file_set: set[str]) -> str | None:
    if spec.startswith(("@/", "~/")):
        bases = [PurePosixPath("src", spec[2:]), PurePosixPath(spec[2:])]
    elif spec.startswith("."):
        bases = [PurePosixPath(os.path.normpath(PurePosixPath(importer).parent / spec).replace("\\", "/"))]
    elif spec.startswith("/"):
        bases = [PurePosixPath(spec.lstrip("/"))]
    else:
        return None
    for base in bases:
        text = base.as_posix()
        candidates = [text]
        stem, ext = os.path.splitext(text)
        if ext in (".js", ".jsx", ".mjs", ".cjs"):
            candidates += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
        candidates += [text + e for e in JS_EXTENSIONS]
        candidates += [f"{text}/index{e}" for e in JS_EXTENSIONS]
        for cand in candidates:
            if cand in file_set:
                return cand
    return None


def js_external_name(spec: str) -> tuple[str, str]:
    if spec.startswith("node:"):
        return spec[5:].split("/")[0], "stdlib"
    parts = spec.split("/")
    name = "/".join(parts[:2]) if spec.startswith("@") and len(parts) > 1 else parts[0]
    return name, "stdlib" if name in NODE_BUILTINS else "package"


# --------------------------------------------------------------------------
# Go
# --------------------------------------------------------------------------

_GO_SINGLE = re.compile(r'^\s*import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
_GO_BLOCK = re.compile(r"^\s*import\s*\((.*?)\)", re.M | re.S)
_GO_SPEC = re.compile(r'"([^"]+)"')


def go_imports(source: str) -> list[str]:
    found = [m.group(1) for m in _GO_SINGLE.finditer(source)]
    for block in _GO_BLOCK.finditer(source):
        found.extend(m.group(1) for m in _GO_SPEC.finditer(block.group(1)))
    return found


def go_module(root: Path) -> tuple[str, str] | None:
    """(module path, go.mod directory relative to root) for the first go.mod found."""
    for path in sorted(root.rglob("go.mod"), key=lambda p: len(p.parts)):
        if any(part in EXCLUDE_DIRS for part in path.relative_to(root).parts):
            continue
        match = re.search(r"^module\s+(\S+)", path.read_text(encoding="utf-8", errors="replace"), re.M)
        if match:
            rel = path.parent.relative_to(root).as_posix()
            return match.group(1), "" if rel == "." else rel
    return None


# --------------------------------------------------------------------------
# Rust
# --------------------------------------------------------------------------

_RS_MOD = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", re.M)
_RS_USE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([^;]+);", re.M)
_RS_EXTERN = re.compile(r"^\s*extern\s+crate\s+(\w+)", re.M)


def _expand_use(path: str) -> list[str]:
    path = " ".join(path.split())
    if "{" not in path:
        return [path.split(" as ")[0].strip()]
    prefix, _, rest = path.partition("{")
    inner = rest.rsplit("}", 1)[0]
    items, depth, current = [], 0, ""
    for ch in inner:
        if ch == "," and depth == 0:
            items.append(current)
            current = ""
            continue
        depth += ch == "{"
        depth -= ch == "}"
        current += ch
    items.append(current)
    out = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        if item == "self":
            out.append(prefix.rstrip(":"))
        else:
            out.extend(_expand_use(prefix + item))
    return out


def _rust_module_dir(rel: str) -> PurePosixPath:
    p = PurePosixPath(rel)
    if p.name in ("mod.rs", "lib.rs", "main.rs"):
        return p.parent
    return p.parent / p.stem


def _rust_crate_root(rel: str, file_set: set[str]) -> PurePosixPath:
    p = PurePosixPath(rel).parent
    while True:
        if (p / "lib.rs").as_posix() in file_set or (p / "main.rs").as_posix() in file_set:
            return p
        if p == p.parent or str(p) in ("", "."):
            return PurePosixPath(rel).parent
        p = p.parent


def _rust_file_for(base: PurePosixPath, segments: list[str], file_set: set[str]) -> str | None:
    for k in range(len(segments), 0, -1):
        stem = base.joinpath(*segments[:k])
        for cand in (f"{stem.as_posix()}.rs", f"{stem.as_posix()}/mod.rs"):
            cand = cand.lstrip("./") if cand.startswith("./") else cand
            if cand in file_set:
                return cand
    return None


def resolve_rust(importer: str, source: str, file_set: set[str]) -> tuple[list[str], list[tuple[str, str]]]:
    internal: list[str] = []
    external: list[tuple[str, str]] = []
    module_dir = _rust_module_dir(importer)
    for match in _RS_MOD.finditer(source):
        hit = _rust_file_for(module_dir, [match.group(1)], file_set)
        if hit:
            internal.append(hit)
    crate_root = _rust_crate_root(importer, file_set)
    for match in _RS_USE.finditer(source):
        for path in _expand_use(match.group(1)):
            segs = [s for s in path.replace("::", " ").split() if s and s != "*"]
            if not segs:
                continue
            head = segs[0]
            if head == "crate":
                hit = _rust_file_for(crate_root, segs[1:], file_set)
            elif head == "super":
                base = module_dir.parent
                rest = segs[1:]
                while rest and rest[0] == "super":
                    base, rest = base.parent, rest[1:]
                hit = _rust_file_for(base, rest, file_set)
            elif head == "self":
                hit = _rust_file_for(module_dir, segs[1:], file_set)
            else:
                hit = None
                external.append((head, "stdlib" if head in RUST_STD else "package"))
            if hit:
                internal.append(hit)
    for match in _RS_EXTERN.finditer(source):
        name = match.group(1)
        external.append((name, "stdlib" if name in RUST_STD else "package"))
    return internal, external


# --------------------------------------------------------------------------
# Graph building
# --------------------------------------------------------------------------

def find_cycles(nodes: list[str], edges: set[tuple[str, str]]) -> list[list[str]]:
    """Strongly connected components with more than one node (iterative Tarjan)."""
    adjacency: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        if a in adjacency and b in adjacency:
            adjacency[a].append(b)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0
    for start in nodes:
        if start in index:
            continue
        work = [(start, iter(adjacency[start]))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(adjacency[child])))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))
    return components


def build_graph(root: Path, include_external: bool = True, exclude_globs: tuple[str, ...] = ()) -> Graph:
    graph = Graph(root=root)
    paths = iter_source_files(root, exclude_globs)
    sources: dict[str, str] = {}
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources[rel] = text
        top = rel.split("/")[0] if "/" in rel else "."
        graph.nodes[rel] = Node(
            id=rel,
            label=PurePosixPath(rel).name,
            lang=LANGUAGES[path.suffix.lower()],
            loc=sum(1 for line in text.splitlines() if line.strip()),
            bytes=len(text.encode("utf-8")),
            tokens=len(text) // 4,
            group=top,
        )
    file_set = set(sources)
    py = PythonResolver(sorted(file_set))
    gomod = go_module(root)
    go_dirs: dict[str, list[str]] = {}
    for rel in file_set:
        if rel.endswith(".go") and not rel.endswith("_test.go"):
            go_dirs.setdefault(PurePosixPath(rel).parent.as_posix(), []).append(rel)

    def add_external(src: str, name: str, kind: str) -> None:
        if include_external and name:
            graph.add_edge(src, graph.external(name, kind))

    for rel, text in sources.items():
        lang = graph.nodes[rel].lang
        if lang == "python":
            for module, level, names in python_imports(text):
                targets, ext = py.resolve(rel, module, level, names)
                for target in targets:
                    graph.add_edge(rel, target)
                if ext:
                    add_external(rel, ext, "stdlib" if ext in PY_STDLIB else "package")
        elif lang in ("javascript", "typescript"):
            for spec in js_imports(text):
                target = resolve_js(rel, spec, file_set)
                if target:
                    graph.add_edge(rel, target)
                elif not spec.startswith((".", "/", "@/", "~/")):
                    add_external(rel, *js_external_name(spec))
        elif lang == "go":
            for imp in go_imports(text):
                if gomod and (imp == gomod[0] or imp.startswith(gomod[0] + "/")):
                    sub = imp[len(gomod[0]):].strip("/")
                    pkg_dir = "/".join(p for p in (gomod[1], sub) if p) or "."
                    for target in go_dirs.get(pkg_dir, [])[:50]:
                        graph.add_edge(rel, target)
                else:
                    first = imp.split("/")[0]
                    if "." in first:
                        add_external(rel, "/".join(imp.split("/")[:3]), "package")
                    else:
                        add_external(rel, imp, "stdlib")
        elif lang == "rust":
            internal, external = resolve_rust(rel, text, file_set)
            for target in internal:
                graph.add_edge(rel, target)
            for name, kind in external:
                add_external(rel, name, kind)
    return graph


def graph_payload(graph: Graph, show_external: bool | None = None) -> dict:
    internal_ids = [n for n, node in graph.nodes.items() if not node.external]
    cycles = find_cycles(internal_ids, graph.edges)
    cycle_of = {member: idx for idx, comp in enumerate(cycles) for member in comp}
    links = []
    for a, b in sorted(graph.edges):
        in_cycle = a in cycle_of and cycle_of.get(a) == cycle_of.get(b)
        links.append({"source": a, "target": b, "cycle": in_cycle})
    internal_nodes = [graph.nodes[n] for n in internal_ids]
    if show_external is None:
        show_external = len(internal_nodes) <= 250
    return {
        "meta": {
            "title": graph.root.name or str(graph.root),
            "generated": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "generator": f"spiderweb v{__version__}",
            "files": len(internal_nodes),
            "externals": len(graph.nodes) - len(internal_nodes),
            "links": len(links),
            "loc": sum(n.loc for n in internal_nodes),
            "tokens": sum(n.tokens for n in internal_nodes),
            "bytes": sum(n.bytes for n in internal_nodes),
            "cycles": cycles,
            "showExternal": show_external,
        },
        "colors": COLORS,
        "nodes": [n.to_json() for n in sorted(graph.nodes.values(), key=lambda x: x.id)],
        "links": links,
    }


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Spiderweb · __TITLE__</title>
<style>
:root{--bg:#090d16;--panel:rgba(15,21,35,.86);--border:#1e293b;--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--cycle:#f43f5e;}
*{box-sizing:border-box}
html,body{margin:0;height:100%;overflow:hidden;color:var(--text);background:radial-gradient(1200px 800px at 50% -10%,#14203a 0%,var(--bg) 60%);font:13px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
#graph{position:fixed;inset:0}
#graph svg{width:100%;height:100%;display:block;cursor:grab}
#graph svg:active{cursor:grabbing}
.panel{position:fixed;background:var(--panel);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);border:1px solid var(--border);border-radius:14px;padding:14px 16px;box-shadow:0 12px 40px rgba(0,0,0,.45)}
#hud{top:16px;left:16px;width:300px}
#hud h1{font-size:15px;margin:0;display:flex;align-items:center;gap:8px;letter-spacing:.01em}
#hud h1 i{width:9px;height:9px;border-radius:50%;background:var(--accent);box-shadow:0 0 12px var(--accent)}
.sub{color:var(--muted);font-size:12px;margin:2px 0 12px;word-break:break-all}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
.stat{background:rgba(2,6,23,.6);border:1px solid var(--border);border-radius:9px;padding:7px 10px}
.stat b{display:block;font-size:17px;font-variant-numeric:tabular-nums}
.stat span{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.07em}
.stat.bad b{color:var(--cycle)}
#search{width:100%;padding:8px 10px;border-radius:9px;border:1px solid var(--border);background:rgba(2,6,23,.7);color:var(--text);outline:none;font:inherit}
#search:focus{border-color:var(--accent)}
.toggle{display:flex;align-items:center;gap:8px;margin-top:10px;color:var(--muted);font-size:12px;cursor:pointer;user-select:none}
.hint{margin-top:10px;color:#64748b;font-size:11px}
kbd{font:11px ui-monospace,Consolas,monospace;background:#1e293b;border-radius:4px;padding:0 4px;color:var(--text)}
#legend{left:16px;bottom:16px;min-width:180px}
#legend h2,#heavy h2{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:0 0 8px}
.legend-item{display:flex;align-items:center;gap:8px;padding:2px 0;cursor:pointer;user-select:none}
.legend-item.off{opacity:.3}
.legend-item em{margin-left:auto;color:var(--muted);font-style:normal;font-variant-numeric:tabular-nums}
.dot{width:10px;height:10px;border-radius:50%;flex:none}
#heavy{top:16px;right:16px;width:270px}
#heavy ol{margin:0;padding:0;list-style:none}
#heavy li{display:flex;gap:8px;align-items:center;padding:4px 6px;border-radius:7px;cursor:pointer}
#heavy li:hover{background:rgba(56,189,248,.08)}
#heavy li .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;direction:rtl;text-align:left}
.seg{display:flex;margin-top:10px;background:rgba(2,6,23,.6);border:1px solid var(--border);border-radius:9px;padding:3px}
.seg button{flex:1;border:0;background:transparent;color:var(--muted);font:inherit;font-size:12px;padding:5px 6px;border-radius:6px;cursor:pointer}
.seg button.on{background:#1e293b;color:var(--text)}
#heavy li em{color:var(--muted);font-style:normal;font-variant-numeric:tabular-nums;font-size:11.5px}
#tooltip{position:fixed;pointer-events:none;opacity:0;transition:opacity .12s;max-width:360px;z-index:5;padding:12px 14px}
.tt-title{font-weight:600;word-break:break-all;margin-bottom:6px}
.tt-grid{display:grid;grid-template-columns:auto 1fr;gap:2px 14px;font-variant-numeric:tabular-nums}
.tt-grid span:nth-child(odd){color:var(--muted)}
.tt-list{margin-top:8px;color:var(--muted);font-size:11.5px;word-break:break-all}
.tt-list b{color:var(--text);font-weight:600}
.link{fill:none;stroke:#3b4a63;stroke-opacity:.55;stroke-width:1.1}
.link.ext{stroke-opacity:.25}
.link.cycle{stroke:var(--cycle);stroke-opacity:.95;stroke-width:1.8}
.link.hl{stroke:var(--accent);stroke-opacity:1;stroke-width:1.8}
.node circle{stroke:#0b1220;stroke-width:1.6;cursor:pointer;transition:stroke .15s}
.node.cyc circle{stroke:var(--cycle);stroke-width:2.4}
.node.match circle{stroke:#fff;stroke-width:3}
.node.focus circle{stroke:var(--accent);stroke-width:3.5}
.label{fill:#dbe4f0;font-size:11px;pointer-events:none;paint-order:stroke;stroke:#090d16;stroke-width:3.5px;stroke-linejoin:round}
.label.ext{fill:#8492a6;font-size:9.5px}
.label.minor{display:none}
.zoomed .label.minor{display:block}
.dim{opacity:.1}
#error{display:none;top:50%;left:50%;transform:translate(-50%,-50%);max-width:420px;text-align:center}
@media (max-width:820px){#heavy{display:none}#hud{width:calc(100% - 32px)}}
</style>
</head>
<body>
<div id="graph"></div>
<div id="hud" class="panel">
  <h1><i></i>Spiderweb</h1>
  <div class="sub" id="subtitle"></div>
  <div class="stats" id="stats"></div>
  <input id="search" type="search" placeholder="Search files…  ( / )" autocomplete="off" spellcheck="false">
  <div class="seg" id="color-mode"><button data-mode="type">Color by file type</button><button data-mode="folder">Color by folder</button></div>
  <label class="toggle"><input type="checkbox" id="ext-toggle"> Show external modules</label>
  <div class="hint">Scroll to zoom · drag to pan · click a node to pin · <kbd>/</kbd> search · <kbd>F</kbd> fit · <kbd>Esc</kbd> clear</div>
</div>
<div id="heavy" class="panel"><h2>Heaviest files · tokens</h2><ol id="heavy-list"></ol></div>
<div id="legend" class="panel"><h2 id="legend-title">File types</h2><div id="legend-items"></div></div>
<div id="tooltip" class="panel"></div>
<div id="error" class="panel"><b>Could not load D3.js</b><p class="sub">This map loads D3 v7 from cdnjs.cloudflare.com. Check your internet connection and reload.</p></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<script id="graph-data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("graph-data").textContent);
  if (!window.d3) { document.getElementById("error").style.display = "block"; return; }
  const META = DATA.meta, COLORS = DATA.colors;
  const LANG_NAMES = {python: "Python", javascript: "JavaScript", typescript: "TypeScript", go: "Go", rust: "Rust", stdlib: "Standard library", package: "Third-party package"};
  const fmt = d3.format(",");
  const FOLDER_PALETTE = ["#5b9cf6", "#f5c542", "#34d399", "#fb923c", "#a78bfa", "#f472b6", "#22d3ee", "#a3e635", "#f87171", "#c4b5fd", "#fbbf24", "#2dd4bf"];
  const internal = DATA.nodes.filter((n) => !n.external);
  const folders = [...new Set(internal.map((n) => n.group))].sort();
  const folderColor = new Map(folders.map((f, i) => [f, FOLDER_PALETTE[i % FOLDER_PALETTE.length]]));
  const folderName = (g) => g === "." ? "(root)" : g + "/";
  const state = {
    showExternal: META.showExternal, hidden: new Set(), focus: null, query: "",
    colorBy: new Set(internal.map((n) => n.lang)).size > 1 ? "type" : "folder"
  };
  const keyOf = (n) => n.external ? n.lang : (state.colorBy === "folder" ? "f:" + n.group : n.lang);
  const cycleNodes = new Set(META.cycles.flat());

  document.title = "Spiderweb · " + META.title;
  document.getElementById("subtitle").textContent = META.title + " · " + META.generator;
  const stats = [
    ["Files", fmt(META.files)], ["Links", fmt(META.links)],
    ["Lines of code", fmt(META.loc)], ["Est. tokens", fmt(META.tokens)],
    ["External deps", fmt(META.externals)], ["Import cycles", fmt(META.cycles.length), META.cycles.length > 0]
  ];
  document.getElementById("stats").innerHTML = stats.map(s =>
    '<div class="stat' + (s[2] ? " bad" : "") + '"><b>' + s[1] + "</b><span>" + s[0] + "</span></div>").join("");

  const extToggle = document.getElementById("ext-toggle");
  extToggle.checked = state.showExternal;
  extToggle.addEventListener("change", () => { state.showExternal = extToggle.checked; build(false); });

  const svg = d3.select("#graph").append("svg");
  const defs = svg.append("defs");
  [["arrow", "#4b5b77"], ["arrow-cycle", "#f43f5e"], ["arrow-hl", "#38bdf8"]].forEach(([id, color]) => {
    defs.append("marker").attr("id", id).attr("viewBox", "0 -4 8 8").attr("refX", 7).attr("refY", 0)
      .attr("markerWidth", 7).attr("markerHeight", 7).attr("orient", "auto")
      .append("path").attr("d", "M0,-4L8,0L0,4").attr("fill", color);
  });
  const viewport = svg.append("g");
  const zoom = d3.zoom().scaleExtent([0.05, 8]).on("zoom", (event) => {
    viewport.attr("transform", event.transform);
    viewport.classed("zoomed", event.transform.k > 1.6);
  });
  svg.call(zoom).on("dblclick.zoom", null);
  svg.on("click", (event) => { if (event.target === svg.node()) setFocus(null); });

  const tooltip = d3.select("#tooltip");
  let nodes = [], links = [], byId = new Map(), inbound = new Map(), outbound = new Map();
  let linkSel, nodeSel, labelSel, sim, positions = new Map(), firstBuild = true;

  const radius = (d) => d.external ? 4.5 + Math.min(6, Math.sqrt(inbound.get(d.id)?.size || 0) * 1.5) : Math.min(34, 5 + Math.sqrt(d.loc) * 0.85);
  const color = (d) => (d.external || state.colorBy === "type" ? COLORS[d.lang] : folderColor.get(d.group)) || "#94a3b8";

  function buildLegend() {
    const counts = d3.rollup(DATA.nodes, (v) => v.length, keyOf);
    const order = state.colorBy === "folder"
      ? folders.map((f) => "f:" + f).concat(["stdlib", "package"])
      : ["python", "typescript", "javascript", "go", "rust", "stdlib", "package"];
    const items = order.filter((k) => counts.has(k));
    const swatch = (k) => k.startsWith("f:") ? folderColor.get(k.slice(2)) : COLORS[k];
    const name = (k) => k.startsWith("f:") ? folderName(k.slice(2)) : LANG_NAMES[k];
    document.getElementById("legend-title").textContent = state.colorBy === "folder" ? "Folders" : "File types";
    d3.select("#legend-items").selectAll("div").data(items, (k) => k).join("div")
      .attr("class", (k) => "legend-item" + (state.hidden.has(k) ? " off" : ""))
      .html((k) => '<span class="dot" style="background:' + swatch(k) + '"></span>' + name(k) + "<em>" + counts.get(k) + "</em>")
      .on("click", (event, k) => { state.hidden.has(k) ? state.hidden.delete(k) : state.hidden.add(k); buildLegend(); build(false); });
  }

  function buildHeavy() {
    const top = internal.slice().sort((a, b) => b.tokens - a.tokens).slice(0, 8);
    d3.select("#heavy-list").selectAll("li").data(top).join("li")
      .html((d) => '<span class="dot" style="background:' + color(d) + '"></span><span class="name" title="' + d.id + '"><bdi>' + d.id + "</bdi></span><em>" + fmt(d.tokens) + "</em>")
      .on("click", (event, d) => { const n = byId.get(d.id); if (n) { setFocus(n); zoomTo(n); } });
  }

  function syncColorMode() {
    document.querySelectorAll("#color-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === state.colorBy));
  }
  document.querySelectorAll("#color-mode button").forEach((b) => b.addEventListener("click", () => {
    if (state.colorBy === b.dataset.mode) return;
    state.colorBy = b.dataset.mode;
    state.hidden.clear();
    syncColorMode(); buildLegend(); buildHeavy();
    if (nodeSel) nodeSel.select("circle").attr("fill", color);
    build(false);
  }));

  function linkPath(d) {
    const sx = d.source.x, sy = d.source.y, tx0 = d.target.x, ty0 = d.target.y;
    const dx = tx0 - sx, dy = ty0 - sy, dist = Math.hypot(dx, dy) || 1;
    const r = radius(d.target) + 3;
    if (d.mutual) {
      const dr = dist * 1.25;
      const angle = Math.atan2(dy, dx) - 0.35;
      const tx = tx0 - Math.cos(angle) * r, ty = ty0 - Math.sin(angle) * r;
      return "M" + sx + "," + sy + "A" + dr + "," + dr + " 0 0,1 " + tx + "," + ty;
    }
    return "M" + sx + "," + sy + "L" + (tx0 - dx / dist * r) + "," + (ty0 - dy / dist * r);
  }

  function draw() {
    linkSel.attr("d", linkPath);
    nodeSel.attr("transform", (d) => "translate(" + d.x + "," + d.y + ")");
    labelSel.attr("x", (d) => d.x).attr("y", (d) => d.y + radius(d) + 13);
  }

  function setDim(test) {
    nodeSel.classed("dim", test);
    labelSel.classed("dim", test);
  }

  function build(fit) {
    if (sim) { sim.stop(); nodes.forEach((n) => positions.set(n.id, {x: n.x, y: n.y})); }
    const visible = DATA.nodes.filter((n) => (state.showExternal || !n.external) && !state.hidden.has(keyOf(n)));
    const ids = new Set(visible.map((n) => n.id));
    nodes = visible.map((n) => Object.assign({}, n, positions.get(n.id) || {}));
    links = DATA.links.filter((l) => ids.has(l.source) && ids.has(l.target)).map((l) => Object.assign({}, l));
    byId = new Map(nodes.map((n) => [n.id, n]));
    inbound = new Map(nodes.map((n) => [n.id, new Set()]));
    outbound = new Map(nodes.map((n) => [n.id, new Set()]));
    const pairs = new Set(links.map((l) => l.source + "\u0000" + l.target));
    links.forEach((l) => {
      l.mutual = pairs.has(l.target + "\u0000" + l.source);
      outbound.get(l.source).add(l.target);
      inbound.get(l.target).add(l.source);
    });

    viewport.selectAll("*").remove();
    linkSel = viewport.append("g").selectAll("path").data(links).join("path")
      .attr("class", (d) => "link" + (d.cycle ? " cycle" : "") + (byId.get(d.target).external ? " ext" : ""))
      .attr("marker-end", (d) => d.cycle ? "url(#arrow-cycle)" : "url(#arrow)");

    const internalCount = nodes.filter((n) => !n.external).length;
    const externalCount = nodes.length - internalCount;
    nodeSel = viewport.append("g").selectAll("g").data(nodes, (d) => d.id).join("g")
      .attr("class", (d) => "node" + (d.external ? " ext" : "") + (cycleNodes.has(d.id) ? " cyc" : ""))
      .call(d3.drag()
        .on("start", (event, d) => { if (!event.active) sim.alphaTarget(0.2).restart(); d.fx = d.x; d.fy = d.y; })
        .on("drag", (event, d) => { d.fx = event.x; d.fy = event.y; })
        .on("end", (event, d) => { if (!event.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }))
      .on("mouseenter", (event, d) => { if (!state.focus) highlight(d); showTooltip(event, d); })
      .on("mousemove", moveTooltip)
      .on("mouseleave", () => { tooltip.style("opacity", 0); if (!state.focus) highlight(null); })
      .on("click", (event, d) => { event.stopPropagation(); setFocus(state.focus === d ? null : d); });
    nodeSel.append("circle").attr("r", radius).attr("fill", color);
    labelSel = viewport.append("g").selectAll("text").data(nodes, (d) => d.id).join("text")
      .text((d) => d.label).attr("text-anchor", "middle")
      .attr("class", (d) => {
        const major = d.external ? externalCount <= 45 : (internalCount <= 140 || radius(d) >= 14);
        return "label" + (d.external ? " ext" : "") + (major ? "" : " minor");
      });

    sim = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id((d) => d.id)
        .distance((l) => (l.target.external ? 60 : 90) + radius(l.source) + radius(l.target))
        .strength((l) => l.target.external ? 0.12 : 0.55))
      .force("charge", d3.forceManyBody().strength((d) => d.external ? -70 : -300).distanceMax(700))
      .force("collide", d3.forceCollide((d) => radius(d) + (d.external ? 10 : 16)))
      .force("x", d3.forceX(0).strength((d) => d.external ? 0 : 0.05))
      .force("y", d3.forceY(0).strength((d) => d.external ? 0 : 0.07))
      .force("ring", d3.forceRadial(Math.max(300, 95 * Math.sqrt(internalCount)), 0, 0)
        .strength((d) => d.external ? 0.35 : 0))
      .stop();
    const ticks = positions.size ? 120 : 320;
    for (let i = 0; i < ticks; i++) sim.tick();
    draw();
    sim.on("tick", draw).alpha(0.05).restart();
    applySearch();
    if (state.focus && byId.has(state.focus.id)) setFocus(byId.get(state.focus.id)); else state.focus = null;
    if (fit || firstBuild) fitView(firstBuild ? 0 : 600);
    firstBuild = false;
  }

  function highlight(d) {
    if (!d) {
      setDim(false);
      linkSel.classed("dim", false).classed("hl", false).attr("marker-end", (l) => l.cycle ? "url(#arrow-cycle)" : "url(#arrow)");
      applySearch();
      return;
    }
    const keep = new Set([d.id, ...inbound.get(d.id), ...outbound.get(d.id)]);
    setDim((n) => !keep.has(n.id));
    linkSel.classed("dim", (l) => l.source.id !== d.id && l.target.id !== d.id)
      .classed("hl", (l) => !l.cycle && (l.source.id === d.id || l.target.id === d.id))
      .attr("marker-end", (l) => l.cycle ? "url(#arrow-cycle)" : (l.source.id === d.id || l.target.id === d.id) ? "url(#arrow-hl)" : "url(#arrow)");
  }

  function setFocus(d) {
    state.focus = d;
    nodeSel.classed("focus", (n) => d && n.id === d.id);
    highlight(d);
  }

  function names(set) {
    const list = [...set].map((id) => byId.get(id)).filter(Boolean).map((n) => n.external ? n.label : n.id);
    const shown = list.slice(0, 6).join(", ");
    return list.length > 6 ? shown + " … +" + (list.length - 6) : shown || "—";
  }

  function showTooltip(event, d) {
    const ins = inbound.get(d.id), outs = outbound.get(d.id);
    let html = '<div class="tt-title">' + (d.external ? d.label : d.id) + "</div><div class=\"tt-grid\">";
    if (d.external) {
      html += "<span>Type</span><span>" + LANG_NAMES[d.lang] + "</span><span>Imported by</span><span>" + fmt(ins.size) + " files</span></div>";
      html += '<div class="tt-list"><b>Used in:</b> ' + names(ins) + "</div>";
    } else {
      html += "<span>Language</span><span>" + LANG_NAMES[d.lang] + "</span>"
        + "<span>Lines of code</span><span>" + fmt(d.loc) + "</span>"
        + "<span>File size</span><span>" + (d.bytes >= 1024 ? (d.bytes / 1024).toFixed(1) + " KB" : d.bytes + " B") + "</span>"
        + "<span>Token weight</span><span>~" + fmt(d.tokens) + " tokens</span>"
        + "<span>Imports</span><span>" + fmt(outs.size) + " outbound</span>"
        + "<span>Imported by</span><span>" + fmt(ins.size) + " inbound</span></div>";
      html += '<div class="tt-list"><b>→</b> ' + names(outs) + "<br><b>←</b> " + names(ins) + "</div>";
      if (cycleNodes.has(d.id)) html += '<div class="tt-list" style="color:#f43f5e">Part of an import cycle</div>';
    }
    tooltip.html(html).style("opacity", 1);
    moveTooltip(event);
  }

  function moveTooltip(event) {
    const box = tooltip.node().getBoundingClientRect();
    let x = event.clientX + 16, y = event.clientY + 16;
    if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 16;
    if (y + box.height > window.innerHeight - 8) y = event.clientY - box.height - 16;
    tooltip.style("left", x + "px").style("top", y + "px");
  }

  function applySearch() {
    const q = state.query.trim().toLowerCase();
    if (!q) { nodeSel.classed("match", false); if (!state.focus) setDim(false); return []; }
    const hits = nodes.filter((n) => n.id.toLowerCase().includes(q) || n.label.toLowerCase().includes(q));
    const hitIds = new Set(hits.map((n) => n.id));
    nodeSel.classed("match", (n) => hitIds.has(n.id));
    setDim((n) => !hitIds.has(n.id));
    linkSel.classed("dim", (l) => !hitIds.has(l.source.id) && !hitIds.has(l.target.id));
    return hits;
  }

  function safeArea() {
    const w = window.innerWidth, h = window.innerHeight;
    const left = w > 1100 ? 330 : 0, right = w > 1100 ? 300 : 0;
    return {x0: left, x1: w - right, y0: 0, y1: h};
  }

  function fitView(duration) {
    if (!nodes.length) return;
    // Large graphs: frame the 3rd-97th percentile so a few far-flung leaves don't shrink everything.
    const xs = nodes.map((n) => n.x).sort((a, b) => a - b), ys = nodes.map((n) => n.y).sort((a, b) => a - b);
    const cut = nodes.length > 60 ? Math.floor(nodes.length * 0.03) : 0, last = nodes.length - 1 - cut;
    const minX = xs[cut] - 40, maxX = xs[last] + 40, minY = ys[cut] - 40, maxY = ys[last] + 40;
    const area = safeArea();
    const scale = Math.min(1.6, 0.92 * Math.min((area.x1 - area.x0) / (maxX - minX), (area.y1 - area.y0) / (maxY - minY)));
    const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    const t = d3.zoomIdentity.translate((area.x0 + area.x1) / 2 - scale * cx, (area.y0 + area.y1) / 2 - scale * cy).scale(scale);
    (duration ? svg.transition().duration(duration) : svg).call(zoom.transform, t);
  }

  function zoomTo(d) {
    const area = safeArea(), k = 1.8;
    svg.transition().duration(600).call(zoom.transform,
      d3.zoomIdentity.translate((area.x0 + area.x1) / 2 - k * d.x, (area.y0 + area.y1) / 2 - k * d.y).scale(k));
  }

  const search = document.getElementById("search");
  search.addEventListener("input", () => { state.query = search.value; if (state.focus) setFocus(null); applySearch(); });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { const hits = applySearch(); if (hits.length) { setFocus(hits[0]); zoomTo(hits[0]); } }
  });
  window.addEventListener("keydown", (event) => {
    if (event.target === search) { if (event.key === "Escape") { search.value = ""; state.query = ""; search.blur(); applySearch(); } return; }
    if (event.key === "/") { event.preventDefault(); search.focus(); }
    else if (event.key === "f" || event.key === "F") fitView(600);
    else if (event.key === "Escape") setFocus(null);
  });

  syncColorMode();
  buildLegend();
  build(true);
  buildHeavy();
})();
</script>
</body>
</html>
"""


def render_html(payload: dict) -> str:
    data = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/").replace("<!--", "<\\!--")
    title = payload["meta"]["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return HTML_TEMPLATE.replace("__TITLE__", title).replace("__DATA__", data)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _color(code: str, text: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spiderweb", description="Generate an interactive D3.js dependency map of a codebase.")
    parser.add_argument("--target", default=".", help="directory to scan (default: current directory)")
    parser.add_argument("--output", default="sentinel_spiderweb.html", help="HTML file to write (default: sentinel_spiderweb.html)")
    parser.add_argument("--json", metavar="FILE", help="also write the raw graph as JSON")
    parser.add_argument("--open", action="store_true", help="open the map in your default browser")
    parser.add_argument("--no-external", action="store_true", help="leave stdlib / third-party modules out of the graph")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="extra glob to skip (repeatable)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    root = Path(args.target).resolve()
    if not root.is_dir():
        print(f"spiderweb: target is not a directory: {root}", file=sys.stderr)
        return 2
    graph = build_graph(root, include_external=not args.no_external, exclude_globs=tuple(args.exclude))
    payload = graph_payload(graph)
    if not payload["meta"]["files"]:
        print(f"spiderweb: no .py/.js/.ts/.go/.rs files found under {root}", file=sys.stderr)
        return 1

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(payload), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    meta = payload["meta"]
    cycles = len(meta["cycles"])
    print(
        _color("1;32", "✔ spiderweb", color)
        + f"  {meta['files']} files · {meta['links']} links · {meta['externals']} external modules · "
        + _color("1;31" if cycles else "2", f"{cycles} import cycle{'s' if cycles != 1 else ''}", color)
    )
    print(f"  {meta['loc']:,} lines of code · ~{meta['tokens']:,} tokens")
    print("  → " + _color("1", str(out.resolve()), color) + f"  ({out.stat().st_size / 1024:.0f} KB)")
    for comp in meta["cycles"][:5]:
        print(_color("31", "  ↻ cycle: ", color) + " → ".join(comp + [comp[0]]))
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
