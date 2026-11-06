// モデルと利用者に見せる文。短い日本語で、誰が何をするかを書く。
// このファイルは、作業の中身を知らない。作業エージェントと検証エージェントの名前は、設定の値をそのまま使う。

const PREFIX = 'embedded-debug-loop_';
export const TOOL = Object.freeze({
  open: `${PREFIX}open`,
  prepare: `${PREFIX}prepare`,
  plan: `${PREFIX}plan`,
  study: `${PREFIX}study`,
  finish: `${PREFIX}finish`,
  issue: `${PREFIX}issue`,
});
export const TOOL_PREFIX = PREFIX;

export const DEFAULT_LABEL = '既定';

export const MSG = Object.freeze({
  stopped: `利用者が Web管理画面で止めました。作業をやめ、${TOOL.finish}（outcome は stopped）で終えてください。`,
  stoppedConclusion: '利用者が Web管理画面で止めました。',
  notRunning: '実行中ではありません。利用者が Web管理画面で「開始」を押してから記録してください。',
  orchestratorDeny:
    '統括エージェントは、ファイルを直したりコマンドを実行したりしません。作業は task で作業エージェントに頼んでください。',
  unexpected: '記録の処理で思わぬ失敗が起きました。同じことが続くときは、利用者に伝えてください。',
});

/** 作業の報告がある試行で、新しい計画なしに作業エージェントへ頼み直そうとしたときの、断る理由。n は今の試行の番号 */
export function workReportedDeny(n) {
  return `この試行の作業は、もう報告されています。続けるときは、先に ${TOOL.plan} で試行 ${n + 1} の計画を記録してから頼んでください。`;
}

// 作業エージェント・検証エージェントの返事を、試行の記録に入れなかった理由（events.jsonl に残す）
export const IGNORED = Object.freeze({
  afterStop: '利用者が止めた後に届いたため。',
  noPlan: '試行の計画が記録される前に届いたため。',
  reported: (n) => `試行 ${n} の作業の報告は、もう記録してあるため。`,
  finished: '実行が終わった後に届いたため。',
});

// 止めたときの結論で、途中で変えたファイルの一覧の見出し。
// 一覧は、ファイルを直すツールで変えたものだけ。コマンドで変わったファイルは分からないので、そう書いておく
export const CHANGED_FILES_HEAD = '途中で変えたファイル（元に戻していません）（コマンドで変わったファイルは含みません）：';

/** 途中で変えたファイルの一覧の行。なければ空の配列 */
export function changedFilesLines(files) {
  return files.length ? [CHANGED_FILES_HEAD, ...files.map((f) => `- ${f}`)] : [];
}

const STOP_PLACE = {
  prep: (n) => `試行 ${n} の前の、準備の確認の途中で止めました。`,
  plan: (n) => `試行 ${n} の計画の途中で止めました。`,
  do: (n) => `試行 ${n} の作業の途中で止めました。`,
  study: (n) => `試行 ${n} の評価の途中で止めました。`,
  after: (n) => `試行 ${n} の評価の後、終わりの記録の前に止めました。`,
};

/** 統括エージェントが記録せずに終わったとき（利用者が止めたとき）の結論。place は { n, step }、files は途中で変えたファイル */
export function stoppedConclusion(place, files = []) {
  const lines = [MSG.stoppedConclusion];
  if (place && STOP_PLACE[place.step]) lines.push(STOP_PLACE[place.step](place.n));
  return [...lines, ...changedFilesLines(files)].join('\n');
}

/**
 * 止めて自動で終えたときに、チャットに出す知らせ（結論と、結果の画面の URL）。
 * 止めた後は統括エージェントの手番が来ないので、チャットには、止めたことしか出ないため。
 */
export function stoppedChatNotice(conclusion, url) {
  return [conclusion, ...(url ? [`結果の画面：${url}#result`] : [])].join('\n');
}

/** prepare の結果の文。ready でなければ、すぐに「開始」を押すよう頼ませない */
export function preparedNext(ready, url, worker = '') {
  if (ready) {
    return `記録しました。利用者に Web管理画面の URL（${url}）を伝え、設定と作業エージェントの準備の内容を確かめて『開始』を押すよう頼んで、手番を終えてください。開始が押されると、このチャットに開始の知らせが届きます。`;
  }
  return [
    '記録しました。作業エージェントの準備ができていません。',
    '利用者に次のことを伝えて、手番を終えてください。いますぐ『開始』を押すようには頼まないでください。',
    '- 作業エージェントの返事の「足りないもの」の部分。書き換えずに、そのまま引用します。',
    ...(worker ? [`- 準備を確かめた作業エージェントの名前「${worker}」。返事に「この作業エージェント」とあれば、この名前のことだと添えます。`] : []),
    '- 直し方。返事に書かれた手順を、省かずにそのまま伝えます。',
    `- 直してから、Web管理画面（${url}）で『開始』を押すこと。開始すると、統括エージェントが準備を確かめ直すこと。`,
  ].join('\n');
}

const or = (value) => (value ? value : DEFAULT_LABEL);

/** 今の設定を、モデルが読める行にする */
export function settingsLines(s) {
  return [
    `- 作業エージェント：${s.worker || '（未設定）'}`,
    `  モデル：${or(s.workerModel)}／考える深さ：${or(s.workerEffort)}`,
    `- 検証エージェント：${s.verifier || '（未設定）'}`,
    `  モデル：${or(s.verifierModel)}／考える深さ：${or(s.verifierEffort)}`,
    `- 繰り返しの上限：${s.maxIterations} 回`,
  ];
}

/** 「開始」が押されたときに、チャットへ送る知らせ。prepared は、同じ作業エージェントで記録した準備の確認（なければ null） */
export function startNotice(run, prepared) {
  const lines = [
    '［Web管理画面からの開始の知らせ］',
    `実行の番号：${run.id}`,
    `頼まれた作業：${run.request}`,
    '合格の条件：',
    ...run.criteria.map((c, i) => `- C${i + 1}：${c}`),
    `繰り返しの上限：${run.maxIterations} 回`,
    `作業エージェント：${run.worker}`,
    `検証エージェント：${run.verifier}`,
  ];
  if (!prepared) lines.push('作業エージェントの準備の確認がまだです。最初に準備の確認を頼んでから、試行を始めてください。');
  else if (prepared.ready === false) lines.push('前の準備の確認では「できていない」でした。最初に準備の確認を頼み直してから、試行を始めてください。');
  lines.push('統括エージェントの手順に沿って、試行 1 の計画から始めてください。');
  return lines.join('\n');
}

/** Issue を手で作ってもらうときの文 */
export function manualIssue(reason, repo, title, body) {
  return [
    `${reason}`,
    `利用者に次の題と本文を示し、https://github.com/${repo}/issues/new から手で作るよう伝えてください。`,
    `題：${title}`,
    '本文：',
    body,
  ].join('\n');
}
