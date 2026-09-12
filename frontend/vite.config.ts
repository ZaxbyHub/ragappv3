/// <reference types="vitest" />
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import { createApiProxy, normalizeBasePath, normalizeViteBase } from './vite.paths'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const appBasename = normalizeBasePath(env.VITE_APP_BASENAME || '')

  return {
    base: normalizeViteBase(appBasename),
    plugins: [react()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: (id) => {
            const chunks: Record<string, string[]> = {
              'vendor-react': ['react', 'react-dom', 'react-router-dom'],
              'vendor-ui': ['framer-motion', '@radix-ui/react-dialog', '@radix-ui/react-select', '@radix-ui/react-tabs'],
              'vendor-state': ['zustand', '@tanstack/react-query', 'axios'],
            }
            for (const [chunkName, modules] of Object.entries(chunks)) {
              if (modules.some((mod) => id.includes(`/node_modules/${mod}/`))) {
                return chunkName
              }
            }
          },
        },
      },
    },
    server: {
      port: 3000,
      host: true,
      proxy: createApiProxy(appBasename),
    },
    test: {
      globals: true,
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      globalSetup: ['./src/test/global-setup.ts'],
      include: ['src/**/*.{test,spec}.{js,mjs,cjs,ts,mts,cts,jsx,tsx}'],
      // Scoped coverage gate (issue #258 / ENH-006): meaningful exercised
      // contracts only — the per-domain HTTP client modules and the Zustand
      // stores — instead of a blanket percentage. See `npm run test:coverage`.
      //
      // Thresholds are measured-baseline minus a ~2pt margin so real
      // regressions fail while normal churn passes. Measured baseline on
      // 2026-09-12 via `npm run test:coverage` (vitest 4.1.11, v8 provider,
      // scoped suites): statements 58.61, branches 61.39, functions 50.84,
      // lines 60.09. Functions is clamped at the documented floor (50) —
      // baseline minus 2 would dip below it. The floor plus the scoped
      // include is pinned by backend/tests/test_issue258_coverage_design.py.
      coverage: {
        provider: 'v8',
        include: ['src/lib/api/**', 'src/stores/**'],
        reporter: ['text'],
        thresholds: {
          statements: 56,
          branches: 59,
          functions: 50,
          lines: 58,
        },
      },
    },
  }
})
