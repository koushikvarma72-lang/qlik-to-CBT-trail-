"""Compatibility entrypoint for running the Flask backend."""

import logging

from backend.app import _active_ai_model, _env_path, _has_ai_provider_configured, _selected_ai_provider, app


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
    print("QVF Decoder - API Server")
    print("http://localhost:5000")
    print(f".env path: {_env_path}")
    provider = _selected_ai_provider()
    model = _active_ai_model(provider)
    if _has_ai_provider_configured(provider):
        print(f"AI provider configured: {provider} / {model}")
    else:
        print("WARNING: No AI provider configured - AI migration will not work.")
        print(f"  Create a .env file at: {_env_path}")
        print("  Example: AI_PROVIDER=gemini and GEMINI_API_KEY=...")
    app.run(debug=True, port=5000, use_reloader=False)
