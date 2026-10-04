import os

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ['DATABASE_URL']

SCHEMA = '''
CREATE TABLE IF NOT EXISTS articles (
    id           SERIAL PRIMARY KEY,
    dedup_key    TEXT UNIQUE NOT NULL,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    link         TEXT NOT NULL DEFAULT '',
    published_at TIMESTAMPTZ NOT NULL,
    is_sport     BOOLEAN NOT NULL DEFAULT FALSE,
    sport_words  JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published_at DESC);

CREATE TABLE IF NOT EXISTS stories (
    id               SERIAL PRIMARY KEY,
    group_hash       TEXT UNIQUE NOT NULL,
    title_ru         TEXT NOT NULL,
    summary_ru       TEXT NOT NULL DEFAULT '',
    key_points_ru    JSONB NOT NULL DEFAULT '[]',
    category         TEXT NOT NULL DEFAULT 'other',
    accepted         BOOLEAN NOT NULL,
    rejection_reason TEXT,
    sources          JSONB NOT NULL DEFAULT '[]',
    source_count     INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stories_created ON stories (created_at DESC);

CREATE TABLE IF NOT EXISTS story_articles (
    story_id   INTEGER NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
    PRIMARY KEY (story_id, article_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id          SERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',
    stats       JSONB NOT NULL DEFAULT '{}'
);
'''


def connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA)
    conn.commit()