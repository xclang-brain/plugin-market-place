// 統括エージェントが呼ぶ記録のツール。断るときは例外を投げず、理由の文を失敗の結果で返す。
// 成功の文には、モデルが次に何をするかを書く。

import { execFile } from 'node:child_process';

import { CHANGED_FILES_HEAD, MSG, TOOL, changedFilesLines, manualIssue, preparedNext, settingsLines } from './messages.mjs';
import { OUTCOMES, matchCriterion, readCriteria, text } from './store.mjs';

const VERDICTS = ['pass', 'fail', 'unknown'];
const ISSUE_TIMEOUT_MS = 30_000;

const fail = (message) => ({ textResultForLlm: message, resultType: 'failure' });
// モデルが数や真偽を文字で渡すこともあるので、受け入れる
const int = (v) => (typeof v === 'string' && /^\d+$/.test(v.trim()) ? Number(v) : v);

/** gh を、シェルを通さずに動かす。結果は { code, stdout, stderr, missing, timedOut } */
export function runCommand(command, args, timeoutMs) {
  return new Promise((resolve) => {
    execFile(command, args, { timeout: timeoutMs, windowsHide: true, maxBuffer: 1024 * 1024 }, (error, stdout, stderr) => {
      resolve({
        code: error ? (typeof error.code === 'number' ? error.code : 1) : 0,
        stdout: String(stdout ?? ''),
        stderr: String(stderr ?? ''),
        missing: error?.code === 'ENOENT',
        timedOut: Boolean(error?.killed),
      });
    });
  });
}

const S = (description) => ({ type: 'string', description });

const DEFINITIONS = {
  open: {
    description:
      '統括エージェントが最初に呼ぶ。頼む作業と合格の条件を記録し、Web管理画面を開いて、URL と今の設定を返す。',
    parameters: {
      type: 'object',
      properties: {
        request: S('頼む作業。利用者の頼みを短くまとめた文'),
        criteria: { type: 'array', items: { type: 'string' }, description: '合格の条件。1つずつ、結果を見て確かめられる文で。番号（C1 など）は付けない' },
      },
      required: ['request'],
    },
  },
  prepare: {
    description: '作業エージェントに頼んだ「準備の確認」の返事を記録する。Web管理画面に、そのまま表示される。',
    parameters: {
      type: 'object',
      properties: {
        worker: S('準備の確認を頼んだ作業エージェントの名前'),
        ready: { type: 'boolean', description: '返事の1行目が「準備：できている」なら true' },
        report: S('作業エージェントの返事の全文'),
      },
      required: ['worker', 'ready', 'report'],
    },
  },
  plan: {
    description: '試行の計画を記録する。作業エージェントに頼む前に呼ぶ。',
    parameters: {
      type: 'object',
      properties: { iteration: { type: 'integer', description: '試行の番号（1 から）' }, plan: S('この試行で試すこと') },
      required: ['iteration', 'plan'],
    },
  },
  study: {
    description: '検証エージェントの評価をもとに、合格の条件ごとの合否と、次にすることを記録する。',
    parameters: {
      type: 'object',
      properties: {
        iteration: { type: 'integer', description: '試行の番号' },
        verdicts: {
          type: 'array',
          description: '条件ごとの合否',
          items: {
            type: 'object',
            properties: {
              criterion: S('合格の条件の番号（C1 など）'),
              result: { type: 'string', enum: VERDICTS, description: 'pass は合格、fail は不合格、unknown は判断できない' },
              reason: S('理由'),
            },
            required: ['criterion', 'result', 'reason'],
          },
        },
        next: S('次にどうするか（1〜2文）'),
      },
      required: ['iteration', 'verdicts', 'next'],
    },
  },
  finish: {
    description: '実行を終え、結論と振り返りを記録する。',
    parameters: {
      type: 'object',
      properties: {
        outcome: { type: 'string', enum: OUTCOMES, description: 'success は合格、failure は上限まで試して不合格、stopped は止めた、needs-human は人の判断が要る' },
        conclusion: S('結論。直ったか、何が原因か、何を変えたかを、利用者に分かる言葉で'),
        retro: S('振り返り。進め方のよくなかった点と改善の案。なければ「なし」'),
      },
      required: ['outcome', 'conclusion', 'retro'],
    },
  },
  issue: {
    description: '振り返りの改善の案を、GitHub の Issue にする。利用者がチャットで「する」と答えたときだけ呼ぶ。',
    parameters: {
      type: 'object',
      properties: { title: S('Issue の題'), body: S('Issue の本文') },
      required: ['title', 'body'],
    },
  },
};

export function createTools({ store, ensureServer, refreshOptions = () => {}, refreshFolder = async () => {}, issueRepo = '', run = runCommand }) {
  const urlLine = () => (store.url ? `URL：${store.url}` : '');
  const running = () => {
    const r = store.activeRun();
    if (!r) return { error: fail(MSG.notRunning) };
    return { run: r };
  };

  const handlers = {
    async open(args) {
      const request = text(args?.request);
      if (!request) return fail('request（頼む作業）が空です。利用者の頼みを短くまとめて入れてください。');
      if (store.run?.status === 'running') {
        return fail(`実行中です。終わるのを待つか、利用者に Web管理画面で止めてもらってください。${urlLine()}`);
      }
      let url;
      try {
        url = await ensureServer();
      } catch (error) {
        return fail(`Web管理画面を開けませんでした（${error?.message ?? error}）。利用者に伝えてください。`);
      }
      await refreshFolder(); // 作業フォルダの設定を読んでから、設定を伝える
      store.draft = { request, criteria: readCriteria(args?.criteria), prepared: null };
      store.changed();
      refreshOptions();
      const w = store.settings.worker;
      const next = w
        ? `次に、作業エージェント「${w}」に、準備の確認だけを頼んでください（作業はさせない）。返事を ${TOOL.prepare} で記録してください。`
        : '作業エージェントが決まっていません。利用者に URL を伝え、Web管理画面で作業エージェントを選んで「開始」を押すよう頼んで、手番を終えてください。';
      return [`Web管理画面を開きました。URL：${url}`, '今の設定：', ...settingsLines(store.settings), next].join('\n');
    },

    async prepare(args) {
      const worker = text(args?.worker);
      const report = typeof args?.report === 'string' ? args.report.trim() : '';
      if (!worker || !report) return fail('worker（作業エージェントの名前）と report（返事の全文）を入れてください。');
      const prepared = { worker, ready: args?.ready === true || args?.ready === 'true', report, at: store.iso() };
      const run = store.activeRun();
      if (run) {
        run.prepared = prepared;
        store.log('prepare', prepared);
        store.changed();
        return prepared.ready
          ? `記録しました。試行 ${run.iteration + 1} の計画を立て、${TOOL.plan} で記録してください。`
          : '記録しました。準備ができていません。作業エージェントの返事の「足りないもの」の部分を、書き換えずにそのまま引用して利用者に伝え、どうするかを ask_user で聞いてください。';
      }
      if (!store.draft) return fail(`先に ${TOOL.open} を呼んでください。`);
      store.draft.prepared = prepared;
      store.changed();
      return preparedNext(prepared.ready, store.url);
    },

    async plan(args) {
      const { run, error } = running();
      if (error) return error;
      const n = int(args?.iteration);
      const plan = text(args?.plan);
      if (!Number.isInteger(n) || !plan) return fail('iteration（試行の番号。整数）と plan（計画）を入れてください。');
      if (n > run.maxIterations) return fail(`上限（${run.maxIterations} 回）に達しています。${TOOL.finish} で終えてください。`);
      const cur = run.iterations[run.iteration - 1];
      const replace = n === run.iteration && cur && !cur.do && !cur.study;
      if (n !== run.iteration + 1 && !replace) {
        return fail(`試行の番号が合いません。次の試行は ${run.iteration + 1} です。`);
      }
      const entry = { n, plan: { text: plan, at: store.iso() }, do: null, study: null };
      if (replace) run.iterations[n - 1] = entry;
      else run.iterations.push(entry);
      run.iteration = n;
      store.log('plan', { iteration: n, plan });
      store.changed();
      return `記録しました。作業エージェント「${run.worker}」に task で頼んでください。`;
    },

    async study(args) {
      const { run, error } = running();
      if (error) return error;
      const n = int(args?.iteration);
      const it = store.current();
      if (!it) return fail(`先に試行の計画を ${TOOL.plan} で記録してください。`);
      if (n !== run.iteration) return fail(`評価を記録できるのは、いまの試行 ${run.iteration} だけです。`);
      const verdicts = Array.isArray(args?.verdicts) ? args.verdicts : [];
      const bad = verdicts.length === 0 || verdicts.some((v) => !text(v?.criterion) || !VERDICTS.includes(v?.result));
      if (bad) return fail('verdicts に、条件ごとの criterion と result（pass・fail・unknown のどれか）と reason を入れてください。');
      const next = text(args?.next);
      it.study = {
        // criterion はモデルが書いたまま。no と text は、実行の合格の条件の番号と文（画面に出す）
        verdicts: verdicts.map((v) => ({ criterion: text(v.criterion), ...matchCriterion(v.criterion, run.criteria), result: v.result, reason: text(v.reason) })),
        next,
        verifierReport: it._verifierReport ?? '',
        // 検証エージェントを呼ばずに評価したか（作業が進めなかったときなど）。後から呼べば true に変わる
        verifierCalled: Boolean(it._verifierCalled || it._verifierReport),
        at: store.iso(),
      };
      delete it._verifierReport;
      delete it._verifierCalled;
      store.log('study', { iteration: n, verdicts: it.study.verdicts, next, verifierCalled: it.study.verifierCalled });
      store.changed();
      if (it.study.verdicts.every((v) => v.result === 'pass')) {
        return `すべての条件が合格です。${TOOL.finish}（outcome は success）で終えてください。`;
      }
      if (n >= run.maxIterations) {
        return `上限（${run.maxIterations} 回）に達しました。${TOOL.finish}（outcome は failure）で終えてください。`;
      }
      return `記録しました。次は試行 ${n + 1} です。評価の指摘をもとに計画を立て、${TOOL.plan} で記録してください。`;
    },

    async finish(args) {
      const run = store.run;
      const autoFinished = run?.status === 'finished' && run.finish?.auto;
      if (!run || (run.status === 'finished' && !autoFinished)) return fail(`終える実行がありません。${urlLine()}`);
      const stopped = run.status === 'stopping' || autoFinished;
      const outcome = stopped ? 'stopped' : args?.outcome;
      const conclusion = text(args?.conclusion);
      if (!OUTCOMES.includes(outcome) || !conclusion) {
        return fail(`outcome（${OUTCOMES.join('・')} のどれか）と conclusion（結論）を入れてください。`);
      }
      // 止めたときの仮の結論は、統括エージェントの結論で置き換える。途中で変えたファイルの一覧は残す
      const files = stopped && !conclusion.includes(CHANGED_FILES_HEAD) ? changedFilesLines(store.changedFiles(run)) : [];
      store.finish({ outcome, conclusion: [conclusion, ...files].join('\n'), retro: text(args?.retro) });
      return `記録しました。結果の画面：${store.url}#result\n利用者に結論を短く伝えてください。`;
    },

    async issue(args) {
      const title = text(args?.title);
      const body = text(args?.body);
      if (!title || !body) return fail('title（題）と body（本文）を入れてください。');
      if (!issueRepo) return fail('Issue の送り先が決まっていません。利用者に、題と本文をそのまま示してください。');
      const result = await run('gh', ['issue', 'create', '--repo', issueRepo, '--title', title, '--body', body], ISSUE_TIMEOUT_MS);
      const url = /https:\/\/github\.com\/\S+\/issues\/\d+/.exec(result.stdout)?.[0];
      if (url) {
        store.log('issue', { title, url });
        return `Issue を作りました：${url}\n利用者に URL を伝えてください。`;
      }
      const reason = result.missing
        ? 'gh（GitHub のコマンド）が見つからないため、Issue を作れませんでした。'
        : result.timedOut
          ? 'gh が 30 秒たっても終わらないため、Issue を作れませんでした。'
          : `gh で Issue を作れませんでした（${result.stderr.trim().split('\n').pop() || `終了コード ${result.code}`}）。gh auth status で認証を確かめてください。`;
      return fail(manualIssue(reason, issueRepo, title, body));
    },
  };

  // 止めたあとは、終わりの記録のほかは断る。どのツールも例外を投げない。
  return Object.entries(DEFINITIONS).map(([short, def]) => ({
    name: TOOL[short],
    description: def.description,
    parameters: def.parameters,
    skipPermission: true,
    defer: 'never',
    handler: async (args) => {
      try {
        if (store.run?.status === 'stopping' && short !== 'finish') return fail(MSG.stopped);
        return await handlers[short](args ?? {});
      } catch (error) {
        console.error(`[Web管理画面] ツール ${TOOL[short]} の処理で失敗しました：`, error);
        return fail(MSG.unexpected);
      }
    },
  }));
}
