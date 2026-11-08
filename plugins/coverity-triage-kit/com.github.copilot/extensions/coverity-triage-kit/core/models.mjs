import { readFileSync } from 'node:fs';
import path from 'node:path';

import { agentStem } from './defaults.mjs';

// Model choices. The default per role is the first candidate of the agent definition's `model:`
// that is available (REQ-404: high reasoning for orchestrator/reviewer, low cost for worker/integrator).

function unquote(s) {
  const t = s.trim();
  if ((t.startsWith('"') && t.endsWith('"')) || (t.startsWith("'") && t.endsWith("'"))) return t.slice(1, -1);
  return t;
}

function stripComment(s) {
  const m = /^(.*?)\s+#.*$/.exec(s);
  return m && !/["']/.test(s) ? m[1] : s;
}

// Reads `model:` (or `models:`) from the front matter: a scalar, [a, b], or a "- a" list
export function parseModelList(markdown) {
  const content = String(markdown ?? '').replace(/^﻿/, '');
  const m = /^---\r?\n([\s\S]*?)\r?\n---/.exec(content);
  if (!m) return [];
  const lines = m[1].split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const km = /^(model|models)\s*:\s*(.*)$/.exec(lines[i]);
    if (!km) continue;
    const rest = stripComment(km[2].trim());
    if (rest.startsWith('[')) {
      return rest.replace(/^\[/, '').replace(/\]\s*$/, '').split(',').map(unquote).filter(Boolean);
    }
    if (rest) return [unquote(rest)];
    const list = [];
    for (let j = i + 1; j < lines.length; j += 1) {
      const im = /^\s+-\s+(.*)$/.exec(lines[j]);
      if (!im) break;
      list.push(unquote(stripComment(im[1])));
    }
    return list.filter(Boolean);
  }
  return [];
}

// The agent definition is <plugin>/com.github.copilot/agents/<name>.agent.md. Missing file → no candidates.
export function readAgentModels(pluginRoot, agentId) {
  const stem = agentStem(agentId);
  if (!pluginRoot || !stem || /[\\/]/.test(stem)) return [];
  try {
    return parseModelList(readFileSync(path.join(pluginRoot, 'com.github.copilot', 'agents', `${stem}.agent.md`), 'utf8'));
  } catch {
    return [];
  }
}

export function pickDefaultModel(candidates, availableIds, currentModel) {
  const ids = new Set(availableIds);
  const hit = candidates.find((m) => ids.has(m));
  if (hit) return hit;
  if (availableIds.length > 0) return currentModel && ids.has(currentModel) ? currentModel : availableIds[0];
  return candidates[0] ?? currentModel ?? '';
}

// The list items' shape is not fixed: strings or { id, name }
export function normalizeModels(list) {
  const out = [];
  const seen = new Set();
  for (const item of Array.isArray(list) ? list : []) {
    const id = typeof item === 'string' ? item : (item?.id ?? item?.modelId ?? item?.model);
    if (typeof id !== 'string' || id === '' || seen.has(id)) continue;
    seen.add(id);
    const name = typeof item === 'object' ? (item.name ?? item.displayName ?? id) : id;
    out.push({ id, name: String(name) });
  }
  return out;
}
