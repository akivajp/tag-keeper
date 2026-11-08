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

function browseHref(root, path, at, file) {
  const q = new URLSearchParams();
  if (at) q.set('at', at);
  if (file) q.set('file', file);
  return `#/browse/${enc(root)}${path ? '/' + encPath(path) : ''}${q.toString() ? `?${q}` : ''}`;
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
    onRemove ? h('button', { class: 'chip-x', title: t('タグを外す'), onclick: (e) => { e.stopPropagation(); onRemove(); } }, '×') : null);
}

/** 既存のタグの候補（入力の補完用）。 */
async function tagDatalist() {
  const { tags } = await api('/api/tags');
  const dl = h('datalist', { id: 'tag-options' }, tags.map((tg) => h('option', { value: tg.tag })));
  return dl;
}

async function editTags(root, paths, tag, op) {
  tag = (tag || '').trim();
  if (!tag) { toast('タグの名前を入れてください', 'warn'); return false; }
  try {
    const r = await api('/api/tags', { root, paths, tag, op });
    toast(op === 'remove' ? t('「{tag}」を {n} 件から外しました', { tag, n: r.changed }) : t('「{tag}」を {n} 件に付けました', { tag, n: r.changed }));
    return true;
  } catch (e) { toast(e.message, 'danger'); return false; }
}

// ---------- 左右のペインの境目・並べ替え（ファイルブラウザ・整理・プランで共通） ----------

/** 左右のペイン。境目をドラッグ（または ←→）して幅を変え、画面ごとにブラウザに覚える。 */
function splitPane(key, left, right, defaultPct = 55) {
  const storeKey = `tag-keeper-split-${key}`;
  const clamp = (v) => Math.min(80, Math.max(20, v));
  let pct = clamp(Number(loadLocal(storeKey, defaultPct)) || defaultPct);
  const box = h('div', { class: 'browse' });
  const apply = () => box.style.setProperty('--split', `${pct}%`);
  const handle = h('div', {
    class: 'splitter', role: 'separator', tabindex: '0', 'aria-orientation': 'vertical',
    title: t('ドラッグで幅を変える（ダブルクリックで元に戻す）'),
  });
  handle.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add('dragging');
    document.body.classList.add('resizing');
    const move = (ev) => {
      const rect = box.getBoundingClientRect();
      pct = clamp(((ev.clientX - rect.left) / rect.width) * 100);
      apply();
    };
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.classList.remove('dragging');
      document.body.classList.remove('resizing');
      saveLocal(storeKey, Math.round(pct * 10) / 10);
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
  });
  handle.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    pct = clamp(pct + (e.key === 'ArrowLeft' ? -2 : 2));
    apply();
    saveLocal(storeKey, pct);
  });
  handle.addEventListener('dblclick', () => { pct = defaultPct; apply(); saveLocal(storeKey, pct); });
  apply();
  setChildren(box, left, handle, right);
  return box;
}

/** 名前の自然な順（「2」が「10」より前）で比べる。 */
function naturalCompare(a, b) {
  return new Intl.Collator(i18n.locale, { numeric: true, sensitivity: 'base' }).compare(a, b);
}

/** 並べ替えの状態（項目と向き）を、画面ごとにブラウザに覚える。 */
function sortState(key, def) {
  const state = loadLocal(`tag-keeper-sort-${key}`, def);
  return {
    field: state.field || def.field,
    dir: state.dir === 'desc' ? 'desc' : state.dir === 'asc' ? 'asc' : def.dir,
    toggle(field) {
      if (this.field === field) this.dir = this.dir === 'asc' ? 'desc' : 'asc';
      else { this.field = field; this.dir = field === 'name' || field === 'path' || field === 'folder' ? 'asc' : 'desc'; }
      saveLocal(`tag-keeper-sort-${key}`, { field: this.field, dir: this.dir });
    },
  };
}

/** 並べ替えのできる見出し。押すと項目を選び、同じ項目をもう一度押すと向きを変える。 */
function sortHeader(label, field, state, onChange, cls = '') {
  const arrow = state.field === field ? (state.dir === 'asc' ? '▲' : '▼') : '';
  return h('th', { class: `sortable ${cls}`, title: t('押すと並べ替え'), onclick: () => { state.toggle(field); onChange(); } },
    label, arrow ? h('span', { class: 'arrow' }, arrow) : null);
}

/** 項目ごとの値の取り出し方で並べ替える（値が同じなら名前の自然な順）。 */
function sortItems(items, state, getters, nameOf) {
  const get = getters[state.field] || getters.name;
  const sign = state.dir === 'asc' ? 1 : -1;
  return [...items].sort((a, b) => {
    const va = get(a), vb = get(b);
    const c = typeof va === 'string' || typeof vb === 'string' ? naturalCompare(String(va ?? ''), String(vb ?? '')) : (va ?? -Infinity) - (vb ?? -Infinity);
    return c * sign || naturalCompare(nameOf(a), nameOf(b));
  });
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
    data.tags.direct.map((tg) => tagChip(tg.tag, live ? async () => { if (await editTags(root, [path], tg.tag, 'remove')) app.render(); } : null)),
    data.tags.inherited.map((tg) => h('span', { class: 'chip inherited', title: t('{from} から継承', { from: tg.from || root }) }, tg.tag)),
    (data.tags.implied || []).map((tg) => h('span', { class: 'chip implied', title: t('{via} の親', { via: tg.via }) }, tg.tag)),
    !data.tags.direct.length && !data.tags.inherited.length ? h('span', { class: 'muted' }, 'タグなし') : null);
  // このフォルダの属性（付けた値は配下が継承する）。定義が1つも無ければ出さない
  const folderAttrs = live && path && (data.attr_defs || []).length
    ? h('details', { class: 'folder-attrs' }, h('summary', { class: 'muted small' }, t('このフォルダの属性（配下に継承）')),
      attrEditor(root, path, data.attrs, data.attr_defs, () => app.render()))
    : null;
  // 今のフォルダの型（付けた・継承・推定）とディレクトリカード
  const folderMeta = live && data.dir && data.dir.type ? dirMetaBlock(root, path, data) : null;
  // 今のフォルダをクラウドの Web 画面で開くリンク（あとから読み込む）
  const folderLinks = h('span', { class: 'folder-links' });
  if (live && path) {
    api(`/api/info?root=${enc(root)}&path=${enc(path)}`).then((info) => setChildren(folderLinks, info.cloud_links.length ? cloudLinks(info.cloud_links) : null)).catch(() => {});
  }
  const folderTagInput = h('input', { list: 'tag-options', placeholder: 'このフォルダにタグを付ける（配下に継承）', disabled: !live || !path });

  // 並べ替え（フォルダは常に先。項目と向きはブラウザに覚える）
  const sort = sortState('browse', { field: 'name', dir: 'asc' });
  const getters = {
    name: (e) => e.name,
    tags: (e) => e.tags.length,
    size: (e) => (e.is_dir ? null : e.size),
    mtime: (e) => (e.mtime ? Date.parse(e.mtime) : null),
  };
  const headRow = h('tr');
  function renderHead() {
    setChildren(headRow, h('th', {}, ''),
      sortHeader(t('名前'), 'name', sort, renderRows),
      sortHeader(t('タグ'), 'tags', sort, renderRows, 'hide-narrow'),
      sortHeader(t('サイズ'), 'size', sort, renderRows, 'num'),
      sortHeader(t('更新日時'), 'mtime', sort, renderRows, 'num hide-narrow'));
  }

  function renderRows() {
    renderHead();
    const f = filter.toLowerCase();
    const shown = data.entries.filter((e) => !f || e.name.toLowerCase().includes(f));
    const dirs = sortItems(shown.filter((e) => e.is_dir), sort, getters, (e) => e.name);
    const files = sortItems(shown.filter((e) => !e.is_dir), sort, getters, (e) => e.name);
    setChildren(tableBody, [...dirs, ...files].map((e) => {
      const rel = joinPath(path, e.name);
      const box = h('input', {
        type: 'checkbox', disabled: !live, checked: selected.has(rel),
        onchange: (ev) => { if (ev.target.checked) selected.add(rel); else selected.delete(rel); updateSel(); },
      });
      const name = e.is_dir
        ? h('a', { href: browseHref(root, rel, at) }, '📁 ', e.name)
        : h('a', { href: '#', onclick: (ev) => { ev.preventDefault(); showDetail(rel, e); } }, fileIcon(e.name), ' ', e.name);
      const row = h('tr', { class: e.exists_now === false ? 'gone' : '' },
        h('td', {}, box),
        h('td', { class: 'path' }, name,
          e.inbox && !(e.dir_type && e.dir_type.type === 'inbox') ? [' ', h('span', { class: 'badge warn' }, '受け皿')] : null,
          e.dir_type ? [' ', h('span', { class: `badge ${e.dir_type.how === 'set' ? 'info' : ''}`, title: e.dir_type.how === 'set' ? t('付けた型') : t('推定: {why}', { why: e.dir_type.source }) }, t(e.dir_type.label))] : null,
          e.exists_now === false ? [' ', h('span', { class: 'badge danger' }, '今は無い')] : null),
        h('td', { class: 'hide-narrow' }, h('div', { class: 'chips' }, e.tags.map((tg) => h('span', { class: 'chip' }, tg)))),
        h('td', { class: 'num' }, e.is_dir ? '' : size(e.size)),
        h('td', { class: 'num hide-narrow' }, when(e.mtime)));
      if (live) {
        // ドラッグで移動: 選んでいる行をつかんだら選んだもの全部、そうでなければその行だけ
        row.draggable = true;
        row.addEventListener('dragstart', (ev) => {
          const paths = selected.has(rel) ? [...selected] : [rel];
          ev.dataTransfer.setData(DRAG_TYPE, JSON.stringify({ root, paths }));
          ev.dataTransfer.effectAllowed = 'move';
        });
        if (e.is_dir) dropTarget(row, rel);
      }
      return row;
    }));
  }

  function updateSel() { selInfo.textContent = selected.size ? t('{n} 件を選択中', { n: num(selected.size) }) : ''; }

  const tagInput = h('input', { list: 'tag-options', placeholder: '選んだものに付けるタグ（例: 種別:請求書）', disabled: !live });

  async function showDetail(rel, entry) {
    setChildren(detail, h('p', { class: 'muted' }, '読み込み中…'));
    const name = rel.split('/').pop();
    const url = fileUrl(root, rel, at);
    const parts = [h('h2', { class: 'path' }, name), h('div', { class: 'muted mono' }, rel)];
    parts.push(h('div', { class: 'buttons' },
      h('a', { class: 'button', href: url, target: '_blank', rel: 'noopener' }, '新しいタブで開く'),
      h('a', { class: 'button', href: fileUrl(root, rel, at, true) }, 'ダウンロード'),
      live ? h('button', { onclick: () => renameDialog(root, rel, false) }, '名前を変える…') : null,
      live ? h('button', { class: 'danger-outline', onclick: () => deleteDialog(root, [{ path: rel, is_dir: false, size: entry.size }]) }, '削除…') : null));
    parts.push(preview(url, name, entry.size, { root, path: rel, snapshot: at }));
    if (live) {
      try {
        const info = await api(`/api/info?root=${enc(root)}&path=${enc(rel)}`);
        if (info.cloud_links.length) parts.splice(3, 0, cloudLinks(info.cloud_links));
        const input = h('input', { list: 'tag-options', placeholder: 'タグを付ける' });
        parts.push(h('h3', {}, 'タグ'),
          h('div', { class: 'chips' },
            info.tags.direct.map((tg) => tagChip(tg.tag, async () => { if (await editTags(root, [rel], tg.tag, 'remove')) { await app.render(); showDetail(rel, entry); } })),
            info.tags.inherited.map((tg) => h('span', { class: 'chip inherited', title: t('{from} から継承', { from: tg.from || root }) }, tg.tag)),
            (info.tags.implied || []).map((tg) => h('span', { class: 'chip implied', title: t('{via} の親', { via: tg.via }) }, tg.tag))),
          h('form', { class: 'inline', onsubmit: async (ev) => { ev.preventDefault(); if (await editTags(root, [rel], input.value, 'add')) { await app.render(); showDetail(rel, entry); } } },
            input, h('button', {}, '付ける')));
        if ((info.attr_defs || []).length) {
          parts.push(h('h3', {}, '属性'), attrEditor(root, rel, info.attrs, info.attr_defs, async () => { await app.render(); showDetail(rel, entry); }));
        }
        parts.push(h('h3', {}, '関係'), relationEditor(root, rel, info, () => showDetail(rel, entry)));
        // 音声・動画の書き起こし（全文検索の索引に入っている）
        if (MEDIA_EXTS.includes((name.split('.').pop() || '').toLowerCase())) {
          const x = info.extract;
          const startTranscribe = async () => {
            const job = await startJob(`/api/transcribe/${enc(root)}`, { paths: [rel] });
            if (job) toast(t('書き起こしを始めました'));
          };
          parts.push(h('h3', {}, '書き起こし'),
            x && x.method === 'transcript' ? [h('p', { class: 'muted small' }, x.note), h('pre', { class: 'preview text transcript' }, x.text || t('（声がありませんでした）')),
              h('button', { class: 'small', onclick: startTranscribe }, t('書き起こし直す'))]
              : x && (x.method === 'error' || x.method === 'none') ? [h('p', { class: 'muted small' }, x.note), h('button', { class: 'small', onclick: startTranscribe }, t('もう一度書き起こす'))]
                : h('button', { class: 'small', onclick: startTranscribe }, t('書き起こす（文字にして検索できるようにする）')));
        }
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
      }, t('このファイルの整理の提案を出す（{model}）', { model: models.current() || t('既定のモデル') })));
      parts.push(h('h3', {}, '整理の提案'), sugHolder);
      // 書類・画像の読み取り（結果は「確認」の画面に出る）
      parts.push(h('div', { class: 'buttons' }, h('button', { class: 'small', onclick: async () => {
        const job = await startJob(`/api/analyze/${enc(root)}`, { paths: [rel] });
        if (job) toast(t('読み取りを始めました。結果は「確認」の画面に出ます'));
      } }, t('この書類・画像を読み取る（種類・日付・相手・金額など）'))));
    }
    parts.push(h('h3', {}, '版の履歴'), await historyBlock(rel));
    setChildren(detail, parts);
  }

  async function historyBlock(rel) {
    try {
      const { versions } = await api(`/api/history?root=${enc(root)}&path=${enc(rel)}`);
      const rows = [...versions].reverse().map((v) => {
        const label = v.is_live ? '今' : `${when(v.first_seen)} 〜 ${when(v.last_seen)}`;
        const moved = v.relpath !== rel ? h('div', { class: 'muted path' }, t('（このときの場所: {path}）', { path: v.relpath })) : null;
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
      return h('p', { class: 'muted' }, t('履歴を読めません: {error}', { error: e.message }));
    }
  }

  async function restore(rel, v) {
    const ok = await confirmDialog('過去の版を復元しますか？',
      h('p', {}, t('{when} の時点の版を、今のファイルの隣に日時付きの別名で置きます。今のファイルは上書きしません。', { when: when(v.last_seen) })), '復元する');
    if (!ok) return;
    try {
      const r = await api('/api/restore', { root, path: rel, version_path: v.relpath, snapshot: v.snapshot_id });
      toast(t('復元しました: {path}', { path: r.restored }));
      app.render();
    } catch (e) { toast(e.message, 'danger'); }
  }

  setChildren(main,
    datalist,
    h('datalist', { id: `folders-${enc(root)}` }),
    h('div', { class: 'crumbs' }, data.crumbs.map((c, i) => [i ? ' / ' : '', live && c.path !== path ? dropTarget(h('a', { href: browseHref(root, c.path, at) }, c.name), c.path) : h('a', { href: browseHref(root, c.path, at) }, c.name)])),
    h('div', { class: 'toolbar' },
      h('label', {}, '時点: ', timeSel),
      at ? h('span', { class: 'badge warn' }, '過去の時点を表示中（読み取りのみ）') : null,
      h('input', { type: 'search', placeholder: '名前で絞り込む', oninput: (e) => { filter = e.target.value; renderRows(); } })),
    h('div', { class: 'card folder-tags' }, folderLinks, h('strong', {}, 'このフォルダのタグ: '), folderTags,
      live && path ? h('form', { class: 'inline', onsubmit: async (ev) => { ev.preventDefault(); if (await editTags(root, [path], folderTagInput.value, 'add')) app.render(); } },
        folderTagInput, h('button', {}, '付ける')) : null,
      folderAttrs, folderMeta),
    live ? h('form', { class: 'toolbar', onsubmit: (ev) => ev.preventDefault() },
      tagInput,
      h('button', { onclick: async () => { if (!selected.size) { toast('ファイルかフォルダを選んでください', 'warn'); return; } if (await editTags(root, [...selected], tagInput.value, 'add')) app.render(); } }, '選んだものに付ける'),
      h('button', { onclick: async () => { if (!selected.size) { toast('ファイルかフォルダを選んでください', 'warn'); return; } if (await editTags(root, [...selected], tagInput.value, 'remove')) app.render(); } }, '外す'),
      h('button', { onclick: () => {
        if (selected.size !== 1) { toast('名前を変えるものを1つだけ選んでください', 'warn'); return; }
        const rel = [...selected][0];
        renameDialog(root, rel, !!data.entries.find((e) => joinPath(path, e.name) === rel)?.is_dir);
      } }, '名前を変える…'),
      h('button', { class: 'danger-outline', onclick: () => {
        if (!selected.size) { toast('削除するものを選んでください', 'warn'); return; }
        deleteDialog(root, [...selected].map((rel) => {
          const e = data.entries.find((x) => joinPath(path, x.name) === rel);
          return { path: rel, is_dir: !!e?.is_dir, size: e?.size };
        }));
      } }, '削除…'),
      h('button', { onclick: () => {
        if (!selected.size) { toast(t('移動するものを選んでください'), 'warn'); return; }
        moveDialog(root, [...selected], path);
      } }, '移動…'),
      h('button', { onclick: () => mkdirDialog(root, path) }, 'フォルダを作る…'),
      selInfo) : null,
    splitPane('browse',
      h('div', { class: 'card table-wrap' }, h('table', {}, h('thead', {}, headRow), tableBody)),
      detail));
  renderRows();
  // ?file=名前 なら、そのファイルの詳細を開いておく（検索の結果の「フォルダを開く」から来たとき）
  const picked = params.get('file');
  const pickedEntry = picked ? data.entries.find((e) => e.name === picked && !e.is_dir) : null;
  if (pickedEntry) showDetail(joinPath(path, picked), pickedEntry);
}

// ---------- フォルダの作成・移動（ファイルブラウザから） ----------

const DRAG_TYPE = 'application/x-tag-keeper';

/** 要素を移動先にする（一覧の行やパンくずを、そのフォルダへのドロップ先にする）。 */
function dropTarget(el, dest) {
  el.addEventListener('dragover', (ev) => {
    if (!ev.dataTransfer.types.includes(DRAG_TYPE)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    el.classList.add('drop-over');
  });
  el.addEventListener('dragleave', () => el.classList.remove('drop-over'));
  el.addEventListener('drop', (ev) => {
    el.classList.remove('drop-over');
    const raw = ev.dataTransfer.getData(DRAG_TYPE);
    if (!raw) return;
    ev.preventDefault();
    const { root, paths } = JSON.parse(raw);
    const moving = paths.filter((p) => p !== dest && !dest.startsWith(`${p}/`));
    if (moving.length) moveConfirm(root, moving, dest);
  });
  return el;
}

/** 移動の確認（移すものと移動先を見せる）。 */
async function moveConfirm(root, paths, dest) {
  const ok = await confirmDialog(t('{n} 件を移動しますか？', { n: paths.length }), [
    h('ul', { class: 'small' }, paths.slice(0, 20).map((p) => h('li', { class: 'path' }, p))),
    paths.length > 20 ? h('p', { class: 'muted' }, t('ほか {n} 件', { n: paths.length - 20 })) : null,
    h('p', {}, t('移動先: {dest}', { dest: dest || '/' })),
    h('p', { class: 'muted small' }, t('実行の前後にスナップショットを撮り、プランの画面から取り消せます。')),
  ], t('移動する'));
  if (ok) await runFileOp(root, 'move', paths.map((path) => ({ path })), { dest_dir: dest });
}

/** 移動のダイアログ（移動先のフォルダを入力すると候補が出る。まだ無いフォルダなら作ってから移す）。 */
async function moveDialog(root, paths, current) {
  const input = h('input', { class: 'name-input', value: current, list: `folders-${enc(root)}`, placeholder: t('移動先のフォルダ（/ でルート直下）') });
  input.addEventListener('input', () => folderSuggest(root, input.value));
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); document.getElementById('confirm').close('ok'); } });
  folderSuggest(root, current);
  setTimeout(() => { input.focus(); input.select(); }, 50);
  const ok = await confirmDialog(t('{n} 件を移動しますか？', { n: paths.length }), [
    h('ul', { class: 'small' }, paths.slice(0, 20).map((p) => h('li', { class: 'path' }, p))),
    paths.length > 20 ? h('p', { class: 'muted' }, t('ほか {n} 件', { n: paths.length - 20 })) : null,
    h('h3', {}, t('移動先')), input,
    h('p', { class: 'muted small' }, t('まだ無いフォルダを入れると、作ってから移します。一覧の行をフォルダの行やパンくずへドラッグしても移せます。')),
  ], t('移動する'));
  const dest = input.value.trim().replace(/^\/+|\/+$/g, '');
  if (!ok || dest === current && paths.every((p) => p.split('/').slice(0, -1).join('/') === current)) return;
  await runFileOp(root, 'move', paths.map((path) => ({ path })), { dest_dir: dest });
}

/** フォルダの作成のダイアログ。 */
async function mkdirDialog(root, parent) {
  const input = h('input', { class: 'name-input', placeholder: t('新しいフォルダの名前') });
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); document.getElementById('confirm').close('ok'); } });
  setTimeout(() => input.focus(), 50);
  const ok = await confirmDialog(t('フォルダを作る'), [h('p', { class: 'muted path' }, `${parent || '/'}`), input], t('作る'));
  const name = input.value.trim();
  if (!ok || !name) return;
  try {
    const r = await api('/api/fileops', { root, op: 'mkdir', parent, name });
    toast(t('フォルダを作りました: {path}', { path: r.created }));
    app.render();
  } catch (e) { toast(e.message, 'danger'); }
}

// ---------- 名前の変更・削除（ファイルブラウザから） ----------

/** 名前の変更・削除の API を呼び、実行のジョブの進み具合を表示する。取り消しはプランの画面から。 */
async function runFileOp(root, op, items, extra = {}) {
  try {
    const r = await api('/api/fileops', { root, op, items, ...extra });
    // 名前を変えた・削除したものは、採用リストに残っていても実行できないので外す
    for (const it of items) cart.remove(root, it.path);
    app.job = r.job;
    app.jobDismissed = null;
    renderJob();
    pollJob();
    const link = h('a', { href: `#/plans/${r.plan_id}` }, 'プランの画面');
    const holder = document.getElementById('alerts');
    // お知らせは最新の1つだけ残す（続けて操作しても積み重ならないように）
    holder.querySelectorAll('.fileop-note').forEach((n) => n.remove());
    const note = h('div', { class: 'alert info fileop-note' }, { rename: '名前を変えています。', move: '移動しています。' }[op] || '隔離フォルダへ移しています。', '取り消すには ', link, ' から。');
    holder.prepend(note);
    setTimeout(() => note.remove(), 15000);
    return true;
  } catch (e) { toast(e.message, 'danger'); return false; }
}

/** 名前の変更のダイアログ（好きな名前を付けられる。提案の名前を使うこともできる）。 */
async function renameDialog(root, rel, isDir) {
  const name = rel.split('/').pop();
  const input = h('input', { class: 'name-input', value: name });
  // Enter で「実行する」にする（フォームの既定は最初のボタン＝「やめる」になるため）
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); document.getElementById('confirm').close('ok'); } });
  const useSuggestion = isDir ? null : h('button', {
    type: 'button',
    onclick: async () => {
      try {
        const q = new URLSearchParams({ root, path: rel, model: models.current() });
        const { suggestion } = await api(`/api/suggestion?${q}`);
        if (suggestion) input.value = suggestion.new_name;
        else toast('まだ提案がありません（詳細の欄の「整理の提案を出す」で作れます）', 'warn');
      } catch (e) { toast(e.message, 'danger'); }
    },
  }, '提案の名前を使う');
  const body = [
    h('p', { class: 'muted path' }, rel),
    input,
    h('div', { class: 'buttons', style: 'margin-top:.5rem' }, useSuggestion),
    h('p', { class: 'muted small' }, isDir
      ? 'フォルダの中のタグとカタログの記録も新しい名前に追従します。'
      : '拡張子も含めて入力してください。'),
    h('p', { class: 'muted small' }, '実行の前後にスナップショットを撮り、プランの画面から取り消せます。'),
  ];
  setTimeout(() => {
    input.focus();
    // 拡張子の前までを選んでおく（題名だけを打ち直しやすいように）
    const dot = isDir ? -1 : name.lastIndexOf('.');
    input.setSelectionRange(0, dot > 0 ? dot : name.length);
  }, 50);
  if (!await confirmDialog(isDir ? 'フォルダの名前を変える' : 'ファイルの名前を変える', body, '名前を変える')) return;
  const newName = input.value.trim();
  if (!newName || newName === name) return;
  await runFileOp(root, 'rename', [{ path: rel, new_name: newName }]);
}

/** 削除のダイアログ（完全には消さず、隔離フォルダへ移す）。 */
async function deleteDialog(root, items) {
  const dirs = items.filter((it) => it.is_dir).length;
  const body = [
    h('ul', { class: 'small' }, items.slice(0, 20).map((it) => h('li', { class: 'path' }, it.is_dir ? '📁 ' : '', it.path, it.is_dir ? t('/（中身ごと）') : ''))),
    items.length > 20 ? h('p', { class: 'muted' }, t('ほか {n} 件', { n: items.length - 20 })) : null,
    h('p', {}, '完全には消さず、隔離フォルダへ移します。同期しているクラウドからは消えますが、このマシンには隔離フォルダを空にするまで残り、プランの画面から元に戻せます。'),
    dirs ? h('p', { class: 'muted small' }, '中身の多いフォルダは、同期クライアントの大量削除の安全装置で同期が止まらないよう、同期を一時停止してから反映します。') : null,
  ];
  if (!await confirmDialog(t('{n} 件を削除しますか？', { n: items.length }), body, '削除する', true)) return;
  await runFileOp(root, 'delete', items.map((it) => ({ path: it.path })));
}

// ---------- クラウドの Web 画面で開くリンク ----------

/** OneDrive で開く・ブラウザ版の Office で開く、などのリンクの並び。 */
function cloudLinks(links) {
  return h('div', { class: 'buttons cloud-links' }, links.map((l) => h('a', {
    class: `button small ${l.kind}`, href: l.url, target: '_blank', rel: 'noopener noreferrer',
  }, l.kind === 'office' ? '✎ ' : '☁ ', t(l.label))));
}

// ---------- ブラウザが直接は再生できない動画のプレビュー ----------

// 書き起こせる種類（tag_keeper.transcribe.MEDIA_EXTS と同じ）
const MEDIA_EXTS = ['mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg', 'opus', 'wma', 'mp4', 'm4v', 'mov', 'webm', 'mkv', 'avi', 'flv', 'wmv', 'mpg', 'mpeg', 'ts', 'm2ts', 'mts', '3gp'];
const CONVERT_VIDEO_EXTS = ['flv', 'f4v', 'mkv', 'avi', 'wmv', 'asf', 'mpg', 'mpeg', 'ts', 'm2ts', 'mts', '3gp', 'vob', 'divx'];

/**
 * flv・mkv・avi などは、サーバーが ffmpeg で再生用の MP4 にしてから再生する。
 * 入れ物だけを替えられるものは数秒で、作り直すものは進み具合（%）を出しながら待つ。
 */
function mediaPreview(ctx, name) {
  const holder = h('div', { class: 'media-view' }, h('p', { class: 'muted' }, t('読み込み中…')));
  const q = new URLSearchParams({ root: ctx.root, path: ctx.path });
  if (ctx.snapshot) q.set('snapshot', ctx.snapshot);
  let timer = null;

  async function poll(retry = false) {
    clearTimeout(timer);
    if (!holder.isConnected && holder.dataset.started) return; // 別のファイルを選んだら問い合わせをやめる
    holder.dataset.started = '1';
    let st;
    try { st = await api(`/api/media/status?${q}${retry ? '&retry=1' : ''}`); }
    catch (e) { setChildren(holder, h('p', { class: 'muted' }, t('この種類はプレビューできません。'), ' ', e.message)); return; }
    if (st.state === 'done') {
      setChildren(holder, h('video', { class: 'preview', src: `/api/media?${q}`, controls: true, preload: 'metadata' }),
        h('p', { class: 'muted small' }, t('再生用に MP4 にしたものを再生しています（元のファイルはそのまま）')));
      return;
    }
    if (st.state === 'error') {
      setChildren(holder, h('div', { class: 'alert warn' }, t('再生用に変換できませんでした: {error}', { error: st.error })),
        h('button', { class: 'small', onclick: () => poll(true) }, t('もう一度')));
      return;
    }
    const pct = Math.round(st.progress * 100);
    setChildren(holder,
      h('p', { class: 'muted' }, st.mode === 'transcode'
        ? t('再生用に作り直しています（{pct}%）…', { pct })
        : t('再生用に MP4 にしています…')),
      h('div', { class: 'bar' }, h('div', { style: `width:${pct}%` })));
    timer = setTimeout(() => poll(), 1000);
  }
  poll();
  return holder;
}

// ---------- Office 文書のプレビュー ----------

const OFFICE_EXTS = ['docx', 'docm', 'xlsx', 'xlsm', 'pptx', 'pptm'];

/**
 * Office 文書のプレビュー。LibreOffice があれば PDF に変換したもの（レイアウトどおり）を既定にし、
 * 中身の文字と表だけを出す表示（速い）にも切り替えられる。
 */
function officePreview(ctx, name) {
  const holder = h('div', { class: 'office-view', translate: 'no' }, h('p', { class: 'muted' }, t('読み込み中…')));
  const q = new URLSearchParams({ root: ctx.root, path: ctx.path });
  if (ctx.snapshot) q.set('snapshot', ctx.snapshot);
  const tabs = h('div', { class: 'buttons office-tabs' });
  const box = h('div', {}, tabs, holder);

  async function showStructure() {
    setChildren(holder, h('p', { class: 'muted' }, t('読み込み中…')));
    try { setChildren(holder, renderOffice(await api(`/api/office?${q}`), (part) => `/api/office/image?${q}&part=${enc(part)}`)); }
    catch (e) { setChildren(holder, h('p', { class: 'muted' }, t('この種類はプレビューできません。'), ' ', e.message)); }
  }
  function showPdf() {
    setChildren(holder, h('p', { class: 'muted small' }, t('PDF に変換しています（初回は少しかかります）…')),
      h('iframe', { class: 'preview pdf', src: `/api/office/pdf?${q}`, title: name }));
  }
  (async () => {
    const st = app.state || await refreshState().catch(() => null);
    if (st && st.office_pdf) {
      setChildren(tabs,
        h('button', { class: 'small', onclick: showPdf }, t('レイアウトどおり（PDF）')),
        h('button', { class: 'small', onclick: showStructure }, t('文字と表だけ（速い）')));
      showPdf();
    } else {
      showStructure();
    }
  })();
  return box;
}

/** /api/office の答えを画面の要素にする（文字は textContent で入れるので安全）。 */
function renderOffice(data, imageUrl) {
  if (data.kind === 'xlsx') {
    const view = h('div');
    const tabs = h('div', { class: 'buttons sheet-tabs' });
    const show = (i) => {
      const sh = data.sheets[i];
      tabs.querySelectorAll('button').forEach((b, j) => b.classList.toggle('active', j === i));
      const width = Math.max(1, ...sh.rows.map((r) => r.length));
      const colName = (n) => { let s = ''; for (n += 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s; return s; };
      setChildren(view, h('div', { class: 'table-wrap sheet' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, ''), Array.from({ length: width }, (_, c) => h('th', {}, colName(c))))),
        h('tbody', {}, sh.rows.map((r, i2) => h('tr', {}, h('th', {}, String(i2 + 1)), Array.from({ length: width }, (_, c) => h('td', {}, r[c] ?? ''))))))),
      sh.truncated ? h('p', { class: 'muted small' }, t('表示しきれない部分は省いています')) : null);
    };
    setChildren(tabs, data.sheets.map((sh, i) => h('button', { class: 'small', onclick: () => show(i) }, sh.name || `#${i + 1}`)));
    if (data.sheets.length) show(0);
    return h('div', {}, data.sheets.length > 1 ? tabs : null, view);
  }
  if (data.kind === 'docx') {
    return h('div', { class: 'doc' }, data.blocks.map((b) => {
      if (b.type === 'more') return h('p', { class: 'muted small' }, t('表示しきれない部分は省いています'));
      if (b.type === 'image') return h('img', { class: 'doc-image', src: imageUrl(b.part), alt: '', loading: 'lazy' });
      if (b.type === 'table') {
        return h('div', { class: 'table-wrap' }, h('table', { class: 'doc-table' }, h('tbody', {}, b.rows.map((r) => h('tr', {}, r.map((c) => h('td', {}, c)))))));
      }
      const runs = b.runs.map((r) => (r.b || r.i ? h(r.b ? 'strong' : 'em', {}, r.t) : r.t));
      if (!runs.length) return h('p', { class: 'empty' }, ' ');
      return h(b.level ? `h${Math.min(6, b.level + 2)}` : 'p', {}, runs);
    }));
  }
  if (data.kind === 'pptx') {
    return h('div', { class: 'slides' }, data.slides.map((sl, i) => h('section', { class: 'slide' },
      h('div', { class: 'muted small' }, `#${i + 1}`),
      sl.title ? h('h3', {}, sl.title) : null,
      sl.paragraphs.length ? h('ul', {}, sl.paragraphs.map((x) => h('li', {}, x))) : null,
      (sl.images || []).map((part) => h('img', { class: 'doc-image', src: imageUrl(part), alt: '', loading: 'lazy' })))));
  }
  return h('p', { class: 'muted' }, t('この種類はプレビューできません。'));
}

function fileIcon(name) {
  const ext = name.split('.').pop().toLowerCase();
  if (['pdf'].includes(ext)) return '📕';
  if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic'].includes(ext)) return '🖼';
  if (['mp4', 'mov', 'mkv', 'webm', 'flv', 'avi', 'wmv', 'mpg', 'ts', 'm4a', 'mp3', 'wav'].includes(ext)) return '🎞';
  if (['xlsx', 'xls', 'csv'].includes(ext)) return '📊';
  if (['docx', 'doc', 'txt', 'md'].includes(ext)) return '📄';
  if (['zip', '7z', 'rar', 'tar', 'gz'].includes(ext)) return '🗜';
  return '📄';
}

/** ファイルの種類に合わせたプレビュー（PDF・画像・音声・動画・テキスト）。 */
function preview(url, name, bytes, ctx = null) {
  const ext = name.split('.').pop().toLowerCase();
  if (ctx && OFFICE_EXTS.includes(ext)) return officePreview(ctx, name);
  if (ctx && CONVERT_VIDEO_EXTS.includes(ext)) return mediaPreview(ctx, name);
  if (['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'].includes(ext)) return h('img', { class: 'preview', src: url, alt: name });
  if (ext === 'pdf') return h('iframe', { class: 'preview pdf', src: url, title: name });
  if (['mp4', 'webm', 'mov', 'm4v'].includes(ext)) {
    // 中が HEVC などでブラウザが再生できなければ、変換して再生に切り替える
    // 詳細の欄が組み上がる前にエラーになることもあるので、入れ物の中身を替える（要素そのものは差し替えない）
    const video = h('video', { class: 'preview', src: url, controls: true, preload: 'metadata' });
    const box = h('div', {}, video);
    if (ctx) video.addEventListener('error', () => setChildren(box, mediaPreview(ctx, name)), { once: true });
    return box;
  }
  if (['m4a', 'mp3', 'wav', 'ogg'].includes(ext)) return h('audio', { src: url, controls: true, preload: 'metadata' });
  if (['txt', 'md', 'csv', 'tsv', 'log', 'json', 'xml', 'html', 'htm', 'yaml', 'yml', 'toml', 'ini', 'py', 'sh', 'rdp', 'set', 'mq4'].includes(ext) && (bytes ?? 0) < 512 * 1024) {
    const pre = h('pre', { class: 'preview text' }, '読み込み中…');
    fetch(url).then((r) => r.text()).then((tg) => { pre.textContent = tg.slice(0, 200000); }).catch(() => { pre.textContent = '読めません'; });
    return pre;
  }
  return h('p', { class: 'muted' }, 'この種類はプレビューできません。');
}

// ---------- 画面: タグ（一覧・名前の変更・削除・複数のタグでの検索） ----------

/** 全文検索の索引の状況（抽出済み・抽出待ち・OCR を使えるか）を1行で出す。読めなければ何も出さない。 */
async function indexStatus(root) {
  let st;
  try { st = await api(`/api/extract/status?root=${enc(root)}`); } catch (e) { return null; }
  const parts = [t('本文の索引: {n} 件', { n: num(st.indexed) })];
  if (st.pending) parts.push(t('抽出待ち {n} 件', { n: num(st.pending) }));
  parts.push(st.ocr ? t('OCR: 使える（{lang}）', { lang: st.ocr })
    : st.ocr_enabled ? t('OCR: tesseract が無いので使えません') : t('OCR: 使わない設定'));
  if (st.waiting_ocr) parts.push(t('OCR を待っているスキャン {n} 件', { n: num(st.waiting_ocr) }));
  if (!st.enabled) parts.push(t('裏での抽出は止めてあります（[extract] enabled）'));
  else if (!st.running) parts.push(t('裏での抽出は Web の画面（serve）が動かします'));
  return h('p', { class: 'muted small' }, parts.join(' ／ '));
}

/**
 * 文の中の検索語に印（<mark>）を付けた要素の並びを返す。大文字・小文字は区別しない。
 * 本文は索引に入れるときに NFKC でそろえてあるので、検索語も同じようにそろえてから探す。
 * 要素は h() と文字列で組み立てる（innerHTML は使わない）。
 */
function highlight(text, query) {
  const terms = query.normalize('NFKC').split(/\s+/).filter(Boolean)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!terms.length) return [text];
  const re = new RegExp(`(${terms.join('|')})`, 'gi');
  return text.split(re).map((part, i) => (i % 2 ? h('mark', {}, part) : part));
}

// 検索の「種類」の選択肢（値はサーバーの fulltext.KINDS のキー）
const SEARCH_KINDS = [['', 'すべての種類'], ['document', '書類（PDF・Office・テキスト）'], ['pdf', 'PDF'], ['office', 'Office 文書'],
  ['text', 'テキスト'], ['image', '画像'], ['video', '動画'], ['audio', '音声'], ['archive', '圧縮ファイル']];

// 属性の型の表示名
const ATTR_TYPE_LABELS = { choice: '択一', flag: 'フラグ', date: '日付', number: '数', text: '文字' };

/** 「5MB」「1.5G」「200k」「1234」をバイト数にする。読めなければ null。 */
function parseSize(text) {
  const m = String(text || '').trim().match(/^(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?$/i);
  if (!m) return null;
  const unit = { '': 1, k: 1024, m: 1024 ** 2, g: 1024 ** 3, t: 1024 ** 4 }[m[2].toLowerCase()];
  return Math.round(parseFloat(m[1]) * unit);
}

/** 属性の値を、札に出す文字にする（フラグは名前だけ）。 */
function attrText(name, value) { return value === true ? name : `${name}: ${value}`; }

/**
 * 属性の編集の欄（ファイルブラウザの詳細・フォルダ・検索の結果で共通）。定義ごとに、型に合った入力を出す。
 * 直接の値が無く、祖先のフォルダから継承しているときは、その値と元のフォルダを添える。
 */
function attrEditor(root, path, attrs, defs, onChange) {
  if (!defs.length) return h('p', { class: 'muted small' }, '属性はまだ定義されていません。「検索」の画面で定義できます。');
  const direct = Object.fromEntries(attrs.direct.map((a) => [a.name, a.value]));
  const inherited = Object.fromEntries(attrs.inherited.map((a) => [a.name, a]));
  const set = async (name, value) => {
    try {
      await api('/api/attrs', { root, paths: [path], name, value });
      if (onChange) await onChange();
    } catch (e) { toast(e.message, 'danger'); }
  };
  return h('div', { class: 'attr-editor' }, defs.map((d) => {
    const cur = direct[d.name];
    const inh = cur === undefined ? inherited[d.name] : undefined;
    const hint = inh ? t('（継承: {value}）', { value: inh.value === true ? '✓' : inh.value }) : '';
    let input;
    if (d.type === 'choice') {
      input = h('select', { onchange: (e) => set(d.name, e.target.value || null) },
        h('option', { value: '' }, hint || '—'),
        d.choices.map((c) => h('option', { value: c, selected: cur === c }, c)));
    } else if (d.type === 'flag') {
      input = h('input', { type: 'checkbox', checked: cur === true, onchange: (e) => set(d.name, e.target.checked ? true : null) });
    } else {
      input = h('input', {
        type: d.type === 'date' ? 'date' : 'text', inputmode: d.type === 'number' ? 'decimal' : null,
        value: cur ?? '', placeholder: hint, onchange: (e) => set(d.name, e.target.value.trim() || null),
      });
    }
    return h('label', { class: 'attr-row' },
      h('span', { class: 'attr-name', translate: 'no' }, d.name), input,
      inh ? h('span', { class: 'muted small' }, t('{from} から継承', { from: inh.from || root })) : null);
  }));
}

/**
 * アイテム間の関係の欄（前の版・派生元・添付先など。F-MD-7）。自身から張ったものと、ほかから張られたものを並べ、
 * 種類と相手のパスを入れて足せる。相手のパスは、名前の一部を入れると候補を出す。
 */
function relationEditor(root, path, info, onChange) {
  const rels = info.relations || { outgoing: [], incoming: [] };
  const call = async (payload) => {
    try { await api('/api/relations', { root, ...payload }); if (onChange) await onChange(); }
    catch (e) { toast(e.message, 'danger'); }
  };
  const link = (p) => h('a', { class: 'path', href: browseHref(root, p.split('/').slice(0, -1).join('/'), '', p.split('/').pop()) }, p);
  const listId = `rel-targets-${Math.random().toString(36).slice(2)}`;
  const kindListId = `${listId}-kinds`;
  const targets = h('datalist', { id: listId });
  let timer = null;
  const target = h('input', { list: listId, placeholder: t('相手のファイル（名前の一部で候補が出ます）'),
    oninput: (e) => {
      clearTimeout(timer);
      const word = e.target.value.trim();
      if (word.length < 2) return;
      timer = setTimeout(async () => {
        try {
          const r = await api(`/api/search?root=${enc(root)}&q=${enc(word)}&folders=1&limit=20`);
          setChildren(targets, r.items.filter((x) => x.path !== path).map((x) => h('option', { value: x.path })));
        } catch { /* 候補が出ないだけ */ }
      }, 250);
    } });
  const kind = h('input', { list: kindListId, value: (info.relation_kinds || ['関連'])[0], class: 'kind-input' });
  return h('div', { class: 'relations' },
    targets, h('datalist', { id: kindListId }, (info.relation_kinds || []).map((k) => h('option', { value: k }))),
    rels.outgoing.length || rels.incoming.length ? h('ul', {},
      rels.outgoing.map((r) => h('li', {}, h('span', { class: 'badge' }, r.kind), ' → ', link(r.path), ' ',
        h('button', { class: 'chip-x', title: t('関係を外す'), onclick: () => call({ from: path, to: r.path, kind: r.kind, op: 'remove' }) }, '×'))),
      rels.incoming.map((r) => h('li', {}, link(r.path), ' → ', h('span', { class: 'badge' }, r.kind), t('（このファイルへ）'), ' ',
        h('button', { class: 'chip-x', title: t('関係を外す'), onclick: () => call({ from: r.path, to: path, kind: r.kind, op: 'remove' }) }, '×'))))
      : h('p', { class: 'muted small' }, '関係はまだありません。'),
    h('form', { class: 'inline', onsubmit: (e) => { e.preventDefault(); if (target.value.trim()) call({ from: path, to: target.value.trim(), kind: kind.value.trim() || '関連' }); } },
      kind, target, h('button', {}, '関係を足す')));
}

/** ディレクトリの型の説明（選択肢の下に出す）。 */
const DIR_TYPE_HELP = {
  normal: '特別な扱いなし。上のフォルダから継承した型も、ここで止まります。',
  bundle: '全体で1つのまとまり（リポジトリ・行事の写真など）。中は移動先の候補にしません。',
  app: 'アプリが管理するフォルダ（Vault・ライブラリなど）。中を移動・改名の候補にしません。',
  dated: '年・月などの日付で分けたフォルダ。構造の見直しで、名前の書式のずれを調べます。',
  numbered: '01_xxx のような番号付きのフォルダ。番号の重複・欠番を調べます。',
  inbox: '受け皿。名前に関係なく「整理」の画面の対象にします。',
  archive: '完了したものの保管場所。構造の見直しの対象から外します。',
  exclude: '走査しません（キャッシュなど）。次の走査から、カタログでは消えた扱いになります。',
};

/**
 * 今のフォルダの型とディレクトリカードの欄（ファイルブラウザ）。型は選ぶとすぐ保存し、カードは編集の画面で書く。
 */
function dirMetaBlock(root, path, data) {
  const cur = data.dir.type;
  const how = { set: t('付けた型'), inherited: t('{from} から継承', { from: cur.source || root }), inferred: t('推定: {why}', { why: cur.source }), none: '' }[cur.how];
  const select = h('select', {
    'aria-label': t('このフォルダの型'),
    onchange: async (e) => {
      try { await api('/api/dirtype', { root, path, type: e.target.value }); app.render(); }
      catch (err) { toast(err.message, 'danger'); }
    },
  }, h('option', { value: '', selected: cur.how !== 'set' }, cur.how === 'set' ? t('推定に任せる') : t('（{label}）', { label: t(cur.label) })),
  data.dir_types.map((dt) => h('option', { value: dt.type, selected: cur.how === 'set' && cur.type === dt.type }, t(dt.label))));
  const card = data.dir.card;
  return h('div', { class: 'dir-meta' },
    h('div', { class: 'toolbar' },
      h('strong', {}, t('このフォルダの型: ')), select,
      how ? h('span', { class: 'muted small' }, how) : null,
      h('span', { class: 'muted small' }, t(DIR_TYPE_HELP[cur.type] || ''))),
    h('div', { class: 'toolbar' },
      h('strong', {}, t('ディレクトリカード: ')),
      card ? h('span', {}, card.description, card.naming ? h('span', { class: 'muted small' }, t('（命名: {naming}）', { naming: card.naming })) : null)
        : h('span', { class: 'muted small' }, t('何を置く場所かを書いておくと、整理の提案が移動先を選ぶときに使います。')),
      h('button', { class: 'small', onclick: () => dirCardDialog(root, path, card) }, card ? t('編集…') : t('書く…'))));
}

/** ディレクトリカードを書く画面。モデルに下書きさせることもできる（保存は「保存する」を押したときだけ）。 */
async function dirCardDialog(root, path, card) {
  const desc = h('textarea', { rows: 3, placeholder: t('何を置く場所か（例: 取引先ごとの請求書。発行月のフォルダに置く）') }, card ? card.description : '');
  const naming = h('input', { value: card ? card.naming : '', placeholder: t('命名規則（例: YYYYMMDD_相手_件名）') });
  const status = h('span', { class: 'muted small' });
  const draft = h('button', {
    type: 'button', class: 'small',
    onclick: async () => {
      draft.disabled = true;
      status.textContent = t('下書きを作っています…');
      try {
        const r = await api('/api/dircard/draft', { root, path, model: models.current() });
        if (r.description) desc.value = r.description;
        if (r.naming) naming.value = r.naming;
        status.textContent = r.note ? t('モデルを使えませんでした（{note}）。命名規則だけを推定しました。', { note: r.note }) : t('下書きしました（{model}）。直してから保存してください。', { model: r.model });
      } catch (e) { status.textContent = e.message; }
      draft.disabled = false;
    },
  }, t('モデルに下書きさせる'));
  const ok = await confirmDialog(t('ディレクトリカード: {path}', { path: path || root }), [
    h('label', { class: 'attr-row' }, h('span', { class: 'attr-name' }, t('説明')), desc),
    h('label', { class: 'attr-row' }, h('span', { class: 'attr-name' }, t('命名規則')), naming),
    h('div', { class: 'toolbar' }, draft, status),
    h('p', { class: 'muted small' }, t('両方を空にして保存すると、カードを外します。')),
  ], t('保存する'));
  if (!ok) return;
  try { await api('/api/dircard', { root, path, description: desc.value, naming: naming.value }); app.render(); }
  catch (e) { toast(e.message, 'danger'); }
}

/** 検索の条件（URL の問い合わせ）を読む。保存した検索の件数を数えるときにも使う。 */
function searchState(params) {
  return {
    include: params.getAll('t'),
    exclude: params.getAll('not'),
    mode: params.get('mode') === 'or' ? 'or' : 'and',
    q: params.get('q') || '',
    text: params.get('text') || '',
    kind: params.get('kind') || '',
    folders: params.get('folders') === '1',
    under: params.get('under') || '',
    after: params.get('after') || '',
    before: params.get('before') || '',
    min: params.get('min') || '',
    max: params.get('max') || '',
    attrs: params.getAll('attr'),
    gone: params.get('gone') === '1',
    collapse: params.get('nocollapse') !== '1',
  };
}

/** 検索の条件を、画面の URL の問い合わせにする（保存した検索にもこの形で残す）。 */
function searchQuery(state) {
  const q = new URLSearchParams();
  state.include.forEach((x) => q.append('t', x));
  state.exclude.forEach((x) => q.append('not', x));
  if (state.mode === 'or') q.set('mode', 'or');
  for (const k of ['q', 'text', 'kind', 'under', 'after', 'before', 'min', 'max']) if (state[k]) q.set(k, state[k]);
  state.attrs.forEach((x) => q.append('attr', x));
  if (state.folders) q.set('folders', '1');
  if (state.gone) q.set('gone', '1');
  if (!state.collapse) q.set('nocollapse', '1');
  return q;
}

/** 条件が1つでもあるか（何も無ければ探さない）。 */
function searchHasCondition(s) {
  return s.include.length || s.exclude.length || s.q || s.text || s.kind || s.under || s.after || s.before || s.min || s.max || s.attrs.length;
}

/** 検索の条件を、検索の API の問い合わせにする。 */
function searchApiQuery(state, root, limit) {
  const q = new URLSearchParams({ root, mode: state.mode });
  state.include.forEach((x) => q.append('tag', x));
  state.exclude.forEach((x) => q.append('not', x));
  for (const k of ['q', 'text', 'kind', 'under', 'after', 'before']) if (state[k]) q.set(k, state[k]);
  const min = parseSize(state.min);
  const max = parseSize(state.max);
  if (min !== null) q.set('min_size', String(min));
  if (max !== null) q.set('max_size', String(max));
  state.attrs.forEach((x) => q.append('attr', x));
  if (state.folders) q.set('folders', '1');
  if (state.gone) q.set('gone', '1');
  if (state.collapse) q.set('collapse', '1');
  if (limit) q.set('limit', String(limit));
  return q;
}

async function pageTags(main, params) {
  // 検索の条件は URL に持つ（#/search?t=含むタグ&not=除くタグ&mode=and|or&q=名前&text=本文&kind=種類&under=場所
  // &after=&before=（更新日）&min=&max=（大きさ）&attr=属性の条件&gone=1&nocollapse=1&folders=1）。#/tags は前の版の URL
  const state = searchState(params);
  const go = () => { const q = searchQuery(state).toString(); location.hash = `#/search${q ? '?' + q : ''}`; };
  const root = await firstRoot();
  const meta = await api('/api/tags');
  const { tags } = meta;
  const attrDefs = meta.attrs || [];
  let filter = '';
  const listEl = h('div', { class: 'tag-list' });

  function toggle(list, tag) {
    const i = list.indexOf(tag);
    if (i >= 0) list.splice(i, 1); else list.push(tag);
  }

  async function renameTag(tag) {
    const input = h('input', { class: 'name-input', value: tag, list: 'tag-options' });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); document.getElementById('confirm').close('ok'); } });
    setTimeout(() => { input.focus(); input.select(); }, 50);
    const ok = await confirmDialog(t('タグの名前を変える'), [
      h('p', { class: 'muted' }, t('「{tag}」が付いたすべてのファイル・フォルダで名前を変えます。', { tag })),
      input,
      h('p', { class: 'muted small' }, t('既にあるタグの名前にすると、2つのタグを1つにまとめます。')),
    ], t('名前を変える'));
    const to = input.value.trim();
    if (!ok || !to || to === tag) return;
    try {
      const r = await api('/api/tags/rename', { from: tag, to });
      toast(t('「{from}」を「{to}」に変えました（{n} 件）', { from: tag, to, n: r.changed }));
      state.include = state.include.map((x) => (x === tag ? to : x));
      state.exclude = state.exclude.map((x) => (x === tag ? to : x));
      go();
      app.render();
    } catch (e) { toast(e.message, 'danger'); }
  }

  async function deleteTag(tag, count) {
    const ok = await confirmDialog(t('タグを削除しますか？'),
      h('p', {}, t('「{tag}」を、付いている {n} 件のファイル・フォルダから外します。ファイル自体には何もしません。', { tag, n: count })), t('削除する'), true);
    if (!ok) return;
    try {
      await api('/api/tags/delete', { tag });
      state.include = state.include.filter((x) => x !== tag);
      state.exclude = state.exclude.filter((x) => x !== tag);
      go();
      app.render();
    } catch (e) { toast(e.message, 'danger'); }
  }

  /** タグの別名と親を見て、足したり外したりする（変えるたびに保存する）。 */
  async function dictionaryDialog(x) {
    const holder = h('div');
    let info = x;
    const reload = async () => {
      const r = await api('/api/tags');
      info = r.tags.find((y) => y.tag === x.tag) || { ...x, aliases: [], parents: [], children: [] };
      render();
    };
    const call = async (path, payload) => {
      try { await api(path, payload); await reload(); } catch (e) { toast(e.message, 'danger'); }
    };
    function render() {
      const aliasIn = h('input', { placeholder: t('別名（例: 会社:ATID）') });
      const parentIn = h('input', { list: 'tag-options', placeholder: t('親のタグ（例: 種別:経理）') });
      setChildren(holder,
        h('h3', {}, '別名'),
        h('p', { class: 'muted small' }, t('別名で付けても探しても、「{tag}」として扱います。既に使われている名前を別名にすると、このタグにまとめます。', { tag: x.tag })),
        h('div', { class: 'chips' }, (info.aliases || []).map((a) => tagChip(a, () => call('/api/tags/alias', { alias: a, op: 'remove' })))),
        h('form', { class: 'inline', onsubmit: (e) => { e.preventDefault(); if (aliasIn.value.trim()) call('/api/tags/alias', { tag: x.tag, alias: aliasIn.value.trim() }); } }, aliasIn, h('button', {}, '足す')),
        h('h3', {}, '親のタグ'),
        h('p', { class: 'muted small' }, t('「{tag}」が付いたものは、親のタグで探しても見つかります。', { tag: x.tag })),
        h('div', { class: 'chips' }, (info.parents || []).map((p) => tagChip(p, () => call('/api/tags/parent', { tag: x.tag, parent: p, op: 'remove' })))),
        h('form', { class: 'inline', onsubmit: (e) => { e.preventDefault(); if (parentIn.value.trim()) call('/api/tags/parent', { tag: x.tag, parent: parentIn.value.trim() }); } }, parentIn, h('button', {}, '足す')),
        (info.children || []).length ? [h('h3', {}, '子のタグ'), h('div', { class: 'chips' }, info.children.map((c) => h('span', { class: 'chip', translate: 'no' }, c)))] : null);
    }
    render();
    await confirmDialog(t('タグの辞書: {tag}', { tag: x.tag }), holder, t('閉じる'));
    app.render();
  }

  // 名前空間（「種別:請求書」の「種別」）ごとにまとめる
  function renderList() {
    const f = filter.toLowerCase();
    const shown = tags.filter((x) => !f || x.tag.toLowerCase().includes(f) || (x.aliases || []).some((a) => a.toLowerCase().includes(f)));
    const groups = new Map();
    for (const x of shown) {
      const ns = x.tag.includes(':') ? x.tag.split(':')[0] : '';
      if (!groups.has(ns)) groups.set(ns, []);
      groups.get(ns).push(x);
    }
    const names = [...groups.keys()].sort((a, b) => (a === '') - (b === '') || naturalCompare(a, b));
    setChildren(listEl, shown.length ? names.map((ns) => h('div', { class: 'tag-group' },
      h('div', { class: 'tag-ns muted small', translate: 'no' }, ns ? `${ns}:` : t('名前空間なし')),
      groups.get(ns).sort((a, b) => naturalCompare(a.tag, b.tag)).map((x) => {
        const inc = state.include.includes(x.tag);
        const exc = state.exclude.includes(x.tag);
        const extra = [
          (x.aliases || []).length ? t('別名: {names}', { names: x.aliases.join('、') }) : '',
          (x.parents || []).length ? t('親: {names}', { names: x.parents.join('、') }) : '',
        ].filter(Boolean).join(' ／ ');
        return h('div', { class: `tag-row ${inc ? 'included' : ''} ${exc ? 'excluded' : ''}` },
          h('button', { class: 'tag-pick', title: t('検索に含める'), onclick: () => { toggle(state.include, x.tag); state.exclude = state.exclude.filter((y) => y !== x.tag); go(); } },
            h('span', { class: 'mark' }, inc ? '✓' : exc ? '⊘' : '＋'),
            h('span', { class: 'tag-name', translate: 'no' }, ns ? x.tag.slice(ns.length + 1) : x.tag,
              extra ? h('span', { class: 'tag-extra muted small' }, extra) : null)),
          h('span', { class: 'muted small num' }, num(x.count)),
          h('button', { class: 'chip-x', title: t('検索から除く'), onclick: () => { toggle(state.exclude, x.tag); state.include = state.include.filter((y) => y !== x.tag); go(); } }, '⊘'),
          h('button', { class: 'chip-x', title: t('別名と親…'), onclick: () => dictionaryDialog(x) }, '⚙'),
          h('button', { class: 'chip-x', title: t('名前を変える…'), onclick: () => renameTag(x.tag) }, '✎'),
          h('button', { class: 'chip-x', title: t('タグを削除'), onclick: () => deleteTag(x.tag, x.count) }, '🗑'));
      }))) : h('p', { class: 'muted' }, tags.length ? t('当てはまるタグがありません。') : t('まだタグがありません。「ファイル」の画面で、ファイルやフォルダに付けられます。')));
  }

  // --- 属性の定義と、属性での絞り込み ---

  async function defineAttrDialog() {
    const name = h('input', { placeholder: t('名前（例: 対応状態）') });
    const type = h('select', {}, Object.entries(ATTR_TYPE_LABELS).map(([v, label]) => h('option', { value: v }, t(label))));
    const choices = h('input', { placeholder: t('選択肢（択一のとき。例: 未対応, 対応中, 対応済み）') });
    const ok = await confirmDialog(t('属性を定義する'), [
      h('label', { class: 'attr-row' }, h('span', { class: 'attr-name' }, t('名前')), name),
      h('label', { class: 'attr-row' }, h('span', { class: 'attr-name' }, t('型')), type),
      h('label', { class: 'attr-row' }, h('span', { class: 'attr-name' }, t('選択肢')), choices),
      h('p', { class: 'muted small' }, t('択一は値を1つだけ持ちます（付け直すと置き換わります）。フォルダに付けた値は、中のファイルが継承します。')),
    ], t('定義する'));
    if (!ok) return;
    try { await api('/api/attrs/defs', { name: name.value.trim(), type: type.value, choices: choices.value }); app.render(); }
    catch (e) { toast(e.message, 'danger'); }
  }

  async function undefineAttr(d) {
    const ok = await confirmDialog(t('属性の定義を消しますか？'),
      h('p', {}, t('「{name}」の定義と、付いている {n} 件の値を外します。ファイル自体には何もしません。', { name: d.name, n: d.count })), t('削除する'), true);
    if (!ok) return;
    try { await api('/api/attrs/defs', { name: d.name, op: 'remove' }); state.attrs = state.attrs.filter((c) => !c.startsWith(d.name)); go(); app.render(); }
    catch (e) { toast(e.message, 'danger'); }
  }

  /** 属性での絞り込みを足す（型に合わせて、値・大小を選ぶ）。 */
  async function attrFilterDialog(d) {
    if (d.type === 'flag') { state.attrs.push(d.name); go(); return; }
    const op = h('select', {}, (d.type === 'number' || d.type === 'date'
      ? [['=', '＝'], ['>=', '以上'], ['<=', '以下'], ['', '値が付いている']]
      : [['=', d.type === 'text' ? '含む' : '＝'], ['', '値が付いている']]).map(([v, label]) => h('option', { value: v }, t(label))));
    const value = d.type === 'choice'
      ? h('select', {}, d.choices.map((c) => h('option', { value: c }, c)))
      : h('input', { type: d.type === 'date' ? 'date' : 'text', inputmode: d.type === 'number' ? 'decimal' : null });
    const ok = await confirmDialog(t('属性で絞り込む: {name}', { name: d.name }), [h('div', { class: 'toolbar' }, op, value)], t('絞り込む'));
    if (!ok) return;
    state.attrs.push(op.value ? `${d.name}${op.value}${value.value}` : d.name);
    go();
  }

  const attrList = h('div', { class: 'tag-list' }, attrDefs.length ? attrDefs.map((d) => h('div', { class: 'tag-row' },
    h('button', { class: 'tag-pick', title: t('この属性で絞り込む…'), onclick: () => attrFilterDialog(d) },
      h('span', { class: 'mark' }, '＋'),
      h('span', { class: 'tag-name', translate: 'no' }, d.name,
        h('span', { class: 'tag-extra muted small' }, t(ATTR_TYPE_LABELS[d.type] || d.type) + (d.choices.length ? `: ${d.choices.join(' / ')}` : '')))),
    h('span', { class: 'muted small num' }, num(d.count)),
    h('button', { class: 'chip-x', title: t('属性の定義を消す'), onclick: () => undefineAttr(d) }, '🗑'))) : h('p', { class: 'muted small' }, t('対応状態・期限・金額などを属性として定義すると、ファイルやフォルダに付けて絞り込めます。')));

  // --- 保存した検索（作業リスト。件数は後から数える） ---

  async function saveSearchDialog() {
    const input = h('input', { class: 'name-input', placeholder: t('名前（例: 要対応の書類）') });
    setTimeout(() => input.focus(), 50);
    const ok = await confirmDialog(t('この条件を保存する'), [input, h('p', { class: 'muted small' }, t('保存した条件は、左の一覧から何度でも開けます（件数つき）。'))], t('保存する'));
    if (!ok || !input.value.trim()) return;
    try { await api('/api/searches', { name: input.value.trim(), query: searchQuery(state).toString() }); app.render(); }
    catch (e) { toast(e.message, 'danger'); }
  }

  const savedList = h('div', { class: 'tag-list' });
  const saved = meta.searches || [];
  setChildren(savedList, saved.length ? saved.map((sv) => {
    const count = h('span', { class: 'muted small num' }, '…');
    const s2 = searchState(new URLSearchParams(sv.query));
    if (searchHasCondition(s2)) {
      api(`/api/search?${searchApiQuery(s2, root, 1)}`).then((r) => { count.textContent = num(r.total); }).catch(() => { count.textContent = '?'; });
    }
    const active = searchQuery(state).toString() === new URLSearchParams(sv.query).toString();
    return h('div', { class: `tag-row ${active ? 'included' : ''}` },
      h('a', { class: 'tag-pick', href: `#/search?${sv.query}` }, h('span', { class: 'mark' }, '★'), h('span', { class: 'tag-name', translate: 'no' }, sv.name)),
      count,
      h('button', { class: 'chip-x', title: t('保存した検索を消す'), onclick: async () => { try { await api('/api/searches', { name: sv.name, op: 'delete' }); app.render(); } catch (e) { toast(e.message, 'danger'); } } }, '🗑'));
  }) : h('p', { class: 'muted small' }, t('条件を入れて「この条件を保存」を押すと、作業リストとしてここに並びます。')));

  const left = h('div', {},
    h('div', { class: 'card' }, h('h2', {}, '保存した検索'), savedList),
    h('div', { class: 'card' },
      h('h2', {}, 'タグ'),
      h('input', { type: 'search', placeholder: t('タグを探す'), oninput: (e) => { filter = e.target.value; renderList(); } }),
      h('p', { class: 'muted small' }, t('＋ で検索に含め、⊘ で除きます。件数は直接付いている数です。')),
      listEl),
    h('div', { class: 'card' },
      h('h2', {}, '属性'),
      attrList,
      h('div', { class: 'buttons' }, h('button', { class: 'small', onclick: defineAttrDialog }, t('属性を定義する…')))));
  renderList();

  // --- 右: 探す条件と、結果・選んだファイルの詳細 ---
  const chipsOf = (list, cls) => list.map((x) => h('span', { class: `chip ${cls}`, translate: 'no' }, x));
  const folderList = h('datalist', { id: `folders-${enc(root)}` });
  const narrowed = state.under || state.after || state.before || state.min || state.max || state.gone || !state.collapse;
  const conditions = [
    h('div', { class: 'toolbar' },
      h('input', { type: 'search', placeholder: t('名前やフォルダで探す'), value: state.q, onchange: (e) => { state.q = e.target.value.trim(); go(); } }),
      h('input', { type: 'search', placeholder: t('本文に含む語'), title: t('PDF・Office・テキストの本文から探します（文字の無いスキャンは OCR で読んだもの）'), value: state.text, onchange: (e) => { state.text = e.target.value.trim(); go(); } }),
      h('select', { 'aria-label': t('種類'), onchange: (e) => { state.kind = e.target.value; go(); } },
        SEARCH_KINDS.map(([v, label]) => h('option', { value: v, selected: state.kind === v }, t(label)))),
      h('label', { class: 'small' }, h('input', { type: 'checkbox', checked: state.folders, onchange: (e) => { state.folders = e.target.checked; go(); } }), ' ', t('フォルダも出す')),
      h('button', { class: 'small', onclick: () => { Object.assign(state, searchState(new URLSearchParams())); go(); } }, t('条件を消す')),
      searchHasCondition(state) ? h('button', { class: 'small', onclick: saveSearchDialog }, t('この条件を保存…')) : null),
    h('details', { class: 'narrow', open: !!narrowed },
      h('summary', { class: 'muted small' }, t('場所・更新日・大きさ・そのほか')),
      h('div', { class: 'toolbar' },
        folderList,
        h('input', { type: 'search', list: `folders-${enc(root)}`, placeholder: t('このフォルダの中だけ'), value: state.under,
          oninput: (e) => folderSuggest(root, e.target.value), onchange: (e) => { state.under = e.target.value.trim().replace(/^\/+|\/+$/g, ''); go(); } }),
        h('label', { class: 'small' }, t('更新日'), ' ', h('input', { type: 'date', value: state.after, onchange: (e) => { state.after = e.target.value; go(); } }),
          ' 〜 ', h('input', { type: 'date', value: state.before, onchange: (e) => { state.before = e.target.value; go(); } })),
        h('label', { class: 'small' }, t('大きさ'), ' ', h('input', { class: 'size-input', placeholder: t('例: 1MB'), value: state.min, onchange: (e) => { state.min = e.target.value.trim(); go(); } }),
          ' 〜 ', h('input', { class: 'size-input', placeholder: t('例: 1GB'), value: state.max, onchange: (e) => { state.max = e.target.value.trim(); go(); } })),
        h('label', { class: 'small' }, h('input', { type: 'checkbox', checked: state.collapse, onchange: (e) => { state.collapse = e.target.checked; go(); } }), ' ', t('同じ内容をまとめる')),
        h('label', { class: 'small' }, h('input', { type: 'checkbox', checked: state.gone, onchange: (e) => { state.gone = e.target.checked; go(); } }), ' ', t('今は無いものも含める')))),
    state.include.length || state.exclude.length || state.attrs.length ? h('div', { class: 'toolbar' },
      h('div', { class: 'chips' }, chipsOf(state.include, 'active'), state.exclude.length ? [h('span', { class: 'muted small' }, t('除外:')), chipsOf(state.exclude, 'excluded-chip')] : null,
        state.attrs.map((c) => h('span', { class: 'chip attr', translate: 'no' }, c,
          h('button', { class: 'chip-x', title: t('この条件を外す'), onclick: () => { state.attrs = state.attrs.filter((x) => x !== c); go(); } }, '×')))),
      state.include.length > 1 ? h('select', { onchange: (e) => { state.mode = e.target.value; go(); } },
        h('option', { value: 'and', selected: state.mode === 'and' }, t('すべて含む（AND）')),
        h('option', { value: 'or', selected: state.mode === 'or' }, t('どれかを含む（OR）'))) : null) : null,
  ];
  const right = h('div', {}, conditions);
  if (!searchHasCondition(state)) {
    right.append(h('p', { class: 'muted' }, t('名前・本文・種類で探すか、左の一覧からタグを選んでください。フォルダに付いたタグは、中のファイルにも効きます。')));
    const st = await indexStatus(root);
    if (st) right.append(st);
  } else {
    let res;
    try { res = await api(`/api/search?${searchApiQuery(state, root)}`); } catch (e) { res = null; right.append(h('div', { class: 'alert danger' }, e.message)); }
    if (res) {
      const sort = sortState('tag-search', { field: 'path', dir: 'asc' });
      const getters = { name: (x) => x.path.split('/').pop(), path: (x) => x.path, size: (x) => (x.is_dir ? null : x.size), mtime: (x) => x.mtime };
      const body = h('tbody');
      const detail = h('div', { class: 'tag-detail' });
      const head = h('tr');
      const renderRows = () => {
        setChildren(head, sortHeader(t('名前'), 'name', sort, renderRows), sortHeader(t('場所'), 'path', sort, renderRows, 'hide-narrow'),
          h('th', {}, t('タグ・属性')), sortHeader(t('サイズ'), 'size', sort, renderRows, 'num'), sortHeader(t('更新日時'), 'mtime', sort, renderRows, 'num hide-narrow'));
        setChildren(body, sortItems(res.items, sort, getters, (x) => x.path).map((x) => {
          const name = x.path.split('/').pop();
          const dir = x.path.split('/').slice(0, -1).join('/');
          return h('tr', { class: `clickable ${x.gone_at ? 'gone' : ''}`, onclick: () => showDetail(x) },
            h('td', { class: 'path' }, x.is_dir ? '📁 ' : `${fileIcon(name)} `, name,
              x.gone_at ? [' ', h('span', { class: 'badge', title: t('{when} に見えなくなりました', { when: when(x.gone_at) }) }, '今は無い')] : null,
              // 本文で探したときは、一致したところの前後を見せる
              x.snippet ? h('div', { class: 'snippet muted small' }, highlight(x.snippet, state.text)) : null,
              x.copies.length ? h('details', { class: 'copies small', onclick: (e) => e.stopPropagation() },
                h('summary', { class: 'muted' }, t('同じ内容があと {n} か所', { n: num(x.copies.length) })),
                h('ul', {}, x.copies.map((p) => h('li', {}, h('a', { href: browseHref(root, p.split('/').slice(0, -1).join('/')) }, p))))) : null),
            h('td', { class: 'path muted hide-narrow' }, h('a', { href: browseHref(root, x.is_dir ? x.path : dir), onclick: (e) => e.stopPropagation() }, dir || '/')),
            h('td', {}, h('div', { class: 'chips' },
              x.tags.direct.map((tg) => h('span', { class: 'chip' }, tg.tag)),
              x.tags.inherited.map((tg) => h('span', { class: 'chip inherited', title: t('{from} から継承', { from: tg.from || root }) }, tg.tag)),
              (x.tags.implied || []).map((tg) => h('span', { class: 'chip implied', title: t('{via} の親', { via: tg.via }) }, tg.tag)),
              x.attrs.direct.map((a) => h('span', { class: 'chip attr' }, attrText(a.name, a.value))),
              x.attrs.inherited.map((a) => h('span', { class: 'chip attr inherited', title: t('{from} から継承', { from: a.from || root }) }, attrText(a.name, a.value))))),
            h('td', { class: 'num' }, x.is_dir ? '' : size(x.size)),
            h('td', { class: 'num hide-narrow' }, when(new Date(x.mtime * 1000).toISOString())));
        }));
      };
      const showDetail = async (x) => {
        const name = x.path.split('/').pop();
        const parts = [h('h2', { class: 'path' }, name), h('div', { class: 'muted mono' }, x.path),
          h('div', { class: 'buttons' },
            h('a', { class: 'button small', href: x.is_dir ? browseHref(root, x.path) : browseHref(root, x.path.split('/').slice(0, -1).join('/'), '', x.gone_at ? '' : name) }, t('フォルダを開く')),
            x.is_dir || x.gone_at ? null : h('a', { class: 'button small', href: fileUrl(root, x.path), target: '_blank', rel: 'noopener' }, t('新しいタブで開く')))];
        if (x.gone_at) {
          parts.push(h('p', { class: 'muted' }, t('{when} に見えなくなりました。過去の版は、フォルダを開いて「この時点を見る」から探せます。', { when: when(x.gone_at) })));
        } else {
          // 属性はここでも付け替えられる（作業リストで対応状態を進めるなど）
          try {
            const info = await api(`/api/info?root=${enc(root)}&path=${enc(x.path)}`);
            if (info.attr_defs.length) parts.push(h('h3', {}, '属性'), attrEditor(root, x.path, info.attrs, info.attr_defs, () => app.render()));
          } catch (e) { /* 属性が読めなくてもプレビューは出す */ }
          if (!x.is_dir) parts.push(preview(fileUrl(root, x.path), name, x.size, { root, path: x.path }));
        }
        setChildren(detail, h('div', { class: 'card' }, parts));
        detail.scrollIntoView({ block: 'nearest' });
      };
      renderRows();
      right.append(
        h('p', { class: 'muted small' }, res.total > res.items.length
          ? t('{total} 件のうち {n} 件を表示', { total: num(res.total), n: num(res.items.length) })
          : t('{n} 件', { n: num(res.total) })),
        detail,
        h('div', { class: 'card table-wrap' }, h('table', {}, h('thead', {}, head), body)));
      // 本文で探したときは、索引がどこまでできているか（抽出待ちが残っていれば、見つからないことがある）
      if (state.text) {
        const st = await indexStatus(root);
        if (st) right.append(st);
      }
    }
  }
  setChildren(main, await tagDatalist(), h('h1', {}, t('検索')), splitPane('tags', left, right, 30));
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
    m.models.map((x) => h('option', { value: x.name, selected: x.name === cur }, `${x.cloud ? '☁ ' : ''}${x.name}${x.vision ? '' : t('（画像なし）')}${x.name === m.default ? t('（既定）') : ''}`)));
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
      return h('div', {}, h('div', { class: 'alert danger' }, t('提案を作れませんでした: {error}', { error: translateString(req.error) })),
        h('button', { onclick: () => { requestSuggestions(root, [path], { force: true }); } }, 'もう一度'));
    }
    if (req && (req.state === 'queued' || req.state === 'running')) {
      return h('div', { class: 'waiting' }, h('div', { class: 'bar indeterminate' }, h('div')),
        h('p', { class: 'muted' }, req.state === 'running' ? t('{model} が内容を読んでいます…', { model: models.current() }) : t('順番待ち（{n} 番目）', { n: req.position ?? '?' })));
    }
    return h('div', {}, h('p', { class: 'muted' }, 'まだ提案がありません。'),
      h('button', { class: 'primary', onclick: () => { requested = true; requestSuggestions(root, [path]); load(); } }, '提案を出す'));
  }

  function render(data) {
    const s = data.suggestion;
    const name = path.split('/').pop();
    const dir = path.split('/').slice(0, -1).join('/');
    const existing = new Set(data.existing_tags);
    const current = new Set(data.tags.direct.map((tg) => tg.tag));
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
      radio(dir, t('（今の場所のまま）{dir}', { dir: dir || '/' }), null, !s.destinations.length),
      radio('__other__', t('その他のフォルダ…'), null, false), other);

    // タグ: 提案（既存・新規）を付ける・却下する。今のタグも見せる
    const tagBox = h('div', { class: 'chips' });
    function renderTags() {
      setChildren(tagBox,
        [...current].map((tg) => h('span', { class: 'chip' }, '✓ ', tg)),
        // チップの中は利用者のデータとして自動では訳さないので、ラベルは t() で訳す
        s.tags.filter((tg) => !current.has(tg)).map((tg) => h('span', { class: `chip suggested ${existing.has(tg) ? '' : 'new'}`, title: t(existing.has(tg) ? '既存のタグ' : '新しいタグ') },
          existing.has(tg) ? '' : h('span', { class: 'tag-new' }, `${t('新規')} `), tg,
          h('button', { class: 'chip-x ok', title: t('付ける'), onclick: () => feedback(tg, 'accept') }, '＋'),
          h('button', { class: 'chip-x', title: t('却下（次から提案しにくくなる）'), onclick: () => feedback(tg, 'reject') }, '×'))),
        data.tags.inherited.map((tg) => h('span', { class: 'chip inherited', title: t('{from} から継承', { from: tg.from || root }) }, tg.tag)));
    }
    async function feedback(tag, decision) {
      try {
        await api('/api/tags/feedback', { root, path, tag, decision, model: s.model });
        if (decision === 'accept') current.add(tag);
        s.tags = s.tags.filter((tg) => tg !== tag || decision === 'accept');
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
      toast(t('採用リストに入れました: {name}', { name: newName }));
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
      s.examples.length ? h('details', {}, h('summary', { class: 'muted small' }, t('参考にした過去の判断（{n} 件）', { n: s.examples.length })), h('ul', { class: 'small' }, s.examples.map((e) => h('li', {}, e)))) : null,
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
      h('strong', {}, t('採用リスト {n} 件', { n: num(items.length) })),
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
      const ok = await confirmDialog(t('{n} 件を移動・改名しますか？', { n: items.length }), [
        h('ul', { class: 'small' }, items.slice(0, 30).map((it) => h('li', { class: 'path' }, `${it.path} → ${it.dest}`))),
        items.length > 30 ? h('p', { class: 'muted' }, t('ほか {n} 件', { n: items.length - 30 })) : null,
        h('p', {}, '移動先に同じ名前があるもの・選んだ後に変わったものは飛ばします。実行の前後にスナップショットを撮り、プランの画面から取り消せます。'),
      ], '実行する');
      if (!ok) return;
    }
    try {
      const r = await api(`/api/organize/${enc(root)}/plan`, { items, apply });
      cart.clear(root);
      render();
      if (onChange) onChange();
      if (apply) { app.job = r.job; app.jobDismissed = null; renderJob(); pollJob(); toast(t('実行を始めました（プラン {id}）', { id: r.plan_id })); }
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

  // 並べ替え（項目と向きはブラウザに覚える）
  const sort = sortState('organize', { field: 'folder', dir: 'asc' });
  const getters = {
    name: (x) => x.path.split('/').pop(),
    folder: (x) => x.path,
    size: (x) => x.size,
    mtime: (x) => x.mtime,
    status: (x) => (cart.items(root)[x.path] ? 2 : x.suggestion ? 1 : 0),
  };

  function visible() {
    const f = filter.toLowerCase();
    const inCart = cart.items(root);
    const shown = files.filter((x) => (!f || x.path.toLowerCase().includes(f)) && !(opts.hideDone && inCart[x.path]));
    return sortItems(shown, sort, getters, (x) => x.path);
  }

  const sortSelect = h('select', {
    title: t('並べ替え'),
    onchange: (e) => { const [field, dir] = e.target.value.split(':'); sort.field = field; sort.dir = dir; saveLocal('tag-keeper-sort-organize', { field, dir }); renderList(); },
  }, [
    ['folder:asc', 'フォルダ順'], ['name:asc', '名前順'], ['mtime:desc', '新しい順'], ['mtime:asc', '古い順'],
    ['size:desc', '大きい順'], ['size:asc', '小さい順'], ['status:asc', '提案の無いものから'], ['status:desc', '提案のあるものから'],
  ].map(([v, label]) => h('option', { value: v, selected: v === `${sort.field}:${sort.dir}` }, t(label))));

  function status(f) {
    if (cart.items(root)[f.path]) return h('span', { class: 'badge ok' }, '採用リスト');
    const r = suggestWatch.byPath[f.path];
    if (r && r.state === 'running') return h('span', { class: 'badge' }, '作成中');
    if (r && r.state === 'queued') return h('span', { class: 'badge' }, t('待ち {n}', { n: r.position ?? '' }));
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
      h('h2', { class: 'path' }, name), h('div', { class: 'muted mono' }, path),
      h('div', { class: 'buttons' }, h('a', { class: 'button small', href: fileUrl(root, path), target: '_blank', rel: 'noopener' }, '新しいタブで開く'),
        h('a', { class: 'button small', href: browseHref(root, path.split('/').slice(0, -1).join('/')) }, 'フォルダを開く'),
        h('button', { class: 'small', onclick: () => renameDialog(root, path, false) }, '名前を変える…'),
        h('button', { class: 'small danger-outline', title: t('Delete キーでも削除できます'), onclick: () => deleteCurrent() }, '削除…')),
      // 提案を先に、プレビューを後に置く（PDF のプレビューで提案が画面の外に押し出されないように）
      suggestionPanel(root, path, { auto: opts.auto, onAccept: () => { bar.render(); move(1); } }),
      h('h3', {}, 'プレビュー'),
      preview(fileUrl(root, path), name, files.find((f) => f.path === path)?.size, { root, path }));
    if (opts.prefetch) {
      const list = visible();
      const i = list.findIndex((f) => f.path === path);
      const next = list.slice(i + 1, i + 3).filter((f) => !f.suggestion).map((f) => f.path);
      requestSuggestions(root, next, { prefetch: true });
    }
  }

  /** 選んでいるファイルを削除する（隔離フォルダへ移す。取り消しはプランの画面から）。 */
  function deleteCurrent() {
    const f = files.find((x) => x.path === current);
    if (f) deleteDialog(root, [{ path: f.path, is_dir: false, size: f.size }]);
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
    if (e.key === 'Delete' && current) { e.preventDefault(); deleteCurrent(); }
  };
  document.addEventListener('keydown', keyHandler);

  const toggle = (key, label) => h('label', { class: 'small' }, h('input', {
    type: 'checkbox', checked: opts[key], onchange: (e) => { opts[key] = e.target.checked; saveLocal('tag-keeper-organize', opts); renderList(); },
  }), ' ', label);
  const pending = files.filter((f) => !f.suggestion).length;

  setChildren(main,
    h('datalist', { id: `folders-${enc(root)}` }),
    await tagDatalist(),
    h('h1', {}, t('整理: {root}', { root })),
    h('p', { class: 'muted' }, t('受け皿（{patterns} に一致するフォルダ）のファイル {n} 件を、フォルダの階層なしで並べています。ファイルを選ぶと、モデルが内容を読んで名前・移動先・タグを提案します。採用したものは採用リストにたまり、まとめて実行できます。',
      { patterns: data.patterns.join(i18n.lang === 'ja' ? '・' : ', '), n: num(files.length) })),
    h('div', { class: 'toolbar' },
      h('input', { type: 'search', placeholder: '名前やフォルダで絞り込む', oninput: (e) => { filter = e.target.value; renderList(); } }),
      await modelSelect(() => app.render()),
      sortSelect,
      toggle('auto', '選んだら提案を出す'), toggle('prefetch', '次の2件を先読み'), toggle('hideDone', '採用したものを隠す'),
      h('button', { disabled: jobRunning() || !pending, title: '裏でまとめて作る（時間がかかります）', onclick: () => startJob(`/api/organize/${enc(root)}/suggest`, { model: models.current() }) }, t('残り {n} 件をまとめて提案', { n: num(pending) }))),
    bar.el,
    splitPane('organize',
      h('div', { class: 'card table-wrap list' }, h('table', { class: 'flat' }, listBody)),
      side, 45));
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
