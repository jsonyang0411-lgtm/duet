#!/bin/zsh
# Claude relay worker in a Terminal window that does only the creators' drafts (PRM_COPILOT); needs prm_workspace in config.json.
# Two of these (start-admin-worker.command + start-prm-worker.command) run side by side: a long
# cc task never holds up a creator's draft (each takes its own lock in ~/.claude-relay).
# Add both to System Settings > General > Login Items instead of start-worker.command.
# Ctrl+C or closing the window stops it; a crash restarts it after 30 s.

cd "${0:A:h}/../.." || { echo "repository folder not found"; read; exit 1; }
printf '\e]0;Claude relay worker (creator drafts)\a'
while true; do
  /usr/bin/python3 tools/claude_relay/worker.py --stay-awake --kinds prm
  code=$?
  [[ $code -eq 0 || $code -eq 130 ]] && break
  echo "worker exited with code $code; restarting in 30 s (Ctrl+C to stop)"
  sleep 30 || break
done
echo "worker stopped. Close this window, or run this file again to restart."
