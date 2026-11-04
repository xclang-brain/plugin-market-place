// 状態の置き場。設定・下書き・実行・過去の実行の一覧を持ち、Web管理画面に渡す形（スナップショット）を作る。
// 作業エージェントと検証エージェントの名前は、ただの文字として扱う（中身は知らない）。

import path from 'node:path';

import { MSG } from './messages.mjs';

export const EFFORTS = Object.freeze(['low', 'medium', 'high', 'xhigh']);
export const OUTCOMES = Object.freeze(['success', 'failure', 'stopped', 'needs-human']);
export const MAX_ITERATIONS_LIMIT = 20;
const MAX_ACTIVITY = 300;
const SAVE_DELAY_MS = 300;
const SETTING_TEXT_KEYS = ['worker', 'verifier', 'workerModel', 'workerEffort', 'verifierModel', 'verifierEffort'];

/** 利用者の入力の誤り。status は HTTP の番号、message は画面に出す文 */
export class UserError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export const text = (value) => (typeof value === 'string' ? value.trim() : '');

/** '<プラグイン>:<名前>' と '<名前>' を同じものとみなして比べる */
export function sameAgent(a, b) {
  const x = text(a);
  const y = text(b);
  if (!x || !y) return false;
  const bare = (s) => s.slice(s.lastIndexOf(':') + 1);
  return x === y || bare(x) === bare(y);
}

// 合格の条件の頭に付いた番号（「C1：」「- C2:」など）。番号は、記録するときにこちらで付ける
const CRITERION_NO = /^\s*(?:[-・*]\s*)?[CcＣｃ]\s*[0-9０-９]+\s*[：:]\s*/;

/** 合格の条件の文から、頭の番号を取り除く */
export const stripCriterionNo = (value) => text(value).replace(CRITERION_NO, '').trim();

/** 合格の条件：配列でも、1行に1つの文でも受ける。頭の番号は取り除く */
export function readCriteria(value) {
  const list = Array.isArray(value) ? value : typeof value === 'string' ? value.split(/\r?\n/) : [];
  return list.map(stripCriterionNo).filter(Boolean);
}

const effortOf = (value) => {
  const v = text(value);
  return v === '既定' || v === 'default' ? '' : v;
};

/**
 * 設定の入力を確かめて、新しい設定を返す。誤りがあれば UserError(400)。
 * 入力は、項目が平らに並んだものでも、settings の中に入ったものでも受ける。
 */
export function readSettingsInput(body, base) {
  const src = body && typeof body === 'object' ? { ...body, ...(body.settings && typeof body.settings === 'object' ? body.settings : {}) } : {};
  const out = { ...base };
  for (const key of SETTING_TEXT_KEYS) if (key in src) out[key] = key.endsWith('Effort') ? effortOf(src[key]) : text(src[key]);
  if ('maxIterations' in src) out.maxIterations = Number(src.maxIterations);
  const problems = [];
  if (!out.worker) problems.push('作業エージェントを選んでください。');
  if (!out.verifier) problems.push('検証エージェントを選んでください。');
  if (!Number.isInteger(out.maxIterations) || out.maxIterations < 1 || out.maxIterations > MAX_ITERATIONS_LIMIT) {
    problems.push(`繰り返しの上限は、1 から ${MAX_ITERATIONS_LIMIT} までの整数にしてください。`);
  }
  for (const key of ['workerEffort', 'verifierEffort']) {
    if (out[key] && !EFFORTS.includes(out[key])) problems.push(`考える深さは、既定・${EFFORTS.join('・')} から選んでください。`);
  }
  if (problems.length > 0) throw new UserError(400, [...new Set(problems)].join(''));
  return out;
}

function pad(n) {
  return String(n).padStart(2, '0');
}

/** 実行の番号 R-YYYYmmdd-HHMMSS（手元の時刻） */
export function runIdOf(date) {
  const d = date;
  return `R-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
}

export function createStore({ defaults, persist, now = () => new Date() }) {
  const base = {
    worker: text(defaults.worker),
    verifier: text(defaults.verifier),
    maxIterations: Number.isInteger(defaults.maxIterations) ? defaults.maxIterations : 5,
    workerModel: '',
    workerEffort: '',
    verifierModel: '',
    verifierEffort: '',
  };
  // 作業フォルダの設定。保存がなければ既定（defaults.json）。保存が壊れていても既定に戻す
  function settingsOf(folder) {
    try {
      const saved = persist.readSettings(folder);
      return saved ? readSettingsInput(saved, base) : { ...base };
    } catch {
      return { ...base };
    }
  }

  let saveTimer = null;
  let unnamed = 0; // 番号のない求めに付ける仮の番号
  const store = {
    version: 1,
    url: null,
    // 設定は作業フォルダごと。フォルダが分かるまでは既定を使う
    folder: null,
    settings: { ...base },
    draft: null,
    run: null,
    runs: persist.listRuns(),
    options: { agents: [], models: [] },
    // エージェントが動いているかの見張り（events.mjs が更新する）
    live: { agents: new Map(), mainSince: null },
    // チャットで答えを待っている求め（番号 → { kind, text }）。実行がなくても持つ
    pending: new Map(),
    waiting: null,
  };

  const iso = () => now().toISOString();

  function saveRunNow() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = null;
    if (store.run) persist.writeRun(publicRun(store.run, true));
  }

  Object.assign(store, {
    iso,
    /** 変わったことを記録し、実行の記録を少し後でまとめて書く */
    changed() {
      store.version += 1;
      if (store.run && !saveTimer) {
        saveTimer = setTimeout(saveRunNow, SAVE_DELAY_MS);
        saveTimer.unref?.();
      }
    },
    flush: saveRunNow,
    /** 動いている（running か stopping の）実行 */
    activeRun: () => (store.run && (store.run.status === 'running' || store.run.status === 'stopping') ? store.run : null),
    isBusy: () => store.live.mainSince !== null || store.live.agents.size > 0,
    current() {
      const run = store.activeRun();
      return run && run.iteration > 0 ? run.iterations[run.iteration - 1] : null;
    },
    /** 作業フォルダが分かったとき・変わったときに、そのフォルダの設定を読む */
    useFolder(dir) {
      const folder = text(dir) ? path.resolve(text(dir)) : '';
      if (!folder || folder === store.folder) return;
      store.folder = folder;
      store.settings = settingsOf(folder);
      store.changed();
    },
    /** 設定を変えて、作業フォルダの設定としてファイルに書く。書けなければ false */
    setSettings(next) {
      store.settings = next;
      store.changed();
      return persist.writeSettings(store.folder ?? path.resolve('.'), next);
    },
    /** チャットで待っていること。新しい求めを足すか（value あり）、答えが出た求めを消す（value が null） */
    setWaiting(requestId, value) {
      const id = text(requestId);
      if (value) store.pending.set(id || `#${(unnamed += 1)}`, value);
      else if (id) store.pending.delete(id);
      else store.pending.clear(); // 番号のない答えは、すべての求めへの答えとみなす
      const list = [...store.pending.values()];
      store.waiting = list.length ? { ...list[list.length - 1] } : null;
      const run = store.activeRun();
      if (run) run.waiting = store.waiting ? { ...store.waiting } : null;
      store.changed();
    },
    /** あとで振り返るための記録。動いている実行がなければ書かない */
    log(type, data = {}) {
      const run = store.activeRun();
      if (run) persist.appendEvent(run.id, { at: iso(), type, ...data });
    },
    /** くわしい記録の1行。新しいものを先頭に置く */
    activity(who, line) {
      const run = store.activeRun();
      if (!run) return null;
      const entry = { at: iso(), who, text: line };
      run.activity.unshift(entry);
      if (run.activity.length > MAX_ACTIVITY) run.activity.length = MAX_ACTIVITY;
      store.log('activity', { who, text: line });
      store.changed();
      return entry;
    },
    newRun({ request, criteria, prepared }) {
      const started = now();
      let id = runIdOf(started);
      for (let n = 2; persist.runExists(id) || store.runs.some((r) => r.id === id); n += 1) id = `${runIdOf(started)}-${n}`;
      const s = store.settings;
      return {
        id,
        status: 'running',
        startedAt: started.toISOString(),
        finishedAt: null,
        request,
        criteria,
        worker: s.worker,
        verifier: s.verifier,
        maxIterations: s.maxIterations,
        models: { workerModel: s.workerModel, workerEffort: s.workerEffort, verifierModel: s.verifierModel, verifierEffort: s.verifierEffort },
        prepared: prepared ?? null,
        iteration: 0,
        active: null,
        waiting: store.waiting ? { ...store.waiting } : null,
        iterations: [],
        finish: null,
        activity: [],
      };
    },
    /** 実行を終える。auto は、統括エージェントが記録せずに終わったとき（止めたとき）の印 */
    finish({ outcome, conclusion, retro = '', auto = false }) {
      const run = store.run;
      run.status = 'finished';
      run.finishedAt = iso();
      run.finish = { outcome, conclusion, retro, at: run.finishedAt, auto };
      run.active = null;
      run.waiting = null;
      const listed = store.runs.find((r) => r.id === run.id);
      if (listed) listed.outcome = outcome;
      persist.appendEvent(run.id, { at: run.finishedAt, type: 'finish', outcome, conclusion, retro, auto });
      store.changed();
      saveRunNow();
    },
    finishStopped() {
      if (store.run?.status === 'stopping') store.finish({ outcome: 'stopped', conclusion: MSG.stoppedConclusion, auto: true });
    },
    snapshot() {
      return {
        version: store.version,
        settings: { ...store.settings },
        options: { agents: store.options.agents, models: store.options.models, efforts: [...EFFORTS] },
        draft: store.draft
          ? { request: store.draft.request, criteria: [...store.draft.criteria], prepared: store.draft.prepared ? { ...store.draft.prepared } : null }
          : null,
        run: store.run ? publicRun(store.run, false) : null,
        runs: store.runs.map((r) => ({ ...r })),
        waiting: store.waiting ? { ...store.waiting } : null,
        folder: store.folder,
      };
    },
  });
  return store;
}

/** 画面とファイルに出す形。内部の印（_ で始まるもの、auto）は出さない */
export function publicRun(run, forFile) {
  const out = {
    id: run.id,
    status: run.status,
    startedAt: run.startedAt,
    finishedAt: run.finishedAt,
    request: run.request,
    criteria: [...run.criteria],
    worker: run.worker,
    verifier: run.verifier,
    maxIterations: run.maxIterations,
    models: { ...run.models },
    prepared: run.prepared ? { ...run.prepared } : null,
    iteration: run.iteration,
    active: run.active ? { ...run.active } : null,
    waiting: run.waiting ? { kind: run.waiting.kind, text: run.waiting.text } : null,
    iterations: run.iterations.map((it) => ({
      n: it.n,
      plan: { ...it.plan },
      do: it.do ? { ...it.do } : null,
      study: it.study ? { ...it.study, verdicts: it.study.verdicts.map((v) => ({ ...v })) } : null,
    })),
    finish: run.finish ? { outcome: run.finish.outcome, conclusion: run.finish.conclusion, retro: run.finish.retro, at: run.finish.at } : null,
    activity: run.activity.map((a) => ({ ...a })),
  };
  if (forFile) out.savedAt = new Date().toISOString();
  return out;
}
