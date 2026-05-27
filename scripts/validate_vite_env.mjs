#!/usr/bin/env node
/**
 * Validate VITE_APP_BASENAME before the Vite build.
 * Mirrors the rules in frontend/src/lib/normalize-base-path.ts.
 * Exits 0 if valid or empty, exits 1 with a clear error if invalid.
 */

const value = process.env.VITE_APP_BASENAME ?? '';

function validate(raw) {
  if (!raw || raw === '/') return;
  if (raw !== raw.trim()) {
    throw new Error('VITE_APP_BASENAME cannot contain leading or trailing whitespace');
  }
  if (/^https?:\/\//i.test(raw) || (raw.startsWith('//') && /[^/]/.test(raw))) {
    throw new Error('VITE_APP_BASENAME must be a path, not a URL');
  }
  if (/[\s;\\?#]/.test(raw) || hasControlCharacter(raw)) {
    throw new Error('VITE_APP_BASENAME contains unsafe characters');
  }
  const inner = raw.replace(/^\/+|\/+$/g, '');
  if (/\/{2,}/.test(inner)) {
    throw new Error('VITE_APP_BASENAME cannot contain duplicate slashes');
  }
  if (inner && inner.split('/').some(p => p === '.' || p === '..')) {
    throw new Error('VITE_APP_BASENAME cannot contain relative path segments');
  }
}

function hasControlCharacter(s) {
  for (const ch of s) {
    const code = ch.charCodeAt(0);
    if (code < 32 || code === 127) return true;
  }
  return false;
}

try {
  validate(value);
  const display = value || '(empty — root deployment)';
  console.log(`validate_vite_env: VITE_APP_BASENAME=${display} OK`);
} catch (err) {
  console.error(`validate_vite_env: FATAL: ${err.message}`);
  console.error('Fix VITE_APP_BASENAME and rebuild.');
  process.exit(1);
}
