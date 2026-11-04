# tla-web — Linux Admin Platform frontend

React 18 + TypeScript + Vite + Tailwind. Persian (fa, RTL) and English,
dark-first theming, mobile-first (used standalone and as the Bale in-app page).

```bash
npm install
npm run dev        # proxies /api to 127.0.0.1:8080 (TLA_WEB_API overrides)
npm run build      # typecheck + production bundle in dist/
```

The FastAPI backend serves `dist/` automatically when present
(`TLA_WEB_DIST`, default `web/dist`). All state comes from `/api/v1`; there
are no mocks. Session cookie + CSRF handled by `src/api/client.ts`.
