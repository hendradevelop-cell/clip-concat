CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE templates (
  id         text PRIMARY KEY,
  name       text NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('clipper','news','avatar')),
  style      jsonb NOT NULL,
  offset_y   numeric NOT NULL DEFAULT -0.34,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE avatars (
  id            text PRIMARY KEY,
  name          text NOT NULL,
  face_path     text NOT NULL,
  voice_profile text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
  id         uuid PRIMARY KEY,
  category   text NOT NULL,
  status     text NOT NULL,
  task_id    text,
  input      jsonb NOT NULL,
  progress   jsonb NOT NULL DEFAULT '{"stages":[],"percent":0}',
  result     jsonb,
  error      text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_category_created_idx ON jobs (category, created_at DESC);
CREATE INDEX jobs_status_idx ON jobs (status, created_at DESC);

CREATE TABLE job_outputs (
  id         bigserial PRIMARY KEY,
  job_id     uuid NOT NULL REFERENCES jobs ON DELETE CASCADE,
  path       text NOT NULL,
  kind       text NOT NULL,
  duration_s numeric,
  w          int,
  h          int,
  meta       jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, path)
);

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

-- Kolom baru untuk database yang sudah terisi: schema ini dipakai ulang lewat
-- `psql -f schema.sql`, jadi perubahan tabel harus idempoten.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS result jsonb;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS task_id text;

CREATE TRIGGER jobs_touch BEFORE UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
