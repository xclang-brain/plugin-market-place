import {
  appendFileSync,
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
  writeSync,
} from 'node:fs';
import { join } from 'node:path';

import { parseJsonLines } from './util.mjs';

// Records (design 8.3): <COPILOT_HOME or ~/.copilot>/plugin-data/<plugin>/runs/<record id>/
//   log.jsonl    events, status changes, notes, in arrival order
//   result.json  the final state (written atomically). Missing = the run did not finish
//   alive        alive mark (epoch ms), rewritten every 5 s while running
//   issues/      filing markers, one per issue draft
// Synchronous fs keeps the order exact and lets tests just advance a fake clock.

const sleepSync = (ms) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

// Windows can refuse a rename for a moment (virus scanners, indexers): retry a few times
function renameRetry(from, to) {
  for (let i = 0; ; i += 1) {
    try {
      renameSync(from, to);
      return;
    } catch (e) {
      if (i >= 5 || (e.code !== 'EPERM' && e.code !== 'EACCES' && e.code !== 'EBUSY')) throw e;
      sleepSync(50 * (i + 1));
    }
  }
}

export function createStore({ dataRoot, clock }) {
  const dirOf = (runId) => join(dataRoot, runId);
  const logPath = (runId) => join(dirOf(runId), 'log.jsonl');
  const resultPath = (runId) => join(dirOf(runId), 'result.json');
  const alivePath = (runId) => join(dirOf(runId), 'alive');
  const issuesDir = (runId) => join(dirOf(runId), 'issues');
  const markerPath = (runId, issueId) => join(issuesDir(runId), `${issueId}.json`);

  function writeAtomic(path, content) {
    const tmp = `${path}.tmp-${process.pid}-${Math.random().toString(36).slice(2, 8)}`;
    writeFileSync(tmp, content, { mode: 0o600 });
    renameRetry(tmp, path);
  }

  return {
    dataRoot,
    dirOf,

    ensureRoot() {
      mkdirSync(dataRoot, { recursive: true, mode: 0o700 });
    },

    createRun(runId, header) {
      mkdirSync(issuesDir(runId), { recursive: true, mode: 0o700 });
      appendFileSync(logPath(runId), `${JSON.stringify(header)}\n`, { mode: 0o600 });
      writeFileSync(alivePath(runId), String(clock.now()), { mode: 0o600 });
    },

    append(runId, entry) {
      appendFileSync(logPath(runId), `${JSON.stringify(entry)}\n`);
    },

    writeAlive(runId) {
      writeFileSync(alivePath(runId), String(clock.now()));
    },

    readAlive(runId) {
      try {
        const n = Number(readFileSync(alivePath(runId), 'utf8').trim());
        return Number.isFinite(n) ? n : null;
      } catch {
        return null;
      }
    },

    writeResult(runId, result) {
      writeAtomic(resultPath(runId), JSON.stringify(result));
    },

    readResult(runId) {
      try {
        return JSON.parse(readFileSync(resultPath(runId), 'utf8'));
      } catch {
        return null;
      }
    },

    hasResult(runId) {
      return existsSync(resultPath(runId));
    },

    readLog(runId) {
      try {
        return parseJsonLines(readFileSync(logPath(runId), 'utf8'));
      } catch {
        return [];
      }
    },

    // Record ids, oldest first
    listRuns() {
      try {
        return readdirSync(dataRoot)
          .filter((name) => {
            try {
              return statSync(logPath(name)).isFile();
            } catch {
              return false;
            }
          })
          .sort();
      } catch {
        return [];
      }
    },

    // Keep `keep` records: delete finished ones, oldest first. Unfinished ones stay.
    purge(keep) {
      const ids = this.listRuns();
      let count = ids.length;
      for (const id of ids) {
        if (count <= keep) break;
        if (!this.hasResult(id)) continue;
        try {
          rmSync(dirOf(id), { recursive: true, force: true });
          count -= 1;
        } catch {
          // leave it when it cannot be removed
        }
      }
    },

    // ---- filing markers ----
    readMarker(runId, issueId) {
      try {
        return JSON.parse(readFileSync(markerPath(runId, issueId), 'utf8'));
      } catch {
        return null;
      }
    },

    // Creates the marker only if it does not exist (exclusive). true when created.
    createMarkerExclusive(runId, issueId, marker) {
      mkdirSync(issuesDir(runId), { recursive: true, mode: 0o700 });
      let fd;
      for (let i = 0; fd === undefined; i += 1) {
        try {
          fd = openSync(markerPath(runId, issueId), 'wx', 0o600);
        } catch (e) {
          if (e.code === 'EEXIST') return false;
          // Windows refuses a create for a moment while another session renames or deletes the same file
          if (i >= 5 || (e.code !== 'EPERM' && e.code !== 'EACCES' && e.code !== 'EBUSY')) throw e;
          sleepSync(50 * (i + 1));
        }
      }
      try {
        writeSync(fd, JSON.stringify(marker));
      } finally {
        closeSync(fd);
      }
      return true;
    },

    // Moves an old marker aside. Only one session can succeed.
    retireMarker(runId, issueId, suffix) {
      try {
        renameSync(markerPath(runId, issueId), `${markerPath(runId, issueId)}.old-${suffix}`);
        return true;
      } catch (e) {
        if (e.code === 'ENOENT') return false;
        throw e;
      }
    },

    replaceMarker(runId, issueId, marker) {
      writeAtomic(markerPath(runId, issueId), JSON.stringify(marker));
    },
  };
}
