# QVF Decoder

This repository has been reorganized so the main app is easier to understand:

- `backend/`: Flask app, QVF extraction, SQL migration, dbt integration, and backend helpers
- `frontend/`: Vite frontend
- `tools/`: debug, verification, and exploratory scripts that are not part of the runtime app
- `docs/`: legacy notes and implementation writeups

## Run the app

- Backend: `python server.py`
- Frontend: `cd frontend && npm run dev`

## Main backend files

- `backend/app.py`: Flask server and API routes
- `backend/qvf_runtime.py`: QVF extraction and script preprocessing
- `backend/sql_migration.py`: SQL generation, parsing, and repair helpers
- `backend/dbt_routes.py`: dbt-related route registration
- `backend/openrouter_client.py`: OpenRouter API client

The old root-level Python module names are kept as small compatibility wrappers so existing commands and imports still continue to work.
