#!/bin/zsh -l
# Double-click to use the finance tracker.
#   - starts the server if it isn't running, then opens your browser
#   - CLOSE THIS WINDOW (or Ctrl+C) to stop the server when you're done
cd "$(dirname "$0")"

if curl -s -o /dev/null --max-time 1 http://localhost:5001; then
  open "http://localhost:5001"
  echo "Already running — opened the browser. You can close this window."
  exit 0
fi

( sleep 1.5; open "http://localhost:5001" ) &
echo "Starting Finance Tracker... close this window to stop it."
exec python app.py
