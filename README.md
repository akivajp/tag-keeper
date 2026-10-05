# tag-keeper

Tag, search and tidy your existing file trees from the outside — without moving a file.

[日本語版 README はこちら](README.ja.md)

> **Status: pre-alpha.** What works today is the read-only inventory: scanning, content
> hashing and a hygiene report. Tagging, search and tidy-up plans come next. See
> [Roadmap](#roadmap).

## Why

Most document managers take ownership of your files: they ingest them into their own
storage and lay them out flat. That is the opposite of how a cloud drive is actually
used. Its folders already mean something — a numbered project folder, a source code
repository with its own layout, a year/month archive — and that meaning lives in each
folder, not in one global category tree.

tag-keeper leaves the tree alone. It keeps its own catalog next to the tree, and from
there it helps you keep the tree clean:

- **It never takes ownership.** No copying, renaming or converting. You keep using your
  file manager and shell as before.
- **It follows what you do outside it.** Files you move or rename keep their records.
- **Changes are proposals.** Anything that would move or delete a file is shown first and
  only runs once you approve it (planned).

## What it does today

```console
$ tag-keeper scan ~/CloudSync/OneDrive     # read-only walk; updates the catalog
$ tag-keeper hash ~/CloudSync/OneDrive     # SHA-256 of files that changed since last time
$ tag-keeper report ~/CloudSync/OneDrive   # what is probably not worth keeping in the cloud
```

- **scan** records path, size, mtime and inode of every file and folder. Renames and
  moves are tracked by inode, so a moved file keeps its row (and its hash).
  - **Safety valve:** if a large share of files vanishes at once — an unmounted drive, a
    sync client accident — the deletions are *held*, not recorded. They are recorded only
    if they are still missing on the next scan (or with `--accept-missing`).
  - A root that is missing or empty aborts the scan instead of looking like "everything
    was deleted". Folders that cannot be read are not treated as gone either.
- **hash** computes SHA-256 only for files that are new or changed, with a progress bar
  and ETA. A file that changes while being hashed is skipped.
- **report** lists candidates by category, with sizes and examples:
  - application data that slipped into a synced folder (game data, caches) — reported
    once per folder, at the deepest folder that matches, so it never swallows your own
    documents next to it
  - rebuildable artifacts (`node_modules`, `__pycache__`, …), VM disk images, installers
    and disk images, temporary files and Office lock files
  - sync conflict copies (OneDrive for Linux `-safeBackup-`, pCloud `[conflicted]`,
    rclone bisync `.conflict1`, device-name suffixes, `(1)` copies)
  - empty files and folders
  - exact duplicates, confirmed by hash (never guessed from size)

The catalog is an SQLite file outside the tree (`~/.local/share/tag-keeper/catalog.db` by
default). Nothing is ever written into the tree.

## Install

Requires Python 3.11+ and Linux.

```console
$ git clone https://github.com/akivajp/tag-keeper.git
$ cd tag-keeper
$ uv run tag-keeper --help
```

## Configuration

Optional. `~/.config/tag-keeper/config.toml`:

```toml
[[roots]]
name = "onedrive"
path = "~/CloudSync/OneDrive"
exclude = ["*.partial"]              # never scanned (glob on the path relative to the root)

[hygiene]
allow = ["archive/**"]               # never reported ("I decided to keep these")
device_names = ["zefat", "hermon"]   # suffixes your sync client adds to conflict copies
app_data_names = ["MyLauncher"]      # extra folder names to treat as application data

[safety]
mass_missing_ratio = 0.2             # hold deletions when more than this share vanishes
mass_missing_min = 100
```

With roots configured, `tag-keeper scan` (no arguments) scans all of them.
Every command accepts `--log-file PATH` to keep a timestamped log.

## Roadmap

1. **Inventory and hygiene report** — read-only. *(you are here)*
2. **Tags and search** — tags with aliases and parents, typed fields, tags inherited from
   folders, a catalog of records kept as append-only logs, a small web UI.
3. **Content extraction** — text, OCR (including Japanese), transcripts; AI suggestions
   that wait for your confirmation.
4. **Tidy-up plans** — placement suggestions for unsorted files based on the folders you
   already have, structure review, and executing approved plans with undo. Version history
   from btrfs snapshots via [btrfs-timeline](https://github.com/akivajp/btrfs-timeline).

## License

MIT
