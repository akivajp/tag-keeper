// tag-keeper の画面。サーバーの JSON API を呼んで、ブラウザ側で画面を組み立てる。
//
// - 画面は URL の # 以降で切り替える（#/ ルート一覧、#/report/<ルート>、#/plans、#/plans/<ID>）
// - ファイル名などの文字列は textContent で入れる（innerHTML は使わない。名前に HTML が含まれても安全なように）
// - 時間のかかる処理は「ジョブ」として裏で動き、画面下の欄に進み具合を1秒ごとに表示する
'use strict';

// ---------- 小さな道具 ----------

/** 要素を作る。attrs の on* はイベント、それ以外は属性。children は文字列か要素。 */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    // CSP で style 属性の書き込みは止められるので、CSSOM 経由で設定する
    else if (k === 'style') el.style.cssText = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

/** 要素の中身を入れ替える（null・undefined・false は飛ばす。replaceChildren は "null" と表示してしまうため）。 */
function setChildren(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

/** バイト数を読みやすい単位にする。 */
function size(n) {
  if (n === null || n === undefined) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${n}B` : `${n.toFixed(1)}${units[i]}`;
}

/** 数を3桁区切りにする。 */
function num(n) { return (n ?? 0).toLocaleString(i18n.locale); }

/** 秒数を「1時間2分」「3分4秒」の形にする。 */
function dur(sec) {
  if (sec === null || sec === undefined || !isFinite(sec)) return '-';
  sec = Math.max(0, Math.round(sec));
  const hh = Math.floor(sec / 3600), mm = Math.floor(sec % 3600 / 60), ss = sec % 60;
  if (hh) return t('{h}時間{m}分', { h: hh, m: mm });
  if (mm) return t('{m}分{s}秒', { m: mm, s: ss });
  return t('{s}秒', { s: ss });
}

/** 経過時間（秒）を、2日以上なら「3日」、それより短ければ dur() の形にする。 */
function ageText(sec) {
  return sec >= 2 * 86400 ? t('{d}日', { d: Math.floor(sec / 86400) }) : dur(sec);
}

/** ISO 8601 の日時を、手元の時刻で読みやすくする。 */
function when(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString(i18n.locale, { dateStyle: 'medium', timeStyle: 'short' });
}

/** API を呼ぶ。失敗したらサーバーのメッセージ付きで例外を投げる。 */
async function api(path, body) {
  const opts = body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* JSON でない応答 */ }
  if (!res.ok) throw new Error((data && data.error) || text || `${res.status} ${res.statusText}`);
  return data;
}

/** 確認の画面を出し、「実行する」が押されたら true を返す。 */
function confirmDialog(title, body, okLabel = '実行する', danger = false) {
  const dlg = document.getElementById('confirm');
  document.getElementById('confirm-title').textContent = title;
  const holder = document.getElementById('confirm-body');
  setChildren(holder, ...(Array.isArray(body) ? body : [body]));
  const ok = document.getElementById('confirm-ok');
  ok.textContent = okLabel;
  ok.className = danger ? 'danger' : 'primary';
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
    dlg.showModal();
  });
}

/** 画面の上に、消えるお知らせを出す。 */
function toast(message, kind = 'info') {
  const el = h('div', { class: `alert ${kind}` }, message);
  document.getElementById('alerts').prepend(el);
  setTimeout(() => el.remove(), 6000);
}

const STATE_LABELS = {
  draft: ['未実行', ''],
  applied: ['実行済み', 'ok'],
  'partially-undone': ['一部を取り消し', 'warn'],
  undone: ['取り消し済み', ''],
  interrupted: ['途中で停止', 'danger'],
  purged: ['隔離を空にした', ''],
};

function stateBadge(state) {
  const [label, kind] = STATE_LABELS[state] || [state, ''];
  return h('span', { class: `badge ${kind}` }, label);
}

function syncBadge(sync) {
  if (!sync) return null;
  if (sync.error) return h('span', { class: 'badge danger' }, `同期: 状態を読めません`);
  const map = {
    active: ['同期: 稼働中', 'ok'], inactive: ['同期: 停止中', 'warn'], failed: ['同期: 失敗', 'danger'],
    // cloud-sync（定期実行）の状態
    idle: ['同期: 待機中', 'ok'], running: ['同期中…', 'info'], waiting: ['同期: 順番待ち', 'info'],
    error: ['同期: 失敗', 'danger'], needs_resync: ['同期: 基準の作り直しが必要', 'danger'], interrupted: ['同期: 中断', 'warn'],
  };
  const [label, kind] = map[sync.state] || [t('同期: {state}', { state: sync.state }), 'warn'];
  return h('span', { class: `badge ${kind}` }, label);
}

// ---------- 全体の状態 ----------

const app = {
  state: null,          // /api/state の結果
  job: null,            // 動いている（または最後の）ジョブ
  jobDismissed: null,   // 閉じた（終わった）ジョブの ID
  checks: {},           // プラン ID → 最後の確認の結果（パス → 問題）
  render: null,         // 今の画面を描き直す関数
};

async function refreshState() {
  app.state = await api('/api/state');
  renderAlerts();
  return app.state;
}

/** どの画面でも出す警告（同期を止めたままのプラン）。 */
function renderAlerts() {
  const holder = document.getElementById('alerts');
  holder.querySelectorAll('.sticky-alert').forEach((e) => e.remove());
  for (const p of (app.state?.plans || [])) {
    if (p.sync_paused === null || p.sync_paused === undefined) continue;
    holder.append(h('div', { class: 'alert danger sticky-alert' },
      h('strong', {}, '常駐の同期が止まったままです'),
      h('p', {}, t('プラン {id} の削除をクラウドに反映できていません。', { id: p.id }), ' ', p.sync_paused ? t('理由: {reason}', { reason: p.sync_paused }) : ''),
      h('a', { href: `#/plans/${p.id}` }, 'プランを開いて再試行する')));
  }
}


// ---------- 同期（cloud-sync）の状態 ----------

let syncTimer = null;
let syncFastUntil = 0;   // 手動で始めた直後は、動き出すまで細かく確かめる（この時刻まで）

/** 同期が動いている間、どの画面でも上部に出す表示を描く。 */
function renderSyncBanner(items) {
  const holder = document.getElementById('alerts');
  holder.querySelectorAll('.sync-banner').forEach((e) => e.remove());
  for (const it of items) {
    if (it.state !== 'running' && it.state !== 'waiting') continue;
    holder.append(h('div', { class: 'alert info sync-banner' },
      h('strong', {}, t('同期中: {root}', { root: it.root })),
      ' ' + (it.started ? t('（{when} に開始）', { when: when(it.started) }) : ''),
      it.progress ? syncProgressView(it.progress) : null));
  }
}

// 同期の段階（サーバーの SyncProgress.phase）の表示名
const SYNC_PHASES = {
  starting: '開始しています', listing: '一覧を作っています', diff: '差分を調べています', applying: '変更を反映しています',
  download: 'クラウドから取り込んでいます', upload: 'クラウドへ送っています', updating: '一覧を更新しています',
  no_changes: '変更はありません', finished: '仕上げています',
};

/** 同期の進み具合（段階・進捗バー・件数・転送量・残り時間・最後に扱ったファイル）を描く。 */
function syncProgressView(p) {
  const st = p.stats || {};
  const total = (p.queued_copies || 0) + (p.queued_deletes || 0);
  const done = Math.min(total, (p.copied || 0) + (p.deleted || 0));
  // 反映する件数が分かれば件数の割合、分からなければ動いていることだけを示す
  const bar = total
    ? h('div', { class: 'bar' }, h('div', { style: `width:${(done / total * 100).toFixed(1)}%` }))
    : h('div', { class: 'bar indeterminate' }, h('div'));
  const figures = [h('span', {}, t(SYNC_PHASES[p.phase] || p.phase))];
  if (total) figures.push(h('span', {}, t('反映 {done} / {total} 件（{pct}%）', { done: num(done), total: num(total), pct: (done / total * 100).toFixed(1) })));
  if (p.queued_copies) figures.push(h('span', {}, t('コピー {done} / {total}', { done: num(p.copied), total: num(p.queued_copies) })));
  if (p.queued_deletes) figures.push(h('span', {}, t('削除 {done} / {total}', { done: num(p.deleted), total: num(p.queued_deletes) })));
  if (!total && st.listed) figures.push(h('span', {}, t('一覧 {n} 件', { n: num(st.listed) })));
  if (st.bytes_total && st.bytes_total !== '0 B') {
    figures.push(h('span', {}, t('転送 {done} / {total}', { done: st.bytes_done, total: st.bytes_total })));
    if (st.speed && st.speed !== '0 B/s') figures.push(h('span', {}, t('速さ {rate}/秒', { rate: st.speed.replace(/\/s$/, '') })));
    if (st.eta && st.eta !== '-') figures.push(h('span', {}, t('転送の残り {eta}', { eta: st.eta })));
  }
  if (st.elapsed) figures.push(h('span', { class: 'muted' }, t('経過 {elapsed}', { elapsed: st.elapsed })));
  if (p.errors) figures.push(h('span', { class: 'badge danger' }, t('エラー {n} 件', { n: num(p.errors) })));
  return h('div', { class: 'sync-progress' },
    bar,
    h('div', { class: 'figures' }, figures),
    p.last_file ? h('div', { class: 'muted path' }, p.last_file) : null,
    p.last_error ? h('div', { class: 'error-line' }, p.last_error) : null);
}

/** 同期の状態を読み、表示を更新する。動いている間は2秒ごと、そうでなければ15秒ごとに確かめる。 */
async function pollSync() {
  clearTimeout(syncTimer);
  let items = [];
  try {
    items = (await api('/api/sync')).items;
  } catch (e) {
    // 一時的な通信の失敗は、次の問い合わせで回復するのを待つ
  }
  const busy = (it) => it.state === 'running' || it.state === 'waiting';
  const before = app.syncBusy || {};
  app.syncBusy = Object.fromEntries(items.map((it) => [it.root, busy(it)]));
  app.syncItems = items;
  renderSyncBanner(items);
  for (const it of items) {
    // 動いていた同期が終わったら知らせて、画面の同期の表示を最新にする
    if (before[it.root] && !busy(it)) {
      if (it.state === 'idle') toast(t('同期が終わりました: {root}', { root: it.root }), 'info');
      else toast(t('同期が失敗しました: {root}: {message}', { root: it.root, message: it.message || it.state }), 'danger');
      if (app.render) app.render();
    }
  }
  syncTimer = setTimeout(pollSync, items.some(busy) || Date.now() < syncFastUntil ? 2000 : 15000);
}

/** 今すぐ1回同期する。 */
async function startSync(root) {
  try {
    await api(`/api/sync/${enc(root)}/start`, {});
    syncFastUntil = Date.now() + 20000;
    toast(t('同期を始めます: {root}', { root: root }), 'info');
    pollSync();
  } catch (e) {
    toast(e.message, 'danger');
  }
}

// ---------- ジョブ（裏で動く処理）の進み具合 ----------

/** ジョブを始める API を呼び、進み具合の表示を始める。 */
async function startJob(path, body = {}) {
  try {
    const job = await api(path, body);
    app.job = job;
    app.jobDismissed = null;
    renderJob();
    pollJob();
    return job;
  } catch (e) {
    toast(e.message, 'danger');
    return null;
  }
}

let pollTimer = null;
async function pollJob() {
  clearTimeout(pollTimer);
  const before = app.job;
  try {
    const { job } = await api('/api/jobs/current?lines=80');
    app.job = job;
  } catch (e) {
    // 一時的な通信の失敗は、次の問い合わせで回復するのを待つ
  }
  renderJob();
  const running = app.job && app.job.status === 'running';
  // 動いていたジョブが終わったら、画面を描き直す
  if (before && before.status === 'running' && app.job && app.job.id === before.id && !running) {
    await onJobFinished(app.job);
  }
  pollTimer = setTimeout(pollJob, running ? 1000 : 5000);
}

async function onJobFinished(job) {
  // 自動で最新にしたもの（job.auto）は知らせず、操作の途中の画面を描き直さない。
  // 終わったら欄を閉じ、失敗したときだけ欄に残す。描き直すのはルートの一覧（数字が変わる画面）だけ
  if (job.auto) {
    if (job.status === 'done') { app.jobDismissed = job.id; renderJob(); }
    await refreshState();
    const onRoots = !location.hash || location.hash === '#' || location.hash === '#/';
    if (onRoots && app.render) app.render();
    return;
  }
  if (job.status === 'failed') toast(t('失敗しました: {title}: {error}', { title: translateString(job.title), error: translateString(job.error || '') }), 'danger');
  else toast(t('終わりました: {title}', { title: translateString(job.title) }), 'info');
  if (job.kind === 'check' && job.result) app.checks[job.result.plan_id] = job.result.problems;
  await refreshState();
  if (job.kind === 'plan' && job.status === 'done' && job.result) {
    location.hash = `#/plans/${job.result.plan_id}`;
    return;
  }
  if (app.render) app.render();
}

/** 画面下の欄に、ジョブの段階・進捗バー・速さ・残り時間・ログを描く。 */
function renderJob() {
  const holder = document.getElementById('job');
  const job = app.job;
  if (!job || job.id === app.jobDismissed) { holder.hidden = true; return; }
  holder.hidden = false;
  const phases = job.phases || [];
  const cur = phases[phases.length - 1];
  const statusBadge = job.status === 'running'
    ? h('span', { class: 'badge' }, '実行中')
    : job.status === 'done' ? h('span', { class: 'badge ok' }, '完了') : h('span', { class: 'badge danger' }, '失敗');

  let bar = h('div', { class: 'bar' }, h('div', { style: 'width:100%' }));
  const figures = [];
  if (cur) {
    const isBytes = cur.unit === 'B';
    const fmt = (v) => (isBytes ? size(v) : `${num(v)} ${t(cur.unit)}`);
    if (cur.total) {
      const pct = Math.min(100, (cur.done / cur.total) * 100);
      bar = h('div', { class: 'bar' }, h('div', { style: `width:${pct.toFixed(1)}%` }));
      figures.push(h('span', {}, t('{done} / {total}（{pct}%）', { done: fmt(cur.done), total: fmt(cur.total), pct: pct.toFixed(1) })));
    } else if (job.status === 'running') {
      bar = h('div', { class: 'bar indeterminate' }, h('div'));
      if (cur.done) figures.push(h('span', {}, fmt(cur.done)));
    }
    if (cur.rate > 0 && job.status === 'running') figures.push(h('span', {}, t('速さ {rate}/秒', { rate: isBytes ? size(cur.rate) : num(Math.round(cur.rate)) + ' ' + t(cur.unit) })));
    if (cur.eta !== null && cur.eta !== undefined && job.status === 'running') figures.push(h('span', {}, t('残り {eta}', { eta: dur(cur.eta) })));
  }
  figures.push(h('span', { class: 'muted' }, t('経過 {elapsed}', { elapsed: dur(job.elapsed) })));

  const lines = h('pre', {}, (job.lines || []).join('\n'));
  const details = h('details', {}, h('summary', { class: 'muted' }, t('ログ（{file}）', { file: job.log_file || '' })), lines);
  if (job.status === 'failed') details.open = true;

  setChildren(holder, 
    h('div', { class: 'head' },
      statusBadge,
      h('span', { class: 'title' }, job.title),
      job.status !== 'running' ? h('button', { onclick: () => { app.jobDismissed = job.id; renderJob(); } }, '閉じる') : null),
    h('div', { class: 'phases' }, phases.map((p, i) => h('span', {
      class: i === phases.length - 1 && job.status === 'running' ? 'current' : 'done',
    }, p.name))),
    bar,
    h('div', { class: 'figures' }, figures),
    job.error ? h('div', { class: 'alert danger' }, job.error) : null,
    details,
  );
  // ログは最新の行が見えるように下端へ寄せる
  lines.scrollTop = lines.scrollHeight;
}

function jobRunning() { return app.job && app.job.status === 'running'; }

// ---------- 画面: ルートの一覧 ----------

/**
 * 走査が古いか。自動で最新にしているなら間隔の2倍（同期の後にも走るので、それより古ければ止まっている）、
 * 自動にしていなければ1日を目安にする。
 */
function scanIsStale(finishedAt, st) {
  const age = (Date.now() - new Date(finishedAt).getTime()) / 1000;
  const limit = st.auto_refresh > 0 ? Math.max(st.auto_refresh * 60 * 2, 3600) : 86400;
  return age > limit ? age : 0;
}

async function pageRoots(main) {
  const st = await refreshState();
  const cards = st.roots.map((r) => {
    const hashedPct = r.files ? (r.hashed / r.files) * 100 : 0;
    const scan = r.last_scan;
    const staleFor = r.scanned ? scanIsStale(scan.finished_at, st) : 0;
    return h('section', { class: 'card' },
      h('h2', {}, r.name, ' ', syncBadge(r.sync)),
      h('div', { class: 'muted mono' }, r.path),
      r.scanned ? h('div', { class: 'stats' },
        stat('ファイル', num(r.files)),
        stat('容量', size(r.size)),
        stat('ハッシュ計算済み', `${hashedPct.toFixed(1)}%`),
        stat('最後の走査', when(scan.finished_at)),
        r.held_files ? stat('状態', h('span', { class: 'badge warn', title: t('次の走査でも見えなければ、消えたと確定します。') },
          t('消失の確定を保留（{n} 件）', { n: num(r.held_files) }))) : null,
        r.relink_pending ? stat('移動の確認', h('a', { class: 'badge warn', href: `#/relink/${enc(r.name)}` },
          t('確認待ち（{n} 件）', { n: num(r.relink_pending) }))) : null,
      ) : h('p', { class: 'muted' }, 'まだ走査していません。'),
      staleFor ? h('div', { class: 'alert warn' },
        t('最後の走査から {age} たっています。カタログ（タグの検索・レポート・移動の追従）が今のファイルと合っていない可能性があります。', { age: ageText(staleFor) }),
        st.auto_refresh > 0 ? null : ' ' + t('設定の [scan] auto_refresh で、自動で最新にできます。')) : null,
      (r.excluded || []).length ? h('p', { class: 'muted small' }, t('走査しないフォルダ: {list}', {
        list: r.excluded.map((x) => t('{path}（{reason}）', { path: x.path, reason: translateString(x.reason) })).join('、'),
      })) : null,
      r.scanned ? h('p', { class: 'muted small' }, st.auto_refresh > 0
        ? t('自動で最新にします（{m} 分ごと{sync}）。', { m: num(st.auto_refresh), sync: st.after_sync && r.sync && r.sync.client === 'cloud-sync' ? t('と、同期が終わるたび') : '' })
        : t('自動では最新にしません。')) : null,
      r.sync && !r.sync.error ? h('p', { class: 'muted' }, r.sync.client === 'cloud-sync'
        ? t('同期: cloud-sync（{service}）。整理プランを実行するときは、定期の同期を止めて隔離し、削除を反映してから再開します。', { service: r.sync.service })
          + (r.sync.last_ok ? ' ' + t('最後に成功: {when}', { when: when(r.sync.last_ok) }) : '')
          + (r.sync.timer && r.sync.timer !== 'active' ? ' ' + t('定期の同期は止まっています。') : '')
        : t('同期クライアント: {client}（{service}）。子が {n} 件以上のフォルダを消すときは、同期を一時停止して削除を反映してから再開します。',
          { client: r.sync.client, service: r.sync.service, n: num(r.sync.threshold) })) : null,
      r.sync && r.sync.state === 'error' && r.sync.message ? h('div', { class: 'alert danger' }, r.sync.message) : null,
      h('div', { class: 'buttons' },
        h('button', { class: 'primary', disabled: jobRunning(), onclick: () => startJob(`/api/roots/${enc(r.name)}/refresh`) }, '最新にする（走査＋ハッシュ）'),
        h('button', { disabled: jobRunning(), onclick: () => startJob(`/api/roots/${enc(r.name)}/scan`) }, '走査だけ'),
        r.scanned ? h('a', { class: 'button', href: `#/report/${enc(r.name)}` }, 'レポートとプランの作成') : null,
        r.sync && r.sync.can_trigger ? h('button', { disabled: r.sync.state === 'running' || r.sync.state === 'waiting', onclick: () => startSync(r.name) }, '今すぐ同期') : null,
      ));
  });
  setChildren(main, 
    h('h1', {}, 'ルート'),
    st.roots.length ? null : h('div', { class: 'alert warn' },
      t('設定ファイル（{path}）に [[roots]] がありません。', { path: st.config_path })),
    ...cards,
    h('p', { class: 'muted' }, t('設定: {config} ／ 隔離先: {quarantine} ／ スナップショット: {snapper} ／ v{version}', { config: st.config_path, quarantine: st.quarantine_dir, snapper: st.snapper_config || t('撮らない'), version: st.version })),
  );
}

// ---------- 画面: 移動の確認（内容ハッシュでは決められなかったもの。F-ID-2） ----------

async function pageRelink(main, root) {
  const { items } = await api(`/api/relink/${enc(root)}`);
  const decide = async (payload, label) => {
    try {
      await api(`/api/relink/${enc(root)}`, payload);
      toast(label);
      app.render();
    } catch (e) { toast(e.message, 'danger'); }
  };
  setChildren(main,
    h('h1', {}, t('移動の確認: {root}', { root })),
    h('p', { class: 'muted' }, '新しく現れたファイルと同じ内容のものが、少し前に複数の場所から消えています。どれが動いてきたものかを選ぶと、そのタグ・属性・関係・版の履歴が引き継がれます。どれでもなければ「新しいファイル」を選んでください。'),
    items.length ? items.map((it) => h('section', { class: 'card' },
      h('h2', { class: 'path' }, h('a', { href: browseHref(root, it.path.split('/').slice(0, -1).join('/'), '', it.path.split('/').pop()) }, it.path)),
      h('div', { class: 'muted small' }, t('{size}・{when} に現れた', { size: size(it.size), when: when(it.first_seen) })),
      h('div', { class: 'table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, '移動元の候補'), h('th', { class: 'hide-narrow' }, '最後に見えた'), h('th', {}, ''))),
        h('tbody', {}, it.candidates.map((c) => h('tr', {},
          h('td', { class: 'path' }, c.path),
          h('td', { class: 'hide-narrow muted' }, when(c.last_seen)),
          h('td', {}, h('button', { class: 'small', onclick: () => decide({ new_id: it.id, old_id: c.id }, t('結び付けました: {path}', { path: c.path })) }, 'これが移動元'))))))),
      h('div', { class: 'buttons' }, h('button', { class: 'small secondary', onclick: () => decide({ new_id: it.id, skip: true }, t('新しいファイルとして扱います: {path}', { path: it.path })) }, '新しいファイル（どれでもない）'))))
      : h('p', { class: 'muted' }, '確認待ちのものはありません。'),
    h('p', {}, h('a', { href: '#/' }, t('ルートの一覧に戻る'))));
}

// ---------- 画面: 確認（AI の読み取りの結果の承認・却下。F-AI-2） ----------

async function pageReview(main, rootArg) {
  const root = rootArg || (await refreshState()).roots[0]?.name;
  if (!root) { setChildren(main, h('p', { class: 'muted' }, 'ルートがありません。')); return; }
  const data = await api(`/api/review/${enc(root)}`);
  // 項目ごとに、付けるかどうかのチェック（既定はすべて付ける）
  const picks = new Map(data.items.map((it) => [it.path, {
    tags: new Set(it.proposal.tags), attrs: new Set(Object.keys(it.proposal.attrs)), selected: false,
  }]));
  const decide = async (items, decision) => {
    if (!items.length) { toast(t('選んでください'), 'warn'); return; }
    try {
      const r = await api(`/api/review/${enc(root)}`, {
        items: items.map((it) => {
          const p = picks.get(it.path);
          return {
            path: it.path, sha256: it.analysis.sha256, decision,
            tags: [...p.tags], attrs: Object.fromEntries(Object.entries(it.proposal.attrs).filter(([k]) => p.attrs.has(k))),
          };
        }),
      });
      toast(decision === 'accept' ? t('{n} 件を承認しました（タグ {tags}・属性 {attrs}）', { n: r.accept, tags: r.tags, attrs: r.attrs }) : t('{n} 件を却下しました', { n: r.reject }));
      app.render();
    } catch (e) { toast(e.message, 'danger'); }
  };
  const fieldLabels = { doc_type: '種類', date: '日付', party: '相手', amount: '金額', due: '期限', action: '必要な対応', summary: '要約', kind: '画像の種類', description: '説明' };
  const cards = data.items.map((it) => {
    const p = picks.get(it.path);
    const d = it.analysis.data;
    const dir = it.path.split('/').slice(0, -1).join('/');
    const name = it.path.split('/').pop();
    const sel = h('input', { type: 'checkbox', onchange: (e) => { p.selected = e.target.checked; } });
    const facts = Object.entries(fieldLabels).filter(([k]) => d[k] !== null && d[k] !== undefined && d[k] !== '')
      .map(([k, label]) => h('div', { class: 'small' }, h('span', { class: 'muted' }, t(label) + ': '), String(d[k])));
    const check = (set, key, text) => h('label', { class: 'chip-check' },
      h('input', { type: 'checkbox', checked: set.has(key), onchange: (e) => { if (e.target.checked) set.add(key); else set.delete(key); } }), ' ', text);
    return h('section', { class: 'card review-card' },
      h('div', { class: 'toolbar' }, sel,
        h('a', { class: 'path', href: browseHref(root, dir, '', name) }, `${fileIcon(name)} ${it.path}`),
        h('span', { class: 'muted small' }, t('{model} が読み取り（{when}）', { model: it.analysis.model, when: when(it.analysis.created_at) }))),
      h('div', { class: 'review-facts' }, facts),
      h('div', { class: 'chips' },
        it.proposal.tags.map((tg) => check(p.tags, tg, tg)),
        Object.entries(it.proposal.attrs).map(([k, v]) => check(p.attrs, k, attrText(k, v)))),
      h('div', { class: 'buttons' },
        h('button', { class: 'small primary', onclick: () => decide([it], 'accept') }, '承認（選んだものを付ける）'),
        h('button', { class: 'small', onclick: () => decide([it], 'reject') }, '却下')));
  });
  const selected = () => data.items.filter((it) => picks.get(it.path).selected);
  const busy = jobRunning();
  setChildren(main,
    h('h1', {}, t('確認: {root}', { root })),
    h('p', { class: 'muted' }, 'モデルが書類・画像から読み取った内容です。付けたいタグと属性を残して承認すると、出所「ai」として付きます。却下したものは、もう出しません。'),
    !data.allow_cloud ? h('div', { class: 'alert info' }, 'このルートは内容を外部へ送らない設定なので、手元のモデルで読み取ります。') : null,
    h('div', { class: 'toolbar' },
      h('button', { class: 'primary', disabled: busy || !data.unread, onclick: () => startJob(`/api/analyze/${enc(root)}`, { limit: 20 }) },
        t('まだ読み取っていないものを読み取る（新しい順に 20 件。残り {n} 件）', { n: num(data.unread) })),
      data.items.length ? [
        h('button', { onclick: () => decide(selected(), 'accept') }, '選んだものを承認'),
        h('button', { onclick: () => decide(selected(), 'reject') }, '選んだものを却下')] : null),
    h('p', { class: 'muted small' }, data.auto ? '新しい書類・画像は、裏で自動で読み取ります（[ai] auto）。' : '自動では読み取りません（設定の [ai] auto = true で、裏で読み取ります）。'),
    cards.length ? cards : h('p', { class: 'muted' }, '確認待ちのものはありません。'));
}

function stat(label, value) {
  return h('div', { class: 'stat' }, h('div', { class: 'label' }, label), h('div', { class: 'value' }, value));
}

function enc(s) { return encodeURIComponent(s); }

// ---------- 画面: レポートとプランの作成 ----------

async function pageReport(main, root) {
  setChildren(main, h('p', { class: 'muted' }, 'レポートを作っています…'));
  const r = await api(`/api/report/${enc(root)}`);
  const byCat = {};
  for (const f of r.findings) (byCat[f.category] ||= []).push(f);
  const cats = r.category_order.filter((c) => byCat[c]);
  const review = r.findings.filter((f) => f.needs_review);

  // プランの作成: カテゴリを選ぶ（既定はすべて）
  const checks = cats.map((c) => {
    const items = byCat[c].filter((f) => !f.needs_review);
    const box = h('input', { type: 'checkbox', checked: true, value: r.category_keys[c] });
    return { box, label: h('label', {}, box, t('{cat}（{n} 件、{size}）', { cat: translateString(c), n: num(items.length), size: size(sum(items, 'size')) })) };
  });
  const reviewBox = h('input', { type: 'checkbox' });
  const form = h('section', { class: 'card' },
    h('h2', {}, 'プランを作る'),
    h('p', { class: 'muted' }, '選んだカテゴリの候補を、隔離するプランにまとめます。作った後に1件ずつ外せます。ファイルはまだ動きません。'),
    h('div', { class: 'form-grid' }, checks.map((c) => c.label)),
    review.length ? h('label', {}, reviewBox, ' ' + t('中身の確認が要る候補（{n} 件）も含める', { n: num(review.length) })) : null,
    h('div', { class: 'buttons', style: 'margin-top:.75rem' },
      h('button', {
        class: 'primary', disabled: jobRunning(), onclick: () => {
          const categories = checks.filter((c) => c.box.checked).map((c) => c.box.value);
          if (!categories.length) { toast('カテゴリを1つ以上選んでください', 'warn'); return; }
          startJob('/api/plans', { root, categories, include_review: reviewBox.checked });
        },
      }, 'プランを作る')));

  const groups = cats.map((c) => {
    const items = [...byCat[c]].sort((a, b) => b.size - a.size);
    return h('details', { class: 'group' },
      h('summary', {}, h('span', { class: 'title' }, c),
        h('span', { class: 'muted' }, t('{n} 件・ファイル {files}・{size}', { n: num(items.length), files: num(sum(items, 'files')), size: size(sum(items, 'size')) }))),
      r.category_help && r.category_help[c] ? h('p', { class: 'muted small' }, translateString(r.category_help[c])) : null,
      h('div', { class: 'body table-wrap' }, findingTable(items, root)));
  });

  // 構造の見直しの提案（F-OR-5）: 種類ごとにまとめ、ファイルブラウザで見直せるようにする
  const issuesBy = {};
  for (const i of r.structure || []) (issuesBy[i.kind] ||= []).push(i);
  const structure = Object.keys(issuesBy).length ? [
    h('h2', {}, '構造の見直しの提案'),
    h('p', { class: 'muted' }, '人の判断が要るので、自動では何もしません。フォルダを開いて見直すか、「残す」で以後は出さないようにできます。保管・まとまり・アプリ管理の型のフォルダの中は見ていません。'),
    ...Object.entries(issuesBy).map(([kind, list]) => h('details', { class: 'group' },
      h('summary', {}, h('span', { class: 'title' }, translateString(list[0].label)), h('span', { class: 'muted' }, t('{n} 件', { n: num(list.length) }))),
      h('div', { class: 'body table-wrap' }, h('table', {},
        h('thead', {}, h('tr', {}, h('th', {}, 'フォルダ'), h('th', {}, '内容'), h('th', {}, ''))),
        h('tbody', {}, list.map((i) => h('tr', {},
          h('td', { class: 'path' }, h('a', { href: browseHref(root, i.path) }, i.path || root),
            i.paths.filter((p) => p !== i.path).map((p) => h('div', {}, h('a', { href: browseHref(root, p) }, p)))),
          h('td', { class: 'muted' }, i.detail),
          h('td', {}, h('button', { class: 'small', onclick: () => keepDialog(root, i.path) }, '残す…'))))))))),
  ] : [];

  // 「残す」と判断したもの（外すと、次のレポートからまた出る）
  const kept = (r.kept || []).length ? h('details', { class: 'group' },
    h('summary', {}, h('span', { class: 'title' }, '「残す」と判断したもの'), h('span', { class: 'muted' }, t('{n} 件', { n: num(r.kept.length) }))),
    h('div', { class: 'body table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'パス'), h('th', {}, '理由'), h('th', {}, ''))),
      h('tbody', {}, r.kept.map((k) => h('tr', {},
        h('td', { class: 'path' }, k.path), h('td', { class: 'muted' }, k.reason),
        h('td', {}, h('button', { class: 'small', onclick: async () => {
          try { await api('/api/keep', { root, path: k.path, op: 'remove' }); app.render(); } catch (e) { toast(e.message, 'danger'); }
        } }, '外す')))))))) : null;

  const dup = r.duplicates.length ? h('details', { class: 'group' },
    h('summary', {}, h('span', { class: 'title' }, '内容が同じファイル'),
      h('span', { class: 'muted' }, t('{n} 組・余分な容量 {waste}（上位 {top} 組を表示）', { n: num(r.duplicate_groups), waste: size(r.duplicate_waste), top: num(r.duplicates.length) }))),
    h('div', { class: 'body table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', { class: 'num' }, 'サイズ'), h('th', { class: 'num' }, '個数'), h('th', { class: 'num' }, '余分'), h('th', {}, 'パス'))),
      h('tbody', {}, r.duplicates.map((g) => h('tr', {},
        h('td', { class: 'num' }, size(g.size)), h('td', { class: 'num' }, g.paths.length),
        h('td', { class: 'num' }, size(g.waste)), h('td', { class: 'path' }, g.paths.map((p) => h('div', {}, p))))))))) : null;

  setChildren(main, 
    h('h1', {}, t('レポート: {root}', { root })),
    h('div', { class: 'stats' },
      stat('ファイル', num(r.files)), stat('フォルダ', num(r.dirs)), stat('合計', size(r.total_bytes)),
      stat('ハッシュ計算済み', num(r.hashed_files)),
      stat('候補', t('{n} 件・{size}', { n: num(r.findings.length), size: size(sum(r.findings, 'size')) }))),
    form,
    h('h2', {}, '置く価値の薄いものの候補'),
    ...groups,
    dup,
    ...structure,
    kept,
  );
}

/** 候補・見直しの提案を「残す」と記録する（理由を書いてもらう）。以後のレポートには出さない。 */
async function keepDialog(root, path) {
  const input = h('input', { class: 'name-input', placeholder: t('理由（例: 検証用に残す・別のバックアップが無い）') });
  setTimeout(() => input.focus(), 50);
  const ok = await confirmDialog(t('「{path}」を残しますか？', { path: path || root }), [
    input, h('p', { class: 'muted small' }, t('以後、整理の候補と構造の見直しに出しません（配下も含む）。レポートの「「残す」と判断したもの」から外せます。')),
  ], t('残す'));
  if (!ok) return;
  try { await api('/api/keep', { root, path, reason: input.value }); app.render(); } catch (e) { toast(e.message, 'danger'); }
}

function sum(items, key) { return items.reduce((a, x) => a + (x[key] || 0), 0); }

function findingTable(items, root) {
  return h('table', {},
    h('thead', {}, h('tr', {}, h('th', {}, 'パス'), h('th', { class: 'hide-narrow' }, '理由'), h('th', { class: 'num' }, 'ファイル'), h('th', { class: 'num' }, '容量'), root ? h('th', {}, '') : null)),
    h('tbody', {}, items.map((f) => h('tr', {},
      h('td', { class: 'path' }, f.relpath + (f.is_dir ? '/' : ''), f.needs_review ? [' ', h('span', { class: 'badge warn' }, t('要確認'))] : null),
      h('td', { class: 'hide-narrow muted' }, f.reason),
      h('td', { class: 'num' }, num(f.files)),
      h('td', { class: 'num' }, size(f.size)),
      root ? h('td', {}, h('button', { class: 'small', onclick: () => keepDialog(root, f.relpath) }, '残す…')) : null))));
}

// ---------- 画面: プランの一覧 ----------

async function pagePlans(main) {
  const st = await refreshState();
  const rows = st.plans.map((p) => h('tr', {},
    h('td', {}, h('a', { href: `#/plans/${p.id}` }, p.id)),
    h('td', {}, p.root),
    h('td', {}, stateBadge(p.state), p.sync_paused !== null && p.sync_paused !== undefined ? [' ', h('span', { class: 'badge danger' }, '同期停止中')] : null),
    h('td', { class: 'num' }, num(p.ops)),
    h('td', { class: 'num' }, size(p.size)),
    h('td', { class: 'num hide-narrow' }, p.quarantined ? t('{n} 件・{size}', { n: num(p.quarantined), size: size(p.quarantined_size) }) : '-')));
  const quarantined = st.plans.reduce((a, p) => a + (p.quarantined_size || 0), 0);
  setChildren(main, 
    h('h1', {}, 'プラン'),
    quarantined ? h('p', { class: 'muted' }, t('隔離フォルダには合計 {size} があります。プランを開くと、取り消すか、隔離を空にできます。', { size: size(quarantined) })) : null,
    st.plans.length ? h('div', { class: 'card table-wrap' }, h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'ID'), h('th', {}, 'ルート名'), h('th', {}, '状態'), h('th', { class: 'num' }, '件数'), h('th', { class: 'num' }, '容量'), h('th', { class: 'num hide-narrow' }, '隔離中'))),
      h('tbody', {}, rows)))
      : h('p', { class: 'muted' }, 'まだプランがありません。ルートの「レポートとプランの作成」から作れます。'));
}

// ---------- 画面: プランの中身・実行・取り消し ----------

async function pagePlan(main, id) {
  const data = await api(`/api/plans/${enc(id)}`);
  const info = data.info;
  const editable = info.state === 'draft';
  const problems = app.checks[id] || {};
  // 除外の状態は画面上で持ち、変えるたびに少し待ってから保存する
  const skip = new Set(data.ops.filter((o) => o.skip).map((o) => o.path));
  let filter = '';

  const byCat = {};
  for (const op of data.ops) (byCat[op.category] ||= []).push(op);
  const cats = [...data.category_order.filter((c) => byCat[c]), ...Object.keys(byCat).filter((c) => !data.category_order.includes(c))];

  const selectionEl = h('span', { class: 'selection' });
  const syncNote = h('div');
  const groupsEl = h('div');

  function selected() { return data.ops.filter((o) => !skip.has(o.path)); }

  /** 大量削除として同期クライアントと連携することになるか（フォルダのファイル数による目安）。 */
  function willSync(ops) {
    const s = data.sync;
    if (!s || s.error) return false;
    if (s.mode === 'always') return true;
    return ops.some((o) => o.is_dir && o.files >= s.threshold);
  }

  function updateSummary() {
    const sel = selected();
    selectionEl.textContent = t('選択 {sel} / {all} 件・ファイル {files}・{size}', { sel: num(sel.length), all: num(data.ops.length), files: num(sum(sel, 'files')), size: size(sum(sel, 'size')) });
    setChildren(syncNote, willSync(sel) ? h('div', { class: 'alert info' },
      h('strong', {}, '大量の削除になります。'),
      ' ' + (data.sync.client === 'cloud-sync'
        ? t('実行すると、cloud-sync の定期の同期（{service}）を止め、動いている同期が終わるのを待って隔離し、削除をクラウドへ反映してから再開します。', { service: data.sync.service })
        : t('実行すると、{client} の常駐の同期（{service}）を一時停止し、隔離した後に削除をクラウドへ反映してから再開します。{client} の大量削除の安全装置（{n} 件）で同期が止まることはありません。',
          { client: data.sync.client, service: data.sync.service, n: num(data.sync.threshold) }))) : '');
  }

  let saveTimer = null;
  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try { await api(`/api/plans/${enc(id)}/skips`, { skip: [...skip] }); }
      catch (e) { toast(t('保存できませんでした: {error}', { error: e.message }), 'danger'); }
    }, 600);
  }

  function setSkip(paths, excluded) {
    for (const p of paths) { if (excluded) skip.add(p); else skip.delete(p); }
    updateSummary();
    renderGroups();
    scheduleSave();
  }

  function statusCell(op) {
    if (op.outcome) {
      const o = op.outcome;
      const map = {
        quarantine: ['隔離済み', 'ok'], skip: [t('飛ばした: {detail}', { detail: translateString(o.detail) }), 'warn'],
        restore: ['元に戻した', ''], 'restore-skip': [t('戻せなかった: {detail}', { detail: translateString(o.detail) }), 'danger'],
        purge: ['完全に削除した', ''], 'purge-skip': [t('消せなかった: {detail}', { detail: translateString(o.detail) }), 'danger'],
      };
      const [label, kind] = map[o.event] || [o.event, ''];
      return h('span', { class: `badge ${kind}` }, label);
    }
    if (problems[op.path]) return h('span', { class: 'badge warn' }, problems[op.path]);
    if (app.checks[id] && !skip.has(op.path)) return h('span', { class: 'badge ok' }, 'OK');
    return '';
  }

  // 並べ替え（既定は容量の大きい順。項目と向きはブラウザに覚える）
  const planSort = sortState('plan', { field: 'size', dir: 'desc' });
  const planGetters = { path: (o) => o.path, reason: (o) => o.reason, files: (o) => o.files, size: (o) => o.size };

  function renderGroups() {
    const open = new Set([...groupsEl.querySelectorAll('details[open]')].map((d) => d.dataset.cat));
    const q = filter.toLowerCase();
    setChildren(groupsEl, ...cats.map((c) => {
      const all = byCat[c];
      const items = sortItems([...all].filter((o) => !q || o.path.toLowerCase().includes(q)), planSort, planGetters, (o) => o.path);
      if (!items.length) return null;
      const on = all.filter((o) => !skip.has(o.path));
      const box = h('input', {
        type: 'checkbox', disabled: !editable, checked: on.length > 0,
        onclick: (e) => e.stopPropagation(),
        onchange: (e) => setSkip(all.map((o) => o.path), !e.target.checked),
      });
      box.indeterminate = on.length > 0 && on.length < all.length;
      const details = h('details', { class: 'group', 'data-cat': c, open: open.has(c) || !!q },
        h('summary', {}, box, h('span', { class: 'title' }, c),
          h('span', { class: 'muted' }, t('{on} / {all} 件・{size}', { on: num(on.length), all: num(all.length), size: size(sum(on, 'size')) }))),
        h('div', { class: 'body table-wrap' }, h('table', {},
          h('thead', {}, h('tr', {}, h('th', {}, ''),
            sortHeader(t('パス'), 'path', planSort, renderGroups),
            sortHeader(t('理由'), 'reason', planSort, renderGroups, 'hide-narrow'),
            sortHeader(t('ファイル数'), 'files', planSort, renderGroups, 'num'),
            sortHeader(t('容量'), 'size', planSort, renderGroups, 'num'),
            h('th', {}, '状態'))),
          h('tbody', {}, items.map((op) => h('tr', { class: skip.has(op.path) ? 'excluded' : '' },
            h('td', {}, h('input', {
              type: 'checkbox', disabled: !editable, checked: !skip.has(op.path),
              onchange: (e) => setSkip([op.path], !e.target.checked),
            })),
            h('td', { class: 'path' }, op.path + (op.is_dir ? '/' : ''),
              op.action === 'move' ? h('div', { class: 'muted' }, '→ ', op.dest) : null),
            h('td', { class: 'hide-narrow muted' }, op.reason),
            h('td', { class: 'num' }, num(op.files)),
            h('td', { class: 'num' }, size(op.size)),
            h('td', { class: 'status' }, statusCell(op))))))));
      return details;
    }));
  }

  async function doApply() {
    const sel = selected();
    const body = [
      h('p', {}, t('{n} 件（ファイル {files}、{size}）を隔離フォルダへ移します。削除はしません。', { n: num(sel.length), files: num(sum(sel, 'files')), size: size(sum(sel, 'size')) })),
      h('ul', {},
        h('li', {}, t('隔離先: {path}', { path: `${app.state?.quarantine_dir || ''}/${id}/` })),
        h('li', {}, app.state?.snapper_config ? t('実行の前後にスナップショットを撮ります（snapper: {config}）', { config: app.state.snapper_config }) : 'スナップショットは撮りません'),
        h('li', {}, '移す直前に、プランの作成時から変わったものは飛ばします'),
        willSync(sel) ? h('li', {}, t('{client} の常駐の同期を一時停止し、削除をクラウドへ反映してから再開します', { client: data.sync.client })) : null,
        h('li', {}, 'クラウドからは消えますが、隔離フォルダを空にするまでこのマシンには残ります。いつでも取り消せます')),
    ];
    if (await confirmDialog('プランを実行しますか？', body, '実行する')) {
      clearTimeout(saveTimer);
      await api(`/api/plans/${enc(id)}/skips`, { skip: [...skip] }).catch(() => {});
      startJob(`/api/plans/${enc(id)}/apply`);
    }
  }

  async function doUndo() {
    const ok = await confirmDialog('取り消しますか？',
      h('p', {}, t('隔離した {n} 件を元の場所へ戻します。元の場所に新しくできたものは上書きしません。戻したものは同期クライアントが再びアップロードします。', { n: num(data.restorable) })),
      '元に戻す');
    if (ok) startJob(`/api/plans/${enc(id)}/undo`);
  }

  /** 隔離したものを完全に消す。元に戻せないことを示し、承認されたときだけ実行する。 */
  async function doPurge() {
    const body = [
      h('p', {}, h('strong', {}, t('隔離した {n} 件（{size}）を完全に削除します。', { n: num(data.purgeable), size: size(data.purgeable_size) }))),
      h('ul', {},
        h('li', {}, '削除したものは、この画面からは元に戻せません。このプランの取り消しもできなくなります。'),
        h('li', {}, t('対象: {path}', { path: `${app.state?.quarantine_dir || ''}/${id}/` })),
        h('li', {}, app.state?.snapper_config
          ? t('削除の前後にスナップショットを撮ります（snapper: {config}）。スナップショットが残っている間は、そこから手作業で取り出せますが、ディスクの容量もその間は戻りません。', { config: app.state.snapper_config })
          : 'スナップショットは撮りません。削除したものは取り出せません。')),
    ];
    if (await confirmDialog('隔離を空にしますか？', body, '完全に削除する', true)) startJob(`/api/plans/${enc(id)}/purge`);
  }

  async function doDelete() {
    if (await confirmDialog('プランを削除しますか？', h('p', {}, 'プランのファイルを消します。ファイル自体には何もしません。'), '削除する', true)) {
      try { await api(`/api/plans/${enc(id)}/delete`, {}); location.hash = '#/plans'; }
      catch (e) { toast(e.message, 'danger'); }
    }
  }

  async function doForceResume() {
    const ok = await confirmDialog('反映せずに同期を再開しますか？',
      [h('p', {}, '削除をクラウドに反映しないまま、常駐の同期を再開します。'),
        h('p', {}, '大量削除のままなら、同期クライアントはまた止まります。状況を確かめた上で選んでください。')],
      '再開する', true);
    if (ok) startJob(`/api/plans/${enc(id)}/sync/resume`);
  }

  const paused = info.sync_paused !== null && info.sync_paused !== undefined;
  const busy = jobRunning();
  setChildren(main, 
    h('h1', {}, t('プラン {id}', { id })),
    h('div', { class: 'stats' },
      stat('状態', stateBadge(info.state)), stat('ルート名', info.root), stat('作成', when(info.created_at)),
      info.quarantined ? stat('隔離中', t('{n} 件・{size}', { n: num(info.quarantined), size: size(info.quarantined_size) })) : null),
    h('div', { class: 'muted mono' }, data.root_path),
    paused ? h('div', { class: 'alert danger' },
      h('strong', {}, '削除をクラウドに反映できず、常駐の同期を止めたままです。'),
      h('p', {}, info.sync_paused || ''),
      h('div', { class: 'buttons' },
        h('button', { class: 'primary', disabled: busy, onclick: () => startJob(`/api/plans/${enc(id)}/sync/retry`) }, '削除の反映を再試行'),
        h('button', { disabled: busy, onclick: doForceResume }, '反映せずに同期を再開…'))) : null,
    h('div', { class: 'buttons', style: 'margin:.75rem 0' },
      editable ? h('button', { disabled: busy, onclick: () => startJob(`/api/plans/${enc(id)}/check`) }, '変わっていないか確認') : null,
      editable ? h('button', { class: 'primary', disabled: busy, onclick: doApply }, '実行する…') : null,
      data.restorable ? h('button', { disabled: busy, onclick: doUndo }, t('取り消す（{n} 件）…', { n: num(data.restorable) })) : null,
      data.purgeable ? h('button', { class: 'danger', disabled: busy, onclick: doPurge }, t('隔離を空にする（{size}）…', { size: size(data.purgeable_size) })) : null,
      editable ? h('button', { class: 'secondary', disabled: busy, onclick: doDelete }, 'プランを削除') : null),
    syncNote,
    h('div', { class: 'toolbar' },
      h('input', { type: 'search', placeholder: 'パスで絞り込む', oninput: (e) => { filter = e.target.value; renderGroups(); } }),
      selectionEl,
      editable ? h('span', { class: 'muted' }, 'チェックを外したものは実行しません（自動で保存）') : null),
    groupsEl,
  );
  updateSummary();
  renderGroups();
}

// ---------- 画面の切り替え ----------

async function route() {
  const main = document.getElementById('main');
  // # の後ろは「パス?問い合わせ」（例: #/browse/onedrive/99_Inbox?at=17533）
  const [hashPath, hashQuery] = (location.hash.replace(/^#/, '') || '/').split('?');
  const parts = hashPath.split('/').filter(Boolean).map(decodeURIComponent);
  const params = new URLSearchParams(hashQuery || '');
  // #/tags は前の版の URL（ブックマーク）のため。検索の画面を開く
  const navOf = { report: 'roots', relink: 'roots', browse: 'browse', organize: 'organize', search: 'search', tags: 'search', review: 'review', plans: 'plans' };
  document.querySelectorAll('[data-nav]').forEach((a) => a.classList.toggle('active', (navOf[parts[0]] || 'roots') === a.dataset.nav));
  let render;
  if (parts[0] === 'browse') render = () => pageBrowse(main, parts[1], parts.slice(2).join('/'), params);
  else if (parts[0] === 'organize') render = () => pageOrganize(main, parts[1]);
  else if (parts[0] === 'search' || parts[0] === 'tags') render = () => pageTags(main, params);
  else if (parts[0] === 'report' && parts[1]) render = () => pageReport(main, parts[1]);
  else if (parts[0] === 'relink' && parts[1]) render = () => pageRelink(main, parts[1]);
  else if (parts[0] === 'review') render = () => pageReview(main, parts[1]);
  else if (parts[0] === 'plans' && parts[1]) render = () => pagePlan(main, parts[1]);
  else if (parts[0] === 'plans') render = () => pagePlans(main);
  else render = () => pageRoots(main);
  app.render = async () => {
    try { await render(); }
    catch (e) { setChildren(main, h('div', { class: 'alert danger' }, e.message)); }
  };
  await app.render();
}

window.addEventListener('hashchange', route);
/** 画面上部の言語とテーマの切り替え。 */
function setupTopbarTools() {
  const lang = document.getElementById('lang-select');
  lang.value = i18n.lang;
  // 選択肢は言語名をそれぞれの言葉で出すので自動では訳さない。説明だけをここで付け直す
  lang.setAttribute('aria-label', t('言語'));
  lang.addEventListener('change', () => {
    i18n.set(lang.value);
    lang.setAttribute('aria-label', t('言語'));
    showTheme();
    // t() で組み立てた文は描き直す（固定の文は i18n.set が訳し直す）
    if (app.render) app.render();
    renderJob();
  });
  const btn = document.getElementById('theme-button');
  const icons = { auto: '◐', light: '☀', dark: '☾' };
  const labels = { auto: 'OS に合わせる', light: 'ライト', dark: 'ダーク' };
  function showTheme() {
    const mode = theme.current();
    btn.textContent = icons[mode];
    btn.title = `${t('テーマ')}: ${t(labels[mode])}`;
  }
  btn.addEventListener('click', () => {
    const modes = theme.modes;
    theme.apply(modes[(modes.indexOf(theme.current()) + 1) % modes.length]);
    showTheme();
  });
  showTheme();
}

window.addEventListener('DOMContentLoaded', () => { setupTopbarTools(); route(); pollJob(); pollSync(); });
