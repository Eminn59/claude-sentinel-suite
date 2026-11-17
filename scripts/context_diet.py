#!/usr/bin/env python3
"""
context_diet: AST-based codebase skeletonizer for Claude Code.

Keeps what Claude needs to *orient* itself in a codebase (imports, class
hierarchies, signatures, type annotations, docstrings) and drops the function
bodies it does not need until it actually edits a file.

    python context_diet.py --target ./src --output .claude/skeleton.md
    python context_diet.py --demo

Python is parsed with the standard-library ``ast`` module. JavaScript,
TypeScript, Go and Rust go through a brace-aware lexer that understands
strings, comments, template literals, regex literals and Rust lifetimes.

Zero dependencies. Python 3.10+.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

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

JS_LIKE = frozenset({"javascript", "typescript"})

DEFAULT_EXCLUDE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".claude", ".idea", ".vscode", ".cache",
    "node_modules", "bower_components", "vendor", "third_party",
    "__pycache__", ".venv", "venv", "env", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
    "dist", "build", "out", "target", ".next", ".nuxt", ".svelte-kit",
    "coverage", ".nightshift",
})

DEFAULT_EXCLUDE_GLOBS = (
    "*.min.js", "*.min.mjs", "*.bundle.js", "*.chunk.js",
    "*_pb2.py", "*_pb2_grpc.py", "*.pb.go", "*.generated.*",
)

MAX_FILE_BYTES = 1_000_000
BODY_PLACEHOLDER = "{ ... }"
MAX_INLINE_VALUE = 80


def estimate_tokens(text: str) -> int:
    """Rough Claude token estimate: about four characters per token."""
    return len(text) // 4


# --------------------------------------------------------------------------
# Python (ast)
# --------------------------------------------------------------------------

def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _docstring(node: ast.AST, mode: str) -> ast.stmt | None:
    if mode == "none":
        return None
    try:
        doc = ast.get_docstring(node, clean=True)  # type: ignore[arg-type]
    except TypeError:
        return None
    if not doc:
        return None
    if mode == "summary":
        doc = doc.strip().split("\n\n", 1)[0].strip()
    return ast.Expr(value=ast.Constant(value=doc))


def _ellipsis() -> ast.stmt:
    return ast.Expr(value=ast.Constant(value=...))


def _shorten_value(node: ast.expr | None) -> ast.expr | None:
    if node is None:
        return None
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - defensive, unparse is total in practice
        return ast.Constant(value=...)
    if len(text) > MAX_INLINE_VALUE or "\n" in text:
        return ast.Constant(value=...)
    return node


_IMPORTS = (ast.Import, ast.ImportFrom)
_TRY_TYPES = tuple(t for t in (getattr(ast, "Try", None), getattr(ast, "TryStar", None)) if t)
_TYPE_ALIAS = getattr(ast, "TypeAlias", None)


def _is_import_block(stmt: ast.stmt) -> bool:
    """True for ``if TYPE_CHECKING:`` or ``try: import x / except ImportError:`` blocks."""
    if isinstance(stmt, ast.If):
        parts = list(stmt.body) + list(stmt.orelse)
    elif isinstance(stmt, _TRY_TYPES):
        parts = list(stmt.body) + list(stmt.orelse) + list(stmt.finalbody)
        for handler in stmt.handlers:
            parts.extend(handler.body)
    else:
        return False
    has_import = False
    for part in parts:
        if isinstance(part, _IMPORTS):
            has_import = True
        elif isinstance(part, (ast.If, *_TRY_TYPES)) and _is_import_block(part):
            has_import = True
        elif isinstance(part, (ast.Assign, ast.AnnAssign, ast.Pass)) or _is_docstring(part):
            continue
        else:
            return False
    return has_import


class _PythonSkeleton:
    def __init__(self, doc_mode: str) -> None:
        self.doc_mode = doc_mode

    def function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.stmt:
        body: list[ast.stmt] = []
        doc = _docstring(node, self.doc_mode)
        if doc is not None:
            body.append(doc)
        body.append(_ellipsis())
        node.body = body
        return node

    def klass(self, node: ast.ClassDef) -> ast.stmt:
        body: list[ast.stmt] = []
        doc = _docstring(node, self.doc_mode)
        if doc is not None:
            body.append(doc)
        members = node.body[1:] if node.body and _is_docstring(node.body[0]) else node.body
        for stmt in members:
            kept = self.member(stmt)
            if kept is not None:
                body.append(kept)
        if not body:
            body.append(_ellipsis())
        node.body = body
        return node

    def member(self, stmt: ast.stmt) -> ast.stmt | None:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return self.function(stmt)
        if isinstance(stmt, ast.ClassDef):
            return self.klass(stmt)
        if isinstance(stmt, _IMPORTS):
            return stmt
        if isinstance(stmt, ast.AnnAssign):
            stmt.value = _shorten_value(stmt.value)
            return stmt
        if isinstance(stmt, ast.Assign):
            stmt.value = _shorten_value(stmt.value)  # type: ignore[assignment]
            return stmt
        if _TYPE_ALIAS is not None and isinstance(stmt, _TYPE_ALIAS):
            return stmt
        if _is_import_block(stmt):
            return stmt
        return None

    def module(self, tree: ast.Module) -> ast.Module:
        body: list[ast.stmt] = []
        doc = _docstring(tree, self.doc_mode)
        if doc is not None:
            body.append(doc)
        members = tree.body[1:] if tree.body and _is_docstring(tree.body[0]) else tree.body
        for stmt in members:
            kept = self.member(stmt)
            if kept is not None:
                body.append(kept)
        tree.body = body
        return tree


_COMPACT_BLOCK = re.compile(
    r"^(?P<head>[ \t]*(?:(?:async[ \t]+)?def|class)[ \t][^\n]*:)\n[ \t]+\.\.\.$",
    re.MULTILINE,
)
_PY_FALLBACK_LINE = re.compile(r"^\s*(?:@|def\s|async\s+def\s|class\s|import\s|from\s+\S+\s+import\s)")


def _fallback_python(source: str) -> str:
    """Line-based skeleton for files that do not parse (Python 2, syntax errors)."""
    kept = [line.rstrip() for line in source.splitlines() if _PY_FALLBACK_LINE.match(line)]
    return "\n".join(kept) + ("\n" if kept else "")


def skeletonize_python(source: str, doc_mode: str = "summary") -> str:
    """Return the skeleton of a Python module: every function body becomes ``...``."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return _fallback_python(source)
    try:
        tree = _PythonSkeleton(doc_mode).module(tree)
        ast.fix_missing_locations(tree)
        text = ast.unparse(tree)
    except (RecursionError, ValueError, TypeError):
        return _fallback_python(source)
    text = _COMPACT_BLOCK.sub(r"\g<head> ...", text).strip()
    return text + "\n" if text else ""


# --------------------------------------------------------------------------
# JavaScript / TypeScript / Go / Rust (brace-aware lexer)
# --------------------------------------------------------------------------

_FUNC_KW = {
    "javascript": re.compile(r"(?<![\w$.])function(?![\w$])"),
    "typescript": re.compile(r"(?<![\w$.])function(?![\w$])"),
    "go": re.compile(r"(?<![\w.])func(?!\w)"),
    "rust": re.compile(r"(?<![\w.])fn(?!\w)"),
}
_CONTAINER_KW = {
    "javascript": re.compile(r"(?<![\w$.])class(?![\w$])"),
    "typescript": re.compile(
        r"(?<![\w$.])(?:class|interface|enum|namespace|module|declare\s+global)(?![\w$])"
    ),
    "go": re.compile(r"(?<![\w.])(?:struct|interface)(?!\w)"),
    "rust": re.compile(r"(?<![\w.])(?:struct|enum|union|impl|trait|mod|extern)(?!\w)"),
}
CONTROL_WORDS = frozenset(
    "if else for while do switch try catch finally with match loop select unsafe defer".split()
)
_FIRST_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_METHOD = re.compile(
    r"""^(?:@[\w$.]+(?:\([^()]*\))?\s*)*          # decorators
        (?:[\w$#*\[\]"'.-]+\s+)*                    # modifiers: static async get public ...
        [\w$#*\[\]"'.-]+\??\s*                      # method name (incl. computed / optional)
        (?:<[^(){}]*>)?\s*                          # generics
        \(.*\)\s*                                   # parameter list
        (?::\s*(?:[^=;{}]|=>)+)?$                   # optional return type
    """,
    re.DOTALL | re.VERBOSE,
)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_RUST_CHAR = re.compile(r"'(?:\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|.)|[^\\'\n])'")
_RUST_RAW = re.compile(r'b?r(#*)"')
_REGEX_PREV_CHARS = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_PREV_WORDS = frozenset(
    "return typeof case do else in of new delete void throw yield await instanceof".split()
)


def _regex_allowed(src: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    ch = src[j]
    if ch.isalnum() or ch in "_$":
        k = j
        while k >= 0 and (src[k].isalnum() or src[k] in "_$"):
            k -= 1
        return src[k + 1 : j + 1] in _REGEX_PREV_WORDS
    return ch in _REGEX_PREV_CHARS


def _skip_regex(src: str, i: int) -> int:
    n = len(src)
    j = i + 1
    in_class = False
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "\n":
            return -1
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
        elif ch == "/":
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            return j
        j += 1
    return -1


def _skip_quoted(src: str, i: int, multiline: bool) -> int:
    quote = src[i]
    n = len(src)
    j = i + 1
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == quote:
            return j + 1
        if ch == "\n" and not multiline:
            return j
        j += 1
    return n


def _skip_template(src: str, i: int, lang: str) -> int:
    n = len(src)
    j = i + 1
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
        elif ch == "`":
            return j + 1
        elif ch == "$" and j + 1 < n and src[j + 1] == "{":
            end = _find_block_end(src, j + 1, lang)
            if end < 0:
                return n
            j = end + 1
        else:
            j += 1
    return n


def _skip_atom(src: str, i: int, lang: str) -> int:
    """If a comment, string, char or regex literal starts at ``i``, return its end index, else -1."""
    ch = src[i]
    nxt = src[i + 1] if i + 1 < len(src) else ""
    if ch == "/" and nxt == "/":
        end = src.find("\n", i)
        return len(src) if end < 0 else end
    if ch == "/" and nxt == "*":
        end = src.find("*/", i + 2)
        return len(src) if end < 0 else end + 2
    if lang == "rust":
        if ch in "rb" and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            match = _RUST_RAW.match(src, i)
            if match:
                closing = '"' + match.group(1)
                end = src.find(closing, match.end())
                return len(src) if end < 0 else end + len(closing)
        if ch == "'":
            match = _RUST_CHAR.match(src, i)
            return match.end() if match else -1  # otherwise it is a lifetime like 'a
        if ch == '"':
            return _skip_quoted(src, i, multiline=True)
        return -1
    if ch in "\"'":
        return _skip_quoted(src, i, multiline=False)
    if ch == "`":
        if lang in JS_LIKE:
            return _skip_template(src, i, lang)
        end = src.find("`", i + 1)  # Go raw string
        return len(src) if end < 0 else end + 1
    if ch == "/" and lang in JS_LIKE and _regex_allowed(src, i):
        return _skip_regex(src, i)
    return -1


def _find_block_end(src: str, i: int, lang: str) -> int:
    """Index of the ``}`` matching the ``{`` at ``i``, or -1 when unbalanced."""
    n = len(src)
    depth = 0
    j = i
    while j < n:
        end = _skip_atom(src, j, lang)
        if end > j:
            j = end
            continue
        ch = src[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _match_paren(text: str, open_index: int) -> int:
    depth = 0
    for k in range(open_index, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return -1


def _tail_statement(raw: str) -> str:
    """The last logical statement of a header (joins lines of a multi-line parameter list)."""
    lines = [line for line in raw.split("\n") if line.strip()]
    acc: list[str] = []
    depth = 0
    for line in reversed(lines):
        acc.insert(0, line.strip())
        depth += line.count(")") - line.count("(")
        if depth <= 0:
            break
    return " ".join(acc)


def classify_block(header: str, lang: str) -> str:
    """Decide whether the block opened after ``header`` is a body to collapse or a scope to keep."""
    raw = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", header))
    flat = " ".join(raw.split())
    if not flat:
        return "descend"
    if flat.endswith("=>"):
        return "collapse"
    if flat[-1] in "=:,([?|&+":
        return "descend"
    func_positions = [m.start() for m in _FUNC_KW[lang].finditer(flat)]
    cont_positions = [m.start() for m in _CONTAINER_KW[lang].finditer(flat)]
    func_pos = func_positions[-1] if func_positions else -1
    cont_pos = cont_positions[-1] if cont_positions else -1
    if func_pos >= 0:
        open_i = flat.find("(", func_pos)
        if open_i >= 0 and _match_paren(flat, open_i) >= 0:
            if cont_pos < func_pos or cont_pos > open_i:
                return "collapse"
        elif open_i < 0:
            return "descend"
        else:
            return "descend"
    if cont_pos >= 0:
        return "descend"
    tail = _tail_statement(raw).lstrip("}); \t")
    first = _FIRST_WORD.match(tail)
    if first and first.group(0) in CONTROL_WORDS:
        return "collapse"
    if lang in JS_LIKE and _METHOD.match(tail):
        before = tail.split("(", 1)[0]
        if "=" not in before and not before.rstrip().endswith(("new", "return")):
            return "collapse"
    return "descend"


def _tidy(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blank = 0
    for line in lines:
        if line:
            blank = 0
            out.append(line)
        else:
            blank += 1
            if blank == 1 and out:
                out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + ("\n" if out else "")


def skeletonize_clike(source: str, lang: str) -> str:
    """Collapse function, method, closure and control-flow bodies to ``{ ... }``."""
    src = source
    n = len(src)
    out: list[str] = []
    i = 0
    seg_start = 0
    stmt_start = 0
    paren = 0
    paren_stack: list[int] = []
    while i < n:
        end = _skip_atom(src, i, lang)
        if end > i:
            i = end
            continue
        ch = src[i]
        if ch in "([":
            paren += 1
        elif ch in ")]":
            paren = max(0, paren - 1)
        elif ch == ";" and paren == 0:
            stmt_start = i + 1
        elif ch == "{":
            if classify_block(src[stmt_start:i], lang) == "collapse":
                close = _find_block_end(src, i, lang)
                if close >= 0:
                    if close - i + 1 <= len(BODY_PLACEHOLDER):  # `{}` or tiny bodies: keep as written
                        i = close + 1
                        stmt_start = i
                        continue
                    out.append(src[seg_start:i])
                    out.append(BODY_PLACEHOLDER)
                    i = close + 1
                    seg_start = i
                    stmt_start = i
                    continue
            paren_stack.append(paren)
            paren = 0
            stmt_start = i + 1
        elif ch == "}":
            paren = paren_stack.pop() if paren_stack else 0
            stmt_start = i + 1
        i += 1
    out.append(src[seg_start:])
    return _tidy("".join(out))


def skeletonize(source: str, language: str, doc_mode: str = "summary") -> str:
    source = source.lstrip("﻿")
    if language == "python":
        return skeletonize_python(source, doc_mode)
    return skeletonize_clike(source, language)


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

@dataclass
class FileResult:
    path: str
    language: str
    original_chars: int
    skeleton: str

    @property
    def original_tokens(self) -> int:
        return self.original_chars // 4

    @property
    def skeleton_tokens(self) -> int:
        return estimate_tokens(self.skeleton)

    @property
    def saved_pct(self) -> float:
        if not self.original_tokens:
            return 0.0
        return 100.0 * (1 - self.skeleton_tokens / self.original_tokens)


def iter_source_files(root: Path, exclude_globs: tuple[str, ...] = ()) -> list[Path]:
    globs = DEFAULT_EXCLUDE_GLOBS + tuple(exclude_globs)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in DEFAULT_EXCLUDE_DIRS
            and not d.endswith(".egg-info")
            and not any(fnmatch.fnmatch(f"{rel_dir}/{d}".lstrip("./"), g) or fnmatch.fnmatch(d, g) for g in exclude_globs)
        )
        for name in sorted(filenames):
            suffix = os.path.splitext(name)[1].lower()
            if suffix not in LANGUAGES:
                continue
            rel = f"{rel_dir}/{name}".lstrip("./") if rel_dir != "." else name
            if any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g) for g in globs):
                continue
            path = Path(dirpath, name)
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append(path)
    return found


def analyze(root: Path, doc_mode: str = "summary", exclude_globs: tuple[str, ...] = ()) -> list[FileResult]:
    results: list[FileResult] = []
    for path in iter_source_files(root, exclude_globs):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        language = LANGUAGES[path.suffix.lower()]
        results.append(
            FileResult(
                path=path.relative_to(root).as_posix(),
                language=language,
                original_chars=len(text),
                skeleton=skeletonize(text, language, doc_mode),
            )
        )
    return results


def summarize(results: list[FileResult]) -> dict:
    original = sum(r.original_tokens for r in results)
    compressed = sum(r.skeleton_tokens for r in results)
    by_lang: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_lang.setdefault(r.language, {"files": 0, "original_tokens": 0, "skeleton_tokens": 0})
        bucket["files"] += 1
        bucket["original_tokens"] += r.original_tokens
        bucket["skeleton_tokens"] += r.skeleton_tokens
    return {
        "files": len(results),
        "original_tokens": original,
        "skeleton_tokens": compressed,
        "saved_pct": round(100.0 * (1 - compressed / original), 1) if original else 0.0,
        "languages": by_lang,
    }


def render_markdown(results: list[FileResult], root_name: str, stats: dict) -> str:
    fence = {"python": "python", "javascript": "javascript", "typescript": "typescript", "go": "go", "rust": "rust"}
    parts = [
        f"# Codebase skeleton: {root_name}",
        "",
        f"> Generated by context_diet v{__version__}. Function bodies are replaced with `...` / `{BODY_PLACEHOLDER}`.",
        f"> {stats['files']} files, {stats['original_tokens']:,} -> {stats['skeleton_tokens']:,} est. tokens "
        f"({stats['saved_pct']}% saved). Read the full file before editing it.",
        "",
    ]
    for r in results:
        if not r.skeleton.strip():
            continue
        parts.append(f"## {r.path}")
        parts.append("")
        parts.append(f"```{fence[r.language]}")
        parts.append(r.skeleton.rstrip("\n"))
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Report rendering (ANSI terminal + SVG "screenshot")
# --------------------------------------------------------------------------

Segment = tuple[str, str]
Line = list[Segment]

ANSI = {
    "": "",
    "dim": "\033[2m",
    "muted": "\033[38;5;245m",
    "bold": "\033[1m",
    "title": "\033[1;38;5;213m",
    "accent": "\033[38;5;117m",
    "green": "\033[1;38;5;114m",
    "yellow": "\033[1;38;5;221m",
    "red": "\033[1;38;5;203m",
    "bar": "\033[38;5;114m",
    "track": "\033[38;5;238m",
    "border": "\033[38;5;61m",
    "prompt": "\033[38;5;114m",
}
SVG_COLORS = {
    "": ("#c9d1d9", "normal"),
    "dim": ("#6e7681", "normal"),
    "muted": ("#8b949e", "normal"),
    "bold": ("#f0f6fc", "bold"),
    "title": ("#ff7ce5", "bold"),
    "accent": ("#79c0ff", "normal"),
    "green": ("#7ee787", "bold"),
    "yellow": ("#f2cc60", "bold"),
    "red": ("#ff7b72", "bold"),
    "bar": ("#7ee787", "normal"),
    "track": ("#30363d", "normal"),
    "border": ("#6c6fd6", "normal"),
    "prompt": ("#7ee787", "normal"),
}
_EIGHTHS = " ▏▎▍▌▋▊▉"


def _bar(pct: float, width: int) -> list[Segment]:
    pct = max(0.0, min(100.0, pct))
    units = round(pct / 100 * width * 8)
    full, rem = divmod(units, 8)
    filled = "█" * full + (_EIGHTHS[rem] if rem else "")
    return [(filled, "bar"), ("░" * (width - len(filled)), "track")]


def _pct_style(pct: float) -> str:
    return "green" if pct >= 70 else "yellow" if pct >= 40 else "red"


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else "…" + text[-(width - 1):]


def build_report(results: list[FileResult], stats: dict, root_name: str, elapsed: float, top: int) -> list[Line]:
    inner = 78
    lines: list[Line] = []
    title = f" CONTEXT DIET  ·  AST skeletonizer  ·  {root_name}"
    lines.append([("╭" + "─" * inner + "╮", "border")])
    lines.append([("│", "border"), (" ■", "title"), (_fit(title, inner - 2).ljust(inner - 2), "bold"), ("│", "border")])
    lines.append([("╰" + "─" * inner + "╯", "border")])
    lines.append([])
    fw = 34
    header = f"  {'FILE'.ljust(fw)} {'LANG'.ljust(10)} {'ORIGINAL':>9} {'SKELETON':>9} {'SAVED':>7}"
    lines.append([(header, "muted")])
    lines.append([("  " + "─" * (inner - 2), "track")])
    ranked = sorted(results, key=lambda r: r.original_tokens, reverse=True)
    for r in ranked[:top]:
        lines.append([
            ("  " + _fit(r.path, fw).ljust(fw) + " ", ""),
            (r.language.ljust(10) + " ", "accent"),
            (f"{r.original_tokens:>9,} ", ""),
            (f"{r.skeleton_tokens:>9,} ", "muted"),
            (f"{r.saved_pct:>6.1f}%", _pct_style(r.saved_pct)),
        ])
    if len(ranked) > top:
        rest = ranked[top:]
        o = sum(r.original_tokens for r in rest)
        s = sum(r.skeleton_tokens for r in rest)
        pct = 100.0 * (1 - s / o) if o else 0.0
        label = f"… {len(rest)} more files"
        lines.append([
            ("  " + label.ljust(fw) + " " + " " * 11, "muted"),
            (f"{o:>9,} ", "muted"),
            (f"{s:>9,} ", "muted"),
            (f"{pct:>6.1f}%", "muted"),
        ])
    lines.append([("  " + "─" * (inner - 2), "track")])
    lines.append([])
    saved = stats["saved_pct"]
    lines.append([("  Total Files Scanned   ", "muted"), (f"{stats['files']:>10,}", "bold")])
    lines.append([("  Original Tokens       ", "muted"), (f"{stats['original_tokens']:>10,}", "bold")])
    lines.append([("  Compressed Tokens     ", "muted"), (f"{stats['skeleton_tokens']:>10,}", "bold")])
    lines.append(
        [("  Percentage Saved      ", "muted"), (f"{saved:>9.1f}% saved", _pct_style(saved)), ("   ", "")]
        + _bar(saved, 30)
    )
    lines.append([])
    langs = sorted(stats["languages"].items(), key=lambda kv: kv[1]["original_tokens"], reverse=True)
    lang_line: Line = [("  by language  ", "muted")]
    for idx, (lang, b) in enumerate(langs):
        pct = 100.0 * (1 - b["skeleton_tokens"] / b["original_tokens"]) if b["original_tokens"] else 0.0
        if idx:
            lang_line.append(("  ·  ", "track"))
        lang_line.append((f"{lang} ×{b['files']} ", "accent"))
        lang_line.append((f"{pct:.1f}%", _pct_style(pct)))
    lines.append(lang_line)
    lines.append([(f"  scanned in {elapsed:.2f}s  ·  tokens ≈ chars / 4  ·  bodies → ...", "dim")])
    return lines


def _supports_color(stream) -> bool:
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def _enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def ensure_utf8_stdout() -> None:
    """Make box-drawing output safe on legacy Windows code pages and pipes."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower()
        if encoding.replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def render_ansi(lines: list[Line], color: bool) -> str:
    out = []
    for line in lines:
        if color:
            out.append("".join(f"{ANSI[style]}{text}\033[0m" if style else text for text, style in line))
        else:
            out.append("".join(text for text, _ in line))
    return "\n".join(out)


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_svg(lines: list[Line], command: str) -> str:
    """Render the report as a terminal-window SVG (crisp in GitHub READMEs, no screenshot tool needed)."""
    char_w, line_h, pad_x, top = 8.4, 20, 22, 58
    all_lines: list[Line] = [[("$ ", "prompt"), (command, "bold")], []] + lines
    cols = max((sum(len(t) for t, _ in line) for line in all_lines), default=80)
    width = int(pad_x * 2 + cols * char_w)
    height = top + len(all_lines) * line_h + 18
    font = "ui-monospace, SFMono-Regular, 'Cascadia Mono', Menlo, Consolas, 'Liberation Mono', monospace"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="context_diet benchmark output">',
        f'<rect width="{width}" height="{height}" rx="12" fill="#0d1117"/>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="none" stroke="#30363d"/>',
        '<circle cx="22" cy="20" r="6" fill="#ff5f57"/><circle cx="42" cy="20" r="6" fill="#febc2e"/><circle cx="62" cy="20" r="6" fill="#28c840"/>',
        f'<text x="{width / 2:.0f}" y="24" text-anchor="middle" fill="#8b949e" font-family="{font}" font-size="12">context_diet.py</text>',
        f'<g font-family="{font}" font-size="14" xml:space="preserve">',
    ]
    for row, line in enumerate(all_lines):
        y = top + row * line_h
        col = 0
        for text, style in line:
            color, weight = SVG_COLORS.get(style, SVG_COLORS[""])
            # Place every run of visible characters at its exact column: renderers collapse
            # whitespace inconsistently, so padding is expressed through x positions instead.
            for run in re.finditer(r"\S+(?: \S+)*", text):
                x = pad_x + (col + run.start()) * char_w
                chunk = run.group(0)
                parts.append(
                    f'<text x="{x:.1f}" y="{y}" fill="{color}" font-weight="{weight}" '
                    f'textLength="{len(chunk) * char_w:.1f}" lengthAdjust="spacingAndGlyphs">{_xml_escape(chunk)}</text>'
                )
            col += len(text)
    parts.append("</g></svg>")
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="context_diet",
        description="Skeletonize a codebase for Claude: keep signatures, types, classes and docstrings; drop bodies.",
    )
    parser.add_argument("--target", default=".", help="directory to analyze (default: current directory)")
    parser.add_argument("--output", help="write the skeleton as Markdown to this file")
    parser.add_argument("--stdout", action="store_true", help="print the skeleton Markdown to stdout instead of the report")
    parser.add_argument("--demo", action="store_true", help="benchmark the claude-sentinel-suite repository itself")
    parser.add_argument("--docstrings", choices=("full", "summary", "none"), default="summary",
                        help="how much of each Python docstring to keep (default: summary = first paragraph)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="extra glob to skip (repeatable), e.g. --exclude 'tests/*'")
    parser.add_argument("--top", type=int, default=12, help="files to list in the report (default: 12)")
    parser.add_argument("--json", action="store_true", help="print machine-readable stats as JSON")
    parser.add_argument("--svg", metavar="FILE", help="also export the report as an SVG terminal screenshot")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_utf8_stdout()
    root = Path(__file__).resolve().parent.parent if args.demo else Path(args.target).resolve()
    if not root.is_dir():
        print(f"context_diet: target is not a directory: {root}", file=sys.stderr)
        return 2

    started = time.perf_counter()
    results = analyze(root, args.docstrings, tuple(args.exclude))
    elapsed = time.perf_counter() - started
    stats = summarize(results)
    root_name = root.name or str(root)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_markdown(results, root_name, stats), encoding="utf-8")

    if args.stdout:
        sys.stdout.write(render_markdown(results, root_name, stats))
        return 0

    if args.json:
        payload = dict(stats, target=str(root), files_detail=[
            {"path": r.path, "language": r.language, "original_tokens": r.original_tokens,
             "skeleton_tokens": r.skeleton_tokens, "saved_pct": round(r.saved_pct, 1)}
            for r in results
        ])
        print(json.dumps(payload, indent=2))
        return 0

    if not results:
        print(f"context_diet: no .py/.js/.ts/.go/.rs files found under {root}", file=sys.stderr)
        return 1

    report = build_report(results, stats, root_name, elapsed, max(1, args.top))
    color = not args.no_color and _supports_color(sys.stdout)
    if color:
        _enable_windows_ansi()
    print()
    print(render_ansi(report, color))
    if args.output:
        print(render_ansi([[("  ✔ skeleton written to ", "green"), (str(args.output), "bold")]], color))
    print()

    if args.svg:
        command = "python scripts/context_diet.py --demo" if args.demo else "python context_diet.py --target " + args.target
        svg_path = Path(args.svg)
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text(render_svg(report, command), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
