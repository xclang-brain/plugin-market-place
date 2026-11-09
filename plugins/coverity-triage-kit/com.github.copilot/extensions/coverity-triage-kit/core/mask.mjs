import os from 'node:os';

// Masking of issue drafts before they are shown for approval and filed to GitHub (REQ-422).
// Drafts are built from run records (tool errors, agent failures), which can hold the credentials,
// the user's name and home folder, internal host names, e-mail addresses and IP addresses.
// The names of the variables that hold credentials come from the batch-run command's settings (secret_envs).

let secretEnvs = [];
export function useSecretEnvs(names) {
  secretEnvs = (Array.isArray(names) ? names : []).filter((n) => typeof n === 'string' && /^[A-Za-z_][A-Za-z0-9_]*$/.test(n));
}

export const MASK = '****';
const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const KEEP_HOSTS = new Set(['github.com', 'www.github.com', 'localhost', '127.0.0.1']);

function usable(value) {
  const v = typeof value === 'string' ? value.trim() : '';
  return v.length >= 3 && !/^\$\{[^}]*\}$/.test(v) ? v : '';
}

export function createMasker({ env = process.env, names = secretEnvs, user, home } = {}) {
  const secrets = [...new Set(names.map((n) => usable(env?.[n])).filter(Boolean))].sort((a, b) => b.length - a.length);
  let account = user;
  if (account === undefined) {
    try {
      account = os.userInfo().username;
    } catch {
      account = '';
    }
  }
  const homeDir = home === undefined ? os.homedir() : home;
  const homes = homeDir && homeDir.length > 3 ? [...new Set([homeDir, homeDir.replace(/\\/g, '/'), homeDir.replace(/\//g, '\\')])] : [];
  const accountRe = usable(account) ? new RegExp(`(^|[\\\\/\\s"'=:(])${escape(account)}(?=$|[\\\\/\\s"'.,;:)])`, 'gi') : null;
  return function mask(text) {
    let s = String(text ?? '');
    for (const v of secrets) s = s.split(v).join(MASK);
    for (const h of homes) s = s.split(h).join('~');
    if (accountRe) s = s.replace(accountRe, `$1${MASK}`);
    s = s.replace(/[\w.+-]+@[\w-]+(?:\.[\w-]+)+/g, `${MASK}@${MASK}`);
    s = s.replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, (ip) => (KEEP_HOSTS.has(ip) ? ip : '*.*.*.*'));
    s = s.replace(/\b(https?:\/\/)([^/\s:"'<>)]+)/gi, (m, scheme, host) => (KEEP_HOSTS.has(host.toLowerCase()) ? m : `${scheme}${MASK}`));
    return s;
  };
}
