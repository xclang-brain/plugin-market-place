// 記録のファイルの読み書き。置き場所は <COPILOT_HOME または ~/.copilot>/plugin-data/embedded-debug-loop/。
// - settings.json：Web管理画面の設定。作業フォルダごとに分けて持つ（{ "folders": { "<作業フォルダ>": { "maxIterations": 5 } } }）。
//   Windows では、作業フォルダの大文字と小文字を区別しない（C:\Work と c:\work は同じフォルダ）
// - runs/<実行の番号>/run.json：実行の記録（変わるたびに書き直す）
// - runs/<実行の番号>/events.jsonl：あとで振り返るための出来事の記録（1行に1件、足していく）
// 書けなくても例外を投げない。理由は標準エラーに出す（標準出力は Copilot CLI との通信に使われる）。

import fs from 'node:fs';
import path from 'node:path';

export const RUN_ID = /^R-\d{8}-\d{6}(-\d+)?$/;
const MAX_LISTED = 50;

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

// 記録には、頼んだ作業やログの行が入る。ほかの利用者に読まれないよう、本人だけが読める権限で作る（Windows では効かない）
const RECORD_DIR_PERMS = 0o700;
const RECORD_FILE_PERMS = 0o600;

// 途中で切れても壊れたファイルを残さないよう、別名で書いてから置き換える
function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: RECORD_DIR_PERMS });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2), { mode: RECORD_FILE_PERMS });
  fs.renameSync(tmp, file);
}

/** そのプロセスが、まだ動いているか（記録を書いた Copilot CLI が終わったかを見る） */
export function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM'; // ほかの利用者のプロセスなら、動いている
  }
}

export function createPersist(baseDir, { platform = process.platform } = {}) {
  const runsDir = path.join(baseDir, 'runs');
  const runDir = (id) => path.join(runsDir, id);
  const report = (what, error) => console.error(`[Web管理画面] ${what}：${error?.message ?? error}`);

  const settingsFile = path.join(baseDir, 'settings.json');
  // 作業フォルダごとの設定の表（folders のほかの項目は読まない）
  const folders = () => {
    const all = readJson(settingsFile)?.folders;
    return all && typeof all === 'object' && !Array.isArray(all) ? all : {};
  };
  // 同じ作業フォルダかを比べる形。Windows のフォルダの名前は、大文字と小文字を区別しない
  const keyOf = platform === 'win32' ? (folder) => folder.toLowerCase() : (folder) => folder;
  const sameFolder = (a, b) => keyOf(a) === keyOf(b);

  return {
    baseDir,
    /** その作業フォルダの設定。なければ null */
    readSettings(folder) {
      const all = folders();
      const found = Object.hasOwn(all, folder) ? folder : Object.keys(all).find((key) => sameFolder(key, folder));
      const saved = found === undefined ? undefined : all[found];
      return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : null;
    },
    /** その作業フォルダの設定を書く。ほかのフォルダの設定は残す */
    writeSettings(folder, settings) {
      try {
        const others = Object.fromEntries(Object.entries(folders()).filter(([key]) => !sameFolder(key, folder)));
        writeJson(settingsFile, { folders: { ...others, [folder]: settings } });
        return true;
      } catch (error) {
        report('設定を保存できませんでした', error);
        return false;
      }
    },
    runExists: (id) => fs.existsSync(runDir(id)),
    readRun: (id) => (RUN_ID.test(id) ? readJson(path.join(runDir(id), 'run.json')) : null),
    writeRun(run) {
      try {
        writeJson(path.join(runDir(run.id), 'run.json'), run);
      } catch (error) {
        report('実行の記録を保存できませんでした', error);
      }
    },
    appendEvent(id, entry) {
      try {
        fs.mkdirSync(runDir(id), { recursive: true, mode: RECORD_DIR_PERMS });
        fs.appendFileSync(path.join(runDir(id), 'events.jsonl'), `${JSON.stringify(entry)}\n`, { mode: RECORD_FILE_PERMS });
      } catch (error) {
        report('出来事の記録を書けませんでした', error);
      }
    },
    removeRun(id) {
      try {
        if (RUN_ID.test(id)) fs.rmSync(runDir(id), { recursive: true, force: true });
      } catch (error) {
        report('実行の記録を消せませんでした', error);
      }
    },
    /**
     * 「実行中」か「止めている途中」のまま残った実行（書いた Copilot CLI が、もう動いていないもの）を閉じる。
     * close(run) は、閉じた後の実行の記録を返す。閉じた実行の番号を返す
     */
    closeInterrupted(close, { alive = processAlive } = {}) {
      const closed = [];
      let names = [];
      try {
        names = fs.readdirSync(runsDir).filter((name) => RUN_ID.test(name)).sort().reverse().slice(0, MAX_LISTED);
      } catch {
        return closed;
      }
      for (const id of names) {
        const run = readJson(path.join(runDir(id), 'run.json'));
        if (!run || (run.status !== 'running' && run.status !== 'stopping') || alive(run.pid)) continue;
        try {
          const done = close(run);
          writeJson(path.join(runDir(id), 'run.json'), done);
          fs.appendFileSync(path.join(runDir(id), 'events.jsonl'),
            `${JSON.stringify({ at: done.finishedAt, type: 'finish', outcome: done.finish.outcome, conclusion: done.finish.conclusion, auto: true, interrupted: true })}\n`,
            { mode: RECORD_FILE_PERMS });
          closed.push(id);
        } catch (error) {
          report(`途中で終わった実行 ${id} を閉じられませんでした`, error);
        }
      }
      return closed;
    },
    // 新しいものから順に、番号・始めた時刻・結果だけを返す
    listRuns() {
      let names = [];
      try {
        names = fs.readdirSync(runsDir).filter((name) => RUN_ID.test(name));
      } catch {
        return [];
      }
      names.sort().reverse();
      return names.slice(0, MAX_LISTED).map((id) => {
        const run = readJson(path.join(runDir(id), 'run.json'));
        return { id, startedAt: run?.startedAt ?? null, outcome: run?.finish?.outcome ?? null };
      });
    },
  };
}
