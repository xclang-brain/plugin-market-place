// REQ-003, REQ-006, design 2.1 and 8.4: the extension knows lower parts only by the names in defaults.json
// and the contracts (4.12, 4.15, 4.16). Names are read from the folders and defaults.json, never written here.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { DEFAULTS_FILE, EXT_DIR, PLUGIN_DIR, PLUGIN_NAME, REAL_DEFAULTS } from './helpers.mjs';

function filesUnder(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...filesUnder(full));
    else out.push(full);
  }
  return out;
}

const listDir = (dir) => (fs.existsSync(dir) ? fs.readdirSync(dir) : []);
const EXT_FILES = filesUnder(EXT_DIR).filter((f) => f !== DEFAULTS_FILE);
const stem = (id) => String(id).split(':').pop();
const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
// a whole name: not part of a longer identifier or path word
const wordRe = (name) => new RegExp(`(?<![A-Za-z0-9_\\-.])${escape(name)}(?![A-Za-z0-9_\\-])`);

function partNames() {
  const skills = new Set(listDir(path.join(PLUGIN_DIR, 'skills')));
  for (const k of ['applySkill', 'setupSkill']) if (REAL_DEFAULTS[k]) skills.add(REAL_DEFAULTS[k]);
  for (const s of REAL_DEFAULTS.pluginSkills ?? []) skills.add(s);
  const agents = new Set(listDir(path.join(PLUGIN_DIR, 'com.github.copilot', 'agents')).filter((f) => f.endsWith('.agent.md')).map((f) => f.slice(0, -'.agent.md'.length)));
  for (const id of [REAL_DEFAULTS.orchestrator, ...Object.values(REAL_DEFAULTS.roles)]) agents.add(stem(id));
  const scripts = new Set([path.basename(REAL_DEFAULTS.runCommand), path.basename(REAL_DEFAULTS.guiCommand)]);
  for (const skill of listDir(path.join(PLUGIN_DIR, 'skills'))) {
    for (const f of listDir(path.join(PLUGIN_DIR, 'skills', skill, 'scripts'))) if (f.endsWith('.py')) scripts.add(f);
  }
  let mcp = [];
  try {
    mcp = Object.keys(JSON.parse(fs.readFileSync(path.join(PLUGIN_DIR, 'mcp.json'), 'utf8')).mcpServers ?? {});
  } catch {
    // no mcp.json yet
  }
  return { skills: [...skills], agents: [...agents], scripts: [...scripts], mcp };
}

// Skill internals the extension must not know (it reads `status`, `summary`, `settings` and the screen URL only)
const INTERNALS = [
  'config.yaml', 'knowledge.md', 'issues.json', 'issues.mapped.json', 'brief.md', 'progress.json', 'approvals.json', 'run.json',
  'operations.log', 'criteria.json', 'result.schema.json', 'review.schema.json', 'plan.schema.json', 'saved.json', 'filter.json',
  'merge_key', 'triage_store', 'COV_USER', 'COV_AUTH_KEY', 'COVERITY_PASSPHRASE', 'cov-build', 'cov-analyze', 'cov-commit-defects',
  'no-grouping', 'fake-issues', 'checker_description', 'confidence_cap', 'group_excluded_cids', 'revision_drift',
];

test('extension files name no skill, agent, script or MCP server (only defaults.json does)', () => {
  const { skills, agents, scripts, mcp } = partNames();
  const names = [...skills, ...agents, ...scripts, ...mcp, PLUGIN_NAME];
  assert.ok(names.length >= 8);
  const hits = [];
  for (const file of EXT_FILES) {
    const content = fs.readFileSync(file, 'utf8');
    for (const name of names) if (wordRe(name).test(content)) hits.push(`${path.relative(EXT_DIR, file)}: ${name}`);
  }
  assert.deepEqual(hits, []);
});

test('extension files hold no skill internals and no Coverity tool names (those are data in defaults.json)', () => {
  const hits = [];
  for (const file of EXT_FILES) {
    const content = fs.readFileSync(file, 'utf8');
    for (const word of INTERNALS) if (content.includes(word)) hits.push(`${path.relative(EXT_DIR, file)}: ${word}`);
    if (/coverity_[a-z]/.test(content)) hits.push(`${path.relative(EXT_DIR, file)}: coverity_ tool name`);
  }
  assert.deepEqual(hits, []);
});

test('defaults.json has the design 8.4 keys; the agent ids point to existing definitions when they exist', () => {
  const keys = Object.keys(REAL_DEFAULTS).sort();
  for (const key of ['orchestrator', 'roles', 'runCommand', 'guiCommand', 'applySkill', 'setupSkill', 'pluginSkills', 'port', 'issueRepo']) assert.ok(keys.includes(key), key);
  assert.deepEqual(Object.keys(REAL_DEFAULTS.roles).sort(), ['integrator', 'reviewer', 'worker']);
  assert.equal(REAL_DEFAULTS.port, 8731);
  assert.match(REAL_DEFAULTS.issueRepo, /^[\w.-]+\/[\w.-]+$/);
  for (const rel of [REAL_DEFAULTS.runCommand, REAL_DEFAULTS.guiCommand]) {
    assert.ok(!rel.startsWith('/') && !rel.includes('..') && !rel.includes('\\'), `${rel} is relative to the plugin folder`);
  }
  const agentsDir = path.join(PLUGIN_DIR, 'com.github.copilot', 'agents');
  for (const id of [REAL_DEFAULTS.orchestrator, ...Object.values(REAL_DEFAULTS.roles)]) {
    const file = path.join(agentsDir, `${stem(id)}.agent.md`);
    if (fs.existsSync(agentsDir) && listDir(agentsDir).length > 0) assert.ok(fs.existsSync(file), `${stem(id)}.agent.md`);
    assert.ok(id.startsWith(`${PLUGIN_NAME}:`), `${id} is qualified with the plugin name`);
  }
});

test('stdout is the CLI channel: no console.log; only node: modules, the SDK (entry only) and own files are imported', () => {
  for (const file of EXT_FILES.filter((f) => /\.(mjs|js)$/.test(f))) {
    const content = fs.readFileSync(file, 'utf8');
    const rel = path.relative(EXT_DIR, file);
    if (!rel.startsWith('ui')) assert.ok(!/console\.log\(/.test(content), `${rel}: console.log`);
    for (const m of content.matchAll(/^\s*import\s[^'"]*['"]([^'"]+)['"]/gm)) {
      const spec = m[1];
      const ok = spec.startsWith('node:') || spec.startsWith('./') || spec.startsWith('../') || (spec === '@github/copilot-sdk/extension' && rel === 'extension.mjs');
      assert.ok(ok, `${rel} imports ${spec}`);
    }
    const code = content
      .split('\n')
      .filter((line) => !/^\s*\/\//.test(line) && !line.includes('FORBIDDEN_PROPS = new Set'))
      .join('\n');
    assert.ok(!/\binnerHTML\b|\bouterHTML\b|\binsertAdjacentHTML\b|\beval\(|document\.write/.test(code), `${rel}: HTML building`);
  }
});

test('the stage-4 guide names commands, not script names or config internals', () => {
  const guide = path.join(PLUGIN_DIR, 'docs', 'guide', '4-screen.md');
  const content = fs.readFileSync(guide, 'utf8');
  const { scripts, agents } = partNames();
  for (const s of scripts) assert.ok(!wordRe(s).test(content), `script name ${s}`);
  for (const a of agents) assert.ok(!wordRe(a).test(content), `agent file name ${a}`);
  assert.doesNotMatch(content, /\b(max_rework|output_dir|max_items|verify\.\w+|agents\.\w+|coverity\.\w+|options\.\w+)\b/);
  assert.doesNotMatch(content, /defaults\.json|adapter\.mjs|messages\.mjs/);
});
