// 3段階目の独立性：統括・検証・エクステンションは、作業の中身（機器・ビルド・転送・ログの取り方・スキルの名前）を知らない。
// 作業エージェントの名前は defaults.json（データ）にだけあり、別の名前に差し替えても、そのまま動く。

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';

import { EXT_DIR, PLUGIN_ROOT, startWith } from './helpers.mjs';

// 単語として出てはいけないもの（前後が英数字・_・- でないとき）
const FORBIDDEN = ['FTP', 'シリアル', 'serial', 'クロスコンパイル', 'Makefile', 'make', 'IPアドレス', 'IP アドレス', 'デプロイ', 'deploy',
  '転送', 'ビルド', 'build', '機器', 'device', 'board', 'ボード', 'sensor', 'ログイン', 'chmod', 'crc32',
  'setup', 'cross-compile', 'log-capture', 'device-worker'];
const TEXT = new Set(['.mjs', '.js', '.json', '.html', '.css', '.md']);

function layer3Files() {
  const out = [];
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (TEXT.has(path.extname(p)) && entry.name !== 'defaults.json') out.push(p);
    }
  };
  walk(EXT_DIR);
  for (const name of ['orchestrator.agent.md', 'verifier.agent.md']) {
    const p = path.join(PLUGIN_ROOT, 'com.github.copilot', 'agents', name);
    if (fs.existsSync(p)) out.push(p);
  }
  return out;
}

const word = (w) => new RegExp(`(?<![A-Za-z0-9_\\-])${w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![A-Za-z0-9_\\-])`, 'i');

test('3段階目のファイルに、作業の中身の言葉がない', () => {
  const problems = [];
  for (const file of layer3Files()) {
    fs.readFileSync(file, 'utf8').split('\n').forEach((line, i) => {
      for (const w of FORBIDDEN) if (word(w).test(line)) problems.push(`${path.relative(PLUGIN_ROOT, file)}:${i + 1}「${w}」 ${line.trim().slice(0, 100)}`);
    });
  }
  assert.deepEqual(problems, []);
});

test('作業・検証・統括エージェントの名前は、defaults.json にだけある（エクステンションのコードに書かない）', () => {
  const defaults = JSON.parse(fs.readFileSync(path.join(EXT_DIR, 'defaults.json'), 'utf8'));
  assert.ok(defaults.worker);
  assert.ok(defaults.verifier);
  assert.ok(defaults.orchestrator);
  for (const file of layer3Files().filter((f) => f.startsWith(EXT_DIR))) {
    const body = fs.readFileSync(file, 'utf8');
    assert.ok(!body.includes(defaults.worker), `${path.relative(PLUGIN_ROOT, file)} に作業エージェントの名前があります`);
    assert.ok(!body.includes(defaults.verifier), `${path.relative(PLUGIN_ROOT, file)} に検証エージェントの名前があります`);
    assert.ok(!body.includes(defaults.orchestrator), `${path.relative(PLUGIN_ROOT, file)} に統括エージェントの名前があります`);
  }
});

test('同梱の defaults.json を読み、名前をそのまま使う（統括エージェントは選択肢から除く）', async (t) => {
  const defaults = JSON.parse(fs.readFileSync(path.join(EXT_DIR, 'defaults.json'), 'utf8'));
  const agents = [defaults.worker, defaults.orchestrator, defaults.verifier].map((id) => ({ id, name: id.split(':').pop(), description: '' }));
  const { fake, app } = await startWith(t, { defaults, fake: { agents } });
  const r = await fake.call('open', { request: '作業' });
  assert.ok(r.text.includes(`作業エージェント「${defaults.worker}」`));
  assert.ok(r.text.includes(`検証エージェント：${defaults.verifier}`));
  const state = await app.control.state();
  assert.deepEqual(state.options.agents.map((a) => a.name), [defaults.worker, defaults.verifier]);
});

test('作業エージェントを、まったく別の名前のエージェントに差し替えても、開始から終わりまで動く', async (t) => {
  const defaults = { worker: 'kitchen:cook', verifier: 'kitchen:taster', maxIterations: 2 };
  const { fake, app } = await startWith(t, { defaults });
  await fake.call('open', { request: 'スープを作る', criteria: ['塩味がちょうどよい'] });
  await fake.call('prepare', { worker: 'kitchen:cook', ready: true, report: '準備：できている' });
  await app.control.start({});
  assert.ok(fake.sent[0].includes('作業エージェント：kitchen:cook'));
  await fake.call('plan', { iteration: 1, plan: '塩を少し' });
  fake.emit('subagent.started', { agentType: 'kitchen:cook', toolCallId: 't' }, 'a1');
  assert.equal(app.store.snapshot().run.active.role, 'worker');
  fake.subagentStop({ agentType: 'kitchen:cook', agentId: 'a1', response: '作りました' });
  fake.subagentStop({ agentType: 'kitchen:taster', agentId: 'a2', response: 'C1：合格' });
  const s = await fake.call('study', { iteration: 1, verdicts: [{ criterion: 'C1', result: 'pass', reason: 'ちょうどよい' }], next: '' });
  assert.match(s.text, /すべての条件が合格です/);
  await fake.call('finish', { outcome: 'success', conclusion: 'できた', retro: 'なし' });
  const run = app.store.snapshot().run;
  assert.equal(run.iterations[0].do.report, '作りました');
  assert.equal(run.iterations[0].study.verifierReport, 'C1：合格');
  assert.equal(run.finish.outcome, 'success');
});
