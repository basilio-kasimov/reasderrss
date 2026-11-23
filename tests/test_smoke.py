"""Шаг 1: инфраструктура тестов работает."""


def test_modules_import():
    # Импорт ловит ошибки вроде перекрытого имени модуля и синтаксис.
    from app import api, categories, db, llm, pipeline  # noqa: F401


def test_schema_creates_all_tables(conn):
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    tables = {row['table_name'] for row in rows}
    assert {'articles', 'stories', 'story_articles', 'feeds', 'runs', 'settings'} <= tables


def test_tables_start_empty(conn):
    assert conn.execute('SELECT count(*) AS n FROM stories').fetchone()['n'] == 0
