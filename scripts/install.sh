#!/bin/sh
# Symlinks the plugin into the Nicotine+ plugins folder, so the installed app
# always runs the code from this repository.
set -eu

REPO_DIR=$(cd "$(dirname "$0")/.." && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
PLUGINS_DIR="$DATA_HOME/nicotine/plugins"
TARGET="$PLUGINS_DIR/nicotine_mcp"

mkdir -p "$PLUGINS_DIR"

if [ -L "$TARGET" ]; then
    rm "$TARGET"
elif [ -e "$TARGET" ]; then
    echo "$TARGET exists and is not a symlink, remove it first" >&2
    exit 1
fi

ln -s "$REPO_DIR/nicotine_mcp" "$TARGET"
echo "Linked $TARGET -> $REPO_DIR/nicotine_mcp"
echo "Enable 'Nicotine MCP' in Nicotine+ under Preferences > Plugins."
