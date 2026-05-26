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

- Backend: `python server.py`
- Frontend: `cd frontend && npm run dev`

## Main backend files

- `backend/app.py`: Flask server and API routes
- `backend/extraction/qvf_runtime.py`: QVF extraction and script preprocessing
- `backend/extraction/qlik_script_parser.py`: Qlik load-script parser
- `backend/migration/sql_generation.py`: SQL generation, post-processing, and repair helpers
- `backend/migration/validator.py`: migration validation and severity scoring
- `backend/migration/ir.py`: structured migration IR and schema contracts
- `backend/integrations/dbt_routes.py`: dbt-related route registration
- `backend/integrations/openrouter_client.py`: AI provider clients for Gemini, Ollama, Groq, and OpenRouter

Old root-level compatibility wrappers were removed. New code should import from the package that owns the behavior.

See `docs/MIGRATION_ARCHITECTURE.md` for the target Qlik -> dbt pipeline.

## AI Provider

Set one provider in `.env`:

```text
AI_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
```

Supported values:

- `gemini`: uses `GEMINI_API_KEY` and `GEMINI_MODEL`
- `ollama`: uses `OLLAMA_BASE_URL` and `OLLAMA_MODEL`, for example `qwen2.5-coder:14b`
- `groq`: uses `GROQ_API_KEY` and `GROQ_MODEL`
- `openrouter`: uses `OPENROUTER_API_KEY` and `OPENROUTER_MODEL`
- `auto`: prefers Gemini, then Groq, then OpenRouter, then Ollama
