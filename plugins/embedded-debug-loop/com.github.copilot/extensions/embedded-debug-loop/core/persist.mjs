// 記録のファイルの読み書き。置き場所は <COPILOT_HOME または ~/.copilot>/plugin-data/embedded-debug-loop/。
// - settings.json：Web管理画面の設定。作業フォルダごとに分けて持つ（{ "folders": { "<作業フォルダ>": { 設定 } } }）
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

// 途中で切れても壊れたファイルを残さないよう、別名で書いてから置き換える
function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2));
  fs.renameSync(tmp, file);
}

export function createPersist(baseDir) {
  const runsDir = path.join(baseDir, 'runs');
  const runDir = (id) => path.join(runsDir, id);
  const report = (what, error) => console.error(`[Web管理画面] ${what}：${error?.message ?? error}`);

  const settingsFile = path.join(baseDir, 'settings.json');
  // 作業フォルダごとの設定の表。古い形（フォルダで分けていないもの）は読まない
  const folders = () => {
    const all = readJson(settingsFile)?.folders;
    return all && typeof all === 'object' && !Array.isArray(all) ? all : {};
  };

  return {
    baseDir,
    /** その作業フォルダの設定。なければ null */
    readSettings(folder) {
      const saved = folders()[folder];
      return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : null;
    },
    /** その作業フォルダの設定を書く。ほかのフォルダの設定は残す */
    writeSettings(folder, settings) {
      try {
        writeJson(settingsFile, { folders: { ...folders(), [folder]: settings } });
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
        fs.mkdirSync(runDir(id), { recursive: true });
        fs.appendFileSync(path.join(runDir(id), 'events.jsonl'), `${JSON.stringify(entry)}\n`);
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
