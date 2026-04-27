<p align="center">
  <img src="marunja_logo.png" width="120" alt="Marunja logo" />
</p>

<h1 align="center">marunja_synch</h1>

<p align="center">
  Nautilus extension that shows OneDrive &amp; SharePoint sync status as a column in list view.<br/>
  Part of the <strong>Marunja Suite</strong> — built for <a href="https://github.com/abraunegg/onedrive">abraunegg/onedrive</a> on Linux.
</p>

---

## What it does

Adds a **Sync** column in Nautilus list view showing the sync state of each file and folder tracked by the `onedrive` client:

| Icon | Meaning |
|------|---------|
| `✓ Synced` | File is in sync with OneDrive / SharePoint |
| `⟳ Pending` | File is queued for sync |
| `✗ Error` | Sync error reported by the client |
| `— Ignored` | Inside a sync folder but not tracked (excluded by `skip_dir`, etc.) |

Supports multiple profiles simultaneously (OneDrive Business + any number of SharePoint libraries).

---

## Requirements

- Ubuntu 22.04+ (or any Debian-based distro)
- [abraunegg/onedrive](https://github.com/abraunegg/onedrive) client configured and running
- Nautilus file manager
- `python3-nautilus`

```bash
sudo apt install python3-nautilus
```

---

## Installation

```bash
git clone https://github.com/msavox/marunja_synch.git
cd marunja_synch
bash install.sh
```

Then in Nautilus (list view): right-click the column header → **Sync**.

### Manual install

```bash
cp marunja_sync.py ~/.local/share/nautilus-python/extensions/
nautilus -q && nautilus &
```

---

## Configuration

Edit the `PROFILES` list at the top of `marunja_sync.py` to match your setup:

```python
PROFILES = [
    {
        "name": "OneDrive",
        "db": os.path.expanduser("~/.config/onedrive/items.sqlite3"),
        "sync_dir": os.path.expanduser("~/onedrive"),
    },
    {
        "name": "SharePoint/HERA",
        "db": os.path.expanduser("~/.config/onedrive-sp-separationring/items.sqlite3"),
        "sync_dir": os.path.expanduser("~/sharepoint/HERA"),
    },
    # Add more SharePoint profiles here...
]
```

Each SharePoint library needs its own config directory created with:

```bash
onedrive --confdir ~/.config/onedrive-sp-<name> --get-sharepoint-drive-id "<Site Name>"
```

Then add the profile to `PROFILES` and re-run `install.sh`.

The extension refreshes the sync status every **30 seconds** (configurable via `REFRESH_INTERVAL`).

---

## How it works

The `onedrive` client stores its state in a SQLite database (`items.sqlite3`) with a `syncStatus` field per item. This extension:

1. Copies the DB at startup (and every 30s) to avoid lock conflicts with the running client
2. Reconstructs full file paths using a recursive SQL CTE on the item tree
3. Builds an in-memory path → status map
4. Hooks into Nautilus via `python3-nautilus` to populate the **Sync** column

---

## Uninstall

```bash
rm ~/.local/share/nautilus-python/extensions/marunja_sync.py
nautilus -q && nautilus &
```
