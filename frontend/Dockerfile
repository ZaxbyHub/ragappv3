# Build stage
FROM node:26-alpine AS build

WORKDIR /app

ENV NODE_ENV=production

COPY package*.json ./

RUN npm ci

COPY . .

ARG VITE_APP_BASENAME=/
ARG VITE_API_URL=
ENV VITE_APP_BASENAME=${VITE_APP_BASENAME}
ENV VITE_API_URL=${VITE_API_URL}

# Validate VITE_APP_BASENAME before build (inlined from scripts/validate_vite_env.mjs)
# to avoid needing a COPY from outside the build context.
RUN node -e "
const raw = process.env.VITE_APP_BASENAME ?? '';
const hasCtrl = function(s) {
  for (const ch of s) { const c = ch.charCodeAt(0); if (c < 32 || c === 127) return true; }
  return false;
};
const validate = function(raw) {
  if (!raw || raw === '/') return;
  if (raw !== raw.trim()) throw new Error('VITE_APP_BASENAME cannot contain leading or trailing whitespace');
  if (/^https?:\/\//i.test(raw) || (raw.startsWith('//') && /[^/]/.test(raw))) throw new Error('VITE_APP_BASENAME must be a path, not a URL');
  if (/[\s;\\\\?#]/.test(raw) || hasCtrl(raw)) throw new Error('VITE_APP_BASENAME contains unsafe characters');
  const inner = raw.replace(/^\/+|\/+$/g, '');
  if (/\/{2,}/.test(inner)) throw new Error('VITE_APP_BASENAME cannot contain duplicate slashes');
  if (inner && inner.split('/').some(function(p) { return p === '.' || p === '..'; })) throw new Error('VITE_APP_BASENAME cannot contain relative path segments');
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
"
RUN npm run build

# Final stage
FROM nginx:alpine

LABEL maintainer="RAGAPP Team"
LABEL description="RAGAPP Frontend - Multi-stage build"

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
