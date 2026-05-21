import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Dev-mode proxy target. Defaults to the canonical local api on
// 8017; overrideable via env so the e2e harness can run on a host
// where 8017 is taken by another local checkout.
// See scripts/run-e2e-server.sh.
const apiPort = process.env.AUTOESSAY_E2E_API_PORT || '8017';
const apiTarget = `http://127.0.0.1:${apiPort}`;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': apiTarget,
      '/healthz': apiTarget,
      '/readyz': apiTarget,
      '/version': apiTarget
    }
  },
  test: {
    environment: 'node',
    setupFiles: './src/setupTests.ts',
    // Stage 3.C: keep vitest scoped to src/ unit tests so it does not
    // pick up Playwright spec files under e2e/ (those run via
    // `npm run e2e`, not vitest).
    include: ['src/**/*.{test,spec}.{ts,tsx}']
  }
});
