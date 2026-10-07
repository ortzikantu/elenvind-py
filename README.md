<img src="elenvind/static/imgs/logo.png" align="right" alt="Logo designed by Hao Wu" width="120" height="120">

<h2>Elenvind</h2> 

A Personal Website Server

> *Elen síla lúmenn' omentielvo.*

## Zero-JS, Zero-Bullshit

- 100% functional without JavaScript.
- 100% free of tracking and advertising.
- A blog, a comment thread and a few static pages — and nothing you have to babysit.

A standard-library-first, **synchronous WSGI** SSR web app on **Gunicorn + SQLite**.
`requirements.txt` pins Gunicorn, Jinja2 and Markdown — no ORM, no DI, no plugin
system, no build step, no asyncio, no writer queue.

---

## Quick start

```bash
git clone https://codeberg.org/ortzikantu/elenvind.git elenvind-py
cd elenvind-py

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.toml config.toml    # at minimum: site_url, title, [static] URLs
python run.py
```

Requires **Python 3.11+** (`tomllib` in the standard library). `python run.py`
validates `config.toml` once, refuses to start on a typo (with a reason), then serves
the site with Gunicorn. There is nothing to generate, no key to paste anywhere, and
exactly **one** optional environment variable: `ELENVIND_DB` (override the SQLite
path, e.g. to isolate staging from production). You never *need* it.

Production equivalent — same app, same config:

```bash
gunicorn --workers 2 --bind 127.0.0.1:6789 elenvind.wsgi:application
# or, to get config.toml's host/port/workers + trusted-proxy wiring:
python run.py --workers 2
```

Behind Nginx: copy `docs/nginx.conf.example`, drop in the paths, add TLS (`certbot`),
put it under systemd, back up with `sqlite3 sqlite.db ".backup …"` (WAL mode — never
raw-copy the file). Step-by-step: [Deployment Guide](docs/DEPLOYMENT.md).

## Architecture: modules do business, Web Core does Web security

```
Gunicorn (sync workers, WSGI)
        │
        ▼
elenvind/wsgi.py          startup(STARTUP_HOOKS) → application  (PEP 3333)
        │
        ▼
elenvind/app.py           Composition Root: the only place that knows both
        │                 sides — route registration order, error pages, and the
        ▼                 data each module gets from another module
elenvind/modules/         blog · pages · auth · users · admin · seo · system
        │                 (modules never import each other)
        ▼
elenvind/core/            config · HTTP · routing · security · session · csrf ·
        │                 auth · templating · markdown · content · database · logging
        ▼
SQLite (WAL)
```

| Rule | Enforced by |
|---|---|
| Core never imports modules or the composition root | `tests/test_architecture.py` |
| Modules never import the composition root | `tests/test_architecture.py` |
| Modules never import each other (collaboration is injected in `app.py`) | `tests/test_architecture.py` |
| No import cycles (module level: zero; Core's legacy lazy cycles: ratcheted) | `tests/test_architecture.py` |
| Modules never touch SQLite, never re-implement security | `tests/test_core_contract.py` |
| All writes go through `write_tx()` | `tests/test_core_contract.py`, `tests/test_c0_*.py` |

Adding a page is just a route — security is applied by the dispatcher, not by you:

```python
@router.route("/hello", methods=["GET"])
def hello(request):
    return html(render_template("hello.html", {"greeting": "Hello"}))

@router.route("/settings", methods=["POST"], auth="required")          # anonymous → 403
def settings(request): ...

@router.route("/admin", methods=["GET"], auth="required", permission="admin")
def admin_home(request): ...
```

A module writes **no** CSRF code, sets **no** cookies, adds **no** security headers and
checks **no** request size — Core does all of that, once. Details:
[Architecture](docs/ARCHITECTURE.md) · [Module Development Guide](docs/development/modules.md).

### Database writes: one entry point

```python
with write_tx() as conn:          # flock(LOCK_EX) on <db>.write.lock → BEGIN IMMEDIATE
    conn.execute("INSERT INTO comment (...) VALUES (...)", (...))
```

Reads use `with connect()`. Every write transaction takes a cross-process `flock` on a
lock file that sits *next to* the database (`sqlite.db` → `sqlite.db.write.lock`), then
`BEGIN IMMEDIATE`, then commits or rolls back — so multiple Gunicorn workers are safe
without a writer process, a queue, or a retry loop. Writes are serialized; reads are not.
The full contract, its limits and what it deliberately does *not* promise:
[Database & C0 write coordination](docs/development/database.md).

## Project layout

```
elenvind/
  app.py        composition root (wiring, injection, startup hooks)
  wsgi.py       production entry point
  core/         framework: HTTP, routing, security, sessions, CSRF, templates,
                markdown, content format, database (connect/write_tx/migrations), logs
  modules/      business: blog, pages, auth, users, admin, seo, system
  templates/    Jinja2 templates (Python prepares data, HTML lives here)
  static/       packaged assets, served by the app with URL mirroring disk
articles/       your Markdown posts          custom_pages/  your standalone pages
i18n/           zh/en/ja message tables      docs/          the handbook
config.toml     your site (validated at startup)
```

## Styling: bring your own, or use the built-in one

| `[static].css` | `use_builtin_css` | What you get |
|---|---|---|
| set | any | your URL (site-relative path or CDN) |
| empty | `true` (default) | the **default** stylesheet, served by the app at `/css/style.css` |
| empty | `false` | no `<link>` at all — you handle styling yourself |

So a fresh clone typesets itself with zero Nginx. The built-in sheet is zero-JS,
variable-driven, has `prefers-color-scheme` *and* `data-theme` dark modes, and is
covered by a drift guard (`tests/test_styles.py`) that fails if a template uses a class
the sheet doesn't define. `style.example.css` at the repo root is a copy of it.

`elenvind/static/` is exposed at `/` with **URL mirroring disk**
(`/css/style.css` → `elenvind/static/css/style.css`): dropping a file in is all it takes.
Files are read per request (edit → refresh, no restart) and served with `ETag` +
`Cache-Control: public, max-age=86400`.

| Directory | Convention | Config key | Default URL |
|---|---|---|---|
| `elenvind/static/css/` | stylesheets | `[static].css` when empty | `/css/style.css` |
| `elenvind/static/imgs/` | images | `[static].favicon` when empty | `/imgs/favicon.ico`/`.png` |
| anything else under `elenvind/static/` | fonts, icons, … | — | by relative path |

**Security boundary:** the resolved path must stay inside `elenvind/static/`; hidden
files/directories are 404; `../`, `%2e%2e`, backslashes and symlinks pointing outside
are rejected. These assets are **public** by design — never put private content here.
Your own `logo`, `hero` and social icons can stay on Nginx/CDN via plain URLs; when
they're empty the element simply isn't rendered, so a fresh clone never shows a broken
image.

## What it is made of

| Concern | Choice |
|---|---|
| Runtime | Python standard library + Gunicorn (WSGI), synchronous business code |
| Rendering | Jinja2 templates, server-side, Zero-JS |
| Database | SQLite (WAL, `PRAGMA foreign_keys=ON`, `user_version` migrations) |
| Write coordination | `write_tx()`: cross-process `flock` on a separate lock file + `BEGIN IMMEDIATE`; multi-worker safe, writes serialized |
| Content | Markdown body + TOML front matter (`+++` fence), rendered by `core.markdown` |
| Markup safety | Whitelist HTML sanitiser in `core.markdown` (stdlib `html.parser`) |
| Sessions | Server-side random tokens in SQLite (not JWT), absolute + idle expiry |
| Passwords | `hashlib.scrypt`, self-describing hashes, transparent rehash on login |
| CSRF | Double-submit cookie, enforced in one dispatcher gate |
| AuthZ | Declarative `auth="required"` / `permission="admin"` on the route |
| Cache | In-process file-snapshot caches for articles and custom pages |
| Security headers | Injected by Core on every response (including 404/500) — modules never set them |
| Observability | One access-log line per request, audit events for auth/comment changes, DEBUG write-transaction detail |

Nothing derives from a shared secret: sessions are opaque random values stored
server-side, CSRF is a double-submit cookie, passwords are self-describing scrypt
hashes. That is why there is no signing key to manage.

## Security defaults

Core appends these on the way out — a module just returns a `Response`:

| Header | Default |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; …` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | camera/microphone/geolocation/payment/usb/… all `()` |
| `Strict-Transport-Security` | only when **configured on** *and* the request is HTTPS |

HTTPS is detected from `X-Forwarded-Proto` **only** when the direct peer is listed in
`[server].trusted_proxies` (default: loopback) — the same list `run.py` hands to
Gunicorn's `forwarded_allow_ips`, so there is exactly one source of truth. Client IPs
are resolved by walking `X-Forwarded-For` from the right, past trusted hops, so a client
cannot forge its own address and slip past the IP rate limits.

Threat model, every control and the reasoning behind them (including why `style-src`
keeps `'unsafe-inline'`):
[Security](docs/SECURITY.md) · [Configuration](docs/CONFIGURATION.md#security-安全响应头).

## Documentation

Everything in Chinese, because we're classy like that. Start at the
**[documentation index](docs/README.md)**; the map:

| | Document | What it is for |
|---|---|---|
| 🚀 | [Deployment Guide](docs/DEPLOYMENT.md) | systemd, Nginx, HTTPS, backups, upgrade |
| ⚙️ | [Configuration Guide](docs/CONFIGURATION.md) | every knob in `config.toml`, with defaults |
| 🧱 | [Architecture](docs/ARCHITECTURE.md) | layers, dependency rules, request pipeline, module map |
| 🔐 | [Security](docs/SECURITY.md) | threat model, controls, proxy trust, headers, logging red lines |
| 🧩 | [Module Development Guide](docs/development/modules.md) | how to add a module (the intended DX) |
| 🗄️ | [Database & C0](docs/development/database.md) | `write_tx()`, lock file, migrations, limits |
| 🧪 | [Testing](docs/development/testing.md) | suites, guards, how to run and extend them |
| 🛠️ | [Ops Guide](docs/OPS_GUIDE.md) | day-2: content, users, comments, logs, limits, FAQ |
| 📄 | [Nginx Config Example](docs/nginx.conf.example) | copy, paste, adjust, ship |
| 🤝 | [Contributing](CONTRIBUTING.md) | patch workflow, style rules, pre-flight checklist |

## Tests

Standard-library `unittest`, no extra dependencies, temp DB and temp content dirs only —
your real `sqlite.db` and `articles/` are never touched.

```bash
python -m unittest discover -s tests -t .        # everything (~140s, 790+ tests)
python -m unittest tests.test_architecture -v    # layering guards
python -m unittest tests.test_core_contract -v   # security contract guards
python -m unittest tests.test_c0_concurrency -v  # real multi-process write coordination
python smoke_driver.py                           # boots real Gunicorn, 61 end-to-end checks
```

They cover the HTTP body parser, CSRF, sessions, auth, comments, the article/page
caches, SEO, config validation, the Markdown renderer + sanitiser (plus a seeded fuzz
harness), the **Core Contract**, the **architecture guards**, real multi-process
`write_tx()` coordination (including `SIGKILL` recovery and concurrent migrations), and
an end-to-end cold start. What each suite is for: [Testing](docs/development/testing.md).

## License

This project is licensed under the [GPL-3.0-or-later](LICENSE) license.

---

No PRs, please.  
Patches only.  
Test it.  
Commit it.  
Generate one with `git format-patch`.  
Then mail it to me.  

Workflow, style rules and the pre-flight checklist live in
[CONTRIBUTING.md](CONTRIBUTING.md):

```bash
git format-patch -1            # the latest commit
git format-patch origin/main   # everything not yet pushed
```
