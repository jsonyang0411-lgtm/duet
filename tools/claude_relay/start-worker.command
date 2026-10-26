#!/bin/zsh
# Claude relay worker in a Terminal window (see README.md, "长期运行").
# Added to System Settings > General > Login Items, it starts at every login.
# Terminal must be allowed to use the folder this repository is in (macOS asks once).
# Ctrl+C or closing the window stops it; a crash restarts it after 30 s.

cd "${0:A:h}/../.." || { echo "repository folder not found"; read; exit 1; }
printf '\e]0;Claude relay worker\a'
while true; do
  /usr/bin/python3 tools/claude_relay/worker.py --stay-awake
  code=$?
  [[ $code -eq 0 || $code -eq 130 ]] && break
  echo "worker exited with code $code; restarting in 30 s (Ctrl+C to stop)"
  sleep 30 || break
done
echo "worker stopped. Close this window, or run this file again to restart."
