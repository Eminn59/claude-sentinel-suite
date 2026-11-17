#!/usr/bin/env python3
"""
nightshift: unattended task runner for Claude Code with Discord / Telegram alerts.

Reads a queue of tasks from a Markdown checklist (TASKS.md) or the command line,
runs them one after another (shell commands or headless ``claude -p`` prompts),
logs output and timing, and reports to Discord and/or Telegram webhooks using
nothing but ``urllib.request``.

TASKS.md format::

    - [ ] `pytest -q`                                 shell command
    - [ ] Run the linter: `ruff check .`              shell command with a title
    - [ ] claude: Fix the failing test in tests/      Claude headless prompt
    - [ ] Update the CHANGELOG for v1.2               plain text = Claude prompt
    - [x] Already done                                skipped

    python nightshift.py --tasks TASKS.md --discord https://discord.com/api/webhooks/...
    python nightshift.py --task "npm test" --task "claude: summarize failing tests"
    python nightshift.py --dry-run

Zero dependencies. Python 3.10+.
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import html
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "1.0.0"

USER_AGENT = f"nightshift/{__version__} (+https://github.com/Eminn59/claude-sentinel-suite)"
TASK_LINE = re.compile(r"^(?P<indent>\s*)[-*+]\s+\[(?P<done>[ xX])\]\s+(?P<body>.+?)\s*$")
CLAUDE_PREFIX = re.compile(r"^(?:claude|prompt|ai)\s*:\s*(?P<prompt>.+)$", re.IGNORECASE)
SHELL_PREFIX = re.compile(r"^(?:sh|shell|run|cmd|bash)\s*:\s*(?P<cmd>.+)$", re.IGNORECASE)
TITLED_CMD = re.compile(r"^(?P<title>[^`]*?)\s*[:—–-]?\s*`(?P<cmd>[^`]+)`\s*$")
CMD_TITLED = re.compile(r"^`(?P<cmd>[^`]+)`\s*(?:[:—–-]+\s*(?P<title>.*))?$")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class Task:
    index: int
    kind: str  # "shell" | "claude"
    payload: str
    title: str
    line_no: int | None = None


@dataclass
class Result:
    task: Task
    status: str  # "passed" | "failed" | "timeout" | "skipped" | "interrupted"
    exit_code: int | None
    duration: float
    tail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "passed"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_task_body(body: str) -> tuple[str, str, str]:
    """Return (kind, payload, title) for one checklist entry or CLI argument."""
    body = body.strip()
    match = CLAUDE_PREFIX.match(body)
    if match:
        prompt = match.group("prompt").strip()
        return "claude", prompt, _shorten(prompt, 70)
    match = SHELL_PREFIX.match(body)
    if match:
        cmd = match.group("cmd").strip().strip("`")
        return "shell", cmd, cmd
    match = CMD_TITLED.match(body)
    if match:
        cmd = match.group("cmd").strip()
        return "shell", cmd, (match.group("title") or "").strip() or cmd
    match = TITLED_CMD.match(body)
    if match:
        cmd = match.group("cmd").strip()
        return "shell", cmd, match.group("title").strip().rstrip(":").strip() or cmd
    return "claude", body, _shorten(body, 70)


def parse_tasks_markdown(text: str) -> list[Task]:
    tasks: list[Task] = []
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = TASK_LINE.match(line)
        if not match or match.group("done") in "xX":
            continue
        kind, payload, title = parse_task_body(match.group("body"))
        tasks.append(Task(index=len(tasks) + 1, kind=kind, payload=payload, title=title, line_no=line_no))
    return tasks


def mark_done(path: Path, line_no: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    idx = line_no - 1
    if 0 <= idx < len(lines):
        lines[idx] = re.sub(r"\[ \]", "[x]", lines[idx], count=1)
        path.write_text("".join(lines), encoding="utf-8")


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# --------------------------------------------------------------------------
# Terminal UI
# --------------------------------------------------------------------------

class Console:
    def __init__(self, color: bool) -> None:
        self.color = color
        self.live = color and sys.stdout.isatty()

    def c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def line(self, text: str = "") -> None:
        if self.live:
            sys.stdout.write("\r\033[2K")
        print(text, flush=True)

    def status(self, text: str) -> None:
        if self.live:
            width = shutil.get_terminal_size((100, 20)).columns - 1
            sys.stdout.write("\r\033[2K" + text[: width + 40])
            sys.stdout.flush()

    def bar(self, done: int, total: int, width: int = 24) -> str:
        filled = int(round(width * done / total)) if total else width
        return self.c("38;5;114", "█" * filled) + self.c("38;5;238", "░" * (width - filled))


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


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

class Notifier:
    def __init__(self, discord: str | None, telegram_token: str | None, telegram_chat: str | None,
                 console: Console, dry_run: bool = False) -> None:
        self.discord = discord
        self.telegram_token = telegram_token
        self.telegram_chat = telegram_chat
        self.console = console
        self.dry_run = dry_run

    @property
    def enabled(self) -> bool:
        return bool(self.discord or (self.telegram_token and self.telegram_chat))

    def _post(self, url: str, payload: dict) -> bool:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    return 200 <= response.status < 300
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 2:
                    retry = exc.headers.get("Retry-After") or "2"
                    try:
                        time.sleep(min(float(retry), 10.0))
                    except ValueError:
                        time.sleep(2)
                    continue
                self.console.line(self.console.c("33", f"  ! webhook rejected the message: HTTP {exc.code}"))
                return False
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                self.console.line(self.console.c("33", f"  ! webhook unreachable: {exc}"))
                return False
        return False

    def send(self, discord_payload: dict, telegram_text: str) -> None:
        if self.dry_run:
            return
        if self.discord:
            self._post(self.discord, discord_payload)
        if self.telegram_token and self.telegram_chat:
            self._post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                {"chat_id": self.telegram_chat, "text": telegram_text, "parse_mode": "HTML",
                 "disable_web_page_preview": True},
            )


STATUS_ICON = {"passed": "✅", "failed": "❌", "timeout": "⏱️", "skipped": "⏭️", "interrupted": "🛑"}


def discord_payload(results: list[Result], total: int, started: _dt.datetime, finished: _dt.datetime,
                    title_suffix: str = "") -> dict:
    passed = sum(r.ok for r in results)
    failed = len(results) - passed
    complete = len(results) == total and not any(r.status == "interrupted" for r in results)
    color = 0x2ECC71 if failed == 0 and complete else (0xE74C3C if failed else 0xF1C40F)
    headline = "🌙 Nightshift " + ("run complete" if complete else "run stopped")
    fields = []
    for r in results[:25]:
        value = f"`{r.task.kind}` · {_fmt_duration(r.duration)}"
        if r.exit_code is not None:
            value += f" · exit {r.exit_code}"
        if r.tail and not r.ok:
            tail = "\n".join(r.tail[-8:])
            tail = tail.replace("```", "'''")
            value += f"\n```\n{tail[-(1000 - len(value) - 10):]}\n```"
        fields.append({
            "name": _shorten(f"{STATUS_ICON[r.status]} {r.task.index}. {r.task.title}", 256),
            "value": value[:1024],
            "inline": False,
        })
    description = (
        f"**{passed}/{total}** tasks passed"
        + (f" · **{failed}** failed" if failed else "")
        + f" · total {_fmt_duration((finished - started).total_seconds())}"
    )
    return {
        "username": "Nightshift",
        "embeds": [{
            "title": (headline + title_suffix)[:256],
            "description": description[:4096],
            "color": color,
            "fields": fields,
            "footer": {"text": _shorten(f"{platform.node()} · {Path.cwd()}", 2048)},
            "timestamp": finished.astimezone(_dt.timezone.utc).isoformat(),
        }],
    }


def telegram_text(results: list[Result], total: int, started: _dt.datetime, finished: _dt.datetime) -> str:
    passed = sum(r.ok for r in results)
    lines = [
        f"<b>🌙 Nightshift</b> — {passed}/{total} passed in {_fmt_duration((finished - started).total_seconds())}",
        f"<i>{html.escape(platform.node())} · {html.escape(str(Path.cwd()))}</i>",
        "",
    ]
    for r in results:
        line = f"{STATUS_ICON[r.status]} <b>{r.task.index}.</b> {html.escape(r.task.title)} · {_fmt_duration(r.duration)}"
        lines.append(line)
        if r.tail and not r.ok:
            tail = html.escape("\n".join(r.tail[-6:]))[-600:]
            lines.append(f"<pre>{tail}</pre>")
    text = "\n".join(lines)
    return text if len(text) <= 4000 else text[:3990] + "…"


def _mask(url: str) -> str:
    return re.sub(r"(webhooks/\d+/|bot)[\w:-]{6,}", lambda m: m.group(1) + "••••••", url)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def build_command(task: Task, claude_bin: str, claude_flags: list[str]) -> tuple[list[str] | str, bool]:
    if task.kind == "claude":
        return [claude_bin, "-p", task.payload, *claude_flags], False
    return task.payload, True


def run_task(task: Task, args: argparse.Namespace, console: Console, log, position: int, total: int) -> Result:
    claude_bin = args.claude_bin or shutil.which("claude") or "claude"
    command, use_shell = build_command(task, claude_bin, shlex.split(args.claude_flags or "", posix=os.name != "nt"))
    started = time.monotonic()
    tail: collections.deque[str] = collections.deque(maxlen=60)
    log.write(f"\n=== [{position}/{total}] {task.kind}: {task.payload}\n")
    log.flush()
    try:
        process = subprocess.Popen(
            command, shell=use_shell, cwd=args.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except (FileNotFoundError, PermissionError) as exc:
        message = f"could not start: {exc}"
        log.write(message + "\n")
        return Result(task, "failed", None, 0.0, [message])

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            stripped = line.rstrip()
            if stripped:
                tail.append(stripped)
        log.flush()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    status = "passed"
    frame = 0
    try:
        while process.poll() is None:
            elapsed = time.monotonic() - started
            if args.timeout and elapsed > args.timeout:
                process.kill()
                status = "timeout"
                break
            last = tail[-1] if tail else ""
            console.status(
                f"  {console.c('38;5;141', SPINNER[frame % len(SPINNER)])} "
                f"{console.c('2', f'[{position}/{total}]')} {_shorten(task.title, 48)} "
                f"{console.c('38;5;117', _fmt_duration(elapsed))}  {console.c('2', _shorten(last, 60))}"
            )
            frame += 1
            time.sleep(0.1)
    except KeyboardInterrupt:
        process.kill()
        process.wait()
        reader.join(timeout=2)
        if process.stdout:
            process.stdout.close()
        return Result(task, "interrupted", None, time.monotonic() - started, list(tail))
    process.wait()
    reader.join(timeout=5)
    if process.stdout:
        process.stdout.close()
    duration = time.monotonic() - started
    code = process.returncode
    if status != "timeout" and code != 0:
        status = "failed"
    log.write(f"=== exit {code} after {duration:.1f}s ({status})\n")
    log.flush()
    return Result(task, status, code, duration, list(tail))


def print_result(console: Console, result: Result, done: int, total: int) -> None:
    color = {"passed": "1;32", "failed": "1;31", "timeout": "1;33", "interrupted": "1;33"}.get(result.status, "2")
    word = {"passed": "PASS", "failed": "FAIL", "timeout": "TIME", "interrupted": "STOP", "skipped": "SKIP"}[result.status]
    console.line(
        f"  {console.c(color, word)}  {console.c('2', f'{result.task.index:>2}.')} "
        f"{_shorten(result.task.title, 52):<52} {console.c('38;5;117', _fmt_duration(result.duration)):>8}"
    )
    if not result.ok and result.tail:
        for tail_line in result.tail[-5:]:
            console.line(console.c("2", "        │ " + _shorten(tail_line, 100)))
    console.line(f"        {console.bar(done, total)} {console.c('2', f'{done}/{total}')}")


def print_header(console: Console, tasks: list[Task], notifier: Notifier, dry_run: bool) -> None:
    console.line()
    console.line(console.c("1;38;5;141", "  🌙 NIGHTSHIFT") + console.c("2", f"  v{__version__}" + ("  ·  dry run" if dry_run else "")))
    channels = []
    if notifier.discord:
        channels.append("Discord")
    if notifier.telegram_token and notifier.telegram_chat:
        channels.append("Telegram")
    console.line(console.c("2", f"  {len(tasks)} task(s) queued · alerts: {', '.join(channels) or 'none'}"))
    console.line()
    for task in tasks:
        tag = console.c("38;5;117" if task.kind == "shell" else "38;5;213", f"{task.kind:<6}")
        console.line(f"  {console.c('2', f'{task.index:>2}.')} {tag} {_shorten(task.title, 64)}")
    console.line()


def print_summary(console: Console, results: list[Result], total: int, elapsed: float, log_path: Path | None) -> None:
    passed = sum(r.ok for r in results)
    color = "1;32" if passed == total else "1;31"
    console.line()
    console.line(f"  {console.c(color, f'{passed}/{total} passed')}  ·  total {_fmt_duration(elapsed)}")
    if log_path:
        console.line(console.c("2", f"  log: {log_path}"))
    console.line()


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

DEMO_TASKS = [
    Task(1, "shell", "pytest -q", "Run the test suite"),
    Task(2, "claude", "Fix the flaky timeout in tests/test_api.py and explain the root cause in FIXES.md",
         "Fix the flaky timeout in tests/test_api.py"),
    Task(3, "shell", "npm run build", "Production build"),
]
DEMO_OUTPUT = {
    1: (2.6, ["collecting ...", "tests/test_api.py ........", "tests/test_parser.py ..............", "64 passed in 2.41s"]),
    2: (4.2, ["Reading tests/test_api.py", "Root cause: fixed 50ms sleep races the mock server",
              "Edited tests/test_api.py (+6 -2)", "Wrote FIXES.md"]),
    3: (3.1, ["> vite build", "transforming (412) modules", "dist/assets/index-3f9c.js  148.2 kB │ gzip: 47.9 kB",
              "✓ built in 2.98s"]),
}


def simulate(tasks: list[Task], console: Console, speed: float) -> list[Result]:
    results = []
    for position, task in enumerate(tasks, start=1):
        duration, output = DEMO_OUTPUT.get(task.index, (1.5, [f"$ {task.payload}", "done"]))
        if task not in DEMO_TASKS:
            duration, output = 1.5, [f"(dry run) would run: {task.payload}"]
        steps = max(1, int(duration * 10))
        for step in range(steps):
            line = output[min(len(output) - 1, step * len(output) // steps)]
            pct = (step + 1) / steps
            console.status(
                f"  {console.c('38;5;141', SPINNER[step % len(SPINNER)])} {console.c('2', f'[{position}/{len(tasks)}]')} "
                f"{_shorten(task.title, 44)} {console.bar(int(pct * 100), 100, 16)} "
                f"{console.c('38;5;117', _fmt_duration(duration * pct))}  {console.c('2', _shorten(line, 50))}"
            )
            if speed > 0:
                time.sleep(0.1 * speed)
        result = Result(task, "passed", 0, duration, output)
        results.append(result)
        print_result(console, result, position, len(tasks))
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nightshift", description="Run a queue of shell commands and Claude prompts unattended.")
    parser.add_argument("--tasks", metavar="FILE", help="Markdown checklist to read (default: TASKS.md if present)")
    parser.add_argument("--task", action="append", default=[], metavar="TASK",
                        help="add a task from the command line; prefix with 'claude:' for a prompt (repeatable)")
    parser.add_argument("--discord", default=os.environ.get("NIGHTSHIFT_DISCORD_WEBHOOK"), metavar="URL",
                        help="Discord webhook URL (env: NIGHTSHIFT_DISCORD_WEBHOOK)")
    parser.add_argument("--telegram-token", default=os.environ.get("NIGHTSHIFT_TELEGRAM_TOKEN"),
                        help="Telegram bot token (env: NIGHTSHIFT_TELEGRAM_TOKEN)")
    parser.add_argument("--telegram-chat", default=os.environ.get("NIGHTSHIFT_TELEGRAM_CHAT_ID"),
                        help="Telegram chat id (env: NIGHTSHIFT_TELEGRAM_CHAT_ID)")
    parser.add_argument("--notify-each", action="store_true", help="send an alert after every task, not only at the end")
    parser.add_argument("--stop-on-fail", action="store_true", help="stop the queue at the first failing task")
    parser.add_argument("--timeout", type=float, default=0, metavar="SECONDS", help="per-task timeout (default: none)")
    parser.add_argument("--mark-done", action="store_true", help="tick finished tasks in the Markdown file ([ ] -> [x])")
    parser.add_argument("--cwd", default=None, help="working directory for tasks (default: current directory)")
    parser.add_argument("--log-dir", default=".nightshift", help="where run logs go (default: .nightshift)")
    parser.add_argument("--claude-bin", default=os.environ.get("NIGHTSHIFT_CLAUDE_BIN"), help="path to the claude CLI")
    parser.add_argument("--claude-flags", default=os.environ.get("NIGHTSHIFT_CLAUDE_FLAGS", ""),
                        help="extra flags for headless Claude runs, e.g. \"--allowedTools 'Edit,Bash(npm test)'\"")
    parser.add_argument("--dry-run", action="store_true", help="simulate the run and preview webhook payloads; executes nothing")
    parser.add_argument("--speed", type=float, default=1.0, help="dry-run animation speed factor (0 = instant)")
    parser.add_argument("--test-webhook", action="store_true", help="send a single test alert and exit")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def load_tasks(args: argparse.Namespace) -> tuple[list[Task], Path | None]:
    tasks: list[Task] = []
    source: Path | None = None
    tasks_path = Path(args.tasks) if args.tasks else (Path("TASKS.md") if Path("TASKS.md").is_file() else None)
    if args.tasks and not tasks_path.is_file():
        raise FileNotFoundError(f"task file not found: {tasks_path}")
    if tasks_path:
        source = tasks_path
        tasks.extend(parse_tasks_markdown(tasks_path.read_text(encoding="utf-8")))
    for raw in args.task:
        kind, payload, title = parse_task_body(raw)
        tasks.append(Task(index=len(tasks) + 1, kind=kind, payload=payload, title=title))
    return tasks, source


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    color = not args.no_color and not os.environ.get("NO_COLOR") and sys.stdout.isatty()
    if color:
        _enable_windows_ansi()
    console = Console(color)
    notifier = Notifier(args.discord, args.telegram_token, args.telegram_chat, console, dry_run=args.dry_run)

    if args.test_webhook:
        if not notifier.enabled:
            print("nightshift: no webhook configured (use --discord or --telegram-token/--telegram-chat)", file=sys.stderr)
            return 2
        now = _dt.datetime.now().astimezone()
        ok_result = Result(Task(1, "shell", "echo ok", "Webhook test"), "passed", 0, 0.1)
        notifier.send(discord_payload([ok_result], 1, now, now, " · test"), telegram_text([ok_result], 1, now, now))
        print("nightshift: test alert sent")
        return 0

    try:
        tasks, source = load_tasks(args)
    except FileNotFoundError as exc:
        print(f"nightshift: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        tasks = tasks or list(DEMO_TASKS)
        print_header(console, tasks, notifier, dry_run=True)
        started = _dt.datetime.now().astimezone()
        results = simulate(tasks, console, args.speed)
        elapsed = sum(r.duration for r in results)
        finished = started + _dt.timedelta(seconds=elapsed)
        print_summary(console, results, len(tasks), elapsed, None)
        console.line(console.c("1", "  Webhook payload preview") + console.c("2", "  (nothing was sent)"))
        if notifier.discord:
            console.line(console.c("2", f"  Discord → {_mask(notifier.discord)}"))
        console.line(console.c("38;5;117", "  ── Discord embed ──"))
        console.line("\n".join("  " + line for line in json.dumps(discord_payload(results, len(tasks), started, finished), indent=2, ensure_ascii=False).splitlines()))
        console.line(console.c("38;5;117", "  ── Telegram message (HTML) ──"))
        console.line("\n".join("  " + line for line in telegram_text(results, len(tasks), started, finished).splitlines()))
        console.line()
        return 0

    if not tasks:
        print("nightshift: no open tasks. Add '- [ ] ...' lines to TASKS.md or pass --task.", file=sys.stderr)
        return 2

    if any(t.kind == "claude" for t in tasks) and not (args.claude_bin or shutil.which("claude")):
        print("nightshift: queue contains Claude prompts but the 'claude' CLI is not on PATH (use --claude-bin)", file=sys.stderr)
        return 2

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"run-{stamp}.log"
    print_header(console, tasks, notifier, dry_run=False)

    started = _dt.datetime.now().astimezone()
    results: list[Result] = []
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"nightshift v{__version__} · started {started.isoformat(timespec='seconds')} · {len(tasks)} tasks\n")
        for position, task in enumerate(tasks, start=1):
            result = run_task(task, args, console, log, position, len(tasks))
            results.append(result)
            print_result(console, result, position, len(tasks))
            if result.ok and args.mark_done and source and task.line_no:
                mark_done(source, task.line_no)
            if args.notify_each and notifier.enabled and result.status != "interrupted":
                now = _dt.datetime.now().astimezone()
                notifier.send(
                    discord_payload([result], len(tasks), started, now, f" · task {position}/{len(tasks)}"),
                    telegram_text([result], len(tasks), started, now),
                )
            if result.status == "interrupted" or (args.stop_on_fail and not result.ok):
                break
    finished = _dt.datetime.now().astimezone()
    summary = {
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "results": [
            {"index": r.task.index, "kind": r.task.kind, "task": r.task.payload, "status": r.status,
             "exit_code": r.exit_code, "duration_s": round(r.duration, 2)}
            for r in results
        ],
    }
    log_path.with_suffix(".json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print_summary(console, results, len(tasks), (finished - started).total_seconds(), log_path)
    if notifier.enabled:
        notifier.send(discord_payload(results, len(tasks), started, finished),
                      telegram_text(results, len(tasks), started, finished))
    return 0 if len(results) == len(tasks) and all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
