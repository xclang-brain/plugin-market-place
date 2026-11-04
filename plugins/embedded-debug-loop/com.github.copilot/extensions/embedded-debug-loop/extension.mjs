// エクステンションの入口。Copilot SDK の joinSession を、つなぎ口（adapter.mjs）に渡すだけにする。
// `@github/copilot-sdk` は Copilot CLI が用意するので、npm で入れない。

import { joinSession } from '@github/copilot-sdk/extension';

import { startAdapter } from './adapter.mjs';

const app = await startAdapter({ joinSession });

// Copilot CLI が終わるとき（SIGTERM）は、記録を書き終えてから終わる
function onSignal() {
  app.shutdown().finally(() => process.exit(0));
}
process.once('SIGTERM', onSignal);
process.once('SIGINT', onSignal);
