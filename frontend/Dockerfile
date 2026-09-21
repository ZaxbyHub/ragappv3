# Build stage
# Pinned to node 22.22 to match CI (ci.yml node-version "22.22.0"); parity is
# enforced by scripts/check_runtime_contract.py. Digest pin (issue #404 / #391)
# freezes the base image for supply-chain integrity; dependabot (docker
# ecosystem, "/frontend") opens PRs on new digests.
FROM node:22.22-alpine@sha256:e58326d0d441090181ac150dc2078d3e2cf6a0d42e809aebba3ef5880935ffdd AS build

WORKDIR /app

# No ENV NODE_ENV=production in the build stage (issue #640): `npm ci` must
# install devDependencies — typescript, vite and the tailwind/postcss plugins
# power the `tsc && vite build` step below, and npm skips devDependencies
# under NODE_ENV=production. vite still emits a production bundle (build mode
# is independent of NODE_ENV); the final stage ships only dist/ anyway.

COPY package*.json ./

RUN npm ci

COPY . .

ARG VITE_APP_BASENAME=/
ARG VITE_API_URL=
ENV VITE_APP_BASENAME=${VITE_APP_BASENAME}
ENV VITE_API_URL=${VITE_API_URL}

# validate_vite_env inlined to avoid dependency on an external file; the
# validation rules and error-message text are identical to
# scripts/validate_vite_env.mjs (config-env-contract parity), with one repo-
# mandated addition: every throw concatenates JSON.stringify(raw) so build
# logs name the received value (issue #567 — pinned by
# frontend/src/base-path-message.test.ts).
# BuildKit heredoc form (same shape the root Dockerfile adopted in #258): the
# previous unquoted multi-line `RUN node -e "..."` block was un-parseable
# Dockerfile syntax (every JS line read as a Dockerfile instruction —
# "unknown instruction: const"); the docker-smoke CI job builds this file, so
# the parse cannot regress silently again (#640).
RUN <<'NODEJS' node
const raw = process.env.VITE_APP_BASENAME ?? '';
const hasCtrl = function(s) {
  for (const ch of s) { const c = ch.charCodeAt(0); if (c < 32 || c === 127) return true; }
  return false;
};
const validate = function(raw) {
  if (!raw || raw === '/') return;
  if (raw !== raw.trim()) throw new Error('VITE_APP_BASENAME cannot contain leading or trailing whitespace: ' + JSON.stringify(raw));
  if (/^https?:\/\//i.test(raw) || (raw.startsWith('//') && /[^/]/.test(raw))) throw new Error('VITE_APP_BASENAME must be a path, not a URL: ' + JSON.stringify(raw));
  if (/[\s;\\\\?#]/.test(raw) || hasCtrl(raw)) throw new Error('VITE_APP_BASENAME contains unsafe characters: ' + JSON.stringify(raw));
  const inner = raw.replace(/^\/+|\/+$/g, '');
  if (/\/{2,}/.test(inner)) throw new Error('VITE_APP_BASENAME cannot contain duplicate slashes: ' + JSON.stringify(raw));
  if (inner && inner.split('/').some(function(p) { return p === '.' || p === '..'; })) throw new Error('VITE_APP_BASENAME cannot contain relative path segments: ' + JSON.stringify(raw));
};
try {
  validate(raw);
  const d = raw || '(empty - root deployment)';
  console.log('validate_vite_env: VITE_APP_BASENAME=' + d + ' OK');
} catch (e) {
  console.error('validate_vite_env: FATAL: ' + e.message);
  console.error('Fix VITE_APP_BASENAME and rebuild.');
  process.exit(1);
}
NODEJS
RUN npm run build

# Final stage
# Digest pin (issue #404 / #391); dependabot (docker, "/frontend") maintains.
FROM nginx:alpine@sha256:db35bfc6b2951e7f8a72db5db120288c127ffaeeb4a6d4b95a26fead017d5913

LABEL maintainer="RAGAPP Team"
LABEL description="RAGAPP Frontend - Multi-stage build"

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
