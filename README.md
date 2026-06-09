# QVF Decoder

This repository has been reorganized so the main app is easier to understand:

- `backend/`: Flask app entrypoint plus small shared app services
- `backend/extraction/`: QVF/QVS extraction, binary decoding, and Qlik script parsing
- `backend/migration/`: IR building, deterministic SQL generation, validation, repair guardrails, and reporting
- `backend/integrations/`: OpenRouter and dbt/dbt Cloud integration routes
- `frontend/`: Vite frontend
- `tools/`: debug, verification, and exploratory scripts that are not part of the runtime app
- `docs/`: legacy notes and implementation writeups

## Run the app

- Backend: `python server.py` (`http://localhost:5000`)
- Frontend: `cd frontend && npm run dev` (`http://localhost:5173`)

## Cloud Deployment

### Backend on Render

The backend is configured for Render with `render.yaml`.

1. Create a Render web service from this repository.
2. Use the included settings: `rootDir: backend`, build command `pip install -r requirements.txt`, and start command `gunicorn app:app`.
3. Add a persistent disk mounted at `/data` with enough capacity for generated QVD artifacts and migration packages.
4. Set production secrets in Render, especially Databricks credentials (`DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_HTTP_PATH`) and any AI provider keys you use.

Generated runtime files are preserved only when written under `/data`. The backend uses:

- `/data/uploads`
- `/data/generated_artifacts`
- `/data/qvd_outputs`
- `/data/migration_packages`
- `/data/logs`

For local development, if `/data` is not writable and `DATA_ROOT` is not set, the backend falls back to `backend/backend_runtime_data`.

### Frontend on Netlify

The frontend is configured for Netlify with `netlify.toml`.

1. Create a Netlify site from this repository.
2. Use the included build settings: base `frontend`, command `npm run build`, publish `dist`.
3. Set `VITE_API_BASE_URL` to the Render backend URL, for example `https://qvd-databricks-backend.onrender.com`.

The frontend prefixes backend-provided `/api/...` download links with `VITE_API_BASE_URL`, so generated files are downloaded from Render instead of Netlify.

## Main backend files

- `backend/app.py`: Flask server and API routes
- `backend/extraction/qvf_runtime.py`: QVF extraction and script preprocessing
- `backend/extraction/qlik_script_parser.py`: Qlik load-script parser
- `backend/migration/sql_generation.py`: SQL generation, post-processing, and repair helpers
- `backend/migration/validator.py`: migration validation and severity scoring
- `backend/migration/ir.py`: structured migration IR and schema contracts
- `backend/integrations/dbt_routes.py`: dbt-related route registration
- `backend/integrations/openrouter_client.py`: AI provider clients for Gemini, Ollama, Groq, and OpenRouter

`server.py` is a compatibility entrypoint for running the backend locally. New code should import from the package that owns the behavior.

See `docs/MIGRATION_ARCHITECTURE.md` for the target Qlik -> dbt pipeline.

## AI Provider

Set one provider in `.env`:

```text
AI_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

Use `.env.example` as the safe template and keep real keys only in local `.env`.

Supported values:

- `gemini`: uses `GEMINI_API_KEY` and `GEMINI_MODEL`
- `ollama`: uses `OLLAMA_BASE_URL` and `OLLAMA_MODEL`, for example `qwen2.5-coder:14b`
- `groq`: uses `GROQ_API_KEY` and `GROQ_MODEL`
- `openrouter`: uses `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`
- `auto`: prefers Gemini, then Groq, then OpenRouter, then Ollama
