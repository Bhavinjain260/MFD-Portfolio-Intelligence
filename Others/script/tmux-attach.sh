cat > /usr/local/bin/tmux-attach.sh <<'EOF'
#!/usr/bin/env bash
# /usr/local/bin/tmux-attach.sh
# Auto-attach to the "Streamlit" tmux session; create it if missing.
# Safe to run in every code-server terminal.

SESSION="Streamlit"

# Fall back to a plain shell if tmux isn't installed
command -v tmux &>/dev/null || exec bash

# -A = attach if it exists, otherwise create it.
# exec replaces this shell so the terminal *is* tmux, not a child of it.
exec tmux new-session -A -s "$SESSION"
EOF