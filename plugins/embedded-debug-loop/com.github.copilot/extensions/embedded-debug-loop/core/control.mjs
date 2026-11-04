// Web管理画面からの操作：状態を見る、設定を保存する、開始する、止める、過去の実行を見る。
// Copilot CLI への呼び出しは port（adapter.mjs が作る）を通す。
// port：send(prompt)、abort()、getSubagentSettings()、updateSubagentSettings(value)、listAgents()、listModels()、
//       currentAgent()（チャットで選ばれているエージェント）、workingDirectory()（作業フォルダ）

import { WHO } from './events.mjs';
import { startNotice } from './messages.mjs';
import { RUN_ID } from './persist.mjs';
import { UserError, publicRun, readCriteria, readSettingsInput, sameAgent, stopPlace, text } from './store.mjs';

const OPTIONS_TTL_MS = 10_000;
const OPTIONS_WAIT_MS = 1_500;

// エージェントの一覧の要素から、選ぶときの名前と説明を取り出す。hidden の名前（id か name）に当たるものは除く
function readAgents(listed, hidden = []) {
  const out = [];
  for (const a of Array.isArray(listed?.agents) ? listed.agents : []) {
    const name = text(a?.id) || text(a?.name);
    if (hidden.includes(text(a?.id)) || hidden.includes(text(a?.name))) continue;
    if (name && !out.some((x) => x.name === name)) out.push({ name, description: text(a?.description) });
  }
  return out;
}

// モデルの一覧の要素の形は決まっていない。id が取れるものだけを使う
function readModels(listed) {
  const out = [];
  for (const m of Array.isArray(listed?.list) ? listed.list : []) {
    const id = typeof m === 'string' ? m : text(m?.id);
    if (id && !out.some((x) => x.id === id)) out.push({ id, name: text(m?.name) || id });
  }
  return out;
}

/** 作業エージェントと検証エージェントのモデル・考える深さを、このセッションの子のエージェントの設定に書く */
export async function applyModelSettings(port, s) {
  const current = await port.getSubagentSettings();
  const sub = current?.subagents && typeof current.subagents === 'object' ? current.subagents : {};
  const agents = { ...(sub.agents ?? {}) };
  const before = JSON.stringify(agents);
  for (const [name, model, effort] of [
    [s.worker, s.workerModel, s.workerEffort],
    [s.verifier, s.verifierModel, s.verifierEffort],
  ]) {
    if (!model && !effort) delete agents[name];
    else agents[name] = { ...(model ? { model } : {}), ...(effort ? { effortLevel: effort } : {}) };
  }
  if (JSON.stringify(agents) === before) return false;
  await port.updateSubagentSettings({ subagents: { ...sub, agents } });
  return true;
}

/**
 * @param {object} options
 * @param {string} [options.orchestrator] 統括エージェントの名前（defaults.json の値）。作業・検証の選択肢から除く
 */
export function createControl({ store, port, persist, orchestrator = '' }) {
  let optionsAt = 0;
  let refreshing = null;
  let folderAt = 0;
  let folderReading = null;

  // 作業・検証の選択肢から除く名前：統括エージェントと、いまチャットで選ばれているエージェント
  // （いま作業・検証に選んでいるエージェントがチャットで選ばれているときは、それは除かない）
  async function hiddenAgents() {
    const hidden = [text(orchestrator)];
    try {
      const current = (await port.currentAgent?.())?.agent;
      const s = store.settings;
      const chosen = [current?.id, current?.name].some((n) => sameAgent(n, s.worker) || sameAgent(n, s.verifier));
      if (current && !chosen) hidden.push(text(current.id), text(current.name));
    } catch {
      // 読めなければ、統括エージェントの名前だけで除く
    }
    return hidden.filter(Boolean);
  }

  /** 作業フォルダを確かめ、変わっていれば、そのフォルダの設定を読む（10 秒に1回まで） */
  function refreshFolder() {
    if (!folderReading && (store.folder === null || Date.now() - folderAt >= OPTIONS_TTL_MS)) {
      folderAt = Date.now();
      folderReading = (async () => {
        try {
          store.useFolder(await port.workingDirectory());
        } catch (error) {
          console.error('[Web管理画面] 作業フォルダを読めませんでした：', error?.message ?? error);
          if (store.folder === null) store.useFolder(process.cwd());
        }
      })().finally(() => {
        folderReading = null;
      });
    }
    return folderReading ?? Promise.resolve();
  }

  /** エージェントとモデルの一覧を読み直す（10 秒に1回まで。読めなければ前のものを残す） */
  function refreshOptions() {
    if (!refreshing && Date.now() - optionsAt >= OPTIONS_TTL_MS) {
      optionsAt = Date.now();
      refreshing = (async () => {
        try {
          const listed = await port.listAgents();
          store.options.agents = readAgents(listed, await hiddenAgents());
        } catch (error) {
          console.error('[Web管理画面] エージェントの一覧を読めませんでした：', error?.message ?? error);
        }
        try {
          store.options.models = readModels(await port.listModels());
        } catch (error) {
          console.error('[Web管理画面] モデルの一覧を読めませんでした：', error?.message ?? error);
        }
      })().finally(() => {
        refreshing = null;
      });
    }
    return refreshing ?? Promise.resolve();
  }

  async function state() {
    const waiting = Promise.all([refreshFolder(), refreshOptions()]);
    await Promise.race([waiting, new Promise((resolve) => setTimeout(resolve, OPTIONS_WAIT_MS).unref?.())]);
    return store.snapshot();
  }

  function busyError() {
    const s = store.activeRun()?.status;
    if (s === 'running') return new UserError(409, '実行中です。止めるか、終わるまで待ってください。');
    if (s === 'stopping') return new UserError(409, '止めている途中です。少し待ってください。');
    return null;
  }

  // 画面の「頼む作業」と「合格の条件」を、下書きに残す（どちらも空で、下書きもないときは作らない）
  function keepDraft(body) {
    if (!body || typeof body !== 'object') return;
    const request = 'request' in body ? text(body.request) : store.draft?.request ?? '';
    const criteria = 'criteria' in body ? readCriteria(body.criteria) : store.draft?.criteria ?? [];
    if (!store.draft && !request && criteria.length === 0) return;
    store.draft = { prepared: null, ...store.draft, request, criteria };
  }

  async function saveSettings(body) {
    const busy = busyError();
    if (busy) throw new UserError(409, '実行中は設定を変えられません。');
    await refreshFolder();
    const next = readSettingsInput(body, store.settings);
    keepDraft(body);
    const saved = store.setSettings(next);
    const warning = saved
      ? null
      : `設定をファイルに保存できませんでした。${persist.baseDir} に書き込めるかを確かめてください。Copilot CLI を閉じるまでは、今の設定で動きます。`;
    return { ok: true, settings: { ...next }, ...(warning ? { warning } : {}) };
  }

  async function start(body) {
    const busy = busyError();
    if (busy) throw busy;
    await refreshFolder();
    const settings = readSettingsInput(body, store.settings);
    const request = text(body?.request ?? store.draft?.request);
    const criteria = readCriteria(body?.criteria ?? store.draft?.criteria);
    if (!request) throw new UserError(400, '頼む作業を書いてください。');
    if (criteria.length === 0) throw new UserError(400, '合格の条件を1つ以上書いてください（1行に1つ）。');
    store.setSettings(settings);

    let warning = null;
    try {
      await applyModelSettings(port, settings);
    } catch (error) {
      warning = `モデルと考える深さを設定できませんでした（${error?.message ?? error}）。エージェントの定義のままで動きます。`;
    }

    const draft = store.draft;
    const previous = store.run;
    const prepared = draft?.prepared && sameAgent(draft.prepared.worker, settings.worker) ? draft.prepared : null;
    const run = store.newRun({ request, criteria, prepared });
    store.run = run;
    store.draft = null;
    store.runs.unshift({ id: run.id, startedAt: run.startedAt, outcome: null });
    store.log('start', { request, criteria, settings, prepared: Boolean(prepared) });
    if (warning) store.activity(WHO.other, warning);
    store.changed();
    try {
      await port.send(startNotice(run, prepared));
    } catch (error) {
      store.run = previous;
      store.draft = draft;
      store.runs = store.runs.filter((r) => r.id !== run.id);
      persist.removeRun(run.id);
      store.changed();
      throw new UserError(500, `チャットに開始の知らせを送れませんでした（${error?.message ?? error}）。Copilot CLI の画面を確かめて、もう一度「開始」を押してください。`);
    }
    store.flush();
    return { ok: true, runId: run.id, ...(warning ? { warning } : {}) };
  }

  async function stop() {
    const run = store.activeRun();
    if (!run) throw new UserError(409, '止める実行がありません。');
    if (run.status === 'stopping') return { ok: true };
    // 止めた所は、押したときの記録で決める（止めた後に届く返事で変わらないように）
    run._stopPlace = stopPlace(run);
    run.status = 'stopping';
    store.activity(WHO.user, 'Web管理画面で「止める」を押しました');
    store.changed();
    try {
      await port.abort();
    } catch (error) {
      store.activity(WHO.other, `止める合図を送れませんでした：${error?.message ?? error}`);
    }
    // 動いているエージェントがなければ、終わりの知らせ（session.idle）は来ないので、ここで終える
    if (!store.isBusy()) store.finishStopped();
    return { ok: true };
  }

  function runDetail(id) {
    if (!RUN_ID.test(id)) throw new UserError(404, 'その実行の記録が見つかりません。');
    if (store.run?.id === id) return publicRun(store.run, false);
    const saved = persist.readRun(id);
    if (!saved) throw new UserError(404, 'その実行の記録が見つかりません。');
    return saved;
  }

  return { state, saveSettings, start, stop, runDetail, refreshOptions, refreshFolder };
}
