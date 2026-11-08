# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.13.0] - 2026-11-08

### Added

- **Paths match across NFC and NFD.** Management data refers to items by path in NFC, so a
  Japanese name written with composed or decomposed characters points at the same item.
- **Relations between two items** (previous version, derived from, attached to, or your own
  kinds) from the file browser's details. They follow moves, are detached when quarantined
  and come back with undo.
- **Moves needing confirmation.** Files whose content matched more than one recently gone
  file are listed under *Confirm moves* on the Roots page; picking the place it moved from
  carries over tags, attributes, relations and version history, and “new file” hides it from
  the list.
- **Export and import** all management data (tags, attributes, relations, dictionary, saved
  searches) as JSONL with `tag-keeper export` / `import` and `GET /api/export`.
- **CLI for the rest**: `tag-keeper tags`, `tag`, `alias`, `parent`, `attr`, `relate`.

## [0.12.0] - 2026-11-08

### Added

- **Tag aliases and parents.** An alias is folded into the real name when tagging and
  searching (making a name in use an alias merges it); a parent tag finds every item with a
  child tag. Manage both from the ⚙ button in the tag list; cycles are refused.
- **Typed attributes**: choice (one value at a time), flag, date, number and text. Define them
  on the Search page and set them on files and folders from the file browser and from search
  results. Folder values are inherited; attributes follow moves and come back with undo.
- **More search filters**: folder, modification date, size and attributes (`=`, `>=`, `<=`,
  or "has a value"); identical files grouped into one row; files that are gone can be
  included. "Open folder" in search results now opens the folder with the file selected.
- **Saved searches**: save the current conditions under a name; they are listed with their
  current counts at the top of the Search page.
- All of these live in the per-machine tag logs as new kinds of lines; older versions skip
  them.

## [0.11.0] - 2026-11-08

### Added

- **Empty the quarantine.** A plan's quarantined items can be deleted for good from the plan
  page — after a confirmation that says they cannot be brought back from the page and the
  plan can no longer be undone — or with `tag-keeper purge <plan> --yes`. Snapshots are
  taken before and after, each deletion is journaled, symlinks are deleted rather than
  followed, and the plans page shows how much the quarantine holds in total.

## [0.10.0] - 2026-11-08

### Added

- **Full-text search.** The Tags page became the Search page: search by name and folder, by
  the text inside PDF, Office and text files, by kind and by tags, in any combination, with
  the matches highlighted. Scanned PDFs without text are read with tesseract when it is
  installed (`jpn+eng` by default). Text is kept by content hash in an SQLite FTS5 trigram
  index, so Japanese works without word splitting; one- and two-letter words are searched
  without the index. While `serve` runs, documents are read in the background at low
  priority (`[extract]`); `tag-keeper extract` does the same from the command line, and
  `tag-keeper search` searches from it. `#/tags` links still open the page.
- **Moves found by content hash.** A move that arrives as "deleted here, new there" — a
  folder renamed on another device and brought in by rclone bisync gets new inodes — is
  matched after hashing: only one-to-one matches (or, inside a folder that moved, the file
  at the same relative path), never files the tool quarantined. The old catalog row comes
  back at the new path, a folder whose files all moved is recorded as one folder move, and
  tags and version history follow (`[scan] relink_days`).
- **The catalog keeps itself up to date** while `serve` runs: each root is refreshed every
  60 minutes and after every cloud-sync run (`[scan] auto_refresh`, `after_sync`). These
  jobs do not pop up toasts or redraw the page you are working on.
- The Roots page warns when the last scan is old and shows how many disappearances are on
  hold.
- The cache of MP4s made for playback is kept under 5 GB, removing the least recently
  watched first.

### Changed

- The catalog schema is now version 3 (`extracts` table rebuilt for the index, `gone_by`
  column). Older catalogs are upgraded on first use; older versions of tag-keeper cannot
  open an upgraded catalog.

### Fixed

- **Restoring an old version could write outside the root**, and the history and
  past-folder views could read outside it, through an empty path or a symlink inside the
  root. These paths are now checked like the rest of the file browser.
- An inbox plan could move a file outside the root through a symlink in its destination;
  moves are now also checked right before they run.
- Plans made from the file browser for a root whose name contains spaces or other
  characters could not be opened or undone from the web UI.
- One damaged line in a plan's journal (power loss while writing) made every page fail to
  load and the undo fail; unreadable lines are now skipped.
- A folder holding only files the scan skips (excluded or not readable as UTF-8) was
  offered as an empty folder and quarantined with its contents.
- A file name that is not valid UTF-8 made the whole scan fail; such names are now skipped
  with a warning.
- When a running cloud-sync run did not finish in time, applying a plan gave up but left the
  sync timer stopped and the plan stuck as interrupted. The timer is now restarted and the
  plan can be run again.
- Applying a plan restarted a sync timer or service that you had stopped yourself.
- Names that differ only in case (OneDrive treats them as the same) were not caught when
  moving, renaming, creating folders or accepting inbox suggestions.
- Renaming a folder and a file inside it between two scans left the file's tags on a path
  that does not exist.
- Tags of a quarantined item stayed on its path and attached themselves to the next file
  that took that name. They are now taken off and put back by undo.

## [0.9.1] - 2026-11-05

### Fixed

- **Tag search with non-ASCII tags.** Searching for a tag such as `種類:請求書` on the Tags page
  failed with "control characters are not allowed"; the search parameters were read as latin-1.
- **English text in the Japanese UI.** Toasts for finished jobs, the category labels on the
  report's plan form and some plan item details were translated to English even in the
  Japanese UI.

## [0.9.0] - 2026-10-30

### Added

- **Sync progress** in the banner shown while a cloud-sync run is going: phase, items applied
  (copies and deletions against what bisync queued), bytes transferred, speed, time left, the
  latest file and errors, read from the cloud-sync log (`progress` in `GET /api/sync`). Only the
  part of the log written since the last poll is read.

## [0.8.0] - 2026-10-18

### Added

- **`sync_client = "cloud-sync"`**: coordinate with an `rclone bisync` job run by a systemd timer
  (a small `cloud-sync` script outside this package). Applying a plan stops the timer, waits for
  a running sync to finish, quarantines, runs one sync that reflects the deletions (with a raised
  `--max-delete` for that run only) and starts the timer again.
- **Sync now** button on the Roots page and a banner on every page while a sync is running
  (`GET /api/sync`, `POST /api/sync/<root>/start`).

## [0.7.0] - 2026-10-09

### Added

- **New folder** in the file browser, created inside the current folder (names OneDrive
  cannot store, and names already taken, are refused).
- **Move** the ticked files and folders to another folder — type a destination (folders are
  suggested; one that does not exist yet is created) or drag rows onto a folder row or the
  breadcrumbs. Moves run as one-off plans with snapshots and undo; tags and catalog records
  follow, a folder cannot be moved into itself, and items inside a ticked folder move with
  it. File moves are added to the decision log as placement examples.

## [0.6.0] - 2026-10-09

### Added

- **Tag search and management.** The Tags page lists tags by namespace with a filter, and
  searches with several tags — all of them or any of them, minus excluded ones, narrowed by
  name, with tags on a folder counting for everything inside. Results sort by column and
  preview the picked file. A tag can be renamed everywhere (renaming onto an existing tag
  merges the two) or deleted; both are single `rename` / `delete` entries in the tag log.
  The search conditions live in the URL.

### Fixed

- Badges inside file-name cells, such as "Inbox" next to an inbox folder or "Gone now" in a
  past snapshot, stayed in Japanese in the English UI.
- Some tests wrote to the real tag log under `~/.local/share/tag-keeper/`; all tests now use
  temporary data and config directories.

## [0.5.0] - 2026-10-08

### Added

- **Play FLV, MKV, AVI and other videos the browser cannot play.** With ffmpeg installed,
  the file is turned into an MP4 for playback: when the video is already H.264 only the
  container changes (a 55 MB FLV takes about half a second), otherwise it is re-encoded
  to H.264 and AAC while the page shows the progress. The MP4 is cached by content hash,
  so seeking works and the next time is instant; the original is untouched. MP4 and MOV
  files the browser fails to play, such as HEVC, switch to this automatically.

## [0.4.2] - 2026-10-08

### Fixed

- **Rename and delete on the inbox page.** The file picked in the flat inbox list had no
  rename or delete buttons; it now has both, and the Delete key opens the delete dialog.
  Items renamed or deleted this way are dropped from the accept list.

## [0.4.1] - 2026-10-08

### Fixed

- **Suggestions follow the document's language.** Names, summaries, reasons and tags were
  pushed toward Japanese by the prompt; they now follow the document (and, for pictures
  without text, the file name), the destination folder's existing names, and the namespaces
  of the tags already in use. Stored suggestions are made again.

### Added

- README screenshots, taken from made-up demo data built by `scripts/make_demo.py`.

## [0.4.0] - 2026-10-08

### Added

- **Office previews.** Excel workbooks show as tables with sheet tabs (date cells as
  dates), Word documents as headings, paragraphs, tables and pasted images, PowerPoint
  decks as slides with their text and pictures — without extra tools. With LibreOffice
  installed, documents are also converted to PDF for an as-laid-out view (cached by
  content hash). Untrusted XML with a DTD is refused and part sizes are capped.
- **Open in OneDrive.** For roots synced by the OneDrive client for Linux, files and
  folders get an "Open in OneDrive" link, and Office documents an "Open in Office for the
  web" link, found through the client's own item database.

## [0.3.0] - 2026-10-08

### Added

- **Resizable panes.** The file browser and the inbox page have a divider between the list
  and the details; drag it (or use ←→, double-click to reset) and the width is remembered
  per page in the browser.
- **Sorting.** File browser columns (name, tags, size, modified) and plan items sort by
  clicking the header; the inbox list has a sort menu (folder, name, date, size, status).
  Names sort naturally ("2" before "10"), folders stay first, and the choice is remembered.

### Changed

- Pages use the full width of the window instead of a centered column.

## [0.2.0] - 2026-10-08

### Added

- **English UI.** The page follows the browser's language (Japanese, otherwise English) and
  can be switched from the top bar; the choice is remembered in the browser. Fixed labels,
  and the categories, steps and checks coming from the server, are translated by a small
  dictionary; file names, paths and tags are never touched, and anything without an
  entry stays as it is.
- **Dark mode switch.** A button in the top bar cycles between following the system,
  light and dark, and is remembered in the browser.

## [0.1.1] - 2026-10-08

### Added

- **Rename and delete from the file browser**, for any file or folder, not only inbox
  files. A rename takes any name you type, or the suggested one. A delete moves items to
  the quarantine folder. Both run as one-off plans, so they get the pre-move check,
  snapshots, journal and undo; renaming a folder carries its tags along, and file renames
  are added to the decision log as naming examples.

## [0.1.0] - 2026-10-08

First release. The web UI and messages are in Japanese.

### Added

- **Inventory.** `scan` walks a root read-only into an SQLite catalog kept outside the
  tree, tracking renames and moves by inode. Mass disappearances are held until a second
  scan confirms them; a missing or empty root aborts the scan. `hash` computes SHA-256
  incrementally.
- **Hygiene report.** `report` lists application data in synced folders, rebuildable
  artifacts, VM images, installers, temporary files, Office lock files, sync conflict copies
  (only when the original is next to them; differing copies are marked for review), empty
  files and folders, and duplicates confirmed by hash.
- **Tidy-up plans.** `plan` writes candidates to an editable TOML file; `apply` previews,
  then moves items to a quarantine folder outside the tree or renames and moves them inside
  it, skipping anything that changed since the plan was written, with snapper pre/post
  snapshots around the run. Every move is journaled and `undo` puts it back without
  overwriting anything new. Destinations are checked against OneDrive's naming rules.
- **Big deletions without stopping the sync client.** With `sync_client = "onedrive"`, a
  plan that removes a folder above the OneDrive client's `classify_as_big_delete` threshold
  stops the resident sync, reflects the deletions with one `onedrive --sync` whose threshold
  is raised just enough (never `--force`), and starts it again. A failed reflection leaves
  the sync stopped, is journaled, and can be retried or abandoned (`tag-keeper sync`).
- **Web UI** (`serve`): roots, report, plans (untick to skip), check, apply and undo, with
  background jobs and a progress panel. Loopback by default; other addresses require HTTP
  Basic auth, and writes must be same-origin.
- **File browser** with in-page previews for types that cannot run scripts; paths never
  leave the root.
- **Tags** on files and folders, inherited from folders, kept in append-only JSONL logs one
  per machine, and following moves seen by `scan` or made by plans.
- **Version history** from btrfs snapshots via btrfs-timeline: versions of a file, a folder
  at any snapshot, restoring an old version beside the current one. Moves are recorded, so
  history continues across them.
- **Inbox suggestions.** Files in folders named like `tmp` or `*未整理*` are listed flat;
  picking one asks an ollama model (local or cloud) for a `YYYYMMDD_title` name, destination
  folders among existing ones and tags, usually within seconds. Accepted suggestions run as
  one plan of renames and moves. Files elsewhere get suggestions on request.
- **Decision log.** Accepted suggestions, tag accept and reject, and undone plans are
  appended per machine; the closest past decisions are given to the model as examples.

[Unreleased]: https://github.com/akivajp/tag-keeper/compare/v0.13.0...HEAD
[0.13.0]: https://github.com/akivajp/tag-keeper/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/akivajp/tag-keeper/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/akivajp/tag-keeper/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/akivajp/tag-keeper/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/akivajp/tag-keeper/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/akivajp/tag-keeper/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/akivajp/tag-keeper/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/akivajp/tag-keeper/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/akivajp/tag-keeper/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/akivajp/tag-keeper/compare/v0.4.2...v0.5.0
[0.4.2]: https://github.com/akivajp/tag-keeper/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/akivajp/tag-keeper/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/akivajp/tag-keeper/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/akivajp/tag-keeper/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/akivajp/tag-keeper/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/akivajp/tag-keeper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/akivajp/tag-keeper/releases/tag/v0.1.0
