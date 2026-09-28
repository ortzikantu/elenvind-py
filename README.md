<img src="static/logo.png" align="right" alt="Logo designed by Hao Wu" width="120" height="120">

# Elenvind 

A Personal Website Server

> *Elen síla lúmenn' omentielvo.*

## Zero-JS, Zero-Bullshit

- 100% functional without JavaScript.
- 100% free of tracking and advertising.

## Documentation

Everything in Chinese, because we're classy like that:

- [EVMD Markup Spec](docs/EVMD_SPEC.md) — the custom markup language for articles & pages
- [Configuration Guide](docs/CONFIGURATION.md) — every knob in config.toml
- [Deployment Guide](docs/DEPLOYMENT.md) — systemd, Nginx, HTTPS, backups
- [Nginx Config Example](docs/nginx.conf.example) — copy, paste, adjust, ship
- [Ops Guide](docs/OPS_GUIDE.md) — articles, comments, logs, rate limits, tests, FAQ

## Getting started

A standard-library-based SSR web app, with **Uvicorn as the ASGI server** — that is the
only runtime dependency (`requirements.txt` also pins Uvicorn's own `click`/`h11`).
No ORM, no template engine, no framework, no build step. Just for fun.

```bash
git clone https://codeberg.org/ortzikantu/elenvind.git elenvind-py
cd elenvind-py

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

Requires **Python 3.11+** (`tomllib` in the standard library).
Then copy the template config and edit it before the first run:

```bash
cp config.example.toml config.toml
# at minimum: site_url, title, [static] URLs
```

For CSS, images, and whatnot, you're on your own. Set up Nginx (or equivalent) or a CDN. I can't be bothered.
Static URLs live in `config.toml` (`[static]` + `params.social.icon`). See the Configuration Guide.
`config.toml` is validated once at startup: a typo means a refused start with a reason, not a 500 later.

### What it is made of

| Concern | Choice |
|---|---|
| Runtime | Python standard library + Uvicorn (ASGI) |
| Rendering | Server-side HTML strings, Zero-JS |
| Database | SQLite (WAL, `PRAGMA foreign_keys=ON`, `user_version` migrations) |
| Markup | EVMD — this project's own format (see `docs/EVMD_SPEC.md`) |
| Sessions | Server-side random tokens in SQLite (not JWT) |
| Passwords | `hashlib.scrypt`, self-describing hashes, transparent rehash on login |
| CSRF | Double-submit cookie, enforced in one dispatcher gate |
| Cache | In-process file-snapshot caches for articles and custom pages |

## Any Tips

You'll need a `SECRET_KEY` environment variable before starting.
Just smash your keyboard. The more random, the better.
```bash
export SECRET_KEY="your-strong-random-secret"
python run.py
```

Too lazy to type? Use the tool below. Pick whatever length you want, 64, 128, 256, 512, anything goes.
```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```
*DON'T TELL ANYONE*

*Note: it's an environment gate these days — sessions are server-side random tokens, so the key itself isn't used for signing anymore. Set it anyway. Consider it tradition.*

HTTPS? Cookies get `Secure` automatically — as long as your reverse proxy speaks `X-Forwarded-Proto`. Behind Nginx, just follow the example config and you're done. No code edits required.
```bash
sudo cp docs/nginx.conf.example /etc/nginx/sites-available/elenvind
```

Using Nginx? Might as well set up Let's Encrypt too.
```bash
sudo certbot certonly --standalone -d yourdomain.com -d www.yourdomain.com
```

Serve it forever with systemd, back up the SQLite database with `.backup` (WAL mode, don't raw-copy the file), and go touch grass. Details in the Deployment Guide.

## Tests

Standard-library `unittest`, no extra dependencies. Temp DB and temp content dirs only —
your real `sqlite.db` and `articles/` are never touched.

```bash
python -m unittest discover -s tests -t .        # everything
python -m unittest tests.test_evmd_block -v      # one module
```

Covers the HTTP body parser, CSRF, sessions, auth, comments, the article/page caches,
SEO, config validation, the EVMD parser (plus a seeded fuzz harness) and an
end-to-end cold start walkthrough.

No PRs, please.  
Patches only.  
Test it.  
Commit it.  
Generate one with `git format-patch`.  
Then mail it to me.  
```bash
# Generate a patch for the latest commit
git format-patch -1
# Generate patches for all commits not yet pushed to the remote branch
git format-patch origin/main
```

## License

This project is licensed under the [GPL-3.0-or-later](LICENSE) license.
