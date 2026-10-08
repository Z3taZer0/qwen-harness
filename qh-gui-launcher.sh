#!/usr/bin/env bash
# Launches the desktop app from this checkout's .venv, wherever the repo lives.
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
exec "$DIR/.venv/bin/python" -m qh.gui "$@"
