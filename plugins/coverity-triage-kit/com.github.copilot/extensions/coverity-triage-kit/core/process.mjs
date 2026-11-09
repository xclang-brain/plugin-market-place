import { spawn } from 'node:child_process';

// The real command runner (a port: tests pass a fake one).
// Runs a short child process without a shell (arguments as an array, input on stdin).
// On timeout it kills the whole process tree (uv starts python as a child).
// Never throws: { code, signal, stdout, stderr, timedOut, spawnError }

function killTree(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  try {
    if (process.platform === 'win32') {
      const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      killer.on('error', () => {
        try {
          child.kill('SIGKILL');
        } catch {
          // already gone
        }
      });
    } else {
      process.kill(-child.pid, 'SIGKILL'); // the child leads its own process group
    }
  } catch {
    try {
      child.kill('SIGKILL');
    } catch {
      // already gone
    }
  }
}

export function runProcess({ command, args = [], input = null, env = process.env, cwd, timeoutMs, clock }) {
  return new Promise((resolve) => {
    let child;
    let done = false;
    let timer = null;
    let stdout = '';
    let stderr = '';
    const finish = (result) => {
      if (done) return;
      done = true;
      if (timer !== null) clock.clearTimeout(timer);
      resolve({ code: null, signal: null, stdout, stderr, timedOut: false, spawnError: null, ...result });
    };
    try {
      child = spawn(command, args, {
        shell: false,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
        env,
        cwd,
        detached: process.platform !== 'win32', // own process group, so the tree can be killed
      });
    } catch (e) {
      finish({ spawnError: e.code ?? String(e.message ?? e) });
      return;
    }
    if (timeoutMs > 0) {
      timer = clock.setTimeout(() => {
        killTree(child);
        finish({ timedOut: true });
      }, timeoutMs);
    }
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (d) => {
      if (stdout.length < 20_000_000) stdout += d;
    });
    child.stderr.on('data', (d) => {
      if (stderr.length < 1_000_000) stderr += d;
    });
    child.on('error', (e) => finish({ spawnError: e.code ?? String(e.message ?? e) }));
    child.on('close', (code, signal) => finish({ code, signal }));
    child.stdin.on('error', () => {
      // the child ended before reading its input; the exit result tells what happened
    });
    if (input !== null && input !== undefined) child.stdin.write(input);
    child.stdin.end();
  });
}
