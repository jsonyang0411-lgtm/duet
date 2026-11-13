# sample_site — 达人选样品网站

A small public website where a creator picks the sample hats she wants and
leaves her shipping details. Python standard library only (3.9+), one process,
one SQLite file, plain WSGI. Deploying it: see **DEPLOY.md** (Chinese, for
whoever runs the server).

## Files

| File | What |
|---|---|
| `app.py` | WSGI app (`create_app(Config)`), creator pages, admin API, threaded stdlib server (`python3 app.py`) |
| `wsgi.py` | `application` for gunicorn / waitress (optional) |
| `config.py` | `Config` and `Config.from_env()` (`SAMPLE_SITE_*` variables) |
| `store.py` | SQLite (WAL, one locked connection, `BEGIN IMMEDIATE` writes) |
| `catalog.py` | catalogue validation, sku index, picture sniffing |
| `forms.py` | form checks, the 34 provinces, phone normalising, masking |
| `pages.py` | server-rendered HTML; every creator text is in `TEXT` |
| `security.py` | rate limiter, token hashing, signed form nonces, log-safe paths |
| `static/` | `site.css`, `boot.js`, `site.js` (ES5, optional: the form works without it) |
| `tools/make_screenshots.py` | phone screenshots with a fake catalogue (dev only) |
| `Dockerfile`, `docker-compose.example.yml`, `Caddyfile.example`, `site.env.example` | deployment |

## Run locally

```bash
cd sample_site
SAMPLE_SITE_ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))") python3 app.py
```

In-process (tests):

```python
from sample_site.app import create_app
from sample_site.config import Config
app = create_app(Config(admin_token="x" * 40, db_path="/tmp/t/site.db"))
```

Tests: `tests/test_sample_site.py` (WSGI in-process, plus the script on the
system Python) and `tests/test_sample_site_flow.py` (Playwright, phone viewport).

## Admin API (`Authorization: Bearer <token>`, JSON)

| Call | Does |
|---|---|
| `GET /admin/api/status` | catalogue version, active links, submission counts (`time` = server clock) |
| `GET /admin/api/images` / `PUT /admin/api/images/<key>` | picture keys / upload (JPEG/PNG/WebP ≤ 400 KB, key = first 32 hex of sha256) |
| `GET` / `PUT /admin/api/catalog` | read / replace the whole catalogue (`version` + 1, unused pictures pruned) |
| `POST /admin/api/links` / `GET /admin/api/links?owner=` | make a personal or general link (token shown once) / list (no tokens) |
| `POST /admin/api/links/<id>/revoke` | revoke |
| `POST /admin/api/submissions/pull` | `new` → `taken`, return every un-acked submission of the owner |
| `POST /admin/api/submissions/ack` | `taken` → `acked`; receiver reduced to masked remnants |

Additive fields beyond the contract: `status.time`; links list `ask_account`,
`max_uses`, `parent_id` (personal links made from a general link carry its
id); pull `purged` (true when the retention purge already masked it).
