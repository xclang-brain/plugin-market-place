import { NO_MATCH } from './messages.mjs';

// End reasons (design 8.2). The code decides them from the batch run's status, not the AI.
//   all_passed   finished; every item done, every review passed, no errors and nothing left
//   max_reached  finished; some items flagged, unreviewed, failed or left over
//   user_stopped the stop button
//   aborted      the run ended midway (detail tells why)

const UNPROCESSED = new Set(['pending', 'in_progress']);

export function finishedOutcome(st, summaryData) {
  const items = (Array.isArray(st?.items) ? st.items : []).filter((i) => i && i.status !== 'merged');
  const total = Number.isInteger(st?.counts?.total) ? st.counts.total : items.length;
  const reviewOn = st?.review !== false;
  let errors = 0;
  let unprocessed = 0;
  let flagged = 0;
  let unreviewed = 0;
  for (const item of items) {
    if (item.status === 'error') errors += 1;
    else if (UNPROCESSED.has(item.status)) unprocessed += 1;
    else if (item.status === 'done' && reviewOn) {
      const outcome = item.review?.outcome;
      if (!outcome || outcome === 'unverified') unreviewed += 1;
      else if (outcome !== 'pass') flagged += 1;
    }
  }
  const listed = Array.isArray(summaryData?.unprocessed) ? summaryData.unprocessed.length : 0;
  unprocessed = Math.max(unprocessed, listed - errors, 0);
  const notes = [];
  if (flagged) notes.push(`要確認 ${flagged} 件`);
  if (unreviewed) notes.push(`未検証 ${unreviewed} 件`);
  if (errors) notes.push(`エラー ${errors} 件`);
  if (unprocessed) notes.push(`未処理 ${unprocessed} 件`);
  if (notes.length > 0) return { kind: 'max_reached', detail: null, notes };
  return { kind: 'all_passed', detail: null, notes: total === 0 ? [NO_MATCH] : [] };
}

// A run the batch script cannot continue (tick said stop): a run-level critical error, or the run was stopped outside the screen
export function haltedByStatus(st) {
  if (!st) return false;
  if (st.halted === true || st.stop === true) return true;
  if (st.stopped === true && st.finished !== true) return true;
  return (Array.isArray(st.errors) ? st.errors : []).some((e) => e?.level === 'critical' && !e.item);
}

export function withUnstoppable(endReason, unstopped, unstoppedJobs) {
  const notes = [...(endReason.notes ?? [])];
  if (unstopped.length) notes.push(`止めきれなかったエージェント ${unstopped.length} 体`);
  if (unstoppedJobs.length) notes.push(`止めきれなかった処理 ${unstoppedJobs.length} 件`);
  return { ...endReason, notes, unstoppable: unstopped.length + unstoppedJobs.length > 0 };
}
