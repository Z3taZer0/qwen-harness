#!/usr/bin/env bash
# Adds "Qwen Harness" to the application menu (pointing at this checkout's launcher).
set -euo pipefail
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
cat > "$APPS/org.antigravity.qh.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Qwen Harness
Comment=Agentic assistant for the local Qwen model
Exec=$DIR/qh-gui-launcher.sh
Icon=user-available-symbolic
Terminal=false
Categories=Utility;Development;
StartupWMClass=org.antigravity.qh
StartupNotify=true
DESKTOP
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
echo "installed $APPS/org.antigravity.qh.desktop"
