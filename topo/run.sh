#!/bin/sh
# Foreground launcher for confetti-topo (manual runs and debugging).
# In production the OpenRC service confetti-topo runs serve.py directly.

cd "$(dirname "$0")" || exit 1

# Pick up topo.env if present so a manual run matches the service's config.
if [ -f ./topo.env ]; then
    while IFS= read -r line; do
        case "$line" in
            \#*|"") continue ;;
            *=*) export "$line" ;;
        esac
    done < ./topo.env
fi

exec python3 ./serve.py
