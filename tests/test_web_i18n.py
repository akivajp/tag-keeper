"""画面の表示言語（日本語・英語）のテスト。

- ソースで t('…') に渡している文が、すべて英語の辞書にあるか（訳し忘れを防ぐ）
- Node があれば、i18n.js を実際に動かして、t() と表示テキストの翻訳を確かめる
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "tag_keeper" / "web" / "static"


def dictionary_keys() -> set[str]:
    """i18n.js の英語の辞書の見出し（日本語の文）。"""
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    body = src[src.index("const I18N_EN = {") : src.index("};", src.index("const I18N_EN = {"))]
    return set(re.findall(r"'((?:[^'\\]|\\.)*)'\s*:", body))


def test_every_t_call_has_an_english_entry() -> None:
    keys = dictionary_keys()
    missing = set()
    for f in ("app.js", "pages.js", "i18n.js"):
        src = (STATIC / f).read_text(encoding="utf-8")
        for m in re.finditer(r"\bt\('((?:[^'\\]|\\.)*)'", src):
            if re.search(r"[぀-ヿ一-鿿]", m.group(1)) and m.group(1) not in keys:
                missing.add(f"{f}: {m.group(1)}")
    assert not missing, "英語の辞書に無い文:\n" + "\n".join(sorted(missing))


def test_static_html_labels_are_translated() -> None:
    """index.html に直接書いた文（メニュー・ダイアログのボタン）も辞書にある。"""
    keys = dictionary_keys()
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    # translate="no" の中（言語の選択肢の「日本語」など）は訳さない
    html = re.sub(r'<(\w+)[^>]*translate="no"[^>]*>.*?</\1>', "", html, flags=re.S)
    texts = {t.strip() for t in re.findall(r">([^<>]+)<", html) if re.search(r"[぀-ヿ一-鿿]", t)}
    labels = set(re.findall(r'aria-label="([^"]+)"', html))
    assert (texts | labels) - keys == set()


NODE_SCRIPT = r"""
// ブラウザの代わりの最小限の部品（i18n.js が読み込み時に触るものだけ）
const store = {};
global.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); } };
Object.defineProperty(globalThis, 'navigator', { value: { languages: ['en-US'] }, configurable: true });
global.document = { documentElement: { lang: '', dataset: {} }, body: null };
global.window = { addEventListener() {} };
global.MutationObserver = class { observe() {} };
global.Node = { TEXT_NODE: 3, ELEMENT_NODE: 1 };
eval(require('fs').readFileSync(process.argv[2], 'utf8') + ';globalThis.t = t; globalThis.translateString = translateString; globalThis.i18n = i18n;');
const out = {
  lang: i18n.lang,
  t: t('{n} 件を選択中', { n: 3 }),
  plain: translateString('  名前を変える…  '),
  category: translateString('アプリのデータ'),
  phase: translateString('実行（隔離・移動）'),
  title: translateString('実行: 20261008-onedrive'),
  reason: translateString('Linux 版 onedrive クライアントの競合コピー。元のファイルと内容が同じ'),
  regen: translateString('node_modules はビルドや実行で作り直せる'),
  prefixed: translateString('見つからない: a/b.pdf'),
  untouched: translateString('利用者のフォルダ名'),
};
i18n.lang = 'ja';
out.ja = t('{n} 件を選択中', { n: 3 });
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node が無い")
def test_translation_in_node(tmp_path: Path) -> None:
    script = tmp_path / "check.js"
    script.write_text(NODE_SCRIPT, encoding="utf-8")
    res = subprocess.run(["node", str(script), str(STATIC / "i18n.js")], capture_output=True, text=True, check=True)
    out = json.loads(res.stdout)
    assert out["lang"] == "en"  # ブラウザの言語が英語なら英語
    assert out["t"] == "3 selected" and out["ja"] == "3 件を選択中"
    assert out["plain"] == "  Rename…  "  # 前後の空白は保つ
    assert out["category"] == "Application data"
    assert out["phase"] == "Applying (quarantine / move)"
    assert out["title"] == "Apply: 20261008-onedrive"
    assert out["reason"] == "OneDrive client for Linux conflict copy. same content as the original"
    assert out["regen"] == "node_modules can be rebuilt"
    assert out["prefixed"] == "Not found: a/b.pdf"
    assert out["untouched"] == "利用者のフォルダ名"  # 辞書に無い文は日本語のまま


def test_badges_inside_paths_are_translated() -> None:
    """ファイル名の欄の中の札（受け皿・今は無い など）も訳す。札の文は辞書にある。"""
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    assert "const I18N_ALWAYS = '.badge';" in src
    keys = dictionary_keys()
    pages = (STATIC / "pages.js").read_text(encoding="utf-8") + (STATIC / "app.js").read_text(encoding="utf-8")
    for m in re.finditer(r"class: 'badge[^']*' \}, '([^']+)'\)", pages):
        if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", m.group(1)):
            assert m.group(1) in keys, m.group(1)
