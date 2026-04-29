#!/bin/bash
# Symlink the skills + hooks shipped in this repo into ~/.claude/.
#
# Symlinks (rather than copies) mean a future `git pull` in this repo
# updates the installed skill in place — no rebuild step needed.
#
# Usage:
#   ./install-skills.sh                # install skills + hooks
#   ./install-skills.sh --auto-update  # also install a daily `git pull` launchd job
#   ./install-skills.sh --uninstall    # remove symlinks (and the launchd job if present)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd -P)"
SKILLS_SRC="$REPO_DIR/skills"
HOOKS_SRC="$REPO_DIR/hooks"
SKILLS_DST="$HOME/.claude/skills"
HOOKS_DST="$HOME/.claude/hooks"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
UPDATER_LABEL="com.iterm2-claude-toolbelt.skill-update"
UPDATER_PLIST="$LAUNCHD_DIR/$UPDATER_LABEL.plist"

mode="install"
[[ "${1:-}" == "--uninstall" ]] && mode="uninstall"
[[ "${1:-}" == "--auto-update" ]] && mode="install-with-updater"

# ── helpers ───────────────────────────────────────────────────────────────────

link_dir() {
  # link_dir <src_dir> <dst_dir>  — symlink each immediate child of src into dst
  local src="$1" dst="$2"
  [[ -d "$src" ]] || return 0
  mkdir -p "$dst"
  for child in "$src"/*; do
    [[ -e "$child" ]] || continue
    local name
    name="$(basename "$child")"
    local target="$dst/$name"
    if [[ -L "$target" || -e "$target" ]]; then
      if [[ -L "$target" && "$(readlink "$target")" == "$child" ]]; then
        echo "= already linked: $target"
        continue
      fi
      echo "! refusing to overwrite existing $target (move it aside first)"
      continue
    fi
    ln -s "$child" "$target"
    echo "+ linked $target -> $child"
  done
}

unlink_dir() {
  # unlink_dir <src_dir> <dst_dir>  — remove symlinks under dst that point into src
  local src="$1" dst="$2"
  [[ -d "$dst" ]] || return 0
  for entry in "$dst"/*; do
    [[ -L "$entry" ]] || continue
    local target
    target="$(readlink "$entry")"
    if [[ "$target" == "$src/"* ]]; then
      rm "$entry"
      echo "- removed $entry"
    fi
  done
}

install_updater() {
  # Daily launchd job: cd into the repo and `git pull --ff-only`.
  mkdir -p "$LAUNCHD_DIR"
  cat > "$UPDATER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$UPDATER_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>cd "$REPO_DIR" &amp;&amp; /usr/bin/git pull --ff-only --quiet</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>4</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>/tmp/$UPDATER_LABEL.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/$UPDATER_LABEL.log</string>
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$UID" "$UPDATER_PLIST" 2>/dev/null \
    || launchctl bootout "gui/$UID/$UPDATER_LABEL" 2>/dev/null && launchctl bootstrap "gui/$UID" "$UPDATER_PLIST"
  echo "+ installed daily updater: $UPDATER_PLIST"
  echo "  next pull: 04:30 local time. Logs: /tmp/$UPDATER_LABEL.log"
}

uninstall_updater() {
  if [[ -f "$UPDATER_PLIST" ]]; then
    launchctl bootout "gui/$UID/$UPDATER_LABEL" 2>/dev/null || true
    rm -f "$UPDATER_PLIST"
    echo "- removed daily updater"
  fi
}

# ── main ──────────────────────────────────────────────────────────────────────

case "$mode" in
  uninstall)
    unlink_dir "$SKILLS_SRC" "$SKILLS_DST"
    unlink_dir "$HOOKS_SRC"  "$HOOKS_DST"
    uninstall_updater
    ;;
  install|install-with-updater)
    link_dir "$SKILLS_SRC" "$SKILLS_DST"
    link_dir "$HOOKS_SRC"  "$HOOKS_DST"
    [[ "$mode" == "install-with-updater" ]] && install_updater
    auto_note=""
    [[ "$mode" == "install-with-updater" ]] && auto_note="A daily launchd job will run \`git pull\` at 04:30 local time."
    cat <<MSG

Done.

The skills now point at $REPO_DIR. To update later, run:
  cd "$REPO_DIR" && git pull
$auto_note

To wire the optional /fork-split prompt-interceptor (skips a Claude turn
when you run /fork-split — faster, no Stop-hook noise), add this to your
~/.claude/settings.json under "hooks":

  "UserPromptSubmit": [
    { "matcher": "", "hooks": [
      { "type": "command", "command": "$HOME/.claude/hooks/fork-split-intercept.sh" }
    ]}
  ]
MSG
    ;;
esac
