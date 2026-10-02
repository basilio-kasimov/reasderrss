from __future__ import annotations
import calendar
import html
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
HTTPS = 'https://' 
FEEDS = {
    'BBC News': HTTPS + 'feeds.bbci.co.uk/news/rss.xml',
    'The Guardian': HTTPS + 'www.theguardian.com/world/rss',
    'The New York Times': HTTPS + 'rss.nytimes.com/services/xml/rss/nyt/World.xml',
    'Fox News': HTTPS + 'moxie.foxnews.com/google-publisher/latest.xml',
    'NBC News': HTTPS + 'feeds.nbcnews.com/nbcnews/public/news',
    'Sky News': HTTPS + 'feeds.skynews.com/feeds/rss/home.xml',
    'Tagesschau': HTTPS + 'www.tagesschau.de/xml/rss2/',
    'France 24': HTTPS + 'www.france24.com/en/rss',
'Al Jazeera English': 'https://www.aljazeera.com/xml/rss/all.xml',
    'Deutsche Welle': 'https://rss.dw.com/rdf/rss-en-all',
    'Euronews': 'https://www.euronews.com/rss?level=theme&name=news',
    'NPR News': 'https://feeds.npr.org/1001/rss.xml',
    'РБК': 'https://rssexport.rbc.ru/rbcnews/news/30/full.rss',
}
LOOKBACK_HOURS = 48
MIN_SOURCES = 2
RSS_TIMEOUT = 6
OLLAMA_TIMEOUT = 600
SIMILARITY_THRESHOLD = 0.60
MAX_ARTICLES = 200
OLLAMA_URL = 'http://' + 'localhost:11434/api/generate'
OLLAMA_MODEL = 'qwen2.5:7b'
EMBEDDING_MODEL = 'paraphrase-multilingual-MiniLM-L12-v2'
ARTICLES_FILE = 'articles.json'
SPORTS_FILE = 'filtered_sports.json'
ARTICLES_PROGRESS_FILE = 'articles_progress.json'
SPORTS_PROGRESS_FILE = 'filtered_sports_progress.json'
RUN_ID = datetime.now().strftime('%Y%m%d_%H%M')
HTML_FILE = f'important_news_{RUN_ID}.html'
HTML_PROGRESS_FILE = f'important_news_{RUN_ID}_progress.html'
HEADERS = {'User-Agent': 'NewsResearchAgent/0.1'}
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
def load(source: str, url: str, cutoff: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    feed = fetch(source, url)
    if feed is None:
        return [], []
    articles: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for entry in feed.entries:
        date = published(entry)
        if date is None or date < cutoff:
            continue
        title = clean(entry.get('title', 'Без заголовка')) or 'Без заголовка'
        summary = clean(entry.get('summary', ''))
        article = {
            'source': source,
            'title': title,
            'summary': summary,
            'link': entry.get('link', '').strip(),
            'published_at': date,
        }
        words = sports(title, summary)
        if words:
            removed.append({
                **article,
                'published_at': date.isoformat(),
                'matched_keywords': words,
            })
        else:
            articles.append(article)
    return articles, removed
def json_article(article: dict[str, Any]) -> dict[str, Any]:
    result = dict(article)
    if isinstance(result.get('published_at'), datetime):
        result['published_at'] = result['published_at'].isoformat()
    return result
def save_json(filename: str, data: Any) -> None:
    temporary = filename + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temporary, filename)
def analysis_items(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    articles = sorted(
        articles,
        key=lambda item: item['published_at'],
        reverse=True,
    )[:MAX_ARTICLES]
    return [
        {
            'source': item['source'],
            'title': item['title'],
            'summary': item['summary'][:1500],
            'link': item['link'],
            'published_at': item['published_at'],
        }
        for item in articles
    ]
def group_articles(articles: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    items = analysis_items(articles)
    if not items:
        return []
    log(f'Загрузка эмбеддингов: {EMBEDDING_MODEL}')
    started = time.monotonic()
    model = SentenceTransformer(EMBEDDING_MODEL)
    log(f'Модель загружена за {time.monotonic() - started:.1f} сек.')
    texts = [f'{item["title"]}. {item["summary"]}' for item in items]
    embeddings = model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    clustering = DBSCAN(
        eps=1.0 - SIMILARITY_THRESHOLD,
        min_samples=1,
        metric='cosine',
    )
    labels = clustering.fit_predict(np.asarray(embeddings))
    groups: dict[int, list[dict[str, Any]]] = {}
    for item, label in zip(items, labels):
        groups.setdefault(int(label), []).append(item)
    result = list(groups.values())
    log(f'Кластеризация завершена. Групп: {len(result)}')
    return result
def ask_ollama(prompt: str) -> dict[str, Any]:
    payload = {
        'model': OLLAMA_MODEL,
        'prompt': prompt,
        'stream': False,
        'format': 'json',
        'options': {'temperature': 0.1},
    }
    request = Request(
        OLLAMA_URL,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=OLLAMA_TIMEOUT) as response:
            result = json.loads(response.read().decode('utf-8'))
    except HTTPError as error:
        details = error.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Ollama HTTP {error.code}: {details}') from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f'Ошибка подключения к Ollama: {error}') from error
    text = result.get('response', '').strip()
    if not text:
        raise RuntimeError('Ollama вернул пустой ответ.')
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f'Ollama вернул невалидный JSON: {text[:500]}') from error
def summarize(group: list[dict[str, Any]]) -> dict[str, Any]:
    sources = sorted({item['source'] for item in group})
    materials = [
        {
            'source': item['source'],
            'title': item['title'],
            'summary': item['summary'],
        }
        for item in group
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
    log(f'Запрос Ollama: {len(sources)} источника, {len(group)} статей')
    result = ask_ollama(prompt)
    category = result.get('category', 'other')
    same_story = bool(result.get('same_story', False))
    accepted = same_story and category in {'politics', 'finance', 'science_tech'}
    news = {
        'title_ru': str(result.get('title_ru', 'Сюжет отклонён')),
        'summary_ru': str(result.get('summary_ru', '')),
        'key_points_ru': result.get('key_points_ru', []),
        'category': category,
        'sources': sources,
        'source_count': len(sources),
        'articles': [
            {
                'source': item['source'],
                'title': item['title'],
                'summary': item['summary'],
                'link': item['link'],
                'published_at': item['published_at'].isoformat(),
            }
            for item in group
        ],
    }
    if not accepted:
        news['rejection_reason'] = 'Ollama сочла материалы разными сюжетами или неподходящей категорией.'
    return {'accepted': accepted, 'news': news}
def links_html(articles: list[dict[str, Any]]) -> str:
    result = []
    for article in articles:
        source = html.escape(str(article['source']))
        title = html.escape(str(article['title']))
        link = html.escape(str(article.get('link', '')), quote=True)
        if link:
            result.append(
                f'<li><a href="{link}" target="_blank" rel="noopener">{source}</a></li>'
            )
        else:
            result.append(f'<li>{source}: {title}</li>')
    return ''.join(result)
def render_html(accepted: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> str:
    headings = {
        'politics': 'Политика',
        'finance': 'Финансы',
        'science_tech': 'Наука и технологии',
    }
    sections: list[str] = []
    for category, heading in headings.items():
        stories = []
        for item in accepted:
            if item['category'] != category:
                continue
            points = ''.join(
                f'<li>{html.escape(str(point))}</li>'
                for point in item.get('key_points_ru', [])
            )
            extra = f'''<details>
<summary>Дополнительная информация: {item['source_count']} источника</summary>
<p><strong>Источники:</strong> {html.escape(', '.join(item['sources']))}</p>
<ul>{points}</ul>
</details>'''
            stories.append(f'''<article class="story">
<h3>{html.escape(item['title_ru'])}</h3>
<div class="summary">{html.escape(item['summary_ru'])}</div>
<h4>Источники</h4>
<ul>{links_html(item['articles'])}</ul>
{extra}
</article>''')
        if stories:
            sections.append(f'<section><h2>{heading}</h2>{"".join(stories)}</section>')
    if rejected:
        stories = []
        for item in rejected:
            titles = ''.join(
                f'<li>{html.escape(article["source"])}: '
                f'{html.escape(article["title"])}</li>'
                for article in item['articles']
            )
            stories.append(f'''<article class="rejected">
<h3>{html.escape(item['title_ru'])}</h3>
<p>{html.escape(item['rejection_reason'])}</p>
<p><strong>Источники группы:</strong> {html.escape(', '.join(item['sources']))}</p>
<ul>{links_html(item['articles'])}</ul>
<details>
<summary>Исходные заголовки</summary>
<ul>{titles}</ul>
</details>
</article>''')
        sections.append(f'''<section class="rejected-section">
<h2>Отклонённые сюжеты</h2>
<p class="note">Эти группы были представлены минимум в трёх источниках, но Ollama не подтвердила их как один сюжет.</p>
{"".join(stories)}
</section>''')
    body = ''.join(sections) or '<p>Подходящих сюжетов не найдено.</p>'
    generated = datetime.now().strftime('%d.%m.%Y %H:%M')
    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Важные новости</title>
<style>
body {{ max-width: 900px; margin: auto; padding: 24px; background: #f3f0e9; color: #20242a; font: 16px Georgia, serif; line-height: 1.6; }}
h1 {{ font-size: 2.2rem; }}
h2 {{ margin-top: 36px; color: #8d321d; border-bottom: 2px solid #b54d2e; }}
.story, .rejected {{ margin: 20px 0; padding: 22px; background: #fffdf8; box-shadow: 0 2px 10px #0002; }}
.story {{ border-left: 5px solid #d4774d; }}
.rejected {{ border-left: 5px solid #999; color: #555; }}
.summary {{ white-space: pre-line; font-size: 1.08rem; color: #20242a; }}
a {{ color: #145c72; }}
details {{ margin-top: 16px; color: #666; font-size: .94rem; }}
summary {{ cursor: pointer; color: #8d321d; }}
.note {{ color: #6d6a63; }}
@media (max-width: 600px) {{ body {{ padding: 14px; }} }}
</style>
</head>
<body>
<h1>Важные новости</h1>
<p class="note">Сформировано: {generated}. Основные сюжеты представлены минимум в трёх источниках.</p>
{body}
</body>
</html>'''
def save_html(filename: str, accepted: list[dict[str, Any]], rejected: list[dict[str, Any]]) -> None:
    temporary = filename + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
        file.write(render_html(accepted, rejected))
    os.replace(temporary, filename)
def main() -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    articles: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    log(f'Период: {cutoff:%Y-%m-%d %H:%M} UTC - {now:%Y-%m-%d %H:%M} UTC')
    log(f'Проверяется RSS-лент: {len(FEEDS)}')
    for number, (source, url) in enumerate(FEEDS.items(), 1):
        log(f'[{number}/{len(FEEDS)}] Загрузка: {source}')
        new_articles, sports_articles = load(source, url, cutoff)
        articles.extend(new_articles)
        removed.extend(sports_articles)
        unique = {
            item['link'] or f'{item["source"]}:{item["title"]}': item
            for item in articles
        }
        articles = sorted(
            unique.values(),
            key=lambda item: item['published_at'],
            reverse=True,
        )
        save_json(ARTICLES_PROGRESS_FILE, [json_article(item) for item in articles])
        save_json(SPORTS_PROGRESS_FILE, removed)
        log(f'  Накоплено статей: {len(articles)}')
    save_json(ARTICLES_FILE, [json_article(item) for item in articles])
    save_json(SPORTS_FILE, removed)
    log(f'RSS-этап завершён. Уникальных публикаций: {len(articles)}')
    groups = group_articles(articles)
    eligible = [
        group for group in groups
        if len({item['source'] for item in group}) >= MIN_SOURCES
    ]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    save_html(HTML_PROGRESS_FILE, accepted, rejected)
    log(f'Групп из минимум трёх источников: {len(eligible)}')
    for number, group in enumerate(eligible, 1):
        log(f'[{number}/{len(eligible)}] Анализ через Ollama')
        try:
            result = summarize(group)
        except RuntimeError as error:
            log(f'[WARNING] Группа пропущена: {error}')
            continue
        if result['accepted']:
            accepted.append(result['news'])
        else:
            rejected.append(result['news'])
        save_html(HTML_PROGRESS_FILE, accepted, rejected)
    save_html(HTML_FILE, accepted, rejected)
    log(f'Итоговый HTML-файл: {HTML_FILE}')
    log(f'Принято сюжетов: {len(accepted)}')
    log(f'Отклонено Ollama: {len(rejected)}')
if __name__ == '__main__':
    main()