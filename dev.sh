#!/usr/bin/env bash
# Lance le serveur en mode DEV (hot-reload sur les .py). Usage: ./dev.sh
set -e
cd "$(dirname "$0")"
RELOAD=1 exec venv/bin/python main.py
