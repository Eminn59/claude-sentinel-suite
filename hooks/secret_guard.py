#!/usr/bin/env python3
"""
secret_guard: Claude Code PreToolUse hook that stops secret leaks and catastrophic commands.

What it does, before the tool runs:

* Bash        deny  catastrophic commands (rm -rf /, mkfs, dd to a disk, fork bombs, format C:)
              ask   risky ones (force push, reset --hard, DROP TABLE, curl | sh, dumping .env / env vars)
* Write/Edit  deny  writing a live-looking credential into any file except a real .env file
* Read        deny  reading .env files, private keys and credential stores into the transcript

Register it in ~/.claude/settings.json (install.sh --with-hooks does this for you):

    {"hooks": {"PreToolUse": [{"matcher": "Bash|Read|Write|Edit|MultiEdit",
      "hooks": [{"type": "command", "command": "python3 /path/to/secret_guard.py"}]}]}}

Try it by hand:

    python secret_guard.py --check "rm -rf ~"
    python secret_guard.py --check-file path/to/file.py

Set SENTINEL_GUARD_DISABLE=1 to switch it off for one session. Zero dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import PurePath

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}")),
    ("AWS access key ID", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe live key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b")),
    ("Discord webhook URL", re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]{30,}")),
    ("Slack webhook URL", re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{20,}")),
    ("Hugging Face token", re.compile(r"\bhf_[A-Za-z0-9]{34,}\b")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
]
GENERIC_SECRET = re.compile(
    r"""(?i)\b[\w-]*(?:api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token|private[_-]?key)[\w-]*["']?\s*[:=]\s*["']([^"'\s]{16,})["']"""
)
PLACEHOLDER = re.compile(
    r"(?i)(?:x{4,}|\*{4,}|\.{3}|your[_-]?|example|placeholder|changeme|change[_-]me|dummy|test|fake|sample|"
    r"<[^>]*>|\$\{|\{\{|process\.env|os\.environ|getenv|redacted|todo|lorem)"
)

SECRET_FILE_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".pgpass", "credentials",
    ".npmrc", ".pypirc", "secrets.json", "service-account.json",
})
SECRET_FILE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
ENV_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "template", "dist", "defaults", "default", "schema"})
ENV_TOKEN = re.compile(r"(?:^|[\s\"'=/\\<>|;&(])(\.env(?:\.[\w.-]+)?)(?=$|[\s\"';|&)>])")

DENY_COMMANDS: list[tuple[str, re.Pattern[str]]] = [
    ("recursive delete of the filesystem root or home directory", re.compile(
        r"\brm\s+(?:-[-\w]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(?:-[-\w]*\s+)*[\"']?"
        r"(?:/|/\*|~|~/|~/\*|\$HOME|\$\{HOME\}|\$HOME/\*|/(?:usr|etc|var|bin|boot|lib|opt|home|Users|System))[\"']?(?=$|[\s;&|)])"
    )),
    ("rm with --no-preserve-root", re.compile(r"--no-preserve-root")),
    ("formatting a filesystem", re.compile(r"(?:^|[\s;&|])mkfs(?:\.\w+)?\s")),
    ("raw write to a disk device", re.compile(r"\bdd\b[^\n]*\bof=/dev/(?:sd|hd|nvme|disk|mmcblk|xvd|vd)")),
    ("overwriting a disk device", re.compile(r">\s*/dev/(?:sd|hd|nvme|disk|mmcblk)\w*")),
    ("fork bomb", re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")),
    ("recursive chmod/chown on the filesystem root", re.compile(r"\bch(?:mod|own)\s+(?:-\w+\s+)*-[a-zA-Z]*R[a-zA-Z]*\s+\S+\s+/(?=$|[\s;&|])")),
    ("formatting a Windows drive", re.compile(r"(?i)(?:^|[\s;&|])format(?:\.com)?\s+[a-z]:")),
    ("recursive delete of a Windows drive or profile", re.compile(
        r"(?i)(?:\brd\b|\brmdir\b|\bdel\b|Remove-Item|\bri\b|\brm\b)[^\n]*(?:/s|-Recurse|-r\b)[^\n]*"
        r"(?:[\"']?[a-z]:\\?[\"']?(?=$|[\s;&|])|\$env:USERPROFILE[\"']?(?=$|[\s;&|])|\$HOME[\"']?(?=$|[\s;&|]))"
    )),
    ("wiping a disk with diskpart / cipher", re.compile(r"(?i)\b(?:diskpart|cipher\s+/w)\b")),
]

ASK_COMMANDS: list[tuple[str, re.Pattern[str]]] = [
    ("recursive delete of the current or parent directory", re.compile(
        r"\brm\s+(?:-[-\w]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(?:-[-\w]*\s+)*[\"']?(?:\.|\.\.|\./|\.\./|\*|\./\*)[\"']?(?=$|[\s;&|)])"
    )),
    ("force push (use --force-with-lease)", re.compile(r"\bgit\s+push\b(?=[^\n]*(?:\s--force(?!-with-lease)\b|\s-f\b|\s\+\w))")),
    ("git push --mirror / --delete", re.compile(r"\bgit\s+push\b[^\n]*\s--(?:mirror|delete)\b")),
    ("git reset --hard discards uncommitted work", re.compile(r"\bgit\s+reset\b[^\n]*--hard\b")),
    ("git clean deletes untracked files", re.compile(r"\bgit\s+clean\b[^\n]*\s-[a-zA-Z]*f")),
    ("discarding all local changes", re.compile(r"\bgit\s+(?:checkout\s+--\s+\.|restore\s+(?:--\w+\s+)*\.)(?=$|[\s;&|])")),
    ("force-deleting a git branch", re.compile(r"\bgit\s+branch\s+(?:-\w*D\w*|--delete\s+--force)\b")),
    ("rewriting git history", re.compile(r"\bgit\s+(?:filter-branch|filter-repo)\b")),
    ("destructive SQL", re.compile(r"(?i)\b(?:DROP\s+(?:DATABASE|SCHEMA|TABLE)|TRUNCATE\s+TABLE)\b")),
    ("DELETE without WHERE", re.compile(r"(?i)\bDELETE\s+FROM\s+[\w.\"`\[\]]+\s*(?:;|$|[\"'])")),
    ("piping a download straight into a shell", re.compile(r"(?i)\b(?:curl|wget|iwr|Invoke-WebRequest)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b|\biex\s*\(\s*(?:New-Object|iwr|Invoke-WebRequest)")),
    ("dumping all environment variables", re.compile(r"(?i)(?:^|[;&|]\s*)(?:printenv|env|set|export\s+-p|Get-ChildItem\s+env:|gci\s+env:|dir\s+env:)\s*(?=$|[;&|>])")),
    ("printing a secret environment variable", re.compile(
        r"(?i)\b(?:echo|printf|print|Write-(?:Host|Output))\b[^\n]*\$(?:env:)?\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)\w*"
    )),
    ("system shutdown / reboot", re.compile(r"(?i)(?:^|[\s;&|])(?:sudo\s+)?(?:shutdown|reboot|halt|poweroff|Stop-Computer|Restart-Computer)\b")),
]


@dataclass
class Decision:
    action: str  # "deny" | "ask"
    reason: str


def is_env_file(name: str) -> bool:
    base = PurePath(name.replace("\\", "/")).name
    if base == ".env":
        return True
    if base.startswith(".env."):
        last = base.rsplit(".", 1)[-1].lower()
        return last not in ENV_TEMPLATE_SUFFIXES
    return False


def is_secret_file(name: str) -> bool:
    base = PurePath(name.replace("\\", "/")).name.lower()
    if is_env_file(base):
        return True
    if base in SECRET_FILE_NAMES:
        return True
    norm = name.replace("\\", "/").lower()
    if norm.endswith((".aws/credentials", ".docker/config.json", ".kube/config", ".ssh/config")):
        return True
    return base.endswith(SECRET_FILE_SUFFIXES) and not base.endswith((".pub.pem", "public.pem", "cert.pem", "ca.pem"))


def find_secret(text: str) -> str | None:
    """Name of the first live-looking credential in ``text``, or None."""
    if not text:
        return None
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if not PLACEHOLDER.search(match.group(0)):
                return label
    for match in GENERIC_SECRET.finditer(text):
        value = match.group(1)
        if PLACEHOLDER.search(value):
            continue
        has_digit = any(c.isdigit() for c in value)
        has_alpha = any(c.isalpha() for c in value)
        if has_digit and has_alpha and len(set(value)) >= 8:
            return "hard-coded credential"
    return None


def check_command(command: str) -> Decision | None:
    if not command or not command.strip():
        return None
    for reason, pattern in DENY_COMMANDS:
        if pattern.search(command):
            return Decision("deny", f"Blocked: {reason}. This command is irreversible; ask the user to run it themselves if it is really intended.")
    secret = find_secret(command)
    if secret:
        return Decision("deny", f"Blocked: the command contains a {secret}. Reference it through an environment variable instead of pasting it.")
    for match in ENV_TOKEN.finditer(command):
        if is_env_file(match.group(1)):
            return Decision("ask", f"The command touches {match.group(1)}, which holds secrets. Confirm before its contents can reach the transcript.")
    for reason, pattern in ASK_COMMANDS:
        if pattern.search(command):
            return Decision("ask", f"Risky command: {reason}. Confirm that this is intended.")
    return None


def evaluate(tool_name: str, tool_input: dict) -> Decision | None:
    if tool_name == "Bash":
        return check_command(str(tool_input.get("command", "")))
    if tool_name == "Read":
        path = str(tool_input.get("file_path", ""))
        if is_secret_file(path):
            return Decision("deny", f"Blocked: {PurePath(path).name} holds secrets. Read the matching .env.example or ask the user for the specific non-secret value you need.")
        return None
    if tool_name in ("Write", "Edit", "MultiEdit"):
        path = str(tool_input.get("file_path", ""))
        if is_env_file(path):
            return None  # .env files are where secrets belong
        chunks = [str(tool_input.get("content", "")), str(tool_input.get("new_string", ""))]
        for edit in tool_input.get("edits", []) or []:
            if isinstance(edit, dict):
                chunks.append(str(edit.get("new_string", "")))
        secret = find_secret("\n".join(chunks))
        if secret:
            return Decision("deny", f"Blocked: this edit would hard-code a {secret} into {PurePath(path).name or 'a file'}. "
                                    "Load it from an environment variable or a gitignored .env file instead.")
    return None


def hook_output(decision: Decision) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision.action,
            "permissionDecisionReason": f"[sentinel secret-guard] {decision.reason}",
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="secret_guard", description="Claude Code PreToolUse guard (reads the hook event from stdin).")
    parser.add_argument("--check", metavar="COMMAND", help="evaluate a shell command and print the decision")
    parser.add_argument("--check-file", metavar="PATH", help="scan a file for hard-coded credentials")
    args = parser.parse_args(argv)

    if args.check is not None:
        decision = check_command(args.check)
        print(json.dumps({"action": decision.action, "reason": decision.reason} if decision else {"action": "allow"}))
        return 0
    if args.check_file:
        with open(args.check_file, encoding="utf-8", errors="replace") as handle:
            secret = find_secret(handle.read())
        print(json.dumps({"file": args.check_file, "secret": secret}))
        return 1 if secret else 0

    if os.environ.get("SENTINEL_GUARD_DISABLE") == "1":
        return 0
    try:
        event = json.loads(sys.stdin.read().lstrip("﻿") or "{}")
    except json.JSONDecodeError:
        return 0  # never break the session on malformed input
    decision = evaluate(str(event.get("tool_name", "")), event.get("tool_input") or {})
    if decision:
        sys.stdout.write(json.dumps(hook_output(decision)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
