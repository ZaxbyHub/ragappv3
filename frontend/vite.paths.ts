// normalizeBasePath is duplicated here because vite.config.ts runs under
// tsconfig.node.json which cannot import from src/ without composite conflicts.
// Keep in sync with src/lib/normalize-base-path.ts.
const UNSAFE_BASE_PATH_PATTERN = /[\s;\\?#]/

function hasControlCharacter(value: string): boolean {
  return Array.from(value).some((char) => {
    const code = char.charCodeAt(0)
    return code < 32 || code === 127
  })
}

export function normalizeBasePath(value?: string | null): string {
  const raw = value ?? ''
  if (!raw) return ''
  if (raw !== raw.trim()) {
    throw new Error('Base path cannot contain leading or trailing whitespace')
  }
  if (/^https?:\/\//i.test(raw) || (raw.startsWith('//') && /[^/]/.test(raw))) {
    throw new Error('Base path must be a path, not a URL')
  }
  if (UNSAFE_BASE_PATH_PATTERN.test(raw) || hasControlCharacter(raw)) {
    throw new Error('Base path contains unsafe characters')
  }
  if (/\/{2,}/.test(raw.replace(/^\/+|\/+$/g, ''))) {
    throw new Error('Base path cannot contain duplicate slashes')
  }

  const stripped = raw.replace(/^\/+|\/+$/g, '')
  if (!stripped) return ''
  if (stripped.split('/').some((part) => part === '.' || part === '..')) {
    throw new Error('Base path cannot contain relative path segments')
  }
  return `/${stripped}`
}

const API_PROXY_TARGET = 'http://localhost:9090'

export interface ApiProxyOptions {
  target: string
  changeOrigin: boolean
  rewrite: (requestPath: string) => string
}

export function normalizeViteBase(value?: string | null): string {
  const base = normalizeBasePath(value)
  return base ? `${base}/` : '/'
}

export function createApiProxyPath(basename: string): string {
  const base = normalizeBasePath(basename)
  return base ? `${base}/api` : '/api'
}

export function rewriteApiProxyPath(requestPath: string, basename: string): string {
  const base = normalizeBasePath(basename)
  if (!base) return requestPath
  if (requestPath === `${base}/api`) return '/api'
  if (requestPath.startsWith(`${base}/api/`)) return requestPath.slice(base.length)
  return requestPath
}

export function createApiProxy(basename: string): Record<string, ApiProxyOptions> {
  const base = normalizeBasePath(basename)
  const rootProxy = {
    target: API_PROXY_TARGET,
    changeOrigin: true,
  }
  if (!base) {
    return {
      '/api': rootProxy,
    }
  }

  return {
    [createApiProxyPath(base)]: {
      target: API_PROXY_TARGET,
      changeOrigin: true,
      rewrite: (requestPath) => rewriteApiProxyPath(requestPath, base),
    },
  }
}
