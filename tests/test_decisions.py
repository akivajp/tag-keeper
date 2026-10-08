"""判断の記録（採用・タグの採否・取り消し）と、それを使った提案の文脈、提案の待ち行列のテスト。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import write
from tag_keeper import organize
from tag_keeper.catalog import record_move
from tag_keeper.config import Config, OrganizeConfig, RootConfig, SafetyConfig
from tag_keeper.decisions import DecisionLog, load_examples
from tag_keeper.scan import scan_root
from tag_keeper.suggest_queue import SuggestQueue
from tag_keeper.tags import TagStore


def accept(log: DecisionLog, path: str, name: str, dest: str, plan_id: str, **info: Any) -> None:
    log.accept([{"root": "r", "path": path, "sha256": path, "info": info, "final": {"name": name, "dest_dir": dest, "tags": ["相手:Revolut"]}, "plan_id": plan_id}])


def test_examples_from_accepts_and_manual_moves(conn: sqlite3.Connection, root_id: int, tmp_path: Path) -> None:
    log = DecisionLog(tmp_path / "decisions", host="a")
    accept(log, "inbox/downloadfile.pdf", "20250414_Revolut_明細書.pdf", "bank/Revolut", "p1", title="Revolut 明細書", doc_type="明細書")
    accept(log, "inbox/x.pdf", "20250101_誤り.pdf", "wrong", "p2", title="Revolut 明細書")
    log.revert("p2")  # 取り消したプランの判断は使わない
    log.tag("r", "inbox/a.pdf", None, "種別:その他", "reject")
    log.tag("r", "inbox/b.pdf", None, "種別:その他", "reject")
    record_move(conn, root_id, 1, "inbox/scan.pdf", "家/領収書/20250301_領収書.pdf", False, "scan", "2026-01-01T00:00:00+00:00")
    record_move(conn, root_id, 2, "a/b.pdf", "c/b.pdf", False, "scan", "2026-01-01T00:00:00+00:00")  # 受け皿の外どうし
    conn.commit()

    index, rejected = load_examples(log, conn, "r", root_id, ["inbox"])
    assert {(e.final_name, e.source) for e in index.examples} == {
        ("20250414_Revolut_明細書.pdf", "accept"),
        ("20250301_領収書.pdf", "move"),
    }
    assert rejected["種別:その他"] == 2
    [(best, _score)] = index.search("Revolut ご利用明細書", k=1)
    assert best.dest_dir == "bank/Revolut"
    assert "移動先「bank/Revolut」" in best.line() and "相手:Revolut" in best.line()


@pytest.fixture
def tree(root: Path) -> Path:
    write(root / "inbox" / "downloadfile.pdf", "x")
    write(root / "bank" / "Revolut" / "20250301_Revolut_明細書.pdf", "a")
    write(root / "家" / "物件" / "契約書.pdf", "b")
    return root


def fake_ollama(monkeypatch: pytest.MonkeyPatch, prompts: list[str]) -> None:
    def generate(config: OrganizeConfig, prompt: str, image: bytes | None = None, **_: Any) -> dict[str, Any]:
        prompts.append(prompt)
        if "内容を読み取ってください" in prompt:
            return {"title": "Revolut 明細書", "doc_type": "明細書", "summary": "Revolut の利用明細", "keywords": ["Revolut"]}
        return {"new_name": "20250414_Revolut_明細書", "date": "20250414", "destinations": [{"index": 1, "reason": "過去と同じ"}], "tags": ["相手:Revolut", " 種別 : 明細書 "]}

    monkeypatch.setattr(organize, "ollama_generate", generate)
    monkeypatch.setattr(organize, "check_ollama", lambda config: None)


def test_examples_and_tags_go_into_the_prompt(conn: sqlite3.Connection, root_id: int, tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scan_root(conn, root_id, tree, SafetyConfig())
    log = DecisionLog(tmp_path / "decisions", host="a")
    log.accept([{"root": "test", "path": "inbox/old.pdf", "sha256": "s", "info": {"title": "Revolut 明細書"}, "final": {"name": "20250301_Revolut_明細書.pdf", "dest_dir": "bank/Revolut", "tags": []}, "plan_id": "p"}])
    for _ in range(2):
        log.tag("test", "inbox/z.pdf", None, "種別:ゴミ", "reject")
    prompts: list[str] = []
    fake_ollama(monkeypatch, prompts)
    tags = TagStore(tmp_path / "tags", host="a")
    tags.add("test", ["bank"], "種別:明細書")
    [sug] = organize.run_suggestions(conn, root_id, tree, OrganizeConfig(), [], root_name="test", decisions=log, tag_counts=tags.all_tags())

    second = prompts[1]
    assert "「old.pdf」" in second and "移動先「bank/Revolut」" in second  # 似た過去の例
    assert "1: bank/Revolut" in second  # 過去の例の移動先は候補の先頭に入る
    assert "種別:明細書" in second.split("# 既存のタグ")[1]
    assert "却下したタグは使わない: 種別:ゴミ" in second
    assert sug.destinations[0]["from_example"] is True
    assert sug.tags == ["相手:Revolut", "種別:明細書"]  # タグの表記はそろえる
    assert sug.examples and sug.info["title"] == "Revolut 明細書"


def test_suggest_queue(tmp_path: Path, tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tag_keeper.catalog import connect, ensure_root

    db = tmp_path / "q.db"
    conn = connect(db)
    scan_root(conn, ensure_root(conn, "test", tree), tree, SafetyConfig())
    conn.close()
    prompts: list[str] = []
    fake_ollama(monkeypatch, prompts)
    config = Config(roots=[RootConfig("test", tree)])
    q = SuggestQueue(db, lambda: config, lambda c: DecisionLog(tmp_path / "decisions"), lambda c: TagStore(tmp_path / "tags"))

    req = q.enqueue("test", "inbox/downloadfile.pdf", "m1")
    missing = q.enqueue("test", "inbox/nope.pdf", "m1", prefetch=True)
    deadline = time.time() + 10
    while time.time() < deadline and any(r["state"] in ("queued", "running") for r in q.status()):
        time.sleep(0.02)
    assert req.state == "done"
    assert missing.state == "error" and "カタログにありません" in missing.error
    # 終わったものを選び直しても、作り直さない（force のときだけ作り直す）
    n = len(prompts)
    assert q.enqueue("test", "inbox/downloadfile.pdf", "m1").state == "done" and len(prompts) == n
    conn = connect(db)
    item = organize.catalog_item(conn, 1, "inbox/downloadfile.pdf")
    assert organize.load_cached(conn, item.sha256, "m1").new_name == "20250414_Revolut_明細書.pdf"
