"""Общие фикстуры тестов.

Тесты запускаются через scripts/run_tests.sh: DATABASE_URL указывает на
временный PostgreSQL, OPENROUTER_API_KEY — фиктивный (настоящих запросов к LLM
тесты не делают).
"""
import os

import pytest

from app import db as app_db

# Защита от запуска на прод-базе: временная база называется test.
assert os.environ['DATABASE_URL'].rsplit('/', 1)[-1] == 'test', (
    'Тесты запускаются только на временной базе — через scripts/run_tests.sh'
)

TABLES = ('story_articles', 'stories', 'articles', 'feeds', 'runs', 'settings')


@pytest.fixture
def conn():
    """Соединение с тестовой базой: схема создана, таблицы пустые."""
    connection = app_db.connect()
    app_db.init_schema(connection)
    connection.execute(f'TRUNCATE {", ".join(TABLES)} RESTART IDENTITY CASCADE')
    connection.commit()
    yield connection
    connection.close()
