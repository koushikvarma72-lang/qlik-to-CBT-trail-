import json
import logging
import time

import requests

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """Raised when the OpenRouter API call fails."""
    pass


def call_openrouter_chat(
    api_key,
    model,
    prompt,
    system_prompt=None,
    temperature=0,
    top_p=1,
    max_tokens=None,
    max_prompt_chars=60_000,
    timeout=60,
    retries=1,
    stream=False,
):
    """Call OpenRouter Chat Completions and return the assistant text.

    When ``stream=True`` this function returns a **generator** that yields text
    chunks as they arrive (identical interface to
    ``call_openrouter_chat_stream``).  When ``stream=False`` (default) it
    returns the full response string as before.
    """
    if stream:
        # Delegate to the dedicated streaming generator
        return call_openrouter_chat_stream(
            api_key=api_key,
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens or 8000,
            max_prompt_chars=max_prompt_chars,
            timeout=timeout,
        )

    if not api_key:
        raise AIClientError("OPENROUTER_API_KEY is not configured.")

    # Guard against context-window overflows for large QVF files
    if max_prompt_chars and len(prompt) > max_prompt_chars:
        truncation_notice = (
            f"\n\n[NOTE: prompt was truncated from {len(prompt):,} to "
            f"{max_prompt_chars:,} characters to fit the model context window.]"
        )
        prompt = prompt[:max_prompt_chars] + truncation_notice

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/genmab/qvf-decoder",
        "X-Title": "QVF Decoder Migration Tool",
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    last_exc = None
    for attempt in range(retries + 1):
        try:
            # Use json= so requests sets Content-Type and serialises correctly;
            # passing data=json.dumps() bypasses requests' built-in handling.
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.exceptions.Timeout as exc:
            last_exc = exc
            logger.warning(
                "OpenRouter API timed out on attempt %d/%d.", attempt + 1, retries + 1
            )
            if attempt < retries:
                time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError as exc:
            raise AIClientError(f"Could not connect to OpenRouter API: {exc}") from exc

        # Retry on server-side errors
        if response.status_code >= 500 and attempt < retries:
            logger.warning(
                "OpenRouter API returned %s on attempt %d/%d; retrying…",
                response.status_code, attempt + 1, retries + 1,
            )
            time.sleep(2 ** attempt)
            continue

        if not response.ok:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise AIClientError(
                f"OpenRouter API returned HTTP {response.status_code}: {detail}"
            )

        try:
            result = response.json()
            logger.info(
                "OpenRouter response OK model=%s status=%s choices=%d",
                model,
                response.status_code,
                len(result.get('choices', [])),
            )
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise AIClientError(
                f"Unexpected OpenRouter response shape: {exc}. "
                f"Raw response: {response.text[:500]}"
            ) from exc

    raise AIClientError(
        f"OpenRouter API request failed after {retries + 1} attempt(s): {last_exc}"
    )


def call_openrouter_chat_stream(
    api_key,
    model,
    prompt,
    system_prompt=None,
    max_tokens=8000,
    temperature=0,
    top_p=1,
    max_prompt_chars=60_000,
    timeout=120,
):
    """Generator that yields text chunks as they arrive from OpenRouter via SSE streaming.

    Args:
        api_key: OpenRouter API key.
        model: Model identifier string.
        prompt: User message content.
        system_prompt: Optional system message.
        max_tokens: Maximum tokens in the response.
        temperature: Sampling temperature.
        top_p: Nucleus sampling parameter.
        max_prompt_chars: Hard cap on prompt length before truncation.
        timeout: Request timeout in seconds.

    Yields:
        str: Text content chunks as they arrive from the model.

    Raises:
        AIClientError: On missing API key, HTTP errors, or connection failures.
    """
    if not api_key:
        raise AIClientError("OPENROUTER_API_KEY is not configured.")

    # Guard against context-window overflows
    if max_prompt_chars and len(prompt) > max_prompt_chars:
        truncation_notice = (
            f"\n\n[NOTE: prompt was truncated from {len(prompt):,} to "
            f"{max_prompt_chars:,} characters to fit the model context window.]"
        )
        prompt = prompt[:max_prompt_chars] + truncation_notice

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/genmab/qvf-decoder",
        "X-Title": "QVF Decoder Migration Tool",
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": True,
    }

    try:
        with requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout) as response:
            if not response.ok:
                raise AIClientError(
                    f"OpenRouter stream error: HTTP {response.status_code} {response.text[:300]}"
                )

            for line in response.iter_lines():
                if not line:
                    continue

                # SSE lines look like: data: {...json...}
                decoded = line.decode("utf-8") if isinstance(line, bytes) else line
                if not decoded.startswith("data: "):
                    continue

                data_str = decoded[6:]  # strip "data: "

                if data_str.strip() == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0]["delta"]
                    content = delta.get("content", "")
                    if content:
                        yield content
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

    except requests.exceptions.Timeout as exc:
        raise AIClientError(f"OpenRouter stream timed out after {timeout}s: {exc}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise AIClientError(f"Could not connect to OpenRouter API for streaming: {exc}") from exc

