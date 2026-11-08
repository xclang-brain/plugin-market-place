// Entry point. It only hands Copilot SDK's joinSession to the adapter.
// `@github/copilot-sdk` is provided by Copilot CLI: it is not installed with npm.

import { joinSession } from '@github/copilot-sdk/extension';

import { startAdapter } from './adapter.mjs';

const app = await startAdapter({ joinSession });

// When Copilot CLI ends (SIGTERM, then SIGKILL after 5 s), close the screen server too
function onSignal() {
  app.shutdown().finally(() => process.exit(0));
}
process.once('SIGTERM', onSignal);
process.once('SIGINT', onSignal);
