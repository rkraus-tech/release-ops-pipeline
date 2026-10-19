-- reports.sql - typical questions about the deploy history
-- Run as any role with SELECT rights, e.g.:
--   psql -d releases -f sql/reports.sql

\echo '=== Last 10 deployments and rollbacks ==='
SELECT to_char(deployed_at, 'YYYY-MM-DD HH24:MI:SS') AS deployed_at,
       action,
       release_id,
       status,
       duration_s AS "duration (s)",
       previous_release,
       triggered_by
FROM   deploy_history
ORDER  BY deployed_at DESC
LIMIT  10;

\echo '=== Deployment success rate (last 30 days) ==='
SELECT count(*)                                              AS deployments,
       count(*) FILTER (WHERE status = 'success')            AS successful,
       count(*) FILTER (WHERE status <> 'success')           AS failed_or_rolled_back,
       round(100.0 * count(*) FILTER (WHERE status = 'success')
             / NULLIF(count(*), 0), 1)                       AS "success rate %"
FROM   deploy_history
WHERE  action = 'deploy'
AND    deployed_at > now() - interval '30 days';

\echo '=== Average duration per action and status ==='
SELECT action,
       status,
       count(*)                       AS runs,
       round(avg(duration_s), 1)      AS "avg duration (s)",
       max(duration_s)                AS "max duration (s)"
FROM   deploy_history
GROUP  BY action, status
ORDER  BY action, status;

\echo '=== Currently live release (according to the history) ==='
SELECT release_id, action, to_char(deployed_at, 'YYYY-MM-DD HH24:MI:SS') AS since
FROM   deploy_history
WHERE  status = 'success'
ORDER  BY deployed_at DESC
LIMIT  1;
