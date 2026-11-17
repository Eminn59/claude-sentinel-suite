# Nightshift queue

Copy this file to `TASKS.md` in your project and run:

```bash
python ~/.claude/skills/sentinel-suite/scripts/nightshift.py --tasks TASKS.md --dry-run      # preview
python ~/.claude/skills/sentinel-suite/scripts/nightshift.py --tasks TASKS.md --mark-done    # run it
```

Rules: one task per `- [ ]` line, run top to bottom. A backticked command is a shell task,
`claude:` (or plain text) is a headless Claude prompt, `- [x]` lines are skipped.

## Tonight

- [ ] Run the test suite: `pytest -q`
- [ ] claude: Fix every failing test in tests/ without changing test expectations, then list the root causes in FIXES.md
- [ ] Re-run the tests: `pytest -q`
- [ ] `npm run build`
- [x] Bump dependencies (done yesterday)
