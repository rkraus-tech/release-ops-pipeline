-- 001_roles_and_database.sql
-- Run once as the PostgreSQL superuser:
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -f sql/001_roles_and_database.sql
--
-- Role concept (least privilege):
--   deploy_writer  group role: may INSERT and SELECT deploy history (no UPDATE/DELETE)
--   deploy_reader  group role: may only SELECT deploy history
--   jenkins        login role, member of deploy_writer (peer auth = Linux user "jenkins")
--   robert         login role, member of deploy_reader (peer auth = Linux user "robert")

CREATE ROLE deploy_writer NOLOGIN;
CREATE ROLE deploy_reader NOLOGIN;

CREATE ROLE jenkins LOGIN IN ROLE deploy_writer;
CREATE ROLE robert  LOGIN IN ROLE deploy_reader;

CREATE DATABASE releases;

-- Nobody may connect by default, only the two group roles
REVOKE ALL ON DATABASE releases FROM PUBLIC;
GRANT CONNECT ON DATABASE releases TO deploy_writer, deploy_reader;
