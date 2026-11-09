// Every sentence the extension shows to a person or gives to a model (Japanese, plain and short).
// The request templates to the orchestrator (design 4.15) live only here.
// Part names never appear in this file: they come from defaults.json as arguments.

export const TITLE = 'Coverity 警告のトリアージ';

// ---- notices (REQ-401, REQ-404, REQ-407, REQ-414) ----
export const OPEN_START_NOTICE = 'URL をブラウザーで開き、条件を確かめて「開始する」を押してください。';
export const CLEAR_NOTICE = '実行中は /clear を使わないでください。実行が途中で切れます。';
export const CHAT_CONFIRM_NOTICE =
  '実行中にエージェントが確認（ツールの許可、AI クレジットの上限、質問）を求めたときは、このチャットで答えてください。画面には、確認を待っていることが出ます。';
export const PREALLOW_NOTICE =
  '任せたいツール（コマンドの実行、ファイルの書き込み、Coverity との通信）を前もって許可するしかたは、プラグインの README にあります。';
export const BUSY_NOTICE =
  'いまの実行が終わるまで、新しい作業は始められません。今の画面で実行の様子を見られます。変えた作業を頼むときは、画面で止めてから、もう一度チャットで頼んでください。';
export const AUTO_NOTICE = 'Auto のため、役割ごとのモデルが効かないことがあります';
export const OVERRIDE_NOTICE = '実行が終わると、このチャットでほかのエージェントに設定したモデルと思考の深さの指定も消えます。';
export const ENDED_AT_STARTUP_NOTICE = '前の実行が途中で終わっていました。画面で結果を見られます。';
export const OPEN_FAILED = 'Web管理画面を開けませんでした。ユーザーに、開けなかったことを伝えてください。';
export const UNSTOPPED_HOW_TO = 'チャットで Esc を2回押すか、Copilot CLI を終えてください。';

// Text the open tool returns to the model (it reads it and tells the user)
export function openText({ url, notices }) {
  const lines = [`Web管理画面の URL：${url}`];
  const list = (notices ?? []).filter((n) => typeof n === 'string' && n !== '');
  if (list.length > 0) lines.push('', 'ユーザーに伝えてください：', ...list.map((n) => `- ${n}`));
  return lines.join('\n');
}

export const OPEN_TOOL_DESCRIPTION =
  'Coverity の警告を一括でトリアージする Web管理画面（設定・監視・結果）を開き、URL と、ユーザーに伝える注意を返します。' +
  'ユーザーの頼みの文を requestText にそのまま渡してください。画面の最初の条件は、頼みの文から拾います。' +
  '作業は、ユーザーが画面で「開始する」を押すと始まります。このツールを呼んだ後に、自分で作業をしたり、エージェントを呼んだりしないでください。' +
  '実行中に呼ぶと、新しい実行は作らず、今の画面の URL を返します。';
export const OPEN_PARAM_DESCRIPTION = 'ユーザーの頼みの文（書き換えずに、そのまま）';

// ---- setup and start (REQ-404〜406) ----
export const unusedPhrasesNotice = (phrases) =>
  `頼みの文の「${phrases.join('」「')}」は使いません。条件は画面で選んでください。`;
export const setupHint = (setupSkill) =>
  setupSkill ? `設定がまだのときや、設定を直すときは、チャットで /${setupSkill} と入力してください。` : '';
export const applyGuide = (applySkill) =>
  `承認の選択と逸脱コメントの手直しは、この画面の「結果」で行い、/${applySkill || '…'} で反映する`;
export const START_WHILE_RUNNING = 'すでに実行中です。';
export const OPEN_FIRST = '先に、チャットで頼んで画面を開いてください。';
export const SETTINGS_NOT_READY = '設定を読み込んでいます。少し待ってから、もう一度押してください。';
export const NOTHING_TO_STOP = '止める実行がありません。';
export const STOPPED_DURING_START = '開始している途中で止められました。';

export const START_ERRORS = Object.freeze({
  overrides: (detail) => `モデルと思考の深さを設定できませんでした：${detail}`,
  overridesTimeout: 'モデルと思考の深さの設定が時間切れになりました。',
  newRun: (detail) => `実行フォルダを作れませんでした：${detail}`,
  startAgent: (detail) => `統括エージェントを起動できませんでした：${detail}`,
  startTimeout: '統括エージェントの起動が時間切れになりました。',
  noAgentId: '統括エージェントの番号が返りませんでした。',
  noOrchestrator: '統括エージェントの名前が設定にありません。',
});

// ---- the request to the orchestrator (design 4.15) ----
function requestBody({ runDir, runCommandPath, roles }) {
  return [
    '## 実行フォルダ',
    runDir,
    '',
    '## 一括実行のコマンド',
    `uv run --native-tls --quiet "${runCommandPath}" <コマンド> …`,
    '（ほかの場所の同じ名前のコマンドではなく、この場所のものを使ってください。）',
    '',
    '## 役割ごとのエージェント',
    `- 作業役：${roles.worker}`,
    `- 検証役：${roles.reviewer}`,
    `- 統合役：${roles.integrator}`,
    '',
  ];
}

const RULES = [
  '- 利用者には質問しないでください。ツールの許可は、利用者がチャットで答えます。',
  '- 終わったら、振り返って見つけた課題を note-issue で書き残し、結果を1行で返してください。',
];

export function buildStartRequest(args) {
  return [
    'Web管理画面から、Coverity の警告の一括トリアージを頼みます。条件は画面で決めてあり、実行フォルダはもう作ってあります。',
    '',
    ...requestBody(args),
    '## 進め方',
    '- 実行フォルダを新しく作らないでください。再開の確認も、始める前の質問もせず、tick から進めてください。',
    ...RULES,
  ].join('\n');
}

export function buildRestartRequest({ restart, ...args }) {
  return [
    `前の統括エージェントが、実行が終わる前に止まりました。同じ実行フォルダの続きから進めてください（始め直し ${restart} 回目）。`,
    '',
    ...requestBody(args),
    '## 進め方',
    '- 実行フォルダを新しく作らないでください。再開の確認も、始める前の質問もせず、tick から続きを進めてください。',
    ...RULES,
  ].join('\n');
}

export const orchestratorName = (recordId) => `統括 ${recordId}`;

// ---- guard (design 8.3). The model sees "Denied by preToolUse hook: <reason>". ----
export const stoppedDeny = (tool) => `この実行は止められたため、ツール「${tool || '（名前なし）'}」は使えません。作業を終えてください。`;
export const orchestratorDeny = (tool) =>
  `統括エージェントは、ツール「${tool || '（名前なし）'}」を使えません。使えるのは、ファイルを読む・探すツール、エージェントを呼ぶツール（task など）、skill、Coverity から読むツール、` +
  '頼みの文にある一括実行のコマンドと結果と設定の画面のコマンドだけです。ファイルを作る・直す仕事は、役のエージェントに頼んでください。';
export const SHELL_DENY =
  '統括エージェントがシェルで動かせるのは、頼みの文にある一括実行のコマンドと、結果と設定の画面のコマンドだけです。1回に1つずつ動かし、引用符の外で ; & | < > ` ( ) { } @ $ や改行を使わないでください（; や && や | でつながない）。"…" の中でも ` と $ は使わず、記号を含む文は \'…\' で囲みます。' +
  '本文や理由に記号や改行があるときは、引用符で囲んだ1つの引数にするか、echo \'<JSON>\' | <コマンド> --stdin の形で渡してください。';
export const REVIEW_DENY = '確かめの結果のファイル（items/<ID>/review.json）を書けるのは、検証役のエージェントだけです。';

// ---- monitor and results ----
export const restartNote = (n, max) => `統括エージェントが実行の途中で止まったので、続きから始め直しました（${n} 回目／上限 ${max} 回）。`;
export const STATUS_TIMEOUT = '実行の様子を読む処理が時間の上限を超えたので止めました。';
export const statusError = (detail) => `実行の様子を読めませんでした：${detail}`;
export const summaryError = (detail) => `実行の一覧を作れませんでした：${detail}`;
export const guiError = (detail) => `結果と設定の画面のリンクを作れませんでした：${detail}`;
export const NO_MATCH = '条件に合う警告はなかった';
export const UV_MISSING = 'uv を起動できませんでした。uv が入っているか確かめてください。';
export const COMMAND_TIMEOUT = 'コマンドが時間の上限を超えたので止めました。';
export const COMMAND_UNREADABLE = 'コマンドの結果を読めませんでした。';
export const COMMAND_FAILED = 'コマンドが失敗しました。';

// "途中で終わった" details (screen and issue text)
export const ABORT_REASON_TEXT = Object.freeze({
  run_halted: '一括実行が続けられなかった',
  orchestrator_ended: '統括エージェントが途中で終えた（始め直しの上限を超えた）',
  orchestrator_failed: '統括エージェントの失敗',
  chat_interrupted: 'チャットからの中断',
  credit_limit: 'AI クレジットの上限',
  cut_off: '/clear で切れた',
  extension_restarted: 'エクステンションの再起動',
});

export const ROLE_LABELS = Object.freeze({
  orchestrator: '統括エージェント',
  worker: '作業エージェント',
  reviewer: '検証エージェント',
  integrator: '統合エージェント',
  other: 'ほかのエージェント',
});
export const ROLE_SHORT = Object.freeze({ orchestrator: '統括', worker: '作業', reviewer: '検証', integrator: '統合' });

// ---- issue filing ----
export const FILING = Object.freeze({
  notFound: 'その課題の案が見つかりません。',
  cannot: (reason) => `今は Issue を起票できません。${reason ?? ''}`.trim(),
  already: 'この案は、すでに起票しました。',
  inProgress: 'この案は、いま起票しています。',
  other: 'ほかの Copilot CLI が、この案を起票しています。',
  spawn: 'gh を起動できませんでした。gh が入っているか確かめてください。',
  timeout: (s) => `gh が時間の上限（${s} 秒）を超えたため止めました。送信は済んでいるかもしれないので、起票できたかは分かりません。`,
  failed: (detail) => `gh が失敗しました。${detail ? `（${detail}）` : ''}ログインが切れていないか、起票先のリポジトリに書き込めるかを確かめてください。`,
  noUrl: 'gh は成功を返しましたが、できた Issue の URL が読めませんでした。起票できたかは分かりません。',
  stale: '起票を始めてから時間がたっても結果が分かりません。起票できたかを確かめられません。',
  searchSpawn: 'gh を起動できなかったため、起票したかを確かめられません。',
  searchTimeout: '起票したかを探す処理が時間の上限を超えたため、起票したかを確かめられません。',
  searchFailed: (detail) => `起票したかを探す処理が失敗したため、起票したかを確かめられません。（${detail}）`,
  searchUnreadable: '起票したかを探す処理の結果が読めなかったため、起票したかを確かめられません。',
  checking: '起票できるかを確かめています。',
  reasons: {
    no_repository: '起票先のリポジトリが決まっていません。',
    not_github: '起票先のリポジトリの書き方が正しくありません（OWNER/REPO の形にします）。',
    no_gh: 'gh（GitHub の公式のコマンド）が見つかりません。',
    not_logged_in: 'gh でログインしていません。',
  },
  steps: {
    repository: 'エクステンションのフォルダの defaults.json の issueRepo に、起票先の GitHub のリポジトリ（OWNER/REPO）を書く。',
    gh: 'gh（GitHub の公式のコマンド）を入れて、gh auth login でログインする。',
    login: 'gh auth login でログインする。',
    access: (repo) => `起票先のリポジトリ（${repo}）に、Issue を書き込めることを確かめる。`,
  },
});
