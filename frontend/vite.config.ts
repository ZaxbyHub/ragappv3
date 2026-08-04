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
    },
  }
})
