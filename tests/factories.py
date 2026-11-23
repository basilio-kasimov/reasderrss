"""Заполнение тестовой базы: сюжеты, статьи, прогоны, ленты.

Время задаётся явно («N часов назад»), чтобы проверять расписание и очистку
без реального ожидания.
"""
import itertools
import json
from datetime import datetime, timedelta, timezone

_counter = itertools.count(1)


def ago(hours=0, minutes=0, days=0):
    return datetime.now(timezone.utc) - timedelta(hours=hours, minutes=minutes, days=days)


def add_story(conn, *, title='Сюжет', category='politics', accepted=True,
              sources=('BBC News', 'CNN', 'DW', 'NPR'), created_at=None):
    sources = list(sources)
    row = conn.execute(
        '''INSERT INTO stories (group_hash, title_ru, summary_ru, key_points_ru, category,
                                accepted, sources, source_count, created_at)
           VALUES (%s, %s, %s, '[]', %s, %s, %s, %s, coalesce(%s, now()))
           RETURNING id''',
        (f'hash-{next(_counter)}', title, f'Текст: {title}', category, accepted,
         json.dumps(sources, ensure_ascii=False), len(sources), created_at),
    ).fetchone()
    conn.commit()
    return row['id']


def add_article(conn, *, source='BBC News', title=None, published_at=None, created_at=None):
    n = next(_counter)
    row = conn.execute(
        '''INSERT INTO articles (dedup_key, source, title, summary, link, published_at, created_at)
           VALUES (%s, %s, %s, '', '', coalesce(%s, now()), coalesce(%s, now()))
           RETURNING id''',
        (f'key-{n}', source, title or f'Статья {n}', published_at, created_at),
    ).fetchone()
    conn.commit()
    return row['id']


def link(conn, story_id, article_id):
    conn.execute('INSERT INTO story_articles (story_id, article_id) VALUES (%s, %s)',
                 (story_id, article_id))
    conn.commit()


def add_run(conn, *, status='success', finished_at=None, started_at=None, stats=None):
    finished_at = finished_at or ago()
    row = conn.execute(
        '''INSERT INTO runs (started_at, finished_at, status, stats)
           VALUES (%s, %s, %s, %s) RETURNING id''',
        (started_at or finished_at - timedelta(minutes=2), finished_at, status,
         json.dumps(stats or {})),
    ).fetchone()
    conn.commit()
    return row['id']


def set_setting(conn, key, value):
    conn.execute(
        '''INSERT INTO settings (key, value) VALUES (%s, %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value''',
        (key, json.dumps(value)),
    )
    conn.commit()


def add_feed(conn, *, name='Test Feed', url='https://example.com/rss', enabled=True):
    row = conn.execute(
        'INSERT INTO feeds (name, url, enabled) VALUES (%s, %s, %s) RETURNING id',
        (name, url, enabled),
    ).fetchone()
    conn.commit()
    return row['id']
