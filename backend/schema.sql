CREATE TABLE IF NOT EXISTS authors (
  user_id TEXT PRIMARY KEY,
  handle TEXT NOT NULL,
  display_name TEXT,
  bio TEXT,
  verified INTEGER,
  follower_count INTEGER,
  following_count INTEGER,
  first_seen_at TEXT,
  last_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_authors_handle ON authors(handle);

CREATE TABLE IF NOT EXISTS tweets (
  tweet_id TEXT PRIMARY KEY,
  author_id TEXT REFERENCES authors(user_id),
  text TEXT,
  created_at TEXT,
  captured_at TEXT,
  last_updated_at TEXT,
  lang TEXT,
  conversation_id TEXT,
  reply_to_tweet_id TEXT,
  reply_to_user_id TEXT,
  quoted_tweet_id TEXT,
  retweeted_tweet_id TEXT,
  media_json TEXT,
  is_my_tweet INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tweets_author ON tweets(author_id);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets(created_at);
CREATE INDEX IF NOT EXISTS idx_tweets_reply ON tweets(reply_to_tweet_id);
CREATE INDEX IF NOT EXISTS idx_tweets_conversation ON tweets(conversation_id);

CREATE TABLE IF NOT EXISTS engagement_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_id TEXT REFERENCES tweets(tweet_id),
  captured_at TEXT,
  likes INTEGER,
  retweets INTEGER,
  replies INTEGER,
  quotes INTEGER,
  views INTEGER,
  bookmarks INTEGER
);
CREATE INDEX IF NOT EXISTS idx_engagement_tweet ON engagement_snapshots(tweet_id, captured_at);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  started_at TEXT,
  ended_at TEXT,
  total_dwell_ms INTEGER,
  tweet_count INTEGER,
  feeds_visited TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(started_at);

CREATE TABLE IF NOT EXISTS impressions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_id TEXT REFERENCES tweets(tweet_id),
  session_id TEXT REFERENCES sessions(session_id),
  first_seen_at TEXT,
  dwell_ms INTEGER,
  feed_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_impressions_time ON impressions(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_impressions_tweet ON impressions(tweet_id);
CREATE INDEX IF NOT EXISTS idx_impressions_session ON impressions(session_id);

CREATE TABLE IF NOT EXISTS my_interactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_id TEXT REFERENCES tweets(tweet_id),
  action TEXT,
  timestamp TEXT
);
CREATE INDEX IF NOT EXISTS idx_interactions_time ON my_interactions(timestamp);
CREATE INDEX IF NOT EXISTS idx_interactions_tweet ON my_interactions(tweet_id);

CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT,
  timestamp TEXT,
  session_id TEXT REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_searches_time ON searches(timestamp);

CREATE TABLE IF NOT EXISTS raw_payloads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_name TEXT,
  payload_json TEXT,
  captured_at TEXT,
  parser_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_op ON raw_payloads(operation_name, captured_at);

-- Dedup ledger. The client stamps every event with crypto.randomUUID() at emit
-- time; retries from the SW queue reuse the same id. INSERT OR IGNORE here is
-- the single canonical dedup check — domain tables stay lean.
CREATE TABLE IF NOT EXISTS event_log (
  event_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  tab_id INTEGER,
  ingested_at TEXT NOT NULL
);

-- Captured GraphQL request shapes. Enrichment worker replays these with new
-- variables against the user's own session. One row per operation_name; every
-- organic request refreshes the row so query_id / features stay current with
-- whatever Twitter's client is shipping this hour.
CREATE TABLE IF NOT EXISTS graphql_templates (
  operation_name TEXT PRIMARY KEY,
  query_id       TEXT NOT NULL,
  url_path       TEXT NOT NULL,
  features_json  TEXT NOT NULL,
  variables_json TEXT NOT NULL,
  bearer         TEXT,
  last_seen_at   TEXT NOT NULL
);

-- Work queue for active enrichment. Populated by periodic sweeps in retention.py.
-- (target_type, target_id, reason) is unique so re-sweeps don't pile up.
CREATE TABLE IF NOT EXISTS enrichment_queue (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type     TEXT NOT NULL,
  target_id       TEXT NOT NULL,
  reason          TEXT NOT NULL,
  priority        INTEGER NOT NULL,
  queued_at       TEXT NOT NULL,
  last_attempt_at TEXT,
  attempts        INTEGER DEFAULT 0,
  succeeded_at    TEXT,
  last_error      TEXT,
  UNIQUE(target_type, target_id, reason)
);
CREATE INDEX IF NOT EXISTS idx_enq_ready ON enrichment_queue(succeeded_at, priority DESC, queued_at);
