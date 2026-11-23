import json
import os
import re
import time
from typing import Any

import httpx

OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'
API_KEY = os.environ['OPENROUTER_API_KEY']
MODEL = os.environ.get('LLM_MODEL', 'qwen/qwen-2.5-72b-instruct:free')
TIMEOUT = 180
# Потолок длины ответа модели в токенах. Без него OpenRouter резервирует под ответ
# весь контекст модели (~131k) и отвечает 402, если на балансе меньше.
# Наш ответ — JSON с заголовком, 2–4 абзацами и фактами, это ~1–2k токенов;
# запас нужен на случай, если модель «думает» перед ответом.
MAX_TOKENS = int(os.environ.get('LLM_MAX_TOKENS', '4000'))
# Потолок токенов на «размышления» reasoning-моделей (qwen3 и др.) внутри MAX_TOKENS.
# Без него модель иногда думает до упора и не успевает дать ответ.
# 0 — размышления выключены совсем.
REASONING_MAX_TOKENS = int(os.environ.get('LLM_REASONING_MAX_TOKENS', '2000'))
MAX_RETRIES = 4
# Провайдеры OpenRouter, которых не используем (через запятую).
# ModelRun игнорировал JSON-режим и после ответа писал текст по кругу до лимита.
IGNORE_PROVIDERS = [
    p.strip() for p in os.environ.get('LLM_IGNORE_PROVIDERS', 'ModelRun').split(',')
    if p.strip()
]


class ModelUnavailableError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # Бесплатные модели иногда оборачивают ответ в ```json ... ```
    match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    return json.loads(text)


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Первый целый JSON-объект в тексте или None.

    Нужен для обрезанных ответов: модель выдала корректный JSON, а потом
    продолжила писать текст до лимита.
    """
    start = text.find('{')
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _tail(value: Any, size: int = 300) -> str:
    """Последние size символов в одну строку — чтобы увидеть, на чём оборвалось."""
    if not value:
        return '—'
    return repr(str(value)[-size:])


def _log_truncated(data: dict[str, Any]) -> None:
    """Диагностика обрезанного ответа: куда ушли токены и чем кончился текст.

    Если reasoning_tokens близко к лимиту — модель «додумалась» до упора;
    если в хвосте content повторяется одна фраза — модель зациклилась.
    """
    usage = data.get('usage') or {}
    details = usage.get('completion_tokens_details') or {}
    message = data['choices'][0].get('message') or {}
    content = message.get('content') or ''
    reasoning = message.get('reasoning') or ''
    print(
        f'[TRUNCATED] model={data.get("model")} provider={data.get("provider")} '
        f'prompt_tokens={usage.get("prompt_tokens")} '
        f'completion_tokens={usage.get("completion_tokens")} '
        f'reasoning_tokens={details.get("reasoning_tokens")} '
        f'content_chars={len(content)} reasoning_chars={len(reasoning)}',
        flush=True,
    )
    print(f'[TRUNCATED] content_tail={_tail(content)}', flush=True)
    if reasoning:
        print(f'[TRUNCATED] reasoning_tail={_tail(reasoning)}', flush=True)


def generate(prompt: str) -> dict[str, Any]:
    payload = {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.1,
        'max_tokens': MAX_TOKENS,
        'reasoning': (
            {'max_tokens': REASONING_MAX_TOKENS} if REASONING_MAX_TOKENS > 0
            else {'enabled': False}
        ),
        'response_format': {'type': 'json_object'},
        # Только провайдеры, поддерживающие все параметры запроса, без исключённых.
        'provider': {'require_parameters': True, 'ignore': IGNORE_PROVIDERS},
    }
    headers = {'Authorization': f'Bearer {API_KEY}'}
    delay = 10
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = httpx.post(
                OPENROUTER_URL, json=payload, headers=headers, timeout=TIMEOUT,
            )
        except httpx.HTTPError as error:
            raise RuntimeError(f'Ошибка сети OpenRouter: {error}') from error
        if response.status_code == 404:
            raise ModelUnavailableError(
                f'Модель {MODEL} недоступна: {response.text[:300]}'
            )
        if response.status_code == 429:
            if attempt == MAX_RETRIES:
                raise RuntimeError('OpenRouter: превышен rate limit (429).')
            print(f'[WARNING] 429, пауза {delay} сек. (попытка {attempt})', flush=True)
            time.sleep(delay)
            delay *= 2
            continue
        if response.status_code != 200:
            raise RuntimeError(
                f'OpenRouter HTTP {response.status_code}: {response.text[:500]}'
            )
        data = response.json()
        try:
            text = data['choices'][0]['message']['content']
        except (KeyError, IndexError) as error:
            raise RuntimeError(f'Неожиданный ответ OpenRouter: {data}') from error
        if data['choices'][0].get('finish_reason') == 'length':
            _log_truncated(data)
            salvaged = _first_json_object(text or '')
            if salvaged is not None:
                print('[TRUNCATED] в начале ответа целый JSON — используем его, '
                      'хвост отброшен', flush=True)
                return salvaged
            raise RuntimeError(f'Ответ обрезан на лимите max_tokens={MAX_TOKENS}.')
        if not text or not text.strip():
            raise RuntimeError('OpenRouter вернул пустой ответ.')
        try:
            return _extract_json(text)
        except json.JSONDecodeError as error:
            raise RuntimeError(f'Невалидный JSON от модели: {text[:500]}') from error
    raise RuntimeError('OpenRouter: все попытки исчерпаны.')