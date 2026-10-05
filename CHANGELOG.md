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
