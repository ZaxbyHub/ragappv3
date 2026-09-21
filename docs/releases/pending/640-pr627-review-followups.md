# 640 — PR #627 review follow-ups: font integrity manifest, frontend Dockerfile defect, frozen-test hardening, highlighter polish (issue #640)

## What changed

- **Font provenance (AC1-AC3, AC7):** `frontend/src/assets/fonts/SHA256SUMS`
  (sha256, size floor, wOF2 magic verified per file by the new
  `font-integrity` test helper — 37 woff2 files) +
  `frontend/scripts/fetch-fonts.mjs` (the css2 fetch script, cross-platform,
  regenerates the fonts AND the manifest). New tests pin the manifest against
  the real vendored bytes and prove the helper rejects truncated / bad-magic /
  hash-mismatched / uncovered fonts (fixtures).
- **frontend/Dockerfile (AC4-AC5):** the un-parseable multi-line
  `RUN node -e "` validate block (present since 3508e278, never built by
  anything) is rewritten to the same BuildKit heredoc form the root Dockerfile
  has used since #258, with rules identical to `scripts/validate_vite_env.mjs`
  (config-env-contract parity) plus the `JSON.stringify(raw)` diagnostic the
  base-path-message test requires; `ENV NODE_ENV=production` removed from the
  build stage so `npm ci` installs devDependencies for `tsc && vite build`;
  new `frontend/.dockerignore` (host node_modules/dist were leaking into the
  build context and shadowing the container install — a second latent defect
  the parse error had been hiding); ci.yml docker-smoke now builds the
  frontend image too.
- **Frozen #572 acceptance-test hardening (AC6, AC8, AC9):** external-origin
  scanning extended to CSS assets (`@import`/`url()` fixtures flagged; real
  tree scanned clean — the `index.css` self-hosting comment no longer spells
  the forbidden hostnames literally, matching the HTML test's
  no-literal-mentions rule); the React Compiler wiring assertion runs on a
  comment-stripped extract of `vite.config.ts` (a commented-out
  `compiler: true` no longer reads as wired — fixture-pinned); the lazy-langs
  test resets its `grammarLoads` accumulator between tests and gained a
  second isolation test.
- **Highlighter (AC10-AC12):** GRAMMAR_LOADERS aliases now share one module
  loader const per base grammar (the `loaded` set genuinely dedupes js/cjs/mjs
  and the misleading comment is fixed); failed grammar loads are negatively
  cached with a 2-attempt retry cap then permanent plain-text degradation for
  the page lifetime (no more per-render chunk re-fetching after a deploy cuts
  a grammar loose); `index.html` preloads the primary text font
  (spline-sans-latin-normal-300-700.woff2) — verified rewritten to the hashed
  asset in both regular and `/knowledgevault` subpath builds.

## Why

PR #627's review left four confirmed LOW follow-ups (issue #640). Each is a
variant of one defect class: an invariant enforced by a check or artifact
weaker than the invariant it claims (existence-only font checks, single-file
external-origin scan, comment-blind wiring regex, un-reset test accumulator,
unfalsifiable vendored-binary provenance, reference-identity dedupe, unmemoized
failure retries, and a Dockerfile nothing ever built).

## Known limitations

- The CSS external-origin scan is textual (comment mentions count), matching
  the #572 HTML test's established semantics; a build-pipeline-level origin
  audit (parsed CSSOM) remains future work if ever needed.
- The negative cache is page-lifetime: a reload retries a failed grammar load
  once more (deliberate — a fixed deploy should recover without code changes).
- SHA256SUMS is an integrity pin (trust-on-first-use): it proves the vendored
  fonts have not drifted since the pin was recorded; it cannot retroactively
  prove the original vendoring was untampered. The css2 fetch script documents
  the origin for independent re-vendoring.
- The fetch script regenerates fonts only on demand; CI does not re-vendor
  fonts (the manifest pins what ships).
