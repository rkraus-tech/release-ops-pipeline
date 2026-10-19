-- 002_deploy_history.sql
-- Run as the PostgreSQL superuser against the "releases" database:
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -d releases -f sql/002_deploy_history.sql

BEGIN;

CREATE TABLE IF NOT EXISTS deploy_history (
    id               bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    deployed_at      timestamptz NOT NULL DEFAULT now(),
    action           text        NOT NULL CHECK (action IN ('deploy', 'rollback')),
    release_id       text        NOT NULL,                 -- e.g. 1.1.0-b2
    version          text        NOT NULL,                 -- e.g. 1.1.0
    status           text        NOT NULL CHECK (status IN ('success', 'failed', 'rolled_back')),
    duration_s       integer     NOT NULL CHECK (duration_s >= 0),
    previous_release text,                                 -- release that was live before
    triggered_by     text        NOT NULL DEFAULT current_user,
    build_url        text                                  -- Jenkins build link, if any
);

COMMENT ON TABLE deploy_history IS 'Append-only audit log of all deployments and rollbacks of release-app';

CREATE INDEX IF NOT EXISTS deploy_history_deployed_at_idx ON deploy_history (deployed_at DESC);

-- Least privilege: writers can only add rows, readers can only read.
-- No role except the owner may UPDATE or DELETE -> the history cannot be rewritten.
REVOKE ALL ON deploy_history FROM PUBLIC;
GRANT SELECT, INSERT ON deploy_history TO deploy_writer;
GRANT SELECT         ON deploy_history TO deploy_reader;

COMMIT;
