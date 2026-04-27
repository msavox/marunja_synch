#!/bin/bash
set -e

EXTENSION_DIR="$HOME/.local/share/nautilus-python/extensions"
EXTENSION="marunja_sync.py"

# Check dependency
if ! python3 -c "from gi.repository import Nautilus" 2>/dev/null; then
    echo "Installing python3-nautilus..."
    sudo apt install -y python3-nautilus
fi

mkdir -p "$EXTENSION_DIR"
cp "$EXTENSION" "$EXTENSION_DIR/$EXTENSION"
echo "Installed to $EXTENSION_DIR/$EXTENSION"

# Restart Nautilus
nautilus -q 2>/dev/null || true
sleep 1
nautilus &
echo "Nautilus restarted. Enable the 'Sync' column: View > Visible Columns > Sync"
