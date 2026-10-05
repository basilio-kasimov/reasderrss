import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from app import db

ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '')
MAX_LIMIT = 100

STORY_FIELDS = '''id, title_ru, summary_ru, key_points_ru, category,
                  sources, source_count, accepted, created_at'''


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect()
    db.init_schema(conn)
    conn.close()
    yield


app = FastAPI(title='News API', version='1.0', lifespan=lifespan)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def require_admin(authorization: str = Header(default='')) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(503, 'ADMIN_TOKEN не задан на сервере.')
    scheme, _, token = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not secrets.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, 'Неверный или отсутствующий админ-токен.')


def serialize(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


# ---------- Публичные эндпоинты ----------

@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/api/v1/stories')
def list_stories(
    conn: psycopg.Connection = Depends(get_conn),
    categories: str | None = Query(default=None),
    min_sources: int = Query(default=1, ge=1),
    exclude_sources: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    since_id: int | None = Query(default=None),
    accepted: bool = Query(default=True),
    limit: int = Query(default=20, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
):
    where = ['accepted = %s']
    params: list[Any] = [accepted]
    if categories:
        where.append('category = ANY(%s)')
        params.append([c.strip() for c in categories.split(',') if c.strip()])
    if min_sources > 1:
        where.append('source_count >= %s')
        params.append(min_sources)
    if exclude_sources:
        where.append('NOT (sources ?| %s)')
        params.append([s.strip() for s in exclude_sources.split(',') if s.strip()])
    if since is not None:
        where.append('created_at > %s')
        params.append(since)
    if since_id is not None:
        where.append('id > %s')
        params.append(since_id)
    condition = ' AND '.join(where)

    total = conn.execute(
        f'SELECT count(*) AS n FROM stories WHERE {condition}', params,
    ).fetchone()['n']
    rows = conn.execute(
        f'''SELECT {STORY_FIELDS} FROM stories
            WHERE {condition}
            ORDER BY id DESC
            LIMIT %s OFFSET %s''',
        params + [limit, offset],
    ).fetchall()
    next_offset = offset + limit if offset + limit < total else None
    return {
        'items': [serialize(row) for row in rows],
        'total': total,
        'next_offset': next_offset,
    }


@app.get('/api/v1/stories/{story_id}')
def get_story(story_id: int, conn: psycopg.Connection = Depends(get_conn)):
    story = conn.execute(
        f'SELECT {STORY_FIELDS}, rejection_reason FROM stories WHERE id = %s',
        (story_id,),
    ).fetchone()
    if story is None:
        raise HTTPException(404, 'Сюжет не найден.')
    articles = conn.execute(
        '''SELECT a.source, a.title, a.summary, a.link, a.published_at
           FROM articles a
           JOIN story_articles sa ON sa.article_id = a.id
           WHERE sa.story_id = %s
           ORDER BY a.published_at DESC''',
        (story_id,),
    ).fetchall()
    result = serialize(story)
    result['articles'] = [serialize(row) for row in articles]
    return result


@app.get('/api/v1/meta')
def meta(conn: psycopg.Connection = Depends(get_conn)):
    categories = [
        row['category'] for row in conn.execute(
            'SELECT DISTINCT category FROM stories ORDER BY category',
        ).fetchall()
    ]
    sources = conn.execute(
        '''SELECT s.name, count(*) AS story_count
           FROM stories, jsonb_array_elements_text(sources) AS s(name)
           WHERE accepted
           GROUP BY s.name
           ORDER BY story_count DESC''',
    ).fetchall()
    last_run = conn.execute(
        "SELECT finished_at FROM runs WHERE status = 'success' "
        'ORDER BY id DESC LIMIT 1',
    ).fetchone()
    return {
        'categories': categories,
        'sources': [serialize(row) for row in sources],
        'last_run_at': serialize(last_run)['finished_at'] if last_run else None,
    }


# ---------- Админ-эндпоинты ----------

class FeedCreate(BaseModel):
    name: str
    url: str
    enabled: bool = True


class FeedPatch(BaseModel):
    name: str | None = None
    url: str | None = None
    enabled: bool | None = None


@app.get('/api/v1/admin/settings', dependencies=[Depends(require_admin)])
def get_settings(conn: psycopg.Connection = Depends(get_conn)):
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    return {row['key']: row['value'] for row in rows}


@app.put('/api/v1/admin/settings', dependencies=[Depends(require_admin)])
def put_settings(
    values: dict[str, Any],
    conn: psycopg.Connection = Depends(get_conn),
):
    for key, value in values.items():
        conn.execute(
            '''INSERT INTO settings (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = now()''',
            (key, json.dumps(value, ensure_ascii=False)),
        )
    conn.commit()
    return get_settings(conn)


@app.get('/api/v1/admin/feeds', dependencies=[Depends(require_admin)])
def list_feeds(conn: psycopg.Connection = Depends(get_conn)):
    rows = conn.execute('SELECT * FROM feeds ORDER BY id').fetchall()
    return [serialize(row) for row in rows]


@app.post('/api/v1/admin/feeds', status_code=201,
          dependencies=[Depends(require_admin)])
def create_feed(feed: FeedCreate, conn: psycopg.Connection = Depends(get_conn)):
    try:
        row = conn.execute(
            '''INSERT INTO feeds (name, url, enabled)
               VALUES (%s, %s, %s) RETURNING *''',
            (feed.name, feed.url, feed.enabled),
        ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, 'Лента с таким именем уже существует.')
    conn.commit()
    return serialize(row)


@app.patch('/api/v1/admin/feeds/{feed_id}', dependencies=[Depends(require_admin)])
def patch_feed(
    feed_id: int,
    patch: FeedPatch,
    conn: psycopg.Connection = Depends(get_conn),
):
    changes = patch.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, 'Нет полей для изменения.')
    assignments = ', '.join(f'{key} = %s' for key in changes)
    row = conn.execute(
        f'UPDATE feeds SET {assignments} WHERE id = %s RETURNING *',
        list(changes.values()) + [feed_id],
    ).fetchone()
    if row is None:
        raise HTTPException(404, 'Лента не найдена.')
    conn.commit()
    return serialize(row)


@app.delete('/api/v1/admin/feeds/{feed_id}', status_code=204,
            dependencies=[Depends(require_admin)])
def delete_feed(feed_id: int, conn: psycopg.Connection = Depends(get_conn)):
    cursor = conn.execute('DELETE FROM feeds WHERE id = %s', (feed_id,))
    if cursor.rowcount == 0:
        raise HTTPException(404, 'Лента не найдена.')
    conn.commit()


@app.get('/api/v1/admin/runs', dependencies=[Depends(require_admin)])
def list_runs(
    conn: psycopg.Connection = Depends(get_conn),
    limit: int = Query(default=20, ge=1, le=MAX_LIMIT),
):
    rows = conn.execute(
        'SELECT * FROM runs ORDER BY id DESC LIMIT %s', (limit,),
    ).fetchall()
    return [serialize(row) for row in rows]


@app.post('/api/v1/admin/runs', status_code=202,
          dependencies=[Depends(require_admin)])
def request_run(conn: psycopg.Connection = Depends(get_conn)):
    conn.execute(
        '''INSERT INTO settings (key, value) VALUES ('run_requested', 'true')
           ON CONFLICT (key) DO UPDATE
           SET value = 'true', updated_at = now()''',
    )
    conn.commit()
    return {'status': 'scheduled',
            'detail': 'Запуск будет подхвачен ближайшим тиком таймера.'}


# Зарезервировано: параметр q= для серверного поиска добавим при необходимости.