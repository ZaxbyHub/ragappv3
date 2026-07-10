# Chat UI Redesign — INACTIVE / UNWIRED SCAFFOLD

> **Status: Not part of the shipped application.** This directory is a stale
> scaffold from an exploratory chat-UI redesign. It is kept only as a historical
> reference and is **not built, imported, or reachable** from the production app.

## Why this notice exists

- The canonical chat UI lives in `frontend/src/` (built by the root `Dockerfile`
  and served at `/chat`).
- The `/chat/redesign` route was removed: `frontend/src/App.tsx` redirects it to
  the canonical `/chat` via `<Navigate to="/chat" replace />`.
- There are **zero imports** of `redesign/frontend/` anywhere under
  `frontend/src/`.
- This directory ships its own, older-pinned npm dependency tree
  (`redesign/frontend/package.json`: react ^18.2.0, zustand ^4.4.7,
  framer-motion ^10.16.16, shadcn-ui ^0.7.4, …) which is **not** the dependency
  set the shipped app uses. Do not treat it as a live project when auditing
  dependencies, indexing in an IDE, or grepping for the canonical chat UI.

## End-state

The intended end-state is to **remove this directory entirely** once
product/design confirm it is not an active branch point. Until that
confirmation, this document-as-inactive notice is the safe interim so
contributors don't have to rediscover the redirect to learn the scaffold is
dead.
