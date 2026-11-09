import { readFileSync } from 'node:fs';
import path from 'node:path';

// defaults.json (design 8.4): the only place that holds agent and skill names and command locations.
// The values are opaque strings to the code.

const text = (v) => (typeof v === 'string' ? v.trim() : '');

export function readDefaultsFile(file) {
  const raw = JSON.parse(readFileSync(file, 'utf8').replace(/^﻿/, ''));
  return normalizeDefaults(raw);
}

export function normalizeDefaults(raw) {
  const v = raw && typeof raw === 'object' ? raw : {};
  const roles = v.roles && typeof v.roles === 'object' ? v.roles : {};
  const port = Number.isInteger(v.port) && v.port >= 0 && v.port <= 65535 ? v.port : 0;
  return Object.freeze({
    orchestrator: text(v.orchestrator),
    roles: Object.freeze({ worker: text(roles.worker), reviewer: text(roles.reviewer), integrator: text(roles.integrator) }),
    runCommand: text(v.runCommand),
    guiCommand: text(v.guiCommand),
    applySkill: text(v.applySkill),
    setupSkill: text(v.setupSkill),
    pluginSkills: Object.freeze((Array.isArray(v.pluginSkills) ? v.pluginSkills : []).map(text).filter(Boolean)),
    port,
    issueRepo: text(v.issueRepo),
    coverityServer: text(v.coverityServer),
    agentDir: text(v.agentDir) || 'com.github.copilot/agents',
    coverityReadTools: Object.freeze((Array.isArray(v.coverityReadTools) ? v.coverityReadTools : []).map(text).filter(Boolean)),
  });
}

// Command location: a path relative to the plugin folder, with forward slashes
export function commandPath(pluginRoot, relative) {
  if (!relative) return '';
  return path.join(pluginRoot, ...relative.split('/').filter(Boolean));
}

// An agent id is '<plugin>:<name>' or '<name>'. The event's agentType matches the whole id or the part after ':'.
export const agentStem = (id) => String(id ?? '').split(':').pop();

export function sameAgent(given, id) {
  const g = String(given ?? '').trim();
  if (!g || !id) return false;
  // a bare name matches the qualified id; a qualified name must match exactly (another plugin may use the same short name)
  return g === id || g === agentStem(id) || (!id.includes(':') && agentStem(g) === id);
}

// Role of an agent type: orchestrator / worker / reviewer / integrator, or null
export function roleOfType(defaults, agentType) {
  if (sameAgent(agentType, defaults.orchestrator)) return 'orchestrator';
  for (const role of ['worker', 'reviewer', 'integrator']) if (sameAgent(agentType, defaults.roles[role])) return role;
  return null;
}
