#!/usr/bin/env python3
"""
handoff: keeps .claude/HANDOFF.md alive across compactions and new sessions.

Two Claude Code hook entry points:

    handoff.py precompact      PreCompact hook. Before Claude Code compacts the context, refresh the
                               "Modified Files" section from git and make sure a HANDOFF.md exists
                               (seeded with the last user request if the session-scribe agent has
                               not written one yet).
    handoff.py session-start   SessionStart hook. If the project has a HANDOFF.md, print it so Claude
                               Code adds it to the new session's context: the session resumes
                               exactly where the last one stopped.

The session-scribe agent writes the full document; this script only guarantees there is always
a recent snapshot. Zero dependencies.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SECTIONS = ["Current Objective", "Completed Steps", "Modified Files", "Pending Blockers", "Next Exact Command"]
HANDOFF_REL = Path(".claude") / "HANDOFF.md"
MAX_CONTEXT_CHARS = 8000
STALE_AFTER_DAYS = 14


def read_event() -> dict:
    try:
        return json.loads(sys.stdin.read().lstrip("﻿") or "{}")
    except json.JSONDecodeError:
        return {}


def git_changes(cwd: Path, limit: int = 40) -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    labels = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "?": "untracked", "U": "conflict"}
    lines = []
    for raw in proc.stdout.splitlines():
        if len(raw) < 4:
            continue
        code, path = raw[:2], raw[3:]
        if path.startswith(".claude/") or path == ".claude":
            continue
        letter = next((c for c in code if c.strip()), "M")
        lines.append(f"- `{path}` ({labels.get(letter, 'changed')})")
    if len(lines) > limit:
        extra = len(lines) - limit
        lines = lines[:limit] + [f"- … and {extra} more (run `git status`)"]
    return lines


def last_user_request(transcript_path: str | None, limit: int = 600) -> str | None:
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return None
    last = None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "user" or entry.get("isMeta"):
                    continue
                content = (entry.get("message") or {}).get("content")
                text = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                    text = "\n".join(p for p in parts if p)
                if not text:
                    continue
                stripped = text.strip()
                if stripped.startswith(("<command-", "<local-command", "<system-reminder", "Caveat:")):
                    continue
                last = stripped
    except OSError:
        return None
    if last and len(last) > limit:
        last = last[: limit - 1].rstrip() + "…"
    return last


def parse_sections(text: str) -> tuple[str, dict[str, str]]:
    """Split a HANDOFF.md into its preamble and ``## Section`` bodies."""
    parts = re.split(r"^##\s+(.+?)\s*$", text, flags=re.M)
    preamble = parts[0]
    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        sections[parts[i].strip()] = parts[i + 1].strip("\n")
    return preamble, sections


def render(preamble: str, sections: dict[str, str]) -> str:
    out = [preamble.rstrip("\n"), ""]
    ordered = SECTIONS + [s for s in sections if s not in SECTIONS]
    for name in ordered:
        if name not in sections:
            continue
        out.append(f"## {name}")
        out.append("")
        out.append(sections[name].strip("\n") or "- _(none)_")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def precompact(event: dict) -> int:
    cwd = Path(event.get("cwd") or os.getcwd())
    target = cwd / HANDOFF_REL
    stamp = _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    trigger = event.get("trigger") or "auto"
    changes = git_changes(cwd) or ["- _(no uncommitted changes, or not a git repository)_"]
    snapshot_line = f"_Auto-snapshot before {trigger} compaction: {stamp}_"

    if target.is_file():
        preamble, sections = parse_sections(target.read_text(encoding="utf-8", errors="replace"))
        preamble = re.sub(r"^_Auto-snapshot before .*_\s*$\n?", "", preamble, flags=re.M).rstrip("\n")
        preamble = preamble + "\n\n" + snapshot_line + "\n"
        sections["Modified Files"] = "\n".join(changes)
        for name in SECTIONS:
            sections.setdefault(name, "- _(not recorded)_")
    else:
        objective = last_user_request(event.get("transcript_path")) or "_(unknown: ask the user to restate the goal)_"
        preamble = "# Session Handoff\n\n" + snapshot_line + "\n"
        sections = {
            "Current Objective": objective,
            "Completed Steps": "- _(not recorded yet: invoke the session-scribe agent to fill this in)_",
            "Modified Files": "\n".join(changes),
            "Pending Blockers": "- _(unknown)_",
            "Next Exact Command": "_(unknown: re-read the objective and the modified files, then continue)_",
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(preamble, sections), encoding="utf-8")
    return 0


def session_start(event: dict) -> int:
    cwd = Path(event.get("cwd") or os.getcwd())
    target = cwd / HANDOFF_REL
    if not target.is_file():
        return 0
    text = target.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return 0
    age_days = (_dt.datetime.now().timestamp() - target.stat().st_mtime) / 86400
    note = ""
    if age_days > STALE_AFTER_DAYS:
        note = f" It is {int(age_days)} days old: confirm with the user that it is still relevant."
    if len(text) > MAX_CONTEXT_CHARS:
        text = text[:MAX_CONTEXT_CHARS] + "\n\n… (truncated; read .claude/HANDOFF.md for the rest)"
    source = event.get("source") or "startup"
    print(
        f"[sentinel-suite] Found .claude/HANDOFF.md from a previous session (session {source}).{note}\n"
        "Resume from it: verify the Modified Files against `git status`, then continue with the Next Exact Command. "
        "Do not redo Completed Steps.\n\n"
        + text
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="handoff", description="HANDOFF.md hooks for Claude Code")
    parser.add_argument("mode", choices=("precompact", "session-start"))
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    event = read_event()
    try:
        return precompact(event) if args.mode == "precompact" else session_start(event)
    except OSError as exc:
        print(f"handoff: {exc}", file=sys.stderr)
        return 0  # a failing hook must never block the session


if __name__ == "__main__":
    sys.exit(main())
