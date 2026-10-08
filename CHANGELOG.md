# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/akivajp/tag-keeper/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/akivajp/tag-keeper/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/akivajp/tag-keeper/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/akivajp/tag-keeper/releases/tag/v0.1.0
