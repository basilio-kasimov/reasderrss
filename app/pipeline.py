from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import feedparser
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.cluster import DBSCAN

from app import db, llm

# Используется только для разового засева пустой таблицы feeds.
# После первого запуска источник правды — таблица, не этот словарь.
SEED_FEEDS = {
    'BBC News': 'https://feeds.bbci.co.uk/news/rss.xml',
    'The Guardian': 'https://www.theguardian.com/world/rss',
    'The New York Times': 'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',
    'Fox News': 'https://moxie.foxnews.com/google-publisher/latest.xml',
    'NBC News': 'https://feeds.nbcnews.com/nbcnews/public/news',
    'Sky News': 'https://feeds.skynews.com/feeds/rss/home.xml',
    'Tagesschau': 'https://www.tagesschau.de/xml/rss2/',
    'France 24': 'https://www.france24.com/en/rss',
    'Al Jazeera English': 'https://www.aljazeera.com/xml/rss/all.xml',
    'Deutsche Welle': 'https://rss.dw.com/rdf/rss-en-all',
    'Euronews': 'https://www.euronews.com/rss?level=theme&name=news',
    'NPR News': 'https://feeds.npr.org/1001/rss.xml',
    'РБК': 'https://rssexport.rbc.ru/rbcnews/news/30/full.rss',
}

DEFAULT_SETTINGS = {
    'pipeline_interval_hours': 2,
    'lookback_hours': 48,
    'min_sources': 2,
}

RSS_TIMEOUT = 6
SIMILARITY_THRESHOLD = 0.60
MAX_ARTICLES = 200
EMBEDDING_MODEL = 'paraphrase-multilingual-MiniLM-L12-v2'
HEADERS = {'User-Agent': 'NewsResearchAgent/0.2'}
SPORT_WORDS = (
    'sport', 'sports', 'football', 'soccer', 'basketball', 'baseball',
    'hockey', 'tennis', 'golf', 'cricket', 'rugby', 'boxing', 'mma',
    'olympic', 'championship', 'league', 'match', 'tournament', 'athlete',
    'nba', 'nfl', 'nhl', 'mlb', 'wimbledon', 'super bowl', 'world cup',
    'fußball', 'fussball', 'bundesliga', 'calcio', 'fútbol', 'futbol',
    'спорт', 'футбол', 'баскетбол', 'хоккей', 'теннис', 'бокс',
    'олимпиад', 'чемпионат', 'матч', 'турнир', 'спортсмен', 'формула-1',
)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {'script', 'style'}:
            self.ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {'script', 'style'} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)

    def text(self) -> str:
        return ' '.join(self.parts)


def log(message: str) -> None:
    print(message, flush=True)


# ---------- Настройки и ленты из БД ----------

def load_settings(conn: Any) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    rows = conn.execute('SELECT key, value FROM settings').fetchall()
    for row in rows:
        settings[row['key']] = row['value']
    return settings


def load_feeds(conn: Any) -> dict[str, str]:
    rows = conn.execute(
        'SELECT name, url FROM feeds WHERE enabled ORDER BY id',
    ).fetchall()
    if rows:
        return {row['name']: row['url'] for row in rows}
    log('Таблица feeds пуста — засеваю стартовым списком.')
    for name, url in SEED_FEEDS.items():
        conn.execute(
            '''INSERT INTO feeds (name, url) VALUES (%s, %s)
               ON CONFLICT (name) DO NOTHING''',
            (name, url),
        )
    conn.commit()
    return dict(SEED_FEEDS)


def run_requested(conn: Any) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'run_requested'",
    ).fetchone()
    return bool(row and row['value'] is True)


def clear_run_request(conn: Any) -> None:
    conn.execute(
        "UPDATE settings SET value = 'false', updated_at = now() "
        "WHERE key = 'run_requested'",
    )
    conn.commit()


def should_run(conn: Any, interval_hours: float) -> bool:
    if run_requested(conn):
        log('Обнаружен ручной запрос запуска (run_requested).')
        return True
    row = conn.execute(
        "SELECT finished_at FROM runs WHERE status = 'success' "
        'ORDER BY id DESC LIMIT 1',
    ).fetchone()
    if row is None or row['finished_at'] is None:
        return True
    elapsed = datetime.now(timezone.utc) - row['finished_at']
    if elapsed >= timedelta(hours=float(interval_hours)):
        return True
    log(f'С последнего запуска прошло {elapsed}, интервал '
        f'{interval_hours} ч. не истёк — выход.')
    return False


# ---------- Сбор статей ----------

def clean(value: str | None) -> str:
    if not value:
        return ''
    parser = TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        value = parser.text()
    except Exception:
        value = re.sub(r'<[^>]+>', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def sports(title: str, summary: str) -> list[str]:
    text = f'{title} {summary}'.casefold()
    return [word for word in SPORT_WORDS if word.casefold() in text]


def published(entry: Any) -> datetime | None:
    parsed = entry.get('published_parsed') or entry.get('updated_parsed')
    if parsed is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def fetch(source: str, url: str) -> Any | None:
    try:
        request = Request(url, headers=HEADERS, method='GET')
        with urlopen(request, timeout=RSS_TIMEOUT) as response:
            feed = feedparser.parse(response.read())
    except HTTPError as error:
        log(f'[ERROR] {source}: HTTP {error.code}')
        return None
    except (URLError, TimeoutError, OSError) as error:
        log(f'[WARNING] {source}: {error}')
        return None
    if feed.bozo:
        log(f'[WARNING] {source}: {feed.bozo_exception}')
    return feed


def dedup_key(source: str, title: str, link: str) -> str:
    return link or f'{source}:{title}'


def collect_articles(conn: Any, feeds: dict[str, str], cutoff: datetime) -> None:
    for number, (source, url) in enumerate(feeds.items(), 1):
        log(f'[{number}/{len(feeds)}] Загрузка: {source}')
        feed = fetch(source, url)
        if feed is None:
            continue
        inserted = 0
        for entry in feed.entries:
            date = published(entry)
            if date is None or date < cutoff:
                continue
            title = clean(entry.get('title', 'Без заголовка')) or 'Без заголовка'
            summary = clean(entry.get('summary', ''))
            link = entry.get('link', '').strip()
            words = sports(title, summary)
            cursor = conn.execute(
                '''INSERT INTO articles
                   (dedup_key, source, title, summary, link,
                    published_at, is_sport, sport_words)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (dedup_key) DO NOTHING''',
                (
                    dedup_key(source, title, link), source, title, summary,
                    link, date, bool(words), json.dumps(words, ensure_ascii=False),
                ),
            )
            inserted += cursor.rowcount
        conn.commit()
        log(f'  Новых статей: {inserted}')


def recent_articles(conn: Any, cutoff: datetime) -> list[dict[str, Any]]:
    cursor = conn.execute(
        '''SELECT id, source, title, summary, link, published_at
           FROM articles
           WHERE published_at >= %s AND NOT is_sport
           ORDER BY published_at DESC
           LIMIT %s''',
        (cutoff, MAX_ARTICLES),
    )
    rows = cursor.fetchall()
    for row in rows:
        row['summary'] = row['summary'][:1500]
    return rows


# ---------- Кластеризация и суммаризация ----------

def group_articles(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not items:
        return []
    log(f'Загрузка эмбеддингов: {EMBEDDING_MODEL}')
    started = time.monotonic()
    model = SentenceTransformer(EMBEDDING_MODEL)
    log(f'Модель загружена за {time.monotonic() - started:.1f} сек.')
    texts = [f'{item["title"]}. {item["summary"]}' for item in items]
    embeddings = model.encode(texts, normalize_embeddings=True)
    clustering = DBSCAN(
        eps=1.0 - SIMILARITY_THRESHOLD, min_samples=1, metric='cosine',
    )
    labels = clustering.fit_predict(np.asarray(embeddings))
    groups: dict[int, list[dict[str, Any]]] = {}
    for item, label in zip(items, labels):
        groups.setdefault(int(label), []).append(item)
    result = list(groups.values())
    log(f'Кластеризация завершена. Групп: {len(result)}')
    return result


def group_hash(group: list[dict[str, Any]]) -> str:
    keys = sorted(dedup_key(i['source'], i['title'], i['link']) for i in group)
    return hashlib.sha256('\n'.join(keys).encode('utf-8')).hexdigest()


def story_exists(conn: Any, hash_value: str) -> bool:
    cursor = conn.execute(
        'SELECT 1 FROM stories WHERE group_hash = %s', (hash_value,),
    )
    return cursor.fetchone() is not None


def summarize(group: list[dict[str, Any]]) -> dict[str, Any]:
    sources = sorted({item['source'] for item in group})
    materials = [
        {'source': i['source'], 'title': i['title'], 'summary': i['summary']}
        for i in group
    ]
    prompt = f'''Ты проверяешь группу новостных публикаций.
Определи, описывают ли все материалы одно событие или один сюжет.
Используй только переданные материалы и не придумывай факты.
Верни только JSON:
{{
  "same_story": true,
  "title_ru": "Краткий заголовок на русском",
  "summary_ru": "Связная русская статья в 2-4 абзацах",
  "key_points_ru": ["Дополнительный факт 1"],
  "category": "politics"
}}
category может быть politics, finance, science_tech или other.
Если материалы относятся к разным событиям или к спорту, верни same_story=false и category=other.
Источники: {json.dumps(sources, ensure_ascii=False)}
Материалы: {json.dumps(materials, ensure_ascii=False)}'''
    log(f'Запрос LLM: {len(sources)} источников, {len(group)} статей')
    result = llm.generate(prompt)
    category = str(result.get('category', 'other'))
    same_story = bool(result.get('same_story', False))
    accepted = same_story and category in {'politics', 'finance', 'science_tech'}
    return {
        'accepted': accepted,
        'title_ru': str(result.get('title_ru', 'Сюжет отклонён')),
        'summary_ru': str(result.get('summary_ru', '')),
        'key_points_ru': result.get('key_points_ru', []),
        'category': category,
        'sources': sources,
        'rejection_reason': None if accepted else
            'LLM сочла материалы разными сюжетами или неподходящей категорией.',
    }


def save_story(conn: Any, hash_value: str,
               story: dict[str, Any], group: list[dict[str, Any]]) -> None:
    cursor = conn.execute(
        '''INSERT INTO stories
           (group_hash, title_ru, summary_ru, key_points_ru, category,
            accepted, rejection_reason, sources, source_count)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id''',
        (
            hash_value, story['title_ru'], story['summary_ru'],
            json.dumps(story['key_points_ru'], ensure_ascii=False),
            story['category'], story['accepted'], story['rejection_reason'],
            json.dumps(story['sources'], ensure_ascii=False),
            len(story['sources']),
        ),
    )
    story_id = cursor.fetchone()['id']
    for item in group:
        conn.execute(
            '''INSERT INTO story_articles (story_id, article_id)
               VALUES (%s, %s) ON CONFLICT DO NOTHING''',
            (story_id, item['id']),
        )
    conn.commit()


# ---------- Главный цикл ----------

def main() -> None:
    conn = db.connect()
    db.init_schema(conn)
    settings = load_settings(conn)
    if not should_run(conn, settings['pipeline_interval_hours']):
        conn.close()
        return
    clear_run_request(conn)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=float(settings['lookback_hours']))
    min_sources = int(settings['min_sources'])
    cursor = conn.execute(
        "INSERT INTO runs (status) VALUES ('running') RETURNING id",
    )
    run_id = cursor.fetchone()['id']
    conn.commit()
    stats = {'accepted': 0, 'rejected': 0, 'skipped_known': 0, 'errors': 0}
    try:
        log(f'Период: {cutoff:%Y-%m-%d %H:%M} UTC - {now:%Y-%m-%d %H:%M} UTC')
        feeds = load_feeds(conn)
        log(f'Активных лент: {len(feeds)}')
        collect_articles(conn, feeds, cutoff)
        items = recent_articles(conn, cutoff)
        log(f'Статей для анализа: {len(items)}')
        groups = group_articles(items)
        eligible = [
            g for g in groups
            if len({i['source'] for i in g}) >= min_sources
        ]
        log(f'Групп из минимум {min_sources} источников: {len(eligible)}')
        for number, group in enumerate(eligible, 1):
            hash_value = group_hash(group)
            if story_exists(conn, hash_value):
                stats['skipped_known'] += 1
                log(f'[{number}/{len(eligible)}] Уже обработана, пропуск')
                continue
            log(f'[{number}/{len(eligible)}] Анализ через LLM')
            try:
                story = summarize(group)
            except RuntimeError as error:
                stats['errors'] += 1
                log(f'[WARNING] Группа пропущена: {error}')
                continue
            save_story(conn, hash_value, story, group)
            stats['accepted' if story['accepted'] else 'rejected'] += 1
        status = 'success'
    except Exception as error:
        status = 'failed'
        log(f'[ERROR] Пайплайн упал: {error}')
        raise
    finally:
        conn.execute(
            '''UPDATE runs SET finished_at = now(), status = %s, stats = %s
               WHERE id = %s''',
            (status, json.dumps(stats, ensure_ascii=False), run_id),
        )
        conn.commit()
        conn.close()
    log(f'Готово. Принято: {stats["accepted"]}, отклонено: {stats["rejected"]}, '
        f'пропущено известных: {stats["skipped_known"]}, ошибок: {stats["errors"]}')


if __name__ == '__main__':
    main()