# 622 — First-setup wizard: model endpoint selection

Follow-up to #570 (no-shipped-defaults). A fresh install now walks the
operator through choosing chat model endpoints during first setup instead
of leaving them to find Settings → Models after hitting a 409.

- **Setup wizard step** (after superadmin creation): provider presets
  (Ollama / LM Studio / vLLM / other OpenAI-compatible) prefill URL shapes
  only; base URL + model name + optional API key per endpoint; "Test
  connection" probes just-typed values and reports
  ok / unreachable / model mismatch inline; instant endpoint is optional
  ("Skip for now"); the whole wizard can be skipped.
- **Persistence reuses PUT /api/settings**: the saved thinking (or instant)
  pair hot-rebinds a live client — chat works immediately, no restart.
- **API keys are first-class secrets** (`chat_api_key`, `instant_api_key`):
  persisted to settings_kv, replayed on restart, write-only on every read
  (GET never echoes the value), redacted below admin, never logged, and
  carried as `Authorization: Bearer` to keyed remote providers. They are
  deliberately absent from `.env.example`.
- **Unconfigured-chat banner**: a dismissible banner deep-links to
  Settings → Models while `chat_configured` is false; the signal is
  computed server-side so non-admins on configured systems never see a
  false banner.
- **Docs**: INSTALLATION.md Step 5 presents the wizard as the primary
  path, env vars as the expert path.
- **Cleanup**: the dead `ModelConnectionSettings.tsx` component (zero
  importers, retired placeholders) is deleted.
