# Environment Variables — ClinicalTriage-Env

Copy `.env.example` to `.env` and fill in the values before running `docker compose up`.

```bash
cp .env.example .env
```

---

## Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HF_TOKEN` | Optional | *(empty)* | Hugging Face API token. Required if you use models hosted on HF Hub that need authentication. Leave blank for fully local/offline operation. |
| `DEFAULT_TASK` | Optional | `task1_esi_assignment` | Backend default task loaded on startup. Valid values: `task1_esi_assignment`, `task2_queue_priority`, `task3_ambiguous_triage`. |

---

## Container-internal Variables (do not set manually)

These are set automatically by `docker-compose.yml` and should **not** be overridden in `.env`:

| Variable | Set In | Value | Description |
|----------|--------|-------|-------------|
| `BACKEND_URL` | `docker-compose.yml` | `http://backend:7860` | Server-side URL used by Next.js rewrites to proxy `/api/*` to the backend. Never exposed to the browser. |
| `NODE_ENV` | `docker-compose.yml` | `production` | Tells Next.js to run in production mode. |
| `PYTHONUNBUFFERED` | `docker-compose.yml` | `1` | Ensures Python logs are not buffered. |

---

## How Requests Flow

```
Browser → :80 → Nginx
              ├─ /api/* → strip prefix → backend:7860
              └─ /*     → frontend:3000 → Next.js rewrites /api/* → backend:7860 (server-side only)
```

The browser **never** needs to know the backend's internal Docker hostname. All API calls go through `http://<EC2-IP>/api/`.
