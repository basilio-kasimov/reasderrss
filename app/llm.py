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
MAX_RETRIES = 4


class ModelUnavailableError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # Бесплатные модели иногда оборачивают ответ в ```json ... ```
    match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    return json.loads(text)


def generate(prompt: str) -> dict[str, Any]:
    payload = {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.1,
        'response_format': {'type': 'json_object'},
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
        if not text or not text.strip():
            raise RuntimeError('OpenRouter вернул пустой ответ.')
        try:
            return _extract_json(text)
        except json.JSONDecodeError as error:
            raise RuntimeError(f'Невалидный JSON от модели: {text[:500]}') from error
    raise RuntimeError('OpenRouter: все попытки исчерпаны.')