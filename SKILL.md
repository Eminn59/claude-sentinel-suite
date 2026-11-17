---
name: sentinel-suite
description: Token-efficient context management for Claude Code. Use when orienting in an unfamiliar or large codebase (get an AST skeleton instead of reading every file), when the user wants a dependency map or asks "what depends on X", when queueing unattended or overnight work with phone alerts, when resuming work from .claude/HANDOFF.md, when a session nears its context limit or is about to end, and when triaging huge logs, auditing a large diff for deleted code, or checking for leaked secrets before a commit.
---

# Sentinel Suite: routing guide

All paths below are relative to this skill's directory: `~/.claude/skills/sentinel-suite/` (Windows: `%USERPROFILE%\.claude\skills\sentinel-suite\`). Use `python3` on macOS/Linux and `python` on Windows. Every script is standard-library-only Python 3.10+.

## 0. Session start: recover first

If `.claude/HANDOFF.md` exists in the project root (the SessionStart hook may already have injected it into your context):

1. Read it **before anything else**.
2. Compare **Modified Files** with `git status --short`. If they disagree, trust git and note it.
3. Continue from **Next Exact Command**. Do not redo **Completed Steps**.
4. Tell the user in one line what you are resuming, e.g. "Resuming: rate limiting. Next: fix middleware order."

## 1. Orienting in a codebase → `scripts/context_diet.py`

Use **before** reading many files, when the user asks "how is this project structured", or when the repo has more than ~15 source files.

```bash
python3 ~/.claude/skills/sentinel-suite/scripts/context_diet.py --target . --output .claude/skeleton.md
```

Then read `.claude/skeleton.md` (signatures, classes, types, imports, docstring summaries; bodies are `...`).

- Scope it: `--target src/payments` for one area, `--exclude 'tests/*'` to skip tests, `--docstrings none` for maximum compression.
- `--json` gives per-file token counts; use it to decide which files are worth reading in full.
- **Never edit a file based on its skeleton.** Read the real file (or the relevant range) before any Edit.
- Re-generate after large refactors; the skeleton does not update itself.

## 2. Dependency questions → `scripts/spiderweb.py`

Use when the user asks what imports what, wants an architecture overview, asks about circular imports, or before moving/renaming a widely used module.

```bash
python3 ~/.claude/skills/sentinel-suite/scripts/spiderweb.py --target . --json .claude/deps.json
```

- Answer from `.claude/deps.json` (`links[]`, `meta.cycles`) instead of grepping imports by hand. Keep it out of your context unless needed; query it with a short Python one-liner.
- Offer the interactive map (`sentinel_spiderweb.html`, add `--open` to launch it) when the user wants to *see* the structure.
- `meta.cycles` lists import cycles; mention them when relevant to the task.

## 3. Unattended / overnight work → `scripts/nightshift.py`

Use when the user wants a queue of tasks to run while they are away, or wants phone alerts when something finishes.

1. Write or update `TASKS.md` in the project root:
   ```markdown
   - [ ] Run the tests: `pytest -q`
   - [ ] claude: Fix any failing test in tests/ and summarize the root cause in FIXES.md
   - [ ] `npm run build`
   ```
2. Preview first: `python3 …/scripts/nightshift.py --tasks TASKS.md --dry-run`
3. Show the user the exact command to start the real run (do **not** start long unattended runs yourself unless asked):
   `python3 …/scripts/nightshift.py --tasks TASKS.md --mark-done --stop-on-fail --discord "$NIGHTSHIFT_DISCORD_WEBHOOK"`
- Headless Claude tasks need permissions: suggest `--claude-flags "--allowedTools 'Edit,Bash(npm test)'"` scoped to what the queue needs. Never suggest `--dangerously-skip-permissions`.
- Webhook URLs and bot tokens are secrets: environment variables only, never in TASKS.md or committed files.

## 4. Micro-agents (Haiku): delegate, don't do it inline

| Agent | Delegate when | You get back |
|---|---|---|
| `session-scribe` | context is long, a milestone is done, the user is wrapping up | updated `.claude/HANDOFF.md` (Objective / Completed / Modified / Blockers / Next command) |
| `sanity-checker` | after any multi-file edit or file rewrite, before every commit | `SANITY: PASS/WARN/FAIL` + `path:line` findings |
| `secret-guard` | before commit/push, before sharing logs, before destructive commands | `SECRETS: CLEAN/LEAK/RISK` + remediation |
| `memory-compactor` | a tool output is > ~200 lines, or exploration results pile up | ≤ 40-line state snapshot |
| `log-triage` | an error log or stack trace is > ~100 lines, CI fails | root cause + exact `file:line` fix + verify command |

Rules:
- Hand them a **brief**, not the whole conversation (see each agent's Input section).
- Their output replaces the raw material: keep the snapshot, drop the 2,000-line log from your reasoning.
- Run `sanity-checker` **and** `secret-guard` before you propose a commit. Fix FAIL/LEAK items first.
- Before the session ends or the context gets tight, call `session-scribe` with: objective, done, blockers, next.

## 5. Hooks (if installed with `--with-hooks`)

- **secret_guard.py** (PreToolUse): denies catastrophic shell commands and hard-coded credentials; asks before risky ones (force push, `reset --hard`, reading `.env`). If it blocks you, do not try to work around it: explain the block to the user and let them decide.
- **handoff.py precompact** (PreCompact): refreshes **Modified Files** in HANDOFF.md before compaction.
- **handoff.py session-start** (SessionStart): injects HANDOFF.md into a new session's context.

## Token discipline (always)

- Skeleton first, full file only for the files you will change.
- Use `Read` with line ranges for big files; `Grep -n` before opening.
- Summarize via `memory-compactor` instead of re-reading long outputs.
- Keep HANDOFF.md current so a fresh session costs one file read, not a re-exploration.
