"""Шаг 3: app/api.py — публичные и админские эндпоинты на тестовой базе."""
import pytest
from fastapi.testclient import TestClient

from app import api, categories
from tests.factories import add_article, add_feed, add_run, add_story, ago, link, set_setting

ADMIN = {'Authorization': f'Bearer {api.ADMIN_TOKEN}'}


@pytest.fixture
def client(conn):
    # conn первым: схема создана и таблицы очищены до запуска приложения.
    with TestClient(api.app) as test_client:
        yield test_client


# ---------- Публичные ----------

def test_health(client):
    assert client.get('/health').json() == {'status': 'ok'}


def test_meta_on_empty_db(client):
    # Регресс 2026-11-22: /meta падал с 500 из-за перекрытого имени модуля.
    response = client.get('/api/v1/meta')
    assert response.status_code == 200
    body = response.json()
    assert body['categories'] == []
    assert body['last_run_at'] is None
    assert body['retention_days'] == 20
    assert body['category_titles'] == categories.TITLES
    assert body['category_titles']['incidents'] == 'Происшествия'


def test_meta_last_run_is_last_success(client, conn):
    add_run(conn, status='success', finished_at=ago(hours=5))
    add_run(conn, status='failed', finished_at=ago(hours=1))   # неудачный не в счёт
    body = client.get('/api/v1/meta').json()
    assert body['last_run_at'] is not None
    from datetime import datetime
    last = datetime.fromisoformat(body['last_run_at'])
    assert ago(hours=5, minutes=1) < last < ago(hours=4, minutes=59)


def test_meta_sources_count_only_accepted(client, conn):
    add_story(conn, sources=['BBC News', 'DW'])
    add_story(conn, sources=['BBC News'], accepted=False)
    sources = {s['name']: s['story_count'] for s in client.get('/api/v1/meta').json()['sources']}
    assert sources == {'BBC News': 1, 'DW': 1}


def test_meta_retention_from_settings(client, conn):
    set_setting(conn, 'retention_days', 7)
    assert client.get('/api/v1/meta').json()['retention_days'] == 7


def test_stories_only_accepted_newest_first(client, conn):
    first = add_story(conn, title='Первый')
    add_story(conn, title='Отклонённый', accepted=False)
    second = add_story(conn, title='Второй')
    body = client.get('/api/v1/stories').json()
    assert [s['id'] for s in body['items']] == [second, first]
    assert body['total'] == 2
    assert body['next_offset'] is None


def test_stories_since_id(client, conn):
    old = add_story(conn)
    new = add_story(conn)
    items = client.get(f'/api/v1/stories?since_id={old}').json()['items']
    assert [s['id'] for s in items] == [new]


def test_stories_filters(client, conn):
    add_story(conn, category='politics', sources=['A', 'B', 'C', 'D'])
    sport = add_story(conn, category='sport', sources=['A', 'B', 'C', 'D', 'E'])
    add_story(conn, category='sport', sources=['A', 'B'])
    items = client.get('/api/v1/stories?categories=sport&min_sources=5').json()['items']
    assert [s['id'] for s in items] == [sport]


def test_stories_exclude_sources(client, conn):
    add_story(conn, sources=['Fox News', 'BBC News'])
    kept = add_story(conn, sources=['DW', 'NPR'])
    items = client.get('/api/v1/stories?exclude_sources=Fox News').json()['items']
    assert [s['id'] for s in items] == [kept]


def test_stories_pagination(client, conn):
    for _ in range(5):
        add_story(conn)
    page = client.get('/api/v1/stories?limit=2&offset=0').json()
    assert len(page['items']) == 2 and page['total'] == 5 and page['next_offset'] == 2
    last = client.get('/api/v1/stories?limit=2&offset=4').json()
    assert len(last['items']) == 1 and last['next_offset'] is None


def test_stories_limit_is_capped(client):
    assert client.get(f'/api/v1/stories?limit={api.MAX_LIMIT + 1}').status_code == 422


def test_story_detail_with_articles(client, conn):
    story = add_story(conn, title='С источниками')
    link(conn, story, add_article(conn, source='BBC News', title='Статья BBC'))
    body = client.get(f'/api/v1/stories/{story}').json()
    assert body['title_ru'] == 'С источниками'
    assert [a['title'] for a in body['articles']] == ['Статья BBC']


def test_story_not_found(client):
    assert client.get('/api/v1/stories/999999').status_code == 404


# ---------- Админские: доступ ----------

@pytest.mark.parametrize('headers', [
    {},
    {'Authorization': 'Bearer wrong-token'},
    {'Authorization': f'Basic {api.ADMIN_TOKEN}'},
])
def test_admin_requires_token(client, headers):
    assert client.get('/api/v1/admin/settings', headers=headers).status_code == 401


def test_admin_disabled_without_server_token(client, monkeypatch):
    monkeypatch.setattr(api, 'ADMIN_TOKEN', '')
    assert client.get('/api/v1/admin/settings', headers=ADMIN).status_code == 503


# ---------- Админские: параметры ----------

def test_get_settings_returns_defaults(client):
    body = client.get('/api/v1/admin/settings', headers=ADMIN).json()
    assert body['retention_days'] == 20
    assert body['min_sources'] == 4
    assert body['pipeline_interval_hours'] == 6


def test_put_settings_saves_and_returns(client):
    body = client.put('/api/v1/admin/settings', headers=ADMIN,
                      json={'min_sources': 5, 'retention_days': 10}).json()
    assert body['min_sources'] == 5 and body['retention_days'] == 10
    again = client.get('/api/v1/admin/settings', headers=ADMIN).json()
    assert again['min_sources'] == 5 and again['retention_days'] == 10


@pytest.mark.parametrize('bad', [2, 0, -5, 5.5, '10', None, True])
def test_put_settings_rejects_bad_retention(client, bad):
    response = client.put('/api/v1/admin/settings', headers=ADMIN,
                          json={'retention_days': bad, 'min_sources': 7})
    assert response.status_code == 422
    assert 'не меньше 3' in response.json()['detail']
    # Ничего не сохранилось, в том числе соседнее поле из того же запроса.
    assert client.get('/api/v1/admin/settings', headers=ADMIN).json()['min_sources'] == 4


def test_put_settings_minimum_retention_accepted(client):
    response = client.put('/api/v1/admin/settings', headers=ADMIN, json={'retention_days': 3})
    assert response.status_code == 200


# ---------- Админские: ленты ----------

def test_feeds_create_list_patch_delete(client):
    created = client.post('/api/v1/admin/feeds', headers=ADMIN,
                          json={'name': 'Test', 'url': 'https://example.com/rss'})
    assert created.status_code == 201
    feed_id = created.json()['id']

    assert [f['name'] for f in client.get('/api/v1/admin/feeds', headers=ADMIN).json()] == ['Test']

    patched = client.patch(f'/api/v1/admin/feeds/{feed_id}', headers=ADMIN,
                           json={'enabled': False})
    assert patched.json()['enabled'] is False

    assert client.delete(f'/api/v1/admin/feeds/{feed_id}', headers=ADMIN).status_code == 204
    assert client.get('/api/v1/admin/feeds', headers=ADMIN).json() == []


def test_feed_duplicate_name_is_409(client, conn):
    add_feed(conn, name='BBC News')
    response = client.post('/api/v1/admin/feeds', headers=ADMIN,
                           json={'name': 'BBC News', 'url': 'https://other.example/rss'})
    assert response.status_code == 409


def test_feed_patch_and_delete_missing_is_404(client):
    assert client.patch('/api/v1/admin/feeds/999', headers=ADMIN,
                        json={'enabled': True}).status_code == 404
    assert client.delete('/api/v1/admin/feeds/999', headers=ADMIN).status_code == 404


def test_feed_patch_without_fields_is_400(client, conn):
    feed_id = add_feed(conn)
    assert client.patch(f'/api/v1/admin/feeds/{feed_id}', headers=ADMIN, json={}).status_code == 400


def test_feed_delete_keeps_articles(client, conn):
    feed_id = add_feed(conn, name='BBC News')
    add_article(conn, source='BBC News')
    client.delete(f'/api/v1/admin/feeds/{feed_id}', headers=ADMIN)
    assert conn.execute('SELECT count(*) AS n FROM articles').fetchone()['n'] == 1


# ---------- Админские: прогоны ----------

def test_request_run_sets_flag(client, conn):
    response = client.post('/api/v1/admin/runs', headers=ADMIN)
    assert response.status_code == 202
    row = conn.execute("SELECT value FROM settings WHERE key = 'run_requested'").fetchone()
    assert row['value'] is True


def test_runs_newest_first(client, conn):
    older = add_run(conn, finished_at=ago(hours=6))
    newer = add_run(conn, status='failed', finished_at=ago(hours=1))
    runs = client.get('/api/v1/admin/runs', headers=ADMIN).json()
    assert [r['id'] for r in runs] == [newer, older]
    assert runs[0]['status'] == 'failed'
