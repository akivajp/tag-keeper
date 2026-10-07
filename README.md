# tag-keeper

Tag, search and tidy your existing file trees from the outside — without moving a file.

[日本語版 README はこちら](README.ja.md)

> **Status: pre-alpha.** What works today is the inventory (scanning, content hashing, a
> hygiene report) and cleaning up with plans you approve, with undo — from the command line
> or a web UI. Tagging and search come next. See [Roadmap](#roadmap).

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
- **Changes are proposals.** Anything that would move a file is written down as a plan,
  shown first, and only runs once you approve it. Nothing is ever deleted: files go to a
  quarantine folder, and every plan can be undone.

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
    rclone bisync `.conflict1`, device-name suffixes, `(1)` copies) — only when the
    original sits next to it, since a lone "copy" may be the only version. Copies whose
    content differs from the original are marked as needing review
  - empty files and folders
  - exact duplicates, confirmed by hash (never guessed from size)

The catalog is an SQLite file outside the tree (`~/.local/share/tag-keeper/catalog.db` by
default). None of these commands writes into the tree.

### Cleaning up with a plan

```console
$ tag-keeper plan onedrive                  # write the report's candidates to a plan file
$ $EDITOR ~/.local/share/tag-keeper/plans/<plan-id>.toml   # delete the lines you want to keep
$ tag-keeper apply <plan file>              # preview only
$ tag-keeper apply <plan file> --yes        # move them to the quarantine folder
$ tag-keeper undo <plan-id> --yes           # put everything back
```

- **plan** writes one line per item, grouped by category. `--category` limits it to some
  categories (`app-data`, `vm-image`, `conflict`, …). Candidates that need review stay out
  unless you pass `--include-review`.
- **apply** previews by default. With `--yes` it moves each item, by rename, to
  `<quarantine_dir>/<plan-id>/` under its original relative path. Right before each move
  it checks that the item has not changed since the plan was written (inode, size,
  modification time, and for folders the file count and total size); changed items are
  skipped and reported.
- With `snapper_config` set, a snapper *pre* snapshot is taken before the run and a *post*
  snapshot after it. If the pre snapshot fails, nothing is moved.
- Every move is appended to a journal (`~/.local/share/tag-keeper/journal/<plan-id>.jsonl`).
  **undo** reads the journal and moves items back; it never overwrites something that has
  appeared at the original path since.
- The catalog is updated as items move, so the next scan does not mistake a large
  quarantine for a mass disappearance.
- Items leave the cloud when the sync client notices they are gone, but they stay on this
  disk until you empty the quarantine folder yourself.

### Big deletions without stopping your sync client

The [OneDrive client for Linux](https://github.com/abraunegg/onedrive) treats the local
removal of a folder with many children (`classify_as_big_delete`, 1000 by default) as an
accident: it refuses to sync and exits. Run under systemd, it keeps exiting on every
restart, so syncing silently stops. When *you* approve a big deletion, tag-keeper
coordinates with the client instead (set `sync_client = "onedrive"` on the root):

1. stop the resident sync (`systemctl --user stop onedrive.service`),
2. quarantine the items,
3. run one `onedrive --sync` with the threshold raised just above the largest folder in this
   plan — not `--force`, which would also let unrelated accidents through,
4. start the resident sync again.

If step 3 fails, the resident sync is left stopped (restarting it would only exit again),
the failure is journaled, and the web UI and `tag-keeper sync <plan-id> --retry` offer to
try again. Small plans are left to the running client as usual.

## Web UI

```console
$ tag-keeper serve                  # http://127.0.0.1:8090/
```

Everything above can be done in the browser: refresh a root (scan and hash), read the
report, create a plan, untick what you want to keep, check it, run it and undo it. Long
operations run in the background, and a panel at the bottom shows each step, a progress
bar, speed, time remaining and the latest log lines. It works on a phone too.

- Only loopback is served by default. To listen elsewhere (for example on a Tailscale
  address), give credentials for HTTP Basic auth with `--auth-file` (one line,
  `user:password`) or `TAG_KEEPER_AUTH`; without them a non-loopback address is refused
  unless you pass `--allow-no-auth`. Basic auth is not encrypted: use it over a VPN such as
  Tailscale, or behind TLS.
- Writes require a same-origin request, and without auth the `Host` header must name the
  bound address (against DNS rebinding).
- One job runs at a time. Each job also writes its own log under
  `~/.local/share/tag-keeper/logs/jobs/`.

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

[plan]
quarantine_dir = "~/.local/share/tag-keeper/quarantine"  # must be on the root's filesystem
snapper_config = "home"              # snapper config for pre/post snapshots ("" = none)
```

Per root, for coordinating with a sync client:

```toml
[[roots]]
name = "onedrive"
path = "~/CloudSync/OneDrive"
sync_client = "onedrive"             # "" (default) = no coordination
sync_service = "onedrive.service"    # the systemd --user unit running `onedrive --monitor`
sync_guard = "auto"                  # "auto": only for big deletions / "always": every run
```

With roots configured, `tag-keeper scan` (no arguments) scans all of them.
Every command accepts `--log-file PATH` to keep a timestamped log.

## Roadmap

1. **Inventory and hygiene report**, and tidy-up plans that quarantine what you approve,
   with snapper snapshots and undo. *(you are here)*
2. **Tags and search** — tags with aliases and parents, typed fields, tags inherited from
   folders, a catalog of records kept as append-only logs, a small web UI.
3. **Content extraction** — text, OCR (including Japanese), transcripts; AI suggestions
   that wait for your confirmation.
4. **Placement and structure** — placement suggestions for unsorted files based on the
   folders you already have, structure review, and renames and moves inside the tree.
   Version history from btrfs snapshots via
   [btrfs-timeline](https://github.com/akivajp/btrfs-timeline).

## License

MIT
