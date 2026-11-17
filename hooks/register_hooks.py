#!/usr/bin/env python3
"""
register_hooks: add (or remove) the Sentinel Suite hooks in Claude Code's settings.json.

    python register_hooks.py                      # install into ~/.claude/settings.json
    python register_hooks.py --uninstall          # remove them again
    python register_hooks.py --dry-run            # print the merged settings, change nothing

Existing settings and hooks are preserved, a timestamped backup is written before any change,
and running it twice is harmless. Zero dependencies.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
from pathlib import Path

SCRIPT_NAMES = ("secret_guard.py", "handoff.py")
GUARD_MATCHER = "Bash|Read|Write|Edit|MultiEdit"


def default_settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


def _quote(path: str) -> str:
    return '"' + path.replace('"', '\\"') + '"'


def desired_hooks(python: str, root: Path) -> dict[str, list[dict]]:
    hooks_dir = (root / "hooks").as_posix()
    py = _quote(Path(python).as_posix())
    return {
        "PreToolUse": [{
            "matcher": GUARD_MATCHER,
            "hooks": [{"type": "command", "command": f"{py} {_quote(hooks_dir + '/secret_guard.py')}", "timeout": 10}],
        }],
        "PreCompact": [{
            "hooks": [{"type": "command", "command": f"{py} {_quote(hooks_dir + '/handoff.py')} precompact", "timeout": 20}],
        }],
        "SessionStart": [{
            "hooks": [{"type": "command", "command": f"{py} {_quote(hooks_dir + '/handoff.py')} session-start", "timeout": 10}],
        }],
    }


def _is_ours(hook: dict) -> bool:
    command = str(hook.get("command", ""))
    return "sentinel" in command.lower() and any(name in command for name in SCRIPT_NAMES)


def strip_ours(settings: dict) -> dict:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            inner = [h for h in group.get("hooks", []) if not (isinstance(h, dict) and _is_ours(h))]
            if inner:
                kept_groups.append(dict(group, hooks=inner))
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def merge(settings: dict, python: str, root: Path) -> dict:
    settings = strip_ours(settings)
    hooks = settings.setdefault("hooks", {})
    for event, groups in desired_hooks(python, root).items():
        hooks.setdefault(event, []).extend(groups)
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="register_hooks", description="Install the Sentinel Suite hooks into Claude Code settings.")
    parser.add_argument("--settings", type=Path, default=default_settings_path(), help="settings.json to edit (default: ~/.claude/settings.json)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                        help="installed suite directory that contains hooks/ (default: this repository)")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter the hooks run with (default: this one)")
    parser.add_argument("--uninstall", action="store_true", help="remove the Sentinel Suite hooks")
    parser.add_argument("--dry-run", action="store_true", help="print the resulting settings without writing")
    args = parser.parse_args(argv)

    path: Path = args.settings.expanduser()
    settings: dict = {}
    if path.is_file():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                settings = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"register_hooks: {path} is not valid JSON ({exc}); fix it first, nothing was changed.", file=sys.stderr)
                return 1
        if not isinstance(settings, dict):
            print(f"register_hooks: {path} does not contain a JSON object; nothing was changed.", file=sys.stderr)
            return 1

    updated = strip_ours(settings) if args.uninstall else merge(settings, args.python, args.root.expanduser())
    text = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        sys.stdout.write(text)
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)
        print(f"  backup  {backup}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    action = "removed from" if args.uninstall else "registered in"
    print(f"  hooks   {action} {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
