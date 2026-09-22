CREATE TABLE IF NOT EXISTS incidents (
 incident_id text PRIMARY KEY, correlation_id text NOT NULL,
 state jsonb NOT NULL DEFAULT '{}', updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ingress (
 source text NOT NULL, source_event_id text NOT NULL, fingerprint text NOT NULL,
 event jsonb NOT NULL, PRIMARY KEY(source, source_event_id)
);
CREATE TABLE IF NOT EXISTS outbox (
 event_id text PRIMARY KEY, topic text NOT NULL, incident_id text NOT NULL,
 event jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz
);
CREATE TABLE IF NOT EXISTS inbox (
 event_id text PRIMARY KEY, incident_id text NOT NULL, event jsonb NOT NULL,
 status text NOT NULL DEFAULT 'pending', attempts int NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT now(), error text,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(next_attempt_at) WHERE status='pending';
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(created_at) WHERE sent_at IS NULL;
CREATE TABLE IF NOT EXISTS approvals (
 plan_id text PRIMARY KEY, incident_id text NOT NULL, plan jsonb NOT NULL,
 expires_at timestamptz NOT NULL, decision jsonb, consumed boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS executions (
 plan_id text PRIMARY KEY, incident_id text NOT NULL, result jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit (
 id bigserial PRIMARY KEY, incident_id text NOT NULL, kind text NOT NULL,
 detail jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
