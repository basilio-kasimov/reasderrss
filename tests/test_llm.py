"""Шаг 2: app/llm.py — запрос к OpenRouter и разбор ответа.

Настоящих запросов нет: httpx.post подменяется заглушкой, которая запоминает
отправленный запрос и возвращает заготовленный ответ.
"""
import json
import os

import httpx
import pytest

from app import llm


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=''):
        self.status_code = status_code
        self._data = data
        self.text = text or (json.dumps(data) if data is not None else '')

    def json(self):
        return self._data


def answer(content, finish_reason='stop', **extra):
    """Ответ OpenRouter в формате chat/completions."""
    return {
        'model': 'test/model',
        'provider': 'TestProvider',
        'choices': [{'finish_reason': finish_reason,
                     'message': {'content': content, **extra}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50},
    }


@pytest.fixture
def openrouter(monkeypatch):
    """Заглушка OpenRouter: очередь ответов и список отправленных запросов."""
    state = {'responses': [], 'requests': []}

    def fake_post(url, json=None, headers=None, timeout=None):
        state['requests'].append({'url': url, 'json': json, 'headers': headers})
        return state['responses'].pop(0)

    monkeypatch.setattr(llm.httpx, 'post', fake_post)
    monkeypatch.setattr(llm.time, 'sleep', lambda seconds: None)  # без реальных пауз
    return state


# ---------- Что уходит в OpenRouter ----------

def test_default_model_is_current():
    # Регресс: без LLM_MODEL код брал бесплатную qwen-2.5-72b:free, которая
    # с начала ноября 2026 не отвечала. В тестовом окружении LLM_MODEL не задан.
    assert 'LLM_MODEL' not in os.environ
    assert llm.MODEL == 'qwen/qwen3.8-27b'


def test_request_has_limits_and_provider_filter(openrouter):
    openrouter['responses'].append(FakeResponse(data=answer('{"ok": true}')))
    llm.generate('промпт')

    payload = openrouter['requests'][0]['json']
    assert payload['max_tokens'] == llm.MAX_TOKENS
    assert payload['reasoning'] == {'max_tokens': llm.REASONING_MAX_TOKENS}
    assert payload['response_format'] == {'type': 'json_object'}
    assert payload['provider']['require_parameters'] is True
    assert 'ModelRun' in payload['provider']['ignore']
    assert payload['messages'] == [{'role': 'user', 'content': 'промпт'}]
    assert openrouter['requests'][0]['headers']['Authorization'] == f'Bearer {llm.API_KEY}'


def test_reasoning_zero_disables_thinking(openrouter, monkeypatch):
    monkeypatch.setattr(llm, 'REASONING_MAX_TOKENS', 0)
    openrouter['responses'].append(FakeResponse(data=answer('{"ok": true}')))
    llm.generate('промпт')
    assert openrouter['requests'][0]['json']['reasoning'] == {'enabled': False}


# ---------- Разбор нормального ответа ----------

def test_plain_json_answer(openrouter):
    openrouter['responses'].append(
        FakeResponse(data=answer('{"title_ru": "Заголовок", "category": "politics"}')))
    assert llm.generate('п') == {'title_ru': 'Заголовок', 'category': 'politics'}


def test_json_in_code_fence(openrouter):
    openrouter['responses'].append(
        FakeResponse(data=answer('```json\n{"title_ru": "Заголовок"}\n```')))
    assert llm.generate('п') == {'title_ru': 'Заголовок'}


def test_empty_answer_is_error(openrouter):
    openrouter['responses'].append(FakeResponse(data=answer('   ')))
    with pytest.raises(RuntimeError, match='пустой ответ'):
        llm.generate('п')


def test_invalid_json_is_error(openrouter):
    openrouter['responses'].append(FakeResponse(data=answer('это не JSON')))
    with pytest.raises(RuntimeError, match='Невалидный JSON'):
        llm.generate('п')


def test_unexpected_shape_is_error(openrouter):
    openrouter['responses'].append(FakeResponse(data={'choices': []}))
    with pytest.raises(RuntimeError, match='Неожиданный ответ'):
        llm.generate('п')


# ---------- Обрезанный ответ (finish_reason = length) ----------

def test_truncated_with_full_json_is_salvaged(openrouter, capsys):
    # Случай ModelRun 2026-11-23: целый JSON, потом текст по кругу до лимита.
    looping = '{"title_ru": "Матч", "category": "sport"}' + '\n\nКатегория: sport.' * 50
    openrouter['responses'].append(FakeResponse(data=answer(looping, 'length')))
    assert llm.generate('п') == {'title_ru': 'Матч', 'category': 'sport'}
    assert 'используем его' in capsys.readouterr().out


def test_truncated_mid_json_is_error_after_retry(openrouter):
    openrouter['responses'] += [
        FakeResponse(data=answer('{"title_ru": "Обрыв на сере', 'length'))
    ] * (llm.TRUNCATED_RETRIES + 1)
    with pytest.raises(RuntimeError, match='обрезан'):
        llm.generate('п')
    assert len(openrouter['requests']) == llm.TRUNCATED_RETRIES + 1


def test_truncated_without_json_is_retried_without_that_provider(openrouter, capsys):
    # Случай 2026-11-24: Parasail писал текст по кругу, JSON в начале не было.
    looping = 'Все четыре материала об одном событии. Категория: политика. ' * 50
    looped = answer(looping, 'length')
    looped['provider'] = 'Parasail'
    openrouter['responses'] += [FakeResponse(data=looped),
                                FakeResponse(data=answer('{"ok": true}'))]
    assert llm.generate('п') == {'ok': True}
    first, second = (r['json']['provider']['ignore'] for r in openrouter['requests'])
    assert 'Parasail' not in first
    assert second == first + ['Parasail']
    out = capsys.readouterr().out
    assert 'повтор без провайдера Parasail' in out
    assert "content_head='Все четыре материала" in out


def test_truncated_reasoning_only_is_error_with_diagnostics(openrouter, capsys):
    # Случай 2026-11-22: все токены ушли на размышления, ответа нет.
    data = answer(None, 'length', reasoning='думаю ' * 100)
    data['usage'] = {'prompt_tokens': 755, 'completion_tokens': 8000,
                     'completion_tokens_details': {'reasoning_tokens': 8000}}
    openrouter['responses'] += [FakeResponse(data=data)] * (llm.TRUNCATED_RETRIES + 1)
    with pytest.raises(RuntimeError, match='обрезан'):
        llm.generate('п')
    out = capsys.readouterr().out
    assert 'reasoning_tokens=8000' in out
    assert 'content_chars=0' in out
    assert 'provider=TestProvider' in out


# ---------- HTTP-ошибки ----------

def test_404_means_model_unavailable(openrouter):
    openrouter['responses'].append(FakeResponse(404, text='No endpoints found'))
    with pytest.raises(llm.ModelUnavailableError):
        llm.generate('п')


def test_402_is_error_with_status(openrouter):
    openrouter['responses'].append(FakeResponse(402, text='requires more credits'))
    with pytest.raises(RuntimeError, match='HTTP 402'):
        llm.generate('п')


def test_429_is_retried_then_succeeds(openrouter):
    openrouter['responses'] += [FakeResponse(429), FakeResponse(429),
                                FakeResponse(data=answer('{"ok": true}'))]
    assert llm.generate('п') == {'ok': True}
    assert len(openrouter['requests']) == 3


def test_429_gives_up_after_max_retries(openrouter):
    openrouter['responses'] += [FakeResponse(429)] * llm.MAX_RETRIES
    with pytest.raises(RuntimeError, match='429'):
        llm.generate('п')
    assert len(openrouter['requests']) == llm.MAX_RETRIES


def test_network_error(monkeypatch):
    def broken_post(*args, **kwargs):
        raise httpx.ConnectError('нет сети')
    monkeypatch.setattr(llm.httpx, 'post', broken_post)
    with pytest.raises(RuntimeError, match='Ошибка сети'):
        llm.generate('п')


# ---------- Вспомогательная функция ----------

@pytest.mark.parametrize('text, expected', [
    ('{"a": 1} хвост', {'a': 1}),
    ('\n\n{"a": {"b": 1}} мусор {"c": 2}', {'a': {'b': 1}}),
    ('{"a": "обрыв', None),
    ('без json', None),
    ('[1, 2, 3]', None),          # не объект
])
def test_first_json_object(text, expected):
    assert llm._first_json_object(text) == expected
