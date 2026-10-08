// 画面の表示言語（日本語・英語）。
//
// - 既定はブラウザの言語（navigator.languages）。画面上部で切り替えると、この端末のブラウザに覚える
// - 変数を含む文は、ソースで t('{n} 件を選択中', { n }) のように書き、言語ごとの語順で組み立てる
// - 固定の文（ボタン名など）と、サーバーから来る文（カテゴリ名・段階名など）は、表示されたテキストを辞書で訳す。
//   ファイル名・パス・タグなど利用者のデータ（translate="no"・.path・.mono・.chip など）は訳さない
// - 辞書に無い文は日本語のまま出す
'use strict';

const I18N_EN = {
  // --- 上部・共通 ---
  'ルート': 'Roots', 'ルート名': 'Root', 'ファイル': 'Files', '整理': 'Inbox', 'タグ': 'Tags', 'プラン': 'Plans',
  '読み込み中…': 'Loading…', 'やめる': 'Cancel', '実行する': 'Run', '閉じる': 'Close',
  '実行中': 'Running', '完了': 'Done', '失敗': 'Failed', 'もう一度': 'Try again', '付ける': 'Add', '外す': 'Remove',
  'ダウンロード': 'Download', '新しいタブで開く': 'Open in a new tab', 'フォルダを開く': 'Open folder', 'プレビュー': 'Preview',
  '名前': 'Name', 'サイズ': 'Size', '更新日時': 'Modified', '状態': 'Status', 'パス': 'Path', '理由': 'Reason',
  '件数': 'Items', '容量': 'Size', 'フォルダ': 'Folders', '合計': 'Total', '作成': 'Created', '候補': 'Candidates',
  '個数': 'Copies', '余分': 'Wasted', '時点': 'When', '時点:': 'As of:', '今': 'Now', '無い': 'None',
  '見る': 'View', '読めません': 'Unreadable', '中身': 'Contents', '採用': 'Accept', 'ファイルと内容': 'File and content',
  '言語': 'Language', 'テーマ': 'Theme', 'ブラウザに合わせる': 'Follow the browser', 'ライト': 'Light', 'ダーク': 'Dark',
  'OS に合わせる': 'Follow the system',

  // --- 処理の進み具合 ---
  'ログ': 'Log', '失敗しました': 'Failed', '終わりました': 'Finished',
  '件': 'items',

  // --- ルート ---
  '最新にする（走査＋ハッシュ）': 'Refresh (scan + hash)', '走査だけ': 'Scan only', 'レポートとプランの作成': 'Report and plans',
  'ハッシュ計算済み': 'Hashed', '最後の走査': 'Last scan', '消失の確定を保留': 'Disappearances on hold',
  'まだ走査していません。': 'Not scanned yet.', 'ルートがありません。': 'No roots.',
  '同期: 稼働中': 'Sync: running', '同期: 停止中': 'Sync: stopped', '同期: 失敗': 'Sync: failed', '同期: 状態を読めません': 'Sync: unknown state',
  '撮らない': 'none',

  // --- レポート ---
  'レポートを作っています…': 'Building the report…', 'プランを作る': 'Create a plan',
  '選んだカテゴリの候補を、隔離するプランにまとめます。作った後に1件ずつ外せます。ファイルはまだ動きません。':
    'Puts the candidates of the chosen categories into a plan that quarantines them. You can untick items afterwards. Nothing moves yet.',
  'カテゴリを1つ以上選んでください': 'Choose at least one category', '置く価値の薄いものの候補': 'Probably not worth keeping',
  '内容が同じファイル': 'Identical files', '要確認': 'Review',

  // --- プラン ---
  'まだプランがありません。ルートの「レポートとプランの作成」から作れます。': 'No plans yet. Create one from “Report and plans” on a root.',
  '隔離中': 'Quarantined', '未実行': 'Not run', '実行済み': 'Applied', '一部を取り消し': 'Partly undone', '取り消し済み': 'Undone',
  '途中で停止': 'Interrupted', '同期停止中': 'Sync stopped', 'ID': 'ID',
  '変わっていないか確認': 'Check for changes', '実行する…': 'Run…', 'プランを削除': 'Delete plan',
  'パスで絞り込む': 'Filter by path', 'チェックを外したものは実行しません（自動で保存）': 'Unticked items are skipped (saved automatically)',
  '大量の削除になります。': 'This is a big deletion.', 'プランを実行しますか？': 'Run this plan?',
  'スナップショットは撮りません': 'No snapshots are taken', '移す直前に、プランの作成時から変わったものは飛ばします': 'Anything that changed since the plan was made is skipped',
  'クラウドからは消えますが、隔離フォルダを空にするまでこのマシンには残ります。いつでも取り消せます':
    'Items leave the cloud but stay on this machine until you empty the quarantine folder. You can undo at any time',
  '取り消しますか？': 'Undo?', '元に戻す': 'Undo', 'プランを削除しますか？': 'Delete this plan?',
  'プランのファイルを消します。ファイル自体には何もしません。': 'Deletes the plan file. Your files are not touched.',
  '削除する': 'Delete', '反映せずに同期を再開しますか？': 'Restart the sync without reflecting the deletions?',
  '削除をクラウドに反映しないまま、常駐の同期を再開します。': 'Restarts the resident sync without reflecting the deletions in the cloud.',
  '大量削除のままなら、同期クライアントはまた止まります。状況を確かめた上で選んでください。': 'If the big deletion is still pending, the sync client will stop again. Check the situation first.',
  '再開する': 'Restart', '削除をクラウドに反映できず、常駐の同期を止めたままです。': 'The deletions could not be reflected in the cloud; the resident sync is still stopped.',
  '削除の反映を再試行': 'Retry reflecting deletions', '反映せずに同期を再開…': 'Restart sync without reflecting…',
  '常駐の同期が止まったままです': 'The resident sync is still stopped', 'プランを開いて再試行する': 'Open the plan to retry',
  'OK': 'OK', '隔離済み': 'Quarantined', '元に戻した': 'Restored',

  // --- ファイル ---
  'ファイルを選ぶと、ここにプレビュー・タグ・履歴を表示します。': 'Pick a file to see its preview, tags and history here.',
  '名前で絞り込む': 'Filter by name', '過去の時点を表示中（読み取りのみ）': 'Showing a past snapshot (read-only)',
  'このフォルダのタグ:': 'Tags on this folder:', 'タグなし': 'No tags', 'このフォルダにタグを付ける（配下に継承）': 'Tag this folder (inherited by its contents)',
  '選んだものに付けるタグ（例: 種別:請求書）': 'Tag for the selection (e.g. type:invoice)', '選んだものに付ける': 'Tag selection',
  '名前を変える…': 'Rename…', '削除…': 'Delete…', '受け皿': 'Inbox', '今は無い': 'Gone now',
  'タグを付ける': 'Add a tag', 'タグを外す': 'Remove tag', '同じ内容のファイル': 'Identical files', '版の履歴': 'Version history',
  '整理の提案': 'Suggestion', '隣に復元…': 'Restore beside…', '過去の版を復元しますか？': 'Restore this version?', '復元する': 'Restore',
  'この種類はプレビューできません。': 'No preview for this type.', 'タグの名前を入れてください': 'Enter a tag name',
  'ファイルかフォルダを選んでください': 'Select files or folders', '名前を変えるものを1つだけ選んでください': 'Select exactly one item to rename',
  '削除するものを選んでください': 'Select items to delete', 'ファイルの名前を変える': 'Rename file', 'フォルダの名前を変える': 'Rename folder',
  '名前を変える': 'Rename', '提案の名前を使う': 'Use the suggested name', '拡張子も含めて入力してください。': 'Include the extension.',
  'フォルダの中のタグとカタログの記録も新しい名前に追従します。': 'Tags and catalog records inside the folder follow the new name.',
  '実行の前後にスナップショットを撮り、プランの画面から取り消せます。': 'Snapshots are taken before and after, and you can undo from the plan page.',
  'まだ提案がありません（詳細の欄の「整理の提案を出す」で作れます）': 'No suggestion yet (ask for one in the details panel)',
  '完全には消さず、隔離フォルダへ移します。同期しているクラウドからは消えますが、このマシンには隔離フォルダを空にするまで残り、プランの画面から元に戻せます。':
    'Nothing is deleted for good: items go to the quarantine folder. They leave the synced cloud but stay on this machine until you empty the quarantine folder, and you can restore them from the plan page.',
  '中身の多いフォルダは、同期クライアントの大量削除の安全装置で同期が止まらないよう、同期を一時停止してから反映します。':
    'For folders with many items, the sync is paused and the deletion reflected first, so the sync client’s big-delete guard does not stop it.',
  '/（中身ごと）': '/ (with contents)', '名前を変えています。': 'Renaming.', '隔離フォルダへ移しています。': 'Moving to quarantine.',
  '取り消すには': 'To undo, open', 'プランの画面': 'the plan page', 'から。': '.',

  // --- タグ ---
  'まだタグがありません。「ファイル」の画面で、ファイルやフォルダに付けられます。': 'No tags yet. Add them to files and folders on the Files page.',
  'フォルダに付いたものは、その配下のすべてが対象です。': 'A tag on a folder applies to everything under it.',
  '見つからない': 'Not found',

  // --- 整理 ---
  '受け皿のフォルダを調べています…': 'Looking through the inboxes…', '名前やフォルダで絞り込む': 'Filter by name or folder',
  '選んだら提案を出す': 'Suggest on select', '次の2件を先読み': 'Read the next two ahead', '採用したものを隠す': 'Hide accepted',
  '裏でまとめて作る（時間がかかります）': 'Build them all in the background (slow)',
  '左の一覧からファイルを選ぶと、プレビューと提案（名前・移動先・タグ）を表示します。↑↓ か j / k で移動、名前の欄で Enter を押すと採用して次へ進みます。':
    'Pick a file on the left to see a preview and a suggestion (name, folder, tags). Move with ↑↓ or j / k; press Enter in the name field to accept and go to the next.',
  'ファイルを選ぶと、モデルが内容を読んで名前・移動先・タグを提案します。採用したものは採用リストにたまり、まとめて実行できます。':
    'Pick a file and a model reads it and suggests a name, a folder and tags. Accepted suggestions collect in the accept list, which runs in one go.',
  '採用リスト': 'Accept list', '提案あり': 'Suggested', '作成中': 'Working', '重複': 'Duplicate',
  '確認して実行…': 'Review and run…', 'プランとして保存': 'Save as a plan', '空にする': 'Clear',
  '提案を読み込み中…': 'Loading suggestion…', 'まだ提案がありません。': 'No suggestion yet.', '提案を出す': 'Suggest',
  'カタログにまだありません。ルートを走査してから選んでください。': 'Not in the catalog yet. Scan the root first.',
  '新しい名前': 'New name', '移動先': 'Destination', '過去の例': 'Past decision', '新しいフォルダ': 'New folder',
  'その他のフォルダ…': 'Another folder…', 'フォルダのパスを入力（候補が出ます）': 'Type a folder path (suggestions appear)',
  '既存のタグ': 'Existing tag', '新しいタグ': 'New tag', '新規': 'New', '却下（次から提案しにくくなる）': 'Reject (suggested less from now on)',
  '自分でタグを付ける': 'Add your own tag', '採用リストに入れる': 'Add to accept list', 'Enter でも採用できます': 'Enter also accepts',
  '作り直す': 'Redo', '作り直しています…': 'Redoing…', '日付は更新日から（書類の中に見つからなかった）': 'Date taken from the file (none found in the document)',
  '名前を入れてください': 'Enter a name', '名前も場所も変わりません': 'Neither the name nor the folder changes',
  '同じ内容のファイルが既にあります:': 'Identical files already exist:', '採用リストは「整理」の画面からまとめて実行できます': 'Run the accept list from the Inbox page',
  '移動先に同じ名前があるもの・選んだ後に変わったものは飛ばします。実行の前後にスナップショットを撮り、プランの画面から取り消せます。':
    'Items whose destination already exists, or that changed after you picked them, are skipped. Snapshots are taken before and after, and you can undo from the plan page.',
  'モデル（☁ はクラウドのモデル。ファイルの内容がクラウドに送られる）': 'Model (☁ = cloud model; file contents are sent to the cloud)',

  // --- 配置・並べ替え ---
  'ドラッグで幅を変える（ダブルクリックで元に戻す）': 'Drag to resize (double-click to reset)', '押すと並べ替え': 'Click to sort',
  '並べ替え': 'Sort', 'フォルダ順': 'By folder', '名前順': 'By name', '新しい順': 'Newest first', '古い順': 'Oldest first',
  '大きい順': 'Largest first', '小さい順': 'Smallest first', '提案の無いものから': 'Unsuggested first', '提案のあるものから': 'Suggested first',
  'ファイル数': 'Files',

  // --- クラウド・Office ---
  'OneDrive で開く': 'Open in OneDrive', 'ブラウザ版の Office で開く': 'Open in Office for the web',
  'PDF に変換しています（初回は少しかかります）…': 'Converting to PDF (the first time takes a moment)…',
  'レイアウトどおり（PDF）': 'As laid out (PDF)', '文字と表だけ（速い）': 'Text and tables only (fast)',
  '表示しきれない部分は省いています': 'Some content is left out',

  // --- 変数を含む文（t() で使う） ---
  '{h}時間{m}分': '{h} h {m} min', '{m}分{s}秒': '{m} min {s} s', '{s}秒': '{s} s',
  '同期: {state}': 'Sync: {state}',
  'プラン {id} の削除をクラウドに反映できていません。': 'The deletions of plan {id} have not reached the cloud.',
  '理由: {reason}': 'Reason: {reason}',
  '失敗しました: {title}: {error}': 'Failed: {title}: {error}', '終わりました: {title}': 'Finished: {title}',
  '速さ {rate}/秒': 'Speed {rate}/s', '残り {eta}': '{eta} left', '経過 {elapsed}': 'Elapsed {elapsed}',
  'ログ（{file}）': 'Log ({file})',
  '同期クライアント: {client}（{service}）。子が {n} 件以上のフォルダを消すときは、同期を一時停止して削除を反映してから再開します。':
    'Sync client: {client} ({service}). When a folder with {n} or more items is removed, the sync is paused, the deletion reflected, and the sync restarted.',
  '設定ファイル（{path}）に [[roots]] がありません。': 'The config file ({path}) has no [[roots]].',
  '設定: {config} ／ 隔離先: {quarantine} ／ スナップショット: {snapper} ／ v{version}': 'Config: {config} / Quarantine: {quarantine} / Snapshots: {snapper} / v{version}',
  '{cat}（{n} 件、{size}）': '{cat} ({n} items, {size})',
  '中身の確認が要る候補（{n} 件）も含める': 'Include candidates that need review ({n})',
  '{n} 件・ファイル {files}・{size}': '{n} items · {files} files · {size}',
  '{n} 組・余分な容量 {waste}（上位 {top} 組を表示）': '{n} groups · {waste} wasted (top {top} shown)',
  'レポート: {root}': 'Report: {root}', '{n} 件・{size}': '{n} items · {size}',
  '選択 {sel} / {all} 件・ファイル {files}・{size}': 'Selected {sel} / {all} · {files} files · {size}',
  '実行すると、{client} の常駐の同期（{service}）を一時停止し、隔離した後に削除をクラウドへ反映してから再開します。{client} の大量削除の安全装置（{n} 件）で同期が止まることはありません。':
    'Running it pauses the resident {client} sync ({service}), quarantines, reflects the deletions in the cloud and restarts the sync. {client}’s big-delete guard ({n} items) will not stop the sync.',
  '保存できませんでした: {error}': 'Could not save: {error}', '飛ばした: {detail}': 'Skipped: {detail}', '戻せなかった: {detail}': 'Not restored: {detail}',
  '{on} / {all} 件・{size}': '{on} / {all} · {size}',
  '{n} 件（ファイル {files}、{size}）を隔離フォルダへ移します。削除はしません。': 'Moves {n} items ({files} files, {size}) to the quarantine folder. Nothing is deleted.',
  '隔離先: {path}': 'Quarantine: {path}', '実行の前後にスナップショットを撮ります（snapper: {config}）': 'Snapshots are taken before and after (snapper: {config})',
  '{client} の常駐の同期を一時停止し、削除をクラウドへ反映してから再開します': 'The resident {client} sync is paused, the deletions reflected in the cloud, and the sync restarted',
  '隔離した {n} 件を元の場所へ戻します。元の場所に新しくできたものは上書きしません。戻したものは同期クライアントが再びアップロードします。':
    'Moves {n} quarantined items back. Anything new at the original paths is not overwritten. The sync client uploads what comes back.',
  'プラン {id}': 'Plan {id}', '取り消す（{n} 件）…': 'Undo ({n})…',
  '「{tag}」を {n} 件から外しました': 'Removed “{tag}” from {n}', '「{tag}」を {n} 件に付けました': 'Added “{tag}” to {n}',
  '{from} から継承': 'Inherited from {from}', '{n} 件を選択中': '{n} selected',
  'このファイルの整理の提案を出す（{model}）': 'Suggest for this file ({model})', '既定のモデル': 'default model',
  '（このときの場所: {path}）': '(then at: {path})', '履歴を読めません: {error}': 'Cannot read the history: {error}',
  '{when} の時点の版を、今のファイルの隣に日時付きの別名で置きます。今のファイルは上書きしません。':
    'Puts the version as of {when} next to the current file under a dated name. The current file is not overwritten.',
  '復元しました: {path}': 'Restored: {path}', 'ほか {n} 件': 'and {n} more', '{n} 件を削除しますか？': 'Delete {n} items?',
  '「{tag}」が付いたもの': 'Tagged “{tag}”', '（画像なし）': ' (no images)', '（既定）': ' (default)',
  '提案を作れませんでした: {error}': 'Could not make a suggestion: {error}', '{model} が内容を読んでいます…': '{model} is reading the file…',
  '順番待ち（{n} 番目）': 'Queued (#{n})', '（今の場所のまま）{dir}': '(stay here) {dir}', '採用リストに入れました: {name}': 'Added to the accept list: {name}',
  '参考にした過去の判断（{n} 件）': 'Past decisions used ({n})', '採用リスト {n} 件': 'Accept list: {n}',
  '{n} 件を移動・改名しますか？': 'Move or rename {n} items?', '実行を始めました（プラン {id}）': 'Started (plan {id})', '待ち {n}': 'Queued {n}',
  '整理: {root}': 'Inbox: {root}',
  '受け皿（{patterns} に一致するフォルダ）のファイル {n} 件を、フォルダの階層なしで並べています。ファイルを選ぶと、モデルが内容を読んで名前・移動先・タグを提案します。採用したものは採用リストにたまり、まとめて実行できます。':
    '{n} files in the inboxes (folders matching {patterns}), listed flat. Pick a file and a model reads it and suggests a name, a folder and tags. Accepted suggestions collect in the accept list, which runs in one go.',
  '残り {n} 件をまとめて提案': 'Suggest the remaining {n} in bulk',
  '{done} / {total}（{pct}%）': '{done} / {total} ({pct}%)',

  // --- サーバーから来る文（カテゴリ・段階・確認の結果など） ---
  'アプリのデータ': 'Application data', '作り直せる生成物': 'Rebuildable artifacts', '仮想マシンのディスク': 'VM disk images',
  'インストーラー・ディスクイメージ': 'Installers and disk images', '競合コピー・複製': 'Conflict copies', 'Office のロックファイル': 'Office lock files',
  'OS・アプリが作るゴミ': 'OS and app junk', '一時ファイル・バックアップ': 'Temporary files and backups', '空ファイル': 'Empty files', '空フォルダ': 'Empty folders',
  '整理（移動・改名）': 'Organize (move / rename)', '削除（隔離フォルダへ）': 'Delete (to quarantine)',
  '既知のアプリのデータ': 'known application data', 'キャッシュ': 'cache', '中身が空': 'empty', '中身が無い': 'empty',
  'Office が編集中に作る一時ファイル': 'temporary file Office creates while editing', 'OS やアプリが自動で作るファイル': 'created automatically by the OS or an app',
  '巨大で、同期やバックアップに向かない': 'huge and unsuited to sync or backup', '再入手できる可能性が高い': 'probably downloadable again',
  'インストーラーらしい名前か、ダウンロードフォルダにある': 'installer-like name, or in a downloads folder',
  'Linux 版 onedrive クライアントの競合コピー': 'OneDrive client for Linux conflict copy', 'pCloud の競合コピー': 'pCloud conflict copy',
  'rclone bisync の競合コピー': 'rclone bisync conflict copy', '競合コピー': 'conflict copy', '番号付きの複製': 'numbered copy', 'コピー': 'copy',
  '元のファイルと内容が同じ': 'same content as the original', '元のファイルと内容が異なる': 'content differs from the original',
  '中身の確認が必要': 'needs review', '元のファイルがある（内容は未比較）': 'the original exists (not compared yet)',
  '別のものに置き換わった': 'replaced by something else', 'プランの作成後に変更された': 'changed since the plan was made',
  '移動先に同じ名前のものがある': 'the destination already exists', '隔離先に同じ名前のものがある': 'already exists in quarantine',
  '元の場所に別のものがある': 'something else is at the original path', '隔離先に見つからない': 'not found in quarantine', '移動先に見つからない': 'not found at the destination',
  'プランで除外': 'excluded in the plan', '名前の変更': 'rename', 'ファイルブラウザから削除': 'deleted from the file browser',
  '走査': 'Scan', 'ハッシュ計算': 'Hashing', '大量削除の確認': 'Checking for big deletions', '実行前のスナップショット': 'Snapshot before',
  '実行後のスナップショット': 'Snapshot after', '常駐の同期を一時停止': 'Pausing the resident sync', '実行（隔離・移動）': 'Applying (quarantine / move)',
  '削除をクラウドに反映': 'Reflecting deletions in the cloud', '常駐の同期を再開': 'Restarting the resident sync', '元に戻す ': 'Undo',
  'レポートの作成': 'Building the report', '対象の状態の記録': 'Recording the current state', '変わっていないかの確認': 'Checking for changes',
  '対象の確認': 'Finding the files', '内容の読み取りと提案': 'Reading and suggesting',
};

// サーバーから来る、変数を含む文（正規表現 → 英語）
const I18N_PATTERNS = [
  [/^(走査|ハッシュ計算|最新にする（走査とハッシュ計算）|プランの作成|整理の提案|実行|取り消し|確認|削除の反映の再試行|常駐の同期の再開): (.+)$/, (m) => `${{
    '走査': 'Scan', 'ハッシュ計算': 'Hash', '最新にする（走査とハッシュ計算）': 'Refresh (scan and hash)', 'プランの作成': 'Create plan',
    '整理の提案': 'Inbox suggestions', '実行': 'Apply', '取り消し': 'Undo', '確認': 'Check', '削除の反映の再試行': 'Retry reflecting deletions',
    '常駐の同期の再開': 'Restart resident sync',
  }[m[1]]}: ${m[2]}`],
  [/^(.+) はビルドや実行で作り直せる$/, (m) => `${m[1]} can be rebuilt`],
  [/^拡張子の無いファイルが ([\d,]+) 件中 ([\d,]+) 件$/, (m) => `${m[2]} of ${m[1]} files have no extension`],
  [/^(\.\S+) は一時ファイルや自動バックアップに使われる$/, (m) => `${m[1]} is used for temporary files and automatic backups`],
  [/^端末名（(.+)）付きの競合コピー$/, (m) => `conflict copy with device name (${m[1]})`],
];

const I18N_JA_CHARS = /[぀-ヿ一-鿿]/;
// 利用者のデータ（ファイル名・パス・タグ・ログ）は訳さない
const I18N_SKIP = '[translate="no"], .path, .mono, .crumbs, .chip, pre, script, style, textarea';

const i18n = {
  lang: 'ja',
  locale: 'ja-JP',
  /** 保存した言語、無ければブラウザの言語（日本語以外は英語）。 */
  detect() {
    let saved = null;
    try { saved = localStorage.getItem('tag-keeper-lang'); } catch { /* 使えなくても動く */ }
    if (saved === 'ja' || saved === 'en') return saved;
    const langs = navigator.languages && navigator.languages.length ? navigator.languages : [navigator.language || 'ja'];
    return langs[0].toLowerCase().startsWith('ja') ? 'ja' : 'en';
  },
  set(lang, remember = true) {
    this.lang = lang;
    this.locale = lang === 'ja' ? 'ja-JP' : 'en-US';
    document.documentElement.lang = lang;
    if (remember) { try { localStorage.setItem('tag-keeper-lang', lang); } catch { /* 使えなくても動く */ } }
    translateTree(document.body);
  },
};

/** 文を訳して、{name} に値を入れる（日本語ならそのまま入れる）。 */
function t(text, params = {}) {
  const src = i18n.lang === 'ja' ? text : (I18N_EN[text] ?? text);
  return src.replace(/\{(\w+)\}/g, (all, k) => (k in params ? String(params[k]) : all));
}

/** 表示されたテキスト1つを英語にする（辞書・正規表現・文ごと・「前置き: 値」の順に試す）。 */
function translateString(s) {
  if (!I18N_JA_CHARS.test(s)) return s;
  const lead = s.match(/^\s*/)[0];
  const tail = s.match(/\s*$/)[0];
  const core = s.trim();
  if (core in I18N_EN) return lead + I18N_EN[core] + tail;
  for (const [re, fn] of I18N_PATTERNS) {
    const m = core.match(re);
    if (m) return lead + fn(m) + tail;
  }
  // 「理由。補足」のように句点でつながった文は、文ごとに訳す
  if (core.includes('。')) {
    const parts = core.split('。').filter(Boolean).map((p) => translateString(p));
    if (parts.some((p) => !I18N_JA_CHARS.test(p))) return lead + parts.join('. ') + tail;
  }
  // 「ありません: パス」のような前置きつきのメッセージは、前置きだけを訳す
  const colon = core.indexOf(': ');
  if (colon > 0 && core.slice(0, colon) in I18N_EN) return lead + I18N_EN[core.slice(0, colon)] + core.slice(colon) + tail;
  return s;
}

const i18nOriginal = new WeakMap();

function translateTextNode(node) {
  const parent = node.parentElement;
  if (!parent || parent.closest(I18N_SKIP)) return;
  const rec = i18nOriginal.get(node);
  const src = rec && node.data === rec.out ? rec.src : node.data;
  const out = i18n.lang === 'ja' ? src : translateString(src);
  i18nOriginal.set(node, { src, out });
  if (node.data !== out) node.data = out;
}

function translateAttrs(el) {
  if (el.closest(I18N_SKIP)) return;
  for (const name of ['placeholder', 'title', 'aria-label']) {
    if (!el.hasAttribute(name)) continue;
    const key = `i18n${name.replace('-', '')}`;
    const cur = el.getAttribute(name);
    if (el.dataset[key] === undefined || cur !== el.dataset[`${key}Out`]) el.dataset[key] = cur;
    const out = i18n.lang === 'ja' ? el.dataset[key] : translateString(el.dataset[key]);
    el.dataset[`${key}Out`] = out;
    if (cur !== out) el.setAttribute(name, out);
  }
}

/** 要素の配下のテキストと属性を、今の言語にそろえる。 */
function translateTree(root) {
  if (!root) return;
  if (root.nodeType === Node.TEXT_NODE) { translateTextNode(root); return; }
  if (root.nodeType !== Node.ELEMENT_NODE) return;
  translateAttrs(root);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) translateTextNode(n);
    else translateAttrs(n);
  }
}

// 画面が描き変わるたびに、増えた部分だけを訳す
const i18nObserver = new MutationObserver((mutations) => {
  if (i18n.lang === 'ja') return;
  for (const m of mutations) {
    if (m.type === 'childList') m.addedNodes.forEach((n) => translateTree(n));
    else if (m.type === 'characterData') translateTextNode(m.target);
    else if (m.type === 'attributes') translateAttrs(m.target);
  }
});

i18n.lang = i18n.detect();
i18n.locale = i18n.lang === 'ja' ? 'ja-JP' : 'en-US';
document.documentElement.lang = i18n.lang;
window.addEventListener('DOMContentLoaded', () => {
  translateTree(document.body);
  i18nObserver.observe(document.body, { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ['placeholder', 'title', 'aria-label'] });
});

// ---------- テーマ（OS に合わせる・ライト・ダーク） ----------

const theme = {
  modes: ['auto', 'light', 'dark'],
  current() { try { return localStorage.getItem('tag-keeper-theme') || 'auto'; } catch { return 'auto'; } },
  apply(mode) {
    if (mode === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    try { localStorage.setItem('tag-keeper-theme', mode); } catch { /* 使えなくても動く */ }
  },
};
theme.apply(theme.current());
