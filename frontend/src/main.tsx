import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import App from './App.tsx'
import './index.css'
// Initialize theme from persisted preference before first paint
import { useThemeStore } from '@/stores/useThemeStore'
import { logSubpathConfig } from './lib/paths'

const queryClient = new QueryClient()

logSubpathConfig();

// ThemedToaster keeps sonner's color scheme aligned with our user-overridable
// theme toggle. We pass "system" through directly so OS-level preference
// changes propagate without an extra effect; explicit "light" / "dark" wins
// over the OS setting.
function ThemedToaster() {
  const theme = useThemeStore((s) => s.theme)
  // sonner only understands light/dark/system; high-contrast is light-based.
  const toasterTheme = theme === 'high-contrast' ? 'light' : theme
  return (
    <Toaster
      position="bottom-right"
      theme={toasterTheme}
      toastOptions={{ className: 'max-w-[90vw] sm:max-w-sm' }}
      richColors
      closeButton
    />
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemedToaster />
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
)
