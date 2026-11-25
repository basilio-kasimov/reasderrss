"""Шаг 4: app/pipeline.py — расписание, очистка, «известность», прогон целиком.

В прогоне main() подменяются: сбор RSS (сеть), группировка (модель эмбеддингов)
и llm.generate (OpenRouter). Всё остальное — настоящий код на тестовой базе.
"""
import pytest

from app import db, llm, pipeline
from tests.factories import add_article, add_run, add_story, ago, link, set_setting


def count(conn, table, where='TRUE'):
    return conn.execute(f'SELECT count(*) AS n FROM {table} WHERE {where}').fetchone()['n']


# ---------- Расписание: should_run / retry_allowed ----------

def test_first_run_ever(conn):
    assert pipeline.should_run(conn, 6) is True


def test_interval_not_elapsed(conn):
    add_run(conn, status='success', finished_at=ago(hours=5))
    assert pipeline.should_run(conn, 6) is False


def test_interval_elapsed(conn):
    add_run(conn, status='success', finished_at=ago(hours=7))
    assert pipeline.should_run(conn, 6) is True


def test_manual_request_overrides_interval(conn):
    add_run(conn, status='success', finished_at=ago(minutes=10))
    set_setting(conn, 'run_requested', True)
    assert pipeline.should_run(conn, 6) is True


def test_interval_counts_from_last_success_not_failure(conn):
    # Неудачный прогон не сдвигает интервал: успешный был 7 ч назад, пора.
    add_run(conn, status='success', finished_at=ago(hours=7))
    add_run(conn, status='failed', finished_at=ago(hours=2))
    assert pipeline.should_run(conn, 6) is True


def test_retry_after_failure_waits_an_hour(conn):
    add_run(conn, status='success', finished_at=ago(hours=7))
    add_run(conn, status='failed', finished_at=ago(minutes=30))
    assert pipeline.should_run(conn, 6) is False


def test_retry_after_failure_allowed_after_an_hour(conn):
    add_run(conn, status='success', finished_at=ago(hours=7))
    add_run(conn, status='failed', finished_at=ago(minutes=pipeline.FAILED_RETRY_MINUTES + 1))
    assert pipeline.should_run(conn, 6) is True


def test_retry_limit_also_without_any_success(conn):
    add_run(conn, status='failed', finished_at=ago(minutes=10))
    assert pipeline.should_run(conn, 6) is False


def test_clear_run_request(conn):
    set_setting(conn, 'run_requested', True)
    pipeline.clear_run_request(conn)
    assert pipeline.run_requested(conn) is False


# ---------- Очистка ----------

def test_cleanup_removes_only_old_data(conn):
    old_story = add_story(conn, created_at=ago(days=25))
    old_article = add_article(conn, created_at=ago(days=25))
    link(conn, old_story, old_article)
    add_run(conn, started_at=ago(days=25), finished_at=ago(days=25))

    fresh_story = add_story(conn, created_at=ago(days=1))
    fresh_article = add_article(conn, created_at=ago(days=1))
    link(conn, fresh_story, fresh_article)
    add_run(conn, finished_at=ago(hours=1))

    pipeline.cleanup_old_data(conn, 20)

    assert [r['id'] for r in conn.execute('SELECT id FROM stories').fetchall()] == [fresh_story]
    assert [r['id'] for r in conn.execute('SELECT id FROM articles').fetchall()] == [fresh_article]
    assert count(conn, 'runs') == 1
    assert count(conn, 'story_articles') == 1      # связь старого сюжета ушла каскадом


@pytest.mark.parametrize('stored, effective', [(20, 20), (3, 3), (2, 3), (0, 3), (-1, 3)])
def test_retention_never_below_minimum(stored, effective):
    assert db.retention_days({'retention_days': stored}) == effective


# ---------- «Известность» группы ----------

def test_story_exists_threshold(conn):
    ids = [add_article(conn) for _ in range(10)]
    story = add_story(conn)
    for article_id in ids[:7]:                       # 7 из 10 — уже в сюжете
        link(conn, story, article_id)
    group = [{'id': i} for i in ids]
    assert pipeline.story_exists(conn, group) is True
    assert pipeline.story_exists(conn, group + [{'id': add_article(conn)}]) is False  # 7 из 11


# ---------- Решение по ответу LLM ----------

@pytest.mark.parametrize('answer, accepted', [
    ({'same_story': True, 'category': 'politics'}, True),
    ({'same_story': True, 'category': 'sport'}, True),
    ({'same_story': True, 'category': 'incidents'}, True),
    ({'same_story': True, 'category': 'other'}, False),      # погода
    ({'same_story': False, 'category': 'politics'}, False),  # разные события
    ({'same_story': True, 'category': 'неизвестная'}, False),
    ({}, False),
])
def test_summarize_acceptance(monkeypatch, answer, accepted):
    monkeypatch.setattr(llm, 'generate', lambda prompt: {'title_ru': 'Т', **answer})
    group = [{'id': 1, 'source': 'BBC News', 'title': 'a', 'summary': ''}]
    assert pipeline.summarize(group)['accepted'] is accepted


@pytest.mark.parametrize('answer, expected', [
    ({'same_story': True, 'category': 'politics', 'reason': ''}, None),
    ({'same_story': False, 'category': 'other', 'reason': 'Матч и выборы'},
     'Разные события: Матч и выборы'),
    ({'same_story': True, 'category': 'other', 'reason': 'Прогноз погоды'},
     'Категория other: Прогноз погоды'),
    ({'same_story': False, 'category': 'other'}, 'LLM сочла материалы разными событиями.'),
    ({'same_story': True, 'category': 'other', 'reason': None},
     'Категория other не принимается.'),
])
def test_rejection_reason_from_llm(monkeypatch, answer, expected):
    # Причина отклонения от модели — чтобы ошибки категорий были видны по базе.
    monkeypatch.setattr(llm, 'generate', lambda prompt: {'title_ru': 'Т', **answer})
    group = [{'id': 1, 'source': 'BBC News', 'title': 'a', 'summary': ''}]
    assert pipeline.summarize(group)['rejection_reason'] == expected


def test_prompt_asks_for_reason(monkeypatch):
    prompts = []
    monkeypatch.setattr(llm, 'generate', lambda prompt: prompts.append(prompt) or {})
    pipeline.summarize([{'id': 1, 'source': 'BBC News', 'title': 'a', 'summary': ''}])
    assert '"reason"' in prompts[0]


def test_prompt_lists_all_categories(monkeypatch):
    prompts = []
    monkeypatch.setattr(llm, 'generate', lambda prompt: prompts.append(prompt) or {})
    pipeline.summarize([{'id': 1, 'source': 'BBC News', 'title': 'a', 'summary': ''}])
    for slug in pipeline.categories.CATEGORIES:
        assert slug in prompts[0]


# ---------- Прогон main() целиком ----------

@pytest.fixture
def run_pipeline(conn, monkeypatch):
    """Готовит прогон: статьи групп кладутся в базу, сеть/модель/LLM — заглушки.

    groups: {имя группы: [источники]}. answers: {имя группы: ответ LLM или исключение}.
    Возвращает функцию запуска и список групп, по которым вызывалась LLM.
    """
    calls = []

    def prepare(groups, answers):
        for name, sources in groups.items():
            for source in sources:
                add_article(conn, source=source, title=f'{name}: {source}')

        monkeypatch.setattr(pipeline, 'collect_articles', lambda *args: None)
        monkeypatch.setattr(pipeline, 'load_feeds', lambda c: {'Test': 'https://example.com'})

        def fake_group(items):
            by_name = {}
            for item in items:
                by_name.setdefault(item['title'].split(':')[0], []).append(item)
            return list(by_name.values())
        monkeypatch.setattr(pipeline, 'group_articles', fake_group)

        def fake_generate(prompt):
            name = next(n for n in answers if f'"{n}: ' in prompt)
            calls.append(name)
            result = answers[name]
            if isinstance(result, Exception):
                raise result
            return {'title_ru': f'Сюжет {name}', 'summary_ru': '…', 'key_points_ru': [],
                    **result}
        monkeypatch.setattr(llm, 'generate', fake_generate)

    def run():
        pipeline.main()
        return conn.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 1').fetchone()

    return prepare, run, calls


FOUR = ['BBC News', 'DW', 'NPR', 'CGTN']


def test_main_success(conn, run_pipeline):
    prepare, run, calls = run_pipeline
    set_setting(conn, 'run_requested', True)
    prepare({'A': FOUR, 'B': FOUR, 'Small': ['BBC News', 'DW']},
            {'A': {'same_story': True, 'category': 'sport'},
             'B': {'same_story': False, 'category': 'other'},
             'Small': {'same_story': True, 'category': 'politics'}})

    last = run()

    assert last['status'] == 'success'
    assert last['stats'] == {'accepted': 1, 'rejected': 1, 'skipped_known': 0, 'errors': 0}
    assert sorted(calls) == ['A', 'B']               # группа из 2 источников до LLM не дошла
    story = conn.execute("SELECT * FROM stories WHERE accepted").fetchone()
    assert story['category'] == 'sport' and story['source_count'] == 4
    assert count(conn, 'story_articles') == 8        # обе группы связаны со статьями
    assert pipeline.run_requested(conn) is False     # флаг ручного запуска сброшен


def test_main_known_group_skipped_without_llm(conn, run_pipeline):
    prepare, run, calls = run_pipeline
    prepare({'A': FOUR}, {'A': {'same_story': True, 'category': 'politics'}})
    run()
    set_setting(conn, 'run_requested', True)
    second = run()
    assert calls == ['A']                            # второй прогон в LLM не ходил
    assert second['stats']['skipped_known'] == 1


def test_main_all_groups_failed_marks_failed_and_skips_cleanup(conn, run_pipeline):
    # Регресс 2–20 ноября: прогоны с ошибками по всем группам считались success.
    prepare, run, calls = run_pipeline
    old_story = add_story(conn, created_at=ago(days=25))
    prepare({'A': FOUR, 'B': FOUR},
            {'A': RuntimeError('OpenRouter HTTP 402'), 'B': RuntimeError('обрезан')})

    last = run()

    assert last['status'] == 'failed'
    assert last['stats']['errors'] == 2
    assert conn.execute('SELECT id FROM stories WHERE id = %s', (old_story,)).fetchone()


def test_main_partial_errors_still_success(conn, run_pipeline):
    prepare, run, _ = run_pipeline
    prepare({'A': FOUR, 'B': FOUR},
            {'A': {'same_story': True, 'category': 'politics'}, 'B': RuntimeError('обрезан')})
    last = run()
    assert last['status'] == 'success'
    assert last['stats']['errors'] == 1 and last['stats']['accepted'] == 1


def test_main_model_unavailable_fails_run(conn, run_pipeline):
    prepare, run, _ = run_pipeline
    prepare({'A': FOUR}, {'A': llm.ModelUnavailableError('404')})
    with pytest.raises(llm.ModelUnavailableError):
        pipeline.main()
    last = conn.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 1').fetchone()
    assert last['status'] == 'failed' and last['finished_at'] is not None


def test_main_success_runs_cleanup(conn, run_pipeline):
    prepare, run, _ = run_pipeline
    old_story = add_story(conn, created_at=ago(days=25))
    prepare({'A': FOUR}, {'A': {'same_story': True, 'category': 'politics'}})
    run()
    assert conn.execute('SELECT id FROM stories WHERE id = %s', (old_story,)).fetchone() is None


def test_main_exits_when_interval_not_elapsed(conn, run_pipeline):
    prepare, run, calls = run_pipeline
    add_run(conn, status='success', finished_at=ago(hours=1))
    prepare({'A': FOUR}, {'A': {'same_story': True, 'category': 'politics'}})
    pipeline.main()
    assert count(conn, 'runs') == 1                  # новый прогон не создан
    assert calls == []


def test_main_respects_min_sources_setting(conn, run_pipeline):
    prepare, run, calls = run_pipeline
    set_setting(conn, 'min_sources', 5)
    prepare({'A': FOUR}, {'A': {'same_story': True, 'category': 'politics'}})
    last = run()
    assert calls == []                               # 4 источника < 5
    assert last['status'] == 'success'
