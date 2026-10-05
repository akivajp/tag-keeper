"""設定ファイルと CLI のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import write
from tag_keeper.cli import main
from tag_keeper.config import ConfigError, load_config


def test_missing_config_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "none.toml")
    assert config.roots == []
    assert config.safety.mass_missing_ratio == 0.2


def test_load_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = write(
        tmp_path / "c.toml",
        """
[[roots]]
name = "onedrive"
path = "~/CloudSync/OneDrive"
exclude = ["*.partial"]

[hygiene]
allow = ["archive/**"]
device_names = ["zefat", "hermon"]

[safety]
mass_missing_min = 50

[plan]
quarantine_dir = "~/quarantine"
snapper_config = "home"
""",
    )
    config = load_config(cfg)
    assert config.roots[0].path == (tmp_path / "CloudSync" / "OneDrive").resolve()
    assert config.roots[0].exclude == ["*.partial"]
    assert config.hygiene.device_names == ["zefat", "hermon"]
    assert config.safety.mass_missing_min == 50
    assert config.plan.quarantine_dir == (tmp_path / "quarantine").resolve()
    assert config.plan.snapper_config == "home"


def test_duplicate_root_names_are_rejected(tmp_path: Path) -> None:
    cfg = write(tmp_path / "c.toml", '[[roots]]\nname="a"\npath="/x"\n[[roots]]\nname="a"\npath="/y"\n')
    with pytest.raises(ConfigError):
        load_config(cfg)


def test_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "data"
    write(root / "docs" / "a.pdf", b"same")
    write(root / "docs" / "a (1).pdf", b"same")
    write(root / "Thumbs.db")
    cfg = write(tmp_path / "c.toml", f'[[roots]]\nname = "data"\npath = "{root}"\n[hygiene]\ndevice_names = []\n')
    db = tmp_path / "catalog.db"
    log_file = tmp_path / "logs" / "run.log"
    common = ["--config", str(cfg), "--db", str(db), "--log-file", str(log_file)]

    assert main([*common, "scan"]) == 0
    assert main([*common, "hash", "data"]) == 0
    out_json = tmp_path / "report.json"
    assert main([*common, "report", "--json", str(out_json)]) == 0
    assert main([*common, "roots"]) == 0

    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data[0]["files"] == 3
    assert data[0]["duplicates"][0]["paths"] == ["docs/a (1).pdf", "docs/a.pdf"]
    assert "走査を終えました" in log_file.read_text(encoding="utf-8")


def test_cli_report_before_scan_fails(tmp_path: Path) -> None:
    root = tmp_path / "data"
    write(root / "a.txt")
    db = tmp_path / "catalog.db"
    assert main(["--config", str(tmp_path / "none.toml"), "--db", str(db), "report", str(root)]) == 2


def test_cli_unknown_root_fails(tmp_path: Path) -> None:
    assert main(["--config", str(tmp_path / "none.toml"), "--db", str(tmp_path / "c.db"), "scan", "nope"]) == 2


def test_help_works(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "scan" in capsys.readouterr().out
