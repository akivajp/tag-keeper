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
  const [data, datalist] = await Promise.all([api(`/api/browse?${q}`), tagDatalist()]);
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

// ---------- 画面: 受け皿の整理の提案 ----------

async function pageOrganize(main, root) {
  root = root || await firstRoot();
  if (!root) { setChildren(main, h('p', { class: 'muted' }, 'ルートがありません。')); return; }
  setChildren(main, h('p', { class: 'muted' }, '受け皿のフォルダを調べています…'));
  const data = await api(`/api/organize/${enc(root)}`);
  const files = data.files;
  const withSug = files.filter((f) => f.suggestion && !f.suggestion.error);
  const pending = files.length - withSug.length;
  // 採用する提案（相対パス → {dest フォルダ, 新しい名前}）
  const accept = new Map();
  const countEl = h('span', { class: 'selection' });
  const busy = jobRunning();

  function updateCount() { countEl.textContent = `採用 ${num(accept.size)} 件`; }

  function row(f) {
    const s = f.suggestion;
    const name = f.path.split('/').pop();
    const dir = f.path.split('/').slice(0, -1).join('/');
    const preview = h('a', { href: fileUrl(root, f.path), target: '_blank', rel: 'noopener' }, fileIcon(name), ' ', name);
    const dup = f.duplicates.length ? h('div', {}, h('span', { class: 'badge warn' }, '同じ内容が既にある'), ' ',
      h('span', { class: 'muted path' }, f.duplicates.slice(0, 2).join(' / '))) : null;
    if (!s) {
      return h('tr', {}, h('td', {}), h('td', { class: 'path' }, preview, dup), h('td', { colspan: 2, class: 'muted' }, 'まだ提案がありません'));
    }
    if (s.error) {
      return h('tr', {}, h('td', {}), h('td', { class: 'path' }, preview, dup), h('td', { colspan: 2 }, h('span', { class: 'badge danger' }, '失敗'), ' ', s.error));
    }
    const nameInput = h('input', { class: 'name-input', value: s.new_name });
    const destSel = h('select', {},
      s.destinations.map((d) => h('option', { value: d.relpath, title: d.reason }, d.new ? `${d.relpath}（新しいフォルダ）` : d.relpath)),
      h('option', { value: dir }, `（今の場所のまま）${dir}`),
      h('option', { value: '__other__' }, 'その他のフォルダ…'));
    const otherInput = h('input', { list: `folders-${enc(root)}`, placeholder: 'フォルダのパスを入力（候補が出ます）', hidden: true });
    otherInput.addEventListener('input', () => folderSuggest(root, otherInput.value));
    const box = h('input', { type: 'checkbox' });
    const reason = h('div', { class: 'muted small' });

    function sync() {
      const destDir = destSel.value === '__other__' ? otherInput.value.trim().replace(/^\/+|\/+$/g, '') : destSel.value;
      otherInput.hidden = destSel.value !== '__other__';
      const d = s.destinations.find((x) => x.relpath === destSel.value);
      reason.textContent = d ? `理由: ${d.reason}` : '';
      if (box.checked) accept.set(f.path, { path: f.path, dest: joinPath(destDir, nameInput.value.trim()), reason: s.summary });
      else accept.delete(f.path);
      updateCount();
    }
    for (const el of [box, destSel]) el.addEventListener('change', sync);
    for (const el of [nameInput, otherInput]) el.addEventListener('input', sync);
    sync();

    const tagButtons = s.tags.length ? h('div', { class: 'chips' }, s.tags.map((t) => h('button', {
      class: 'chip', title: 'このファイルにタグを付ける',
      onclick: async (ev) => { if (await editTags(root, [f.path], t, 'add')) ev.target.classList.add('active'); },
    }, `＋${t}`))) : null;

    return h('tr', {},
      h('td', {}, box),
      h('td', { class: 'path' }, preview, h('div', { class: 'muted small' }, `${s.doc_type || ''} ${s.summary || ''}`), dup, tagButtons),
      h('td', {}, nameInput,
        s.date_source === 'file_date' ? h('div', { class: 'muted small' }, '日付は更新日から（書類の中に見つからなかった）') : null),
      h('td', {}, destSel, otherInput, reason));
  }

  // 親フォルダごとにまとめる
  const groups = new Map();
  for (const f of files) {
    const dir = f.path.split('/').slice(0, -1).join('/');
    if (!groups.has(dir)) groups.set(dir, []);
    groups.get(dir).push(f);
  }

  async function makePlan() {
    if (!accept.size) { toast('採用する提案にチェックを入れてください', 'warn'); return; }
    try {
      const r = await api(`/api/organize/${enc(root)}/plan`, { items: [...accept.values()] });
      toast(`整理プランを作りました（${r.ops} 件）。内容を確かめてから実行してください`);
      location.hash = `#/plans/${r.plan_id}`;
    } catch (e) { toast(e.message, 'danger'); }
  }

  setChildren(main,
    h('datalist', { id: `folders-${enc(root)}` }),
    h('h1', {}, `整理: ${root}`),
    h('p', { class: 'muted' }, `受け皿（${data.patterns.join('・')} に一致するフォルダ）のファイルについて、手元のモデル（${data.model}）が内容を読んで、`
      + '名前（既定は YYYYMMDD_題名。移動先の命名の慣習があれば合わせる）と、既存のフォルダから移動先を提案します。採用したものだけが整理プランになり、確認してから実行します。'),
    h('div', { class: 'stats' },
      stat('受け皿のフォルダ', num(data.inbox.length)), stat('ファイル', num(files.length)),
      stat('提案あり', num(withSug.length)), stat('未作成・失敗', num(pending))),
    h('div', { class: 'buttons' },
      h('button', { class: 'primary', disabled: busy || !pending, onclick: () => startJob(`/api/organize/${enc(root)}/suggest`, {}) },
        `提案を作る（未作成 ${num(pending)} 件。1件あたり十数秒）`),
      h('button', { disabled: busy || !withSug.length, onclick: async () => {
        if (await confirmDialog('提案をすべて作り直しますか？', h('p', {}, `${num(files.length)} 件をモデルにもう一度読ませます。時間がかかります。`), '作り直す')) {
          startJob(`/api/organize/${enc(root)}/suggest`, { force: true });
        }
      } }, 'すべて作り直す…')),
    h('details', { class: 'group' }, h('summary', {}, h('span', { class: 'title' }, '受け皿のフォルダ'), h('span', { class: 'muted' }, `${data.inbox.length} 件`)),
      h('div', { class: 'body' }, h('ul', {}, data.inbox.map((d) => h('li', { class: 'path' }, h('a', { href: browseHref(root, d) }, d)))))),
    h('div', { class: 'toolbar' }, countEl, h('button', { class: 'primary', onclick: makePlan }, '採用したもので整理プランを作る')),
    ...[...groups.entries()].map(([dir, fs]) => h('details', { class: 'group', open: fs.some((f) => f.suggestion) },
      h('summary', {}, h('span', { class: 'title path' }, dir || '(ルート直下)'), h('span', { class: 'muted' }, `${num(fs.length)} 件`)),
      h('div', { class: 'body table-wrap' }, h('table', { class: 'organize' },
        h('thead', {}, h('tr', {}, h('th', {}, '採用'), h('th', {}, 'ファイルと内容'), h('th', {}, '新しい名前'), h('th', {}, '移動先'))),
        h('tbody', {}, fs.map(row)))))),
  );
  updateCount();
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
