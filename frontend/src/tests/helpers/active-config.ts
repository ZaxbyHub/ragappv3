/**
 * Active-config source extractor (issue #640, AC8).
 *
 * Weakness being closed: the React-Compiler wiring regex
 * (/react\(\s*\{[\s\S]*?compiler:\s*true/) matched the RAW vite.config.ts
 * source, and `[\s\S]*?` crosses comment boundaries — a commented-out
 * `// compiler: true` above an active `compiler: false` read as "wired".
 * Extracting only the active (non-commented) source first makes such
 * regexes comment-immune.
 *
 * Limitation (accepted for this config's shape): the stripper is a plain
 * scanner, not a tokenizer — string or template literals that CONTAIN
 * comment markers (e.g. a "https://…" URL, where `//` would start a
 * "comment") are treated as comments and removed. vite.config.ts and the
 * fixtures it is pinned against contain no such strings; if that changes,
 * swap this for a real tokenizer before extending the contract.
 */

/**
 * Strip `//` line comments and `/* ... *\/` block comments from TypeScript
 * source, leaving active code (and its whitespace) intact.
 */
export function extractActiveConfigSource(src: string): string {
  let out = "";
  let i = 0;
  while (i < src.length) {
    const two = src.slice(i, i + 2);
    if (two === "/*") {
      // Block comment: skip through the closing marker (or EOS).
      const close = src.indexOf("*/", i + 2);
      i = close === -1 ? src.length : close + 2;
    } else if (two === "//") {
      // Line comment: skip through the end of the line (keep the newline so
      // line-based structure survives).
      const newline = src.indexOf("\n", i + 2);
      i = newline === -1 ? src.length : newline;
    } else {
      out += src[i];
      i += 1;
    }
  }
  return out;
}
