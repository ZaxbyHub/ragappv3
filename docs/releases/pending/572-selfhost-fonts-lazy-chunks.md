# feat(frontend): self-host fonts, lazy shiki grammars, React Compiler (#572)

## What changed
- The SPA no longer contacts `fonts.googleapis.com`/`fonts.gstatic.com` on page load. The four font families the app's styles actually reference (Inter, Spline Sans, Commissioner, Electrolize) are vendored under `frontend/src/assets/fonts/` (37 woff2 files, ~1.1 MB, same families/weights/unicode-range subsets Google's css2 API served) with local `@font-face` rules using `font-display: swap`. The files are Vite-bundled assets, so hashed URLs carry the subpath base automatically.
- Chat code blocks and canvas code previews share a new lazy highlighter (`frontend/src/lib/highlighter.ts`): shiki's fine-grained core with the JavaScript regex engine and one dynamic import per supported grammar. A code fence now fetches only its own grammar's chunk instead of all 21, and the production build no longer ships chunks for the ~180 unsupported grammars or the 622 kB oniguruma wasm (dist JS drops from 17 MB/485 chunks to 8.9 MB/201).
- React Compiler is enabled via `@vitejs/plugin-react` 6's native `compiler` option (`react({ compiler: true })` in `vite.config.ts`), backed by the `oxc-transform-react` devDependency pinned to plugin-react's peerOptional range; the existing Vitest suite runs through the same pipeline unmodified. (The v4/v5-era `babel` option no longer exists in plugin-react 6 — an initial babel-based wiring was inert and was replaced after CI's node-config typecheck caught it.)

## Why
The README promises "Self-host everything on your own infrastructure" — a per-page-load dependency on Google's font CDN contradicted it, and the render-blocking stylesheet link was mislabeled "non-blocking". The fixed 21-language list in both duplicated loaders defeated shiki's per-grammar code splitting, and importing the full `shiki` bundle shipped every bundled grammar. (Workstream L, PR 3 of 4; findings C17 + E12.)

## Migration
No operator action. Fonts are bundled assets; no CDN allowlisting is needed (and none was ever required for assets). Deployments behind restrictive egress now load fonts offline.

## Caveats
- `cpp` code still fetches a ~780 kB grammar chunk — that grammar is irreducibly large; it loads only when C/C++ code actually renders.
- Mermaid's parser core (~647 kB) still loads on first diagram render; unchanged by this release (already lazy).
- Syntax highlighting now uses shiki's JavaScript regex engine (`forgiving` mode) instead of the oniguruma wasm; all 21 supported languages are covered by an explicit per-language test, but pathological grammar patterns degrade to unstyled spans instead of erroring.
- "Source Serif 4" was requested by the old CDN link but referenced nowhere in the app's styles; it is intentionally not vendored.
- React Compiler's ESLint companion plugin is NOT enabled: it has no stable release (npm has only beta/rc versions) and its recommended rules fail this repo's lint gate with 21 pre-existing errors, 19 caused by the repo's deliberate react-hooks rule disablings. The compiler itself is active (verified by an on/off A/B build producing different output) and the full suite passes under it.
