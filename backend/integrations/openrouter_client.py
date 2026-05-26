import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """Raised when the OpenRouter API call fails."""
    pass


def _truncate_prompt(prompt, max_prompt_chars):
    if max_prompt_chars and len(prompt) > max_prompt_chars:
        truncation_notice = (
            f"\n\n[NOTE: prompt was truncated from {len(prompt):,} to "
            f"{max_prompt_chars:,} characters to fit the model context window.]"
        )
        return prompt[:max_prompt_chars] + truncation_notice
    return prompt


def _messages(prompt, system_prompt=None):
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _extract_openai_compatible_text(result, provider_name):
    try:
        return result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIClientError(
            f"Unexpected {provider_name} response shape: {exc}. "
            f"Raw response: {str(result)[:500]}"
        ) from exc


def _post_json_with_retries(url, headers, payload, timeout, retries, provider_name):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.exceptions.Timeout as exc:
            last_exc = exc
            logger.warning(
                "%s API timed out on attempt %d/%d.",
                provider_name,
                attempt + 1,
                retries + 1,
            )
            if attempt < retries:
                time.sleep(2 ** attempt)
            continue
        except requests.exceptions.ConnectionError as exc:
            raise AIClientError(f"Could not connect to {provider_name} API: {exc}") from exc

        if response.status_code >= 500 and attempt < retries:
            logger.warning(
                "%s API returned %s on attempt %d/%d; retrying...",
                provider_name,
                response.status_code,
                attempt + 1,
                retries + 1,
            )
            time.sleep(2 ** attempt)
            continue

        if not response.ok:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise AIClientError(f"{provider_name} API returned HTTP {response.status_code}: {detail}")

        try:
            return response.json()
        except ValueError as exc:
            raise AIClientError(f"{provider_name} API returned non-JSON response: {response.text[:500]}") from exc

    raise AIClientError(f"{provider_name} API request failed after {retries + 1} attempt(s): {last_exc}")


def call_openai_compatible_chat(
    api_key,
    model,
    prompt,
    base_url,
    provider_name,
    system_prompt=None,
    temperature=0,
    top_p=1,
    max_tokens=None,
    max_prompt_chars=60_000,
    timeout=60,
    retries=1,
):
    if not api_key:
        raise AIClientError(f"{provider_name} API key is not configured.")

    prompt = _truncate_prompt(prompt, max_prompt_chars)
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": _messages(prompt, system_prompt)}
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    result = _post_json_with_retries(url, headers, payload, timeout, retries, provider_name)
    return _extract_openai_compatible_text(result, provider_name)


def call_groq_chat(
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
):
    return call_openai_compatible_chat(
        api_key=api_key,
        model=model,
        prompt=prompt,
        base_url="https://api.groq.com/openai/v1",
        provider_name="Groq",
        system_prompt=system_prompt,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        max_prompt_chars=max_prompt_chars,
        timeout=timeout,
        retries=retries,
    )


def call_gemini_chat(
    api_key,
    model,
    prompt,
    system_prompt=None,
    temperature=0,
    top_p=1,
    max_tokens=None,
    max_prompt_chars=120_000,
    timeout=90,
    retries=1,
):
    if not api_key:
        raise AIClientError("GEMINI_API_KEY is not configured.")

    prompt = _truncate_prompt(prompt, max_prompt_chars)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {},
    }
    if system_prompt:
        payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
    if temperature is not None:
        payload["generationConfig"]["temperature"] = temperature
    if top_p is not None:
        payload["generationConfig"]["topP"] = top_p
    if max_tokens is not None:
        payload["generationConfig"]["maxOutputTokens"] = max_tokens
    thinking_budget = os.environ.get("GEMINI_THINKING_BUDGET", "0").strip()
    if thinking_budget:
        try:
            payload["generationConfig"]["thinkingConfig"] = {
                "thinkingBudget": int(thinking_budget)
            }
        except ValueError:
            logger.warning("Ignoring invalid GEMINI_THINKING_BUDGET=%r", thinking_budget)

    result = _post_json_with_retries(url, headers, payload, timeout, retries, "Gemini")
    try:
        candidate = result["candidates"][0]
        parts = candidate["content"]["parts"]
        text = "".join(part.get("text", "") for part in parts)
        finish_reason = candidate.get("finishReason")
        if finish_reason and finish_reason != "STOP":
            logger.warning(
                "Gemini finished with reason=%s output_chars=%d usage=%s",
                finish_reason,
                len(text),
                result.get("usageMetadata"),
            )
        return text
    except (KeyError, IndexError, TypeError) as exc:
        raise AIClientError(
            f"Unexpected Gemini response shape: {exc}. Raw response: {str(result)[:500]}"
        ) from exc


def call_ollama_chat(
    base_url,
    model,
    prompt,
    system_prompt=None,
    temperature=0,
    top_p=1,
    max_tokens=None,
    max_prompt_chars=60_000,
    timeout=180,
    retries=0,
):
    prompt = _truncate_prompt(prompt, max_prompt_chars)
    url = base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "messages": _messages(prompt, system_prompt),
        "stream": False,
        "options": {},
    }
    if temperature is not None:
        payload["options"]["temperature"] = temperature
    if top_p is not None:
        payload["options"]["top_p"] = top_p
    if max_tokens is not None:
        payload["options"]["num_predict"] = max_tokens

    result = _post_json_with_retries(url, {}, payload, timeout, retries, "Ollama")
    try:
        return result["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise AIClientError(
            f"Unexpected Ollama response shape: {exc}. Raw response: {str(result)[:500]}"
        ) from exc


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
            max_tokens=max_tokens or 3000,
            max_prompt_chars=max_prompt_chars,
            timeout=timeout,
        )

    if not api_key:
        raise AIClientError("OPENROUTER_API_KEY is not configured.")

    prompt = _truncate_prompt(prompt, max_prompt_chars)

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/genmab/qvf-decoder",
        "X-Title": "QVF Decoder Migration Tool",
    }

    payload = {"model": model, "messages": _messages(prompt, system_prompt)}
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
    max_tokens=3000,
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

