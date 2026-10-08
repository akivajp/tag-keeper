// ファイルブラウザ（プレビュー・タグ・版の履歴）、タグの一覧、受け皿の整理の提案の画面。
// 道具（h・setChildren・api・size・num・when・toast など）は app.js のものを使う。
'use strict';

/** 最初のルート名（「ファイル」「整理」をルートの指定なしで開いたとき用）。 */
async function firstRoot() {
  const st = app.state || await refreshState();
  return st.roots.length ? st.roots[0].name : null;
}

/** 相対パスの各部分を URL 用に符号化してつなぐ。 */
function encPath(path) { return path.split('/').filter(Boolean).map(enc).join('/'); }

function browseHref(root, path, at) {
  return `#/browse/${enc(root)}${path ? '/' + encPath(path) : ''}${at ? `?at=${enc(at)}` : ''}`;
}

function fileUrl(root, path, snapshot, download) {
  const q = new URLSearchParams({ root, path });
  if (snapshot) q.set('snapshot', snapshot);
  if (download) q.set('download', '1');
  return `/api/file?${q}`;
}

function joinPath(dir, name) { return dir ? `${dir}/${name}` : name; }

function tagChip(tag, onRemove, kind = '') {
  return h('span', { class: `chip ${kind}` }, tag,
    onRemove ? h('button', { class: 'chip-x', title: 'タグを外す', onclick: (e) => { e.stopPropagation(); onRemove(); } }, '×') : null);
}

/** 既存のタグの候補（入力の補完用）。 */
async function tagDatalist() {
  const { tags } = await api('/api/tags');
  const dl = h('datalist', { id: 'tag-options' }, tags.map((t) => h('option', { value: t.tag })));
  return dl;
}

async function editTags(root, paths, tag, op) {
  tag = (tag || '').trim();
  if (!tag) { toast('タグの名前を入れてください', 'warn'); return false; }
  try {
    const r = await api('/api/tags', { root, paths, tag, op });
    toast(op === 'remove' ? `「${tag}」を ${r.changed} 件から外しました` : `「${tag}」を ${r.changed} 件に付けました`);
    return true;
  } catch (e) { toast(e.message, 'danger'); return false; }
}

// ---------- 画面: ファイルブラウザ ----------

let snapshotCache = {};

async function pageBrowse(main, root, path, params) {
  root = root || await firstRoot();
  if (!root) { setChildren(main, h('p', { class: 'muted' }, 'ルートがありません。')); return; }
  path = path || '';
  const at = params.get('at') || '';
  const q = new URLSearchParams({ root, path });
  if (at) q.set('snapshot', at);
  const [data, datalist] = await Promise.all([api(`/api/browse?${q}`), tagDatalist(), models.load()]);
  if (!snapshotCache[root]) {
    try { snapshotCache[root] = (await api(`/api/snapshots?root=${enc(root)}`)).snapshots; }
    catch { snapshotCache[root] = []; }
  }
  const snaps = snapshotCache[root];
  const live = !at;
  const selected = new Set();
  let filter = '';
  const detail = h('aside', { class: 'detail' }, h('p', { class: 'muted' }, 'ファイルを選ぶと、ここにプレビュー・タグ・履歴を表示します。'));
  const tableBody = h('tbody');
  const selInfo = h('span', { class: 'muted' });

  // 時点の選択（今 / スナップショット）
  const timeSel = h('select', {
    onchange: (e) => { location.hash = browseHref(root, path, e.target.value); },
  }, h('option', { value: '' }, '今'), snaps.map((s) => h('option', { value: s.id, selected: s.id === at },
    `${when(s.taken_at)}（#${s.id}${s.description ? ' ' + s.description : ''}）`)));

  // このフォルダのタグ
  const folderTags = h('div', { class: 'chips' },
    data.tags.direct.map((t) => tagChip(t.tag, live ? async () => { if (await editTags(root, [path], t.tag, 'remove')) app.render(); } : null)),
    data.tags.inherited.map((t) => h('span', { class: 'chip inherited', title: `${t.from || root} から継承` }, t.tag)),
    !data.tags.direct.length && !data.tags.inherited.length ? h('span', { class: 'muted' }, 'タグなし') : null);
  const folderTagInput = h('input', { list: 'tag-options', placeholder: 'このフォルダにタグを付ける（配下に継承）', disabled: !live || !path });

  function renderRows() {
    const f = filter.toLowerCase();
    setChildren(tableBody, data.entries.filter((e) => !f || e.name.toLowerCase().includes(f)).map((e) => {
      const rel = joinPath(path, e.name);
      const box = h('input', {
        type: 'checkbox', disabled: !live, checked: selected.has(rel),
        onchange: (ev) => { if (ev.target.checked) selected.add(rel); else selected.delete(rel); updateSel(); },
      });
      const name = e.is_dir
        ? h('a', { href: browseHref(root, rel, at) }, '📁 ', e.name)
        : h('a', { href: '#', onclick: (ev) => { ev.preventDefault(); showDetail(rel, e); } }, fileIcon(e.name), ' ', e.name);
      return h('tr', { class: e.exists_now === false ? 'gone' : '' },
        h('td', {}, box),
        h('td', { class: 'path' }, name,
          e.inbox ? [' ', h('span', { class: 'badge warn' }, '受け皿')] : null,
          e.exists_now === false ? [' ', h('span', { class: 'badge danger' }, '今は無い')] : null),
        h('td', { class: 'hide-narrow' }, h('div', { class: 'chips' }, e.tags.map((t) => h('span', { class: 'chip' }, t)))),
        h('td', { class: 'num' }, e.is_dir ? '' : size(e.size)),
        h('td', { class: 'num hide-narrow' }, when(e.mtime)));
    }));
  }

  function updateSel() { selInfo.textContent = selected.size ? `${num(selected.size)} 件を選択中` : ''; }

  const tagInput = h('input', { list: 'tag-options', placeholder: '選んだものに付けるタグ（例: 種別:請求書）', disabled: !live });

  async function showDetail(rel, entry) {
    setChildren(detail, h('p', { class: 'muted' }, '読み込み中…'));
    const name = rel.split('/').pop();
    const url = fileUrl(root, rel, at);
    const parts = [h('h2', {}, name), h('div', { class: 'muted mono' }, rel)];
    parts.push(h('div', { class: 'buttons' },
      h('a', { class: 'button', href: url, target: '_blank', rel: 'noopener' }, '新しいタブで開く'),
      h('a', { class: 'button', href: fileUrl(root, rel, at, true) }, 'ダウンロード')));
    parts.push(preview(url, name, entry.size));
    if (live) {
      try {
        const info = await api(`/api/info?root=${enc(root)}&path=${enc(rel)}`);
        const input = h('input', { list: 'tag-options', placeholder: 'タグを付ける' });
        parts.push(h('h3', {}, 'タグ'),
          h('div', { class: 'chips' },
            info.tags.direct.map((t) => tagChip(t.tag, async () => { if (await editTags(root, [rel], t.tag, 'remove')) { await app.render(); showDetail(rel, entry); } })),
            info.tags.inherited.map((t) => h('span', { class: 'chip inherited', title: `${t.from || root} から継承` }, t.tag))),
          h('form', { class: 'inline', onsubmit: async (ev) => { ev.preventDefault(); if (await editTags(root, [rel], input.value, 'add')) { await app.render(); showDetail(rel, entry); } } },
            input, h('button', {}, '付ける')));
        if (info.same_content.length) {
          parts.push(h('h3', {}, '同じ内容のファイル'), h('ul', {}, info.same_content.map((p) => h('li', { class: 'path' },
            h('a', { href: browseHref(root, p.split('/').slice(0, -1).join('/')) }, p)))));
        }
      } catch (e) { parts.push(h('div', { class: 'alert danger' }, e.message)); }
    }
    if (live) {
      // 既存のデータへの提案は、押したときだけ作る（受け皿の外のファイルを勝手に読ませないため）
      const sugHolder = h('div', {}, h('button', {
        onclick: () => setChildren(sugHolder, suggestionPanel(root, rel, {
          auto: true, onAccept: () => toast('採用リストは「整理」の画面からまとめて実行できます'),
        })),
      }, `このファイルの整理の提案を出す（${models.current() || '既定のモデル'}）`));
      parts.push(h('h3', {}, '整理の提案'), sugHolder);
    }
    parts.push(h('h3', {}, '版の履歴'), await historyBlock(rel));
    setChildren(detail, parts);
  }

  async function historyBlock(rel) {
    try {
      const { versions } = await api(`/api/history?root=${enc(root)}&path=${enc(rel)}`);
      const rows = [...versions].reverse().map((v) => {
        const label = v.is_live ? '今' : `${when(v.first_seen)} 〜 ${when(v.last_seen)}`;
        const moved = v.relpath !== rel ? h('div', { class: 'muted path' }, `（このときの場所: ${v.relpath}）`) : null;
        const actions = !v.is_live && v.exists ? h('div', { class: 'buttons' },
          h('a', { class: 'button small', href: fileUrl(root, v.relpath, v.snapshot_id), target: '_blank', rel: 'noopener' }, '見る'),
          live ? h('button', { class: 'small', onclick: () => restore(rel, v) }, '隣に復元…') : null) : null;
        return h('tr', {},
          h('td', {}, label, moved),
          h('td', { class: 'num' }, v.exists ? size(v.size) : h('span', { class: 'badge' }, '無い')),
          h('td', { class: 'num hide-narrow' }, v.exists ? when(v.mtime) : ''),
          h('td', {}, actions));
      });
      return h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, '時点'), h('th', { class: 'num' }, 'サイズ'), h('th', { class: 'num hide-narrow' }, '更新日時'), h('th', {}, ''))),
        h('tbody', {}, rows)));
    } catch (e) {
      return h('p', { class: 'muted' }, `履歴を読めません: ${e.message}`);
    }
  }

  async function restore(rel, v) {
    const ok = await confirmDialog('過去の版を復元しますか？',
      h('p', {}, `${when(v.last_seen)} の時点の版を、今のファイルの隣に日時付きの別名で置きます。今のファイルは上書きしません。`), '復元する');
    if (!ok) return;
    try {
      const r = await api('/api/restore', { root, path: rel, version_path: v.relpath, snapshot: v.snapshot_id });
      toast(`復元しました: ${r.restored}`);
      app.render();
    } catch (e) { toast(e.message, 'danger'); }
  }

  setChildren(main,
    datalist,
    h('datalist', { id: `folders-${enc(root)}` }),
    h('div', { class: 'crumbs' }, data.crumbs.map((c, i) => [i ? ' / ' : '', h('a', { href: browseHref(root, c.path, at) }, c.name)])),
    h('div', { class: 'toolbar' },
      h('label', {}, '時点: ', timeSel),
      at ? h('span', { class: 'badge warn' }, '過去の時点を表示中（読み取りのみ）') : null,
      h('input', { type: 'search', placeholder: '名前で絞り込む', oninput: (e) => { filter = e.target.value; renderRows(); } })),
    h('div', { class: 'card folder-tags' }, h('strong', {}, 'このフォルダのタグ: '), folderTags,
      live && path ? h('form', { class: 'inline', onsubmit: async (ev) => { ev.preventDefault(); if (await editTags(root, [path], folderTagInput.value, 'add')) app.render(); } },
        folderTagInput, h('button', {}, '付ける')) : null),
    live ? h('form', { class: 'toolbar', onsubmit: (ev) => ev.preventDefault() },
      tagInput,
      h('button', { onclick: async () => { if (!selected.size) { toast('ファイルかフォルダを選んでください', 'warn'); return; } if (await editTags(root, [...selected], tagInput.value, 'add')) app.render(); } }, '選んだものに付ける'),
      h('button', { onclick: async () => { if (!selected.size) { toast('ファイルかフォルダを選んでください', 'warn'); return; } if (await editTags(root, [...selected], tagInput.value, 'remove')) app.render(); } }, '外す'),
      selInfo) : null,
    h('div', { class: 'browse' },
      h('div', { class: 'card table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, ''), h('th', {}, '名前'), h('th', { class: 'hide-narrow' }, 'タグ'), h('th', { class: 'num' }, 'サイズ'), h('th', { class: 'num hide-narrow' }, '更新日時'))),
        tableBody)),
      detail));
  renderRows();
}

function fileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  if (['pdf'].includes(ext)) return '📕';
  if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic'].includes(ext)) return '🖼';
  if (['mp4', 'mov', 'mkv', 'webm', 'm4a', 'mp3', 'wav'].includes(ext)) return '🎞';
  if (['xlsx', 'xls', 'csv'].includes(ext)) return '📊';
  if (['docx', 'doc', 'txt', 'md'].includes(ext)) return '📄';
  if (['zip', '7z', 'rar', 'tar', 'gz'].includes(ext)) return '🗜';
  return '📄';
}

/** ファイルの種類に合わせたプレビュー（PDF・画像・音声・動画・テキスト）。 */
function preview(url, name, bytes) {
  const ext = name.split('.').pop().toLowerCase();
  if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'].includes(ext)) return h('img', { class: 'preview', src: url, alt: name });
  if (ext === 'pdf') return h('iframe', { class: 'preview pdf', src: url, title: name });
  if (['mp4', 'webm', 'mov'].includes(ext)) return h('video', { class: 'preview', src: url, controls: true, preload: 'metadata' });
  if (['m4a', 'mp3', 'wav', 'ogg'].includes(ext)) return h('audio', { src: url, controls: true, preload: 'metadata' });
  if (['txt', 'md', 'csv', 'tsv', 'log', 'json', 'xml', 'html', 'htm', 'yaml', 'yml', 'toml', 'ini', 'py', 'sh', 'rdp', 'set', 'mq4'].includes(ext) && (bytes ?? 0) < 512 * 1024) {
    const pre = h('pre', { class: 'preview text' }, '読み込み中…');
    fetch(url).then((r) => r.text()).then((t) => { pre.textContent = t.slice(0, 200000); }).catch(() => { pre.textContent = '読めません'; });
    return pre;
  }
  return h('p', { class: 'muted' }, 'この種類はプレビューできません。');
}

// ---------- 画面: タグ ----------

async function pageTags(main, tag) {
  const { tags } = await api('/api/tags');
  const list = h('div', { class: 'chips big' }, tags.length
    ? tags.map((t) => h('a', { class: `chip ${t.tag === tag ? 'active' : ''}`, href: `#/tags?tag=${enc(t.tag)}` }, `${t.tag}（${t.count}）`))
    : h('span', { class: 'muted' }, 'まだタグがありません。「ファイル」の画面で、ファイルやフォルダに付けられます。'));
  let items = null;
  if (tag) {
    const r = await api(`/api/tags/items?tag=${enc(tag)}`);
    items = h('div', { class: 'card table-wrap' }, h('h2', {}, `「${tag}」が付いたもの`),
      h('p', { class: 'muted' }, 'フォルダに付いたものは、その配下のすべてが対象です。'),
      h('table', {}, h('tbody', {}, r.items.map((it) => {
        const dir = it.is_dir ? it.path : it.path.split('/').slice(0, -1).join('/');
        return h('tr', {},
          h('td', { class: 'path' }, h('a', { href: browseHref(it.root, dir) }, it.is_dir ? '📁 ' : fileIcon(it.path) + ' ', it.path || it.root),
            it.exists ? null : [' ', h('span', { class: 'badge danger' }, '見つからない')]),
          h('td', { class: 'muted' }, it.root));
      }))));
  }
  setChildren(main, h('h1', {}, 'タグ'), list, items);
}

// ---------- 整理の提案の部品（受け皿の一覧とファイルブラウザで共通） ----------

/** 局所的な保存（使えない環境でも画面は動くように、失敗は無視する）。 */
function loadLocal(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function saveLocal(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 保存できなくても動く */ }
}

/** 採用リスト（ルートごと。この端末のブラウザにだけ残す）。 */
const cart = {
  items(root) { return loadLocal(`tag-keeper-cart-${root}`, {}); },
  put(root, item) { const all = this.items(root); all[item.path] = item; saveLocal(`tag-keeper-cart-${root}`, all); },
  remove(root, path) { const all = this.items(root); delete all[path]; saveLocal(`tag-keeper-cart-${root}`, all); },
  clear(root) { saveLocal(`tag-keeper-cart-${root}`, {}); },
};

/** 使うモデル（画面で選んだもの。無ければ設定の既定）。 */
const models = {
  list: null,
  async load() {
    if (!this.list) { try { this.list = await api('/api/models'); } catch { this.list = { default: '', models: [] }; } }
    return this.list;
  },
  current() { return loadLocal('tag-keeper-model', '') || (this.list && this.list.default) || ''; },
  set(name) { saveLocal('tag-keeper-model', name); },
};

async function modelSelect(onchange) {
  const m = await models.load();
  const cur = models.current();
  return h('select', { title: 'モデル（☁ はクラウドのモデル。ファイルの内容がクラウドに送られる）', onchange: (e) => { models.set(e.target.value); onchange(); } },
    m.models.map((x) => h('option', { value: x.name, selected: x.name === cur }, `${x.cloud ? '☁ ' : ''}${x.name}${x.vision ? '' : '（画像なし）'}${x.name === m.default ? '（既定）' : ''}`)));
}

/** 提案の待ち行列の様子を見張り、変わったら知らせる。 */
const suggestWatch = {
  root: null,
  byPath: {},
  listeners: new Set(),
  timer: null,
  async poll() {
    clearTimeout(this.timer);
    if (!this.root) return;
    let pending = false;
    try {
      const { requests } = await api(`/api/suggest/queue?root=${enc(this.root)}`);
      const before = this.byPath;
      this.byPath = {};
      for (const r of requests) if (r.model === models.current()) this.byPath[r.path] = r;
      pending = requests.some((r) => r.state === 'queued' || r.state === 'running');
      for (const fn of this.listeners) fn(before, this.byPath);
    } catch { /* 次の問い合わせで回復する */ }
    this.timer = setTimeout(() => this.poll(), pending ? 1000 : 4000);
  },
  watch(root) { if (this.root !== root) { this.root = root; this.byPath = {}; } this.poll(); },
};

async function requestSuggestions(root, paths, { force = false, prefetch = false } = {}) {
  if (!paths.length) return;
  try {
    await api('/api/suggest', { root, paths, model: models.current(), force, prefetch });
    suggestWatch.watch(root);
  } catch (e) { toast(e.message, 'danger'); }
}

/**
 * 1ファイルの提案（名前・移動先・タグ）を表示・編集する部品。
 * 提案がまだ無ければ、auto なら待ち行列に入れ、できるまで状態を表示する。
 */
function suggestionPanel(root, path, { auto = true, onAccept = null } = {}) {
  const holder = h('div', { class: 'suggestion' }, h('p', { class: 'muted' }, '提案を読み込み中…'));
  let loaded = false;
  let requested = false;

  async function load() {
    const q = new URLSearchParams({ root, path, model: models.current() });
    let data;
    try { data = await api(`/api/suggestion?${q}`); } catch (e) { setChildren(holder, h('div', { class: 'alert danger' }, e.message)); return; }
    if (!data.suggestion) {
      const req = suggestWatch.byPath[path];
      if (!requested && auto && data.known && !req) { requested = true; requestSuggestions(root, [path]); }
      setChildren(holder, waiting(data, suggestWatch.byPath[path]));
      return;
    }
    loaded = true;
    render(data);
  }

  function waiting(data, req) {
    if (!data.known) return h('div', { class: 'alert warn' }, 'カタログにまだありません。ルートを走査してから選んでください。');
    if (req && req.state === 'error') {
      return h('div', {}, h('div', { class: 'alert danger' }, `提案を作れませんでした: ${req.error}`),
        h('button', { onclick: () => { requestSuggestions(root, [path], { force: true }); } }, 'もう一度'));
    }
    if (req && (req.state === 'queued' || req.state === 'running')) {
      return h('div', { class: 'waiting' }, h('div', { class: 'bar indeterminate' }, h('div')),
        h('p', { class: 'muted' }, req.state === 'running' ? `${models.current()} が内容を読んでいます…` : `順番待ち（${req.position ?? '?'} 番目）`));
    }
    return h('div', {}, h('p', { class: 'muted' }, 'まだ提案がありません。'),
      h('button', { class: 'primary', onclick: () => { requested = true; requestSuggestions(root, [path]); load(); } }, '提案を出す'));
  }

  function render(data) {
    const s = data.suggestion;
    const name = path.split('/').pop();
    const dir = path.split('/').slice(0, -1).join('/');
    const existing = new Set(data.existing_tags);
    const current = new Set(data.tags.direct.map((t) => t.tag));
    const nameInput = h('input', { class: 'name-input', value: s.new_name });
    // 移動先: 提案（過去の例から・新しいフォルダの印つき）＋今の場所＋その他
    let dest = s.destinations.length ? s.destinations[0].relpath : dir;
    const other = h('input', { list: `folders-${enc(root)}`, placeholder: 'フォルダのパスを入力（候補が出ます）', hidden: true });
    other.addEventListener('input', () => { dest = other.value.trim().replace(/^\/+|\/+$/g, ''); folderSuggest(root, other.value); });
    const group = `dest-${Math.random().toString(36).slice(2)}`;
    const radio = (value, label, extra, checked) => h('label', { class: 'dest' },
      h('input', { type: 'radio', name: group, value, checked, onchange: () => { dest = value === '__other__' ? other.value.trim() : value; other.hidden = value !== '__other__'; } }),
      h('span', {}, h('span', { class: 'path' }, label), extra));
    const dests = h('div', { class: 'dests' },
      s.destinations.map((d, i) => radio(d.relpath, d.relpath, [
        d.from_example ? [' ', h('span', { class: 'badge ok' }, '過去の例')] : null,
        d.new ? [' ', h('span', { class: 'badge warn' }, '新しいフォルダ')] : null,
        d.reason ? h('div', { class: 'muted small' }, d.reason) : null], i === 0)),
      radio(dir, `（今の場所のまま）${dir || '/'}`, null, !s.destinations.length),
      radio('__other__', 'その他のフォルダ…', null, false), other);

    // タグ: 提案（既存・新規）を付ける・却下する。今のタグも見せる
    const tagBox = h('div', { class: 'chips' });
    function renderTags() {
      setChildren(tagBox,
        [...current].map((t) => h('span', { class: 'chip' }, '✓ ', t)),
        s.tags.filter((t) => !current.has(t)).map((t) => h('span', { class: `chip suggested ${existing.has(t) ? '' : 'new'}`, title: existing.has(t) ? '既存のタグ' : '新しいタグ' },
          existing.has(t) ? '' : h('span', { class: 'tag-new' }, '新規 '), t,
          h('button', { class: 'chip-x ok', title: '付ける', onclick: () => feedback(t, 'accept') }, '＋'),
          h('button', { class: 'chip-x', title: '却下（次から提案しにくくなる）', onclick: () => feedback(t, 'reject') }, '×'))),
        data.tags.inherited.map((t) => h('span', { class: 'chip inherited', title: `${t.from || root} から継承` }, t.tag)));
    }
    async function feedback(tag, decision) {
      try {
        await api('/api/tags/feedback', { root, path, tag, decision, model: s.model });
        if (decision === 'accept') current.add(tag);
        s.tags = s.tags.filter((t) => t !== tag || decision === 'accept');
        renderTags();
      } catch (e) { toast(e.message, 'danger'); }
    }
    renderTags();
    const tagInput = h('input', { list: 'tag-options', placeholder: '自分でタグを付ける' });

    function accept() {
      const newName = nameInput.value.trim();
      if (!newName) { toast('名前を入れてください', 'warn'); return; }
      const target = joinPath(dest, newName);
      if (target === path) { toast('名前も場所も変わりません', 'warn'); return; }
      cart.put(root, { path, dest: target, model: s.model, name: newName, summary: s.summary });
      toast(`採用リストに入れました: ${newName}`);
      if (onAccept) onAccept();
    }
    nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); accept(); } });

    setChildren(holder,
      h('div', { class: 'muted small' }, `${s.doc_type || ''}${s.model ? ` ／ ${s.model}` : ''}${s.cached ? '' : ''}`),
      s.summary ? h('p', {}, s.summary) : null,
      s.note ? h('p', { class: 'muted small' }, s.note) : null,
      data.same_content.length ? h('div', { class: 'alert warn' }, '同じ内容のファイルが既にあります: ', data.same_content.slice(0, 3).join(' / ')) : null,
      h('h3', {}, '新しい名前'), nameInput,
      s.date_source === 'file_date' ? h('div', { class: 'muted small' }, '日付は更新日から（書類の中に見つからなかった）') : null,
      h('h3', {}, '移動先'), dests,
      h('h3', {}, 'タグ'), tagBox,
      h('form', { class: 'inline', onsubmit: async (ev) => { ev.preventDefault(); if (await editTags(root, [path], tagInput.value, 'add')) { current.add(tagInput.value.trim()); tagInput.value = ''; renderTags(); } } }, tagInput, h('button', {}, '付ける')),
      s.examples.length ? h('details', {}, h('summary', { class: 'muted small' }, `参考にした過去の判断（${s.examples.length} 件）`), h('ul', { class: 'small' }, s.examples.map((e) => h('li', {}, e)))) : null,
      h('div', { class: 'buttons', style: 'margin-top:.75rem' },
        h('button', { class: 'primary', onclick: accept, title: 'Enter でも採用できます' }, '採用リストに入れる'),
        h('button', { onclick: () => { loaded = false; requestSuggestions(root, [path], { force: true }); setChildren(holder, h('p', { class: 'muted' }, '作り直しています…')); } }, '作り直す')),
    );
    nameInput.focus({ preventScroll: true });
  }

  const listener = (_before, now) => {
    if (!holder.isConnected) { suggestWatch.listeners.delete(listener); return; }
    const r = now[path];
    if (!loaded && r) load();
  };
  suggestWatch.listeners.add(listener);
  suggestWatch.watch(root);
  load();
  return holder;
}

/** 採用リストの帯（件数・中身の確認・実行）。 */
function cartBar(root, onChange) {
  const bar = h('div', { class: 'cart' });
  function render() {
    const items = Object.values(cart.items(root));
    setChildren(bar,
      h('strong', {}, `採用リスト ${num(items.length)} 件`),
      items.length ? h('details', {}, h('summary', { class: 'muted' }, '中身'),
        h('ul', { class: 'small' }, items.map((it) => h('li', { class: 'path' }, `${it.path} → ${it.dest} `,
          h('button', { class: 'chip-x', title: '外す', onclick: () => { cart.remove(root, it.path); render(); if (onChange) onChange(); } }, '×'))))) : null,
      h('button', { class: 'primary', disabled: !items.length || jobRunning(), onclick: () => run(true) }, '確認して実行…'),
      h('button', { disabled: !items.length, onclick: () => run(false) }, 'プランとして保存'),
      items.length ? h('button', { onclick: () => { cart.clear(root); render(); if (onChange) onChange(); } }, '空にする') : null);
  }
  async function run(apply) {
    const items = Object.values(cart.items(root));
    if (apply) {
      const ok = await confirmDialog(`${items.length} 件を移動・改名しますか？`, [
        h('ul', { class: 'small' }, items.slice(0, 30).map((it) => h('li', { class: 'path' }, `${it.path} → ${it.dest}`))),
        items.length > 30 ? h('p', { class: 'muted' }, `ほか ${items.length - 30} 件`) : null,
        h('p', {}, '移動先に同じ名前があるもの・選んだ後に変わったものは飛ばします。実行の前後にスナップショットを撮り、プランの画面から取り消せます。'),
      ], '実行する');
      if (!ok) return;
    }
    try {
      const r = await api(`/api/organize/${enc(root)}/plan`, { items, apply });
      cart.clear(root);
      render();
      if (onChange) onChange();
      if (apply) { app.job = r.job; app.jobDismissed = null; renderJob(); pollJob(); toast(`実行を始めました（プラン ${r.plan_id}）`); }
      else location.hash = `#/plans/${r.plan_id}`;
    } catch (e) { toast(e.message, 'danger'); }
  }
  render();
  return { el: bar, render };
}

// ---------- 画面: 受け皿の整理（フラットな一覧と、選んだファイルの提案） ----------

async function pageOrganize(main, root) {
  root = root || await firstRoot();
  if (!root) { setChildren(main, h('p', { class: 'muted' }, 'ルートがありません。')); return; }
  setChildren(main, h('p', { class: 'muted' }, '受け皿のフォルダを調べています…'));
  await models.load();
  const data = await api(`/api/organize/${enc(root)}?model=${enc(models.current())}`);
  const files = data.files;
  const opts = loadLocal('tag-keeper-organize', { auto: true, prefetch: true, hideDone: false });
  let filter = '';
  let current = null;
  const listBody = h('tbody');
  const side = h('aside', { class: 'detail' }, h('p', { class: 'muted' }, '左の一覧からファイルを選ぶと、プレビューと提案（名前・移動先・タグ）を表示します。↑↓ か j / k で移動、名前の欄で Enter を押すと採用して次へ進みます。'));
  const bar = cartBar(root, () => renderList());

  function visible() {
    const f = filter.toLowerCase();
    const inCart = cart.items(root);
    return files.filter((x) => (!f || x.path.toLowerCase().includes(f)) && !(opts.hideDone && inCart[x.path]));
  }

  function status(f) {
    if (cart.items(root)[f.path]) return h('span', { class: 'badge ok' }, '採用リスト');
    const r = suggestWatch.byPath[f.path];
    if (r && r.state === 'running') return h('span', { class: 'badge' }, '作成中');
    if (r && r.state === 'queued') return h('span', { class: 'badge' }, `待ち ${r.position ?? ''}`);
    if (r && r.state === 'error') return h('span', { class: 'badge danger' }, '失敗');
    if (f.suggestion || (r && r.state === 'done')) return h('span', { class: 'badge ok' }, '提案あり');
    return '';
  }

  function renderList() {
    setChildren(listBody, visible().map((f) => {
      const name = f.path.split('/').pop();
      const dir = f.path.split('/').slice(0, -1).join('/');
      return h('tr', { class: f.path === current ? 'selected' : '', onclick: () => select(f.path) },
        h('td', { class: 'path' }, fileIcon(name), ' ', name, h('div', { class: 'muted small' }, dir),
          f.suggestion ? h('div', { class: 'small' }, '→ ', f.suggestion.new_name) : null),
        h('td', { class: 'num hide-narrow' }, size(f.size)),
        h('td', {}, status(f), f.duplicates.length ? [' ', h('span', { class: 'badge warn', title: f.duplicates.join('\n') }, '重複')] : null));
    }));
  }

  function select(path) {
    current = path;
    renderList();
    const name = path.split('/').pop();
    setChildren(side,
      h('h2', {}, name), h('div', { class: 'muted mono' }, path),
      h('div', { class: 'buttons' }, h('a', { class: 'button small', href: fileUrl(root, path), target: '_blank', rel: 'noopener' }, '新しいタブで開く'),
        h('a', { class: 'button small', href: browseHref(root, path.split('/').slice(0, -1).join('/')) }, 'フォルダを開く')),
      // 提案を先に、プレビューを後に置く（PDF のプレビューで提案が画面の外に押し出されないように）
      suggestionPanel(root, path, { auto: opts.auto, onAccept: () => { bar.render(); move(1); } }),
      h('h3', {}, 'プレビュー'),
      preview(fileUrl(root, path), name, files.find((f) => f.path === path)?.size));
    if (opts.prefetch) {
      const list = visible();
      const i = list.findIndex((f) => f.path === path);
      const next = list.slice(i + 1, i + 3).filter((f) => !f.suggestion).map((f) => f.path);
      requestSuggestions(root, next, { prefetch: true });
    }
  }

  function move(delta) {
    const list = visible();
    if (!list.length) return;
    const i = list.findIndex((f) => f.path === current);
    const next = list[Math.min(list.length - 1, Math.max(0, (i < 0 ? -1 : i) + delta))];
    if (next) {
      select(next.path);
      listBody.querySelector('tr.selected')?.scrollIntoView({ block: 'nearest' });
    }
  }

  // 一覧の印（待ち・作成中・提案あり）を、待ち行列の様子に合わせて更新する
  const listener = (_b, now) => {
    if (!listBody.isConnected) { suggestWatch.listeners.delete(listener); return; }
    for (const f of files) if (now[f.path] && now[f.path].state === 'done' && !f.suggestion) f.suggestion = { new_name: '…' };
    renderList();
  };
  suggestWatch.listeners.add(listener);
  suggestWatch.watch(root);

  const keyHandler = (e) => {
    if (!listBody.isConnected) { document.removeEventListener('keydown', keyHandler); return; }
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
    if (e.key === 'ArrowDown' || e.key === 'j') { e.preventDefault(); move(1); }
    if (e.key === 'ArrowUp' || e.key === 'k') { e.preventDefault(); move(-1); }
  };
  document.addEventListener('keydown', keyHandler);

  const toggle = (key, label) => h('label', { class: 'small' }, h('input', {
    type: 'checkbox', checked: opts[key], onchange: (e) => { opts[key] = e.target.checked; saveLocal('tag-keeper-organize', opts); renderList(); },
  }), ' ', label);
  const pending = files.filter((f) => !f.suggestion).length;

  setChildren(main,
    h('datalist', { id: `folders-${enc(root)}` }),
    await tagDatalist(),
    h('h1', {}, `整理: ${root}`),
    h('p', { class: 'muted' }, `受け皿（${data.patterns.join('・')} に一致するフォルダ）のファイル ${num(files.length)} 件を、フォルダの階層なしで並べています。`
      + 'ファイルを選ぶと、モデルが内容を読んで名前・移動先・タグを提案します。採用したものは採用リストにたまり、まとめて実行できます。'),
    h('div', { class: 'toolbar' },
      h('input', { type: 'search', placeholder: '名前やフォルダで絞り込む', oninput: (e) => { filter = e.target.value; renderList(); } }),
      await modelSelect(() => app.render()),
      toggle('auto', '選んだら提案を出す'), toggle('prefetch', '次の2件を先読み'), toggle('hideDone', '採用したものを隠す'),
      h('button', { disabled: jobRunning() || !pending, title: '裏でまとめて作る（時間がかかります）', onclick: () => startJob(`/api/organize/${enc(root)}/suggest`, { model: models.current() }) }, `残り ${num(pending)} 件をまとめて提案`)),
    bar.el,
    h('div', { class: 'browse' },
      h('div', { class: 'card table-wrap list' }, h('table', { class: 'flat' }, listBody)),
      side));
  renderList();
}

let folderTimer = null;
function folderSuggest(root, word) {
  clearTimeout(folderTimer);
  folderTimer = setTimeout(async () => {
    try {
      const { folders } = await api(`/api/folders?root=${enc(root)}&q=${enc(word)}`);
      const dl = document.getElementById(`folders-${enc(root)}`);
      if (dl) setChildren(dl, folders.map((f) => h('option', { value: f })));
    } catch { /* 候補が出ないだけなので無視する */ }
  }, 250);
}
