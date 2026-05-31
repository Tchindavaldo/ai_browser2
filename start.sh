#!/usr/bin/env bash
# Lance le serveur en mode normal (sans reload). Usage: ./start.sh
set -e
cd "$(dirname "$0")"
exec venv/bin/python main.py
