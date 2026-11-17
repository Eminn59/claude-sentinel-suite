#!/usr/bin/env bash
# claude-sentinel-suite installer for macOS / Linux
#
#   ./install.sh               link the skill + agents into ~/.claude
#   ./install.sh --with-hooks  also register the secret-guard and handoff hooks in settings.json
#   ./install.sh --uninstall   remove everything this script installed
#
# Uses symlinks, so `git pull` in this repository updates the installed copy.
# Honors CLAUDE_CONFIG_DIR if you keep Claude Code's config somewhere else.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL_NAME="sentinel-suite"
SKILL_DIR="$CLAUDE_DIR/skills/$SKILL_NAME"
AGENTS_DIR="$CLAUDE_DIR/agents"
WITH_HOOKS=0
UNINSTALL=0

if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BOLD=""; GREEN=""; YELLOW=""; RED=""; DIM=""; RESET=""
fi

say()  { printf '%s\n' "  $*"; }
ok()   { printf '%s\n' "  ${GREEN}✔${RESET} $*"; }
warn() { printf '%s\n' "  ${YELLOW}!${RESET} $*"; }
die()  { printf '%s\n' "  ${RED}✖ $*${RESET}" >&2; exit 1; }

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --with-hooks) WITH_HOOKS=1 ;;
    --uninstall)  UNINSTALL=1 ;;
    -h|--help)    usage ;;
    *) die "unknown option: $arg (see --help)" ;;
  esac
done

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      "$candidate" -c 'import sys; print(sys.executable)'
      return 0
    fi
  done
  return 1
}

backup_if_real() {
  # Move a real file/dir out of the way (never touches symlinks).
  local path="$1"
  if [ -e "$path" ] && [ ! -L "$path" ]; then
    local backup
    backup="$path.backup.$(date +%Y%m%d%H%M%S)"
    mv "$path" "$backup"
    warn "existing $(basename "$path") moved to ${DIM}$backup${RESET}"
  fi
}

printf '\n%s\n\n' "  ${BOLD}claude-sentinel-suite${RESET} ${DIM}→ $CLAUDE_DIR${RESET}"

PYTHON="$(find_python)" || die "Python 3.10+ is required (install it from python.org or your package manager)."

if [ "$UNINSTALL" -eq 1 ]; then
  if [ -L "$SKILL_DIR" ]; then rm "$SKILL_DIR"; ok "removed skill link $SKILL_DIR"; fi
  for agent in "$REPO_DIR"/agents/*.md; do
    target="$AGENTS_DIR/$(basename "$agent")"
    if [ -L "$target" ]; then rm "$target"; ok "removed agent $(basename "$agent" .md)"; fi
  done
  if [ -f "$CLAUDE_DIR/settings.json" ]; then
    "$PYTHON" "$REPO_DIR/hooks/register_hooks.py" --uninstall --settings "$CLAUDE_DIR/settings.json"
  fi
  printf '\n%s\n\n' "  ${GREEN}Uninstalled.${RESET} Restart Claude Code to apply."
  exit 0
fi

mkdir -p "$CLAUDE_DIR/skills" "$AGENTS_DIR"

backup_if_real "$SKILL_DIR"
ln -sfn "$REPO_DIR" "$SKILL_DIR"
ok "skill    ${BOLD}$SKILL_NAME${RESET} ${DIM}→ $SKILL_DIR${RESET}"

count=0
for agent in "$REPO_DIR"/agents/*.md; do
  target="$AGENTS_DIR/$(basename "$agent")"
  backup_if_real "$target"
  ln -sfn "$agent" "$target"
  count=$((count + 1))
done
ok "agents   $count Haiku micro-agents ${DIM}→ $AGENTS_DIR${RESET}"

chmod +x "$REPO_DIR"/scripts/*.py "$REPO_DIR"/hooks/*.py 2>/dev/null || true

if [ "$WITH_HOOKS" -eq 1 ]; then
  "$PYTHON" "$REPO_DIR/hooks/register_hooks.py" --settings "$CLAUDE_DIR/settings.json" --root "$SKILL_DIR" --python "$PYTHON"
  ok "hooks    secret-guard (PreToolUse) · handoff (PreCompact, SessionStart)"
else
  say "${DIM}hooks    skipped (re-run with --with-hooks to enable secret-guard + auto-handoff)${RESET}"
fi

cat <<EOF

  ${GREEN}${BOLD}Done.${RESET} Restart Claude Code, then try:

    ${BOLD}$PYTHON "$SKILL_DIR/scripts/context_diet.py" --demo${RESET}
    ${BOLD}$PYTHON "$SKILL_DIR/scripts/spiderweb.py" --target . --open${RESET}
    ${BOLD}$PYTHON "$SKILL_DIR/scripts/nightshift.py" --dry-run${RESET}

EOF
