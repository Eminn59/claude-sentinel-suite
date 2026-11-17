import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hooks"))

import handoff  # noqa: E402
import nightshift  # noqa: E402
import register_hooks  # noqa: E402


def run_hook(mode, event):
    return subprocess.run(
        [sys.executable, str(ROOT / "hooks" / "handoff.py"), mode],
        input=json.dumps(event), capture_output=True, text=True, encoding="utf-8",
    )


class HandoffTests(unittest.TestCase):
    def test_precompact_creates_and_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            transcript = cwd / "t.jsonl"
            transcript.write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": "Add rate limiting to the API"}}) + "\n"
                + json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}) + "\n",
                encoding="utf-8",
            )
            proc = run_hook("precompact", {"cwd": str(cwd), "transcript_path": str(transcript), "trigger": "auto"})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            text = (cwd / ".claude" / "HANDOFF.md").read_text(encoding="utf-8")
            for section in handoff.SECTIONS:
                self.assertIn(f"## {section}", text)
            self.assertIn("Add rate limiting to the API", text)

            # A scribe-written file keeps its content; only Modified Files + snapshot line change.
            custom = text.replace("_(unknown)_", "Middleware order is wrong")
            (cwd / ".claude" / "HANDOFF.md").write_text(custom, encoding="utf-8")
            run_hook("precompact", {"cwd": str(cwd), "trigger": "manual"})
            updated = (cwd / ".claude" / "HANDOFF.md").read_text(encoding="utf-8")
            self.assertIn("Middleware order is wrong", updated)
            self.assertEqual(updated.count("_Auto-snapshot before"), 1)
            self.assertIn("manual compaction", updated)

    def test_session_start_injects_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self.assertEqual(run_hook("session-start", {"cwd": str(cwd)}).stdout, "")
            (cwd / ".claude").mkdir()
            (cwd / ".claude" / "HANDOFF.md").write_text("# Session Handoff\n\n## Next Exact Command\n\npytest -x\n", encoding="utf-8")
            out = run_hook("session-start", {"cwd": str(cwd), "source": "resume"}).stdout
            self.assertIn("Found .claude/HANDOFF.md", out)
            self.assertIn("pytest -x", out)


class RegisterHooksTests(unittest.TestCase):
    def test_merge_is_idempotent_and_preserves_user_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            user_hook = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}
            settings.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [user_hook]}}), encoding="utf-8")
            args = ["--settings", str(settings), "--root", "/home/me/.claude/skills/sentinel-suite", "--python", "/usr/bin/python3"]
            with redirect_stdout(io.StringIO()):
                register_hooks.main(args)
                register_hooks.main(args)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(data["model"], "opus")
            self.assertEqual(len(data["hooks"]["PreToolUse"]), 2)
            self.assertEqual(len(data["hooks"]["PreCompact"]), 1)
            self.assertIn("session-start", data["hooks"]["SessionStart"][0]["hooks"][0]["command"])
            with redirect_stdout(io.StringIO()):
                register_hooks.main(["--settings", str(settings), "--uninstall"])
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(data["hooks"], {"PreToolUse": [user_hook]})
            self.assertTrue(list(Path(tmp).glob("settings.json.bak-*")))


class NightshiftTests(unittest.TestCase):
    def test_parse_markdown(self):
        text = """# Queue
- [ ] `pytest -q`
- [ ] Run the linter: `ruff check .`
- [ ] claude: Fix the failing test
- [ ] Update the changelog
- [x] Done already
```
- [ ] not a task (inside a fence)
```
"""
        tasks = nightshift.parse_tasks_markdown(text)
        self.assertEqual([(t.kind, t.payload) for t in tasks], [
            ("shell", "pytest -q"),
            ("shell", "ruff check ."),
            ("claude", "Fix the failing test"),
            ("claude", "Update the changelog"),
        ])
        self.assertEqual(tasks[1].title, "Run the linter")
        self.assertEqual(tasks[0].line_no, 2)

    def test_dry_run(self):
        with redirect_stdout(io.StringIO()) as captured:
            code = nightshift.main(["--dry-run", "--speed", "0", "--no-color", "--tasks", str(ROOT / "TASKS.example.md")])
        self.assertEqual(code, 0)
        out = captured.getvalue()
        self.assertIn("Webhook payload preview", out)
        self.assertIn('"embeds"', out)

    def test_real_run_marks_done_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp) / "TASKS.md"
            py = Path(sys.executable).as_posix()
            tasks.write_text(
                f'- [ ] ok: `"{py}" -c "print(42)"`\n- [ ] bad: `"{py}" -c "import sys; sys.exit(3)"`\n',
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                code = nightshift.main(["--tasks", str(tasks), "--mark-done", "--no-color",
                                        "--log-dir", str(Path(tmp) / "logs"), "--cwd", tmp])
            self.assertEqual(code, 1)
            lines = tasks.read_text(encoding="utf-8").splitlines()
            self.assertTrue(lines[0].startswith("- [x]"))
            self.assertTrue(lines[1].startswith("- [ ]"))
            summary = json.loads(next((Path(tmp) / "logs").glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual([r["status"] for r in summary["results"]], ["passed", "failed"])
            self.assertEqual(summary["results"][1]["exit_code"], 3)

    def test_discord_payload_limits(self):
        import datetime as dt

        now = dt.datetime.now().astimezone()
        task = nightshift.Task(1, "shell", "x", "t" * 400)
        result = nightshift.Result(task, "failed", 1, 2.0, ["line " * 100] * 30)
        payload = nightshift.discord_payload([result], 1, now, now)
        field = payload["embeds"][0]["fields"][0]
        self.assertLessEqual(len(field["name"]), 256)
        self.assertLessEqual(len(field["value"]), 1024)
        self.assertEqual(payload["embeds"][0]["color"], 0xE74C3C)


if __name__ == "__main__":
    unittest.main()
