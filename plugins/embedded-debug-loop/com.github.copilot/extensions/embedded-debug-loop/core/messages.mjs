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

/** 「開始」が押されたときに、チャットへ送る知らせ */
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
