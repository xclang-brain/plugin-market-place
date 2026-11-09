// Reads the user's request text for the screen's first values (REQ-405).
// Impact, count, condition file and initial analysis phrases fill the setup form.
// Other condition-like phrases are listed as "not used" so the screen can say so.

import { LIMIT_MAX, LIMIT_MIN } from './constants.mjs';

const IMPACTS = { high: 'High', medium: 'Medium', low: 'Low', unspecified: 'Unspecified' };

// Order matters: negative initial-analysis phrases before positive ones.
const RULES = [
  { field: 'filter', re: /[A-Za-z0-9_][A-Za-z0-9_.-]*\.ya?ml/g, value: (m) => m[0] },
  {
    field: 'initial',
    re: /(?:初回の?|最初に?|はじめに?|初めに?)?解析(?:は|を)?(?:しない|せず|なし|不要)(?:で(?:ください)?)?/g,
    value: () => 'しない',
  },
  {
    field: 'initial',
    re: /(?:最初|はじめ|初め)に(?:ビルドと)?解析(?:して(?:から)?|する|も)?|初回解析(?:を|も)?(?:して|する|あり)?|解析してから/g,
    value: () => 'する',
  },
  {
    field: 'impact',
    re: /(?:impact\s*(?:が|は|を|=|:)?\s*)?\b(high|medium|low|unspecified)\b(?:\s*(?:だけ|のみ|の警告|に絞って|で絞って))?/gi,
    value: (m) => IMPACTS[m[1].toLowerCase()],
  },
  {
    field: 'limit',
    re: /(\d+)\s*(?:件|items?\b)(?:\s*(?:だけ|まで|のみ|に絞って))?/gi,
    value: (m) => {
      const n = Number(m[1]);
      return Number.isInteger(n) && n >= LIMIT_MIN && n <= LIMIT_MAX ? n : undefined;
    },
  },
];

// Negated conditions (「High 以外」「Low は除外」「not High」): the screen cannot take them, so they are
// reported as not used before the rules run, instead of being read as the opposite condition.
const NEGATED = [
  /(?:(?:impact\s*(?:が|は|を|=|:)?\s*)?\b(?:high|medium|low|unspecified)\b|\d+\s*(?:件|items?\b)|[A-Za-z0-9_][A-Za-z0-9_.-]*\.ya?ml|(?:初回の?|最初の?)?解析)\s*(?:は|を|が)?\s*(?:以外(?:の\S*)?|を?除いて|を?除く|を?除外(?:して|する)?|でない|ではない|じゃない|でなく|ではなく)/gi,
  /\b(?:except|excluding|not|without|other than)\s+(?:impact\s+)?(?:high|medium|low|unspecified)\b/gi,
];

// Condition-like phrases the screen cannot take (they are reported as not used)
const UNUSED_PATTERNS = [
  /ビルド(?:して|で|での|による)?(?:確かめ|検証|確認)\S*/g,
  /(?:同じ原因(?:の|は|を)?\S*)?(?:統合(?:して|も|する)|まとめて)\S*/g,
  /(?:前回の|前の)?(?:続きから|再開)\S*/g,
  /\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g, // checker-like names (NULL_RETURNS)
  /\b(?:MISRA|CERT)\b[^、。,，\n]*/gi,
  /\S*\/\S+|\S+\.(?:c|h|cc|cpp|hpp|cxx)\b/g, // paths and source files
  /\S*解析\S*/g, // initial-analysis wording the rules did not take
  /\S*\d+\s*(?:件|items?\b)\S*/gi, // count wording the rules did not take
];
const KEYWORD = /(だけ|のみ|以外|を除いて|を除く|に絞って|で絞って|に限って)/;
const CLAUSE = /[^、。,，．!！?？\n]+/g;

const blank = (s) => ' '.repeat(s.length);

/**
 * @returns {{ text: string, values: { filter?: string, impact?: string, limit?: number, initial?: string },
 *             used: Array<{ phrase: string, field: string }>, unused: string[] }}
 */
export function parseRequest(input) {
  const original = typeof input === 'string' ? input : '';
  let s = original.normalize('NFKC');
  const values = {};
  const used = [];
  const unused = [];
  const addUnused = (phrase) => {
    const p = phrase.replace(/\s+/g, ' ').trim();
    if (p && !unused.includes(p)) unused.push(p);
  };

  for (const re of NEGATED) {
    s = s.replace(re, (m) => {
      addUnused(m);
      return blank(m);
    });
  }

  for (const rule of RULES) {
    s = s.replace(rule.re, (...args) => {
      const m = args.slice(0, -2);
      const phrase = m[0].trim();
      const value = rule.value(m);
      if (value === undefined || values[rule.field] !== undefined) {
        // out of range, or the same field said twice: the screen keeps the first value
        if (values[rule.field] === undefined || values[rule.field] !== value) addUnused(phrase);
      } else {
        values[rule.field] = value;
        used.push({ phrase, field: rule.field });
      }
      return blank(m[0]);
    });
  }

  // "<something> だけ" in a clause: report from the clause start to the keyword
  s = s.replace(CLAUSE, (clause) => {
    const k = KEYWORD.exec(clause);
    if (!k) return clause;
    const head = clause.slice(0, k.index + k[0].length);
    addUnused(head);
    return blank(head) + clause.slice(head.length);
  });

  for (const re of UNUSED_PATTERNS) {
    s = s.replace(re, (m) => {
      addUnused(m);
      return blank(m);
    });
  }

  // keep the order the phrases appear in the request
  const norm = original.normalize('NFKC');
  unused.sort((a, b) => norm.indexOf(a) - norm.indexOf(b));
  return { text: original, values, used, unused: unused.filter((p) => !/^(?:だけ|のみ|以外|を除いて|を除く)$/.test(p)) };
}
