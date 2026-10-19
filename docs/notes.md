# Project Notes – release-ops-pipeline

## Phase 1 – Planning & repository setup
- Decisions: Jenkins runs natively (apt, systemd) on a dedicated Debian 13 VM `ops01` on the existing Proxmox host `pve01` – realistic always-on build server, Bash runs in the same OS as the deploy target. Docker on the ThinkPad was rejected (Windows/WSL quirks, not always on).
- Database: PostgreSQL instead of SQLite – real DB server with a least-privilege role and proper secret handling.
- Created empty GitHub repo first (no README/license via GitHub) to avoid unrelated histories on the first push; added a placeholder README locally.
- Folder structure: jenkins, scripts, sql, python, app, screenshots, docs (`.gitkeep` files because Git does not track empty folders).
- `.gitattributes` forces LF for all Linux-side files (*.sh, Jenkinsfile, *.py, *.sql). Reason: CRLF from Windows breaks Bash (`$'\r': command not found`). Verified with `git check-attr eol -- scripts/deploy.sh`.
- `.gitignore` excludes secrets (.env, .pgpass, keys), Python caches, runtime logs (except log-scanner samples) and unredacted screenshots (`screenshots/raw/`).
- Both files start with a comment line so a UTF-8 BOM (Windows PowerShell 5.1) cannot invalidate the first rule.

