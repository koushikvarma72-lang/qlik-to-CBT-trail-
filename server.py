"""Compatibility entrypoint for running the Flask backend."""

from backend.app import OPENROUTER_API_KEY, _env_path, app


if __name__ == '__main__':
    print("QVF Decoder - API Server")
    print("http://localhost:5000")
    print(f".env path: {_env_path}")
    if OPENROUTER_API_KEY:
        print(f"OpenRouter API key detected (starts with: {OPENROUTER_API_KEY[:12]}...)")
    else:
        print("WARNING: No OpenRouter API key detected - AI migration will not work.")
        print(f"  Create a .env file at: {_env_path}")
        print("  Add: OPENROUTER_API_KEY=sk-or-v1-...")
    app.run(debug=True, port=5000)
