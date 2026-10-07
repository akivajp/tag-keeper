# Changelog

## Unreleased

### Added

- `scan`: read-only walk of a root into an SQLite catalog kept outside the tree.
  Renames and moves are tracked by inode. Mass disappearances are held until confirmed by
  a second scan; missing or empty roots abort the scan; unreadable folders are not treated
  as gone.
- `hash`: incremental SHA-256 with a progress bar; files that change while being hashed
  are skipped.
- `report`: hygiene report — application data in synced folders, rebuildable artifacts,
  VM images, installers, temporary files, Office lock files, sync conflict copies, empty
  files and folders, and exact duplicates confirmed by hash. `--json` for machine output.
- `roots`: list configured roots and the state of the catalog.
- TOML configuration (`~/.config/tag-keeper/config.toml`) and `--log-file` on every command.
- `plan` / `apply` / `undo`: tidy-up plans. `plan` writes the report's candidates to an
  editable TOML file, one line per item. `apply` previews by default; with `--yes` it moves
  items to a quarantine folder outside the tree, skipping anything that changed since the
  plan was written, with snapper pre/post snapshots around the run. Every move is
  journaled, and `undo` puts items back without overwriting anything new. The catalog is
  updated as items move, so a large quarantine is not mistaken for a mass disappearance.

- Big deletions without stopping the sync client: with `sync_client = "onedrive"` on a
  root, a plan that removes a folder above the OneDrive client's `classify_as_big_delete`
  threshold stops the resident sync, quarantines, reflects the deletions with one
  `onedrive --sync` whose threshold is raised just enough (never `--force`), and starts the
  sync again. A failed reflection leaves the sync stopped, is journaled, and can be retried
  (`tag-keeper sync <plan-id> --retry`) or abandoned (`--resume`).
- `serve`: a web UI for everything above — roots, report, plan editing (untick to skip),
  check, apply and undo — with background jobs and a progress panel (steps, bar, speed,
  time remaining, latest log lines). Loopback by default; other addresses require HTTP
  Basic auth. Writes must be same-origin.
- Plans accept `skip = true` on an item, so items can be left out without deleting lines.

### Changed

- `report`: conflict copies are reported only when the original is next to them (a lone
  "copy" may be the only version). Copies whose content differs from the original are
  marked as needing review and stay out of plans unless `--include-review` is given.
