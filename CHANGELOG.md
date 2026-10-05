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

### Changed

- `report`: conflict copies are reported only when the original is next to them (a lone
  "copy" may be the only version). Copies whose content differs from the original are
  marked as needing review and stay out of plans unless `--include-review` is given.
