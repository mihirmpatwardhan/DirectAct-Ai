import { createRoot } from 'react-dom/client'
import './index.css'
import { AppRouter } from './router.tsx'

// StrictMode intentionally double-mounts components in dev, which creates a
// WebSocket connect → cleanup → reconnect storm. Removed for stability.
createRoot(document.getElementById('root')!).render(
  <AppRouter />
)
