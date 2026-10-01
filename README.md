<img src="elenvind/static/imgs/logo.png" align="right" alt="Logo designed by Hao Wu" width="120" height="120">

<h2>Elenvind</h2> 

A Personal Website Server

> *Elen síla lúmenn' omentielvo.*

## Zero-JS, Zero-Bullshit

- 100% functional without JavaScript.
- 100% free of tracking and advertising.

## Documentation

Everything in Chinese, because we're classy like that:

- [Feature Development Guide](docs/development/features.md) — how to add a Feature (the intended DX)
- [Configuration Guide](docs/CONFIGURATION.md) — every knob in config.toml
- [Deployment Guide](docs/DEPLOYMENT.md) — systemd, Nginx, HTTPS, backups
- [Nginx Config Example](docs/nginx.conf.example) — copy, paste, adjust, ship
- [Ops Guide](docs/OPS_GUIDE.md) — articles, comments, logs, rate limits, tests, FAQ

## Getting started

A standard-library-first SSR web app, with **Uvicorn as the ASGI server**.
`requirements.txt` pins Uvicorn (plus its `click`/`h11`), Jinja2 and Markdown —
no ORM, no DI, no plugin system, no framework, no build step.

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

### Styling: bring your own, or use the built-in one

| `[static].css` | `use_builtin_css` | What you get |
|---|---|---|
| set | any | your URL (site-relative path or CDN) |
| empty | `true` (default) | the **default** stylesheet, served by the app at `/css/style.css` |
| empty | `false` | no `<link>` at all — you handle styling yourself |

So `python run.py` alone gives you a fully typeset site, with zero Nginx required.
The built-in sheet is zero-JS, variable-driven, has both `prefers-color-scheme` and
`data-theme` dark modes, and is covered by a drift guard
(`tests/test_styles.py`) that fails if a template uses a class the sheet doesn't define.
`style.example.css` at the repo root is a copy of it — start from that if you want to
take over the look entirely.

Default assets live inside the package and are served by the app itself. The whole
`elenvind/static/` tree is exposed at `/`, with **URL mirroring disk**:

```
/css/style.css  ->  elenvind/static/css/style.css
/imgs/logo.svg  ->  elenvind/static/imgs/logo.svg
```

So dropping a file into that tree is all it takes to make it available — no registry,
no config edit.

| Directory | Convention | Config key | Default URL |
|---|---|---|---|
| `elenvind/static/css/` | stylesheets | `[static].css` when empty | `/css/style.css` |
| `elenvind/static/imgs/` | images | `[static].favicon` when empty | `/imgs/favicon.ico`/`.png` |
| anything else under `elenvind/static/` | fonts, icons, … | — | by relative path |

Same rule throughout: **config wins, packaged default fills in.** Order is
`[static].css`/`favicon` → packaged file → nothing rendered. Responses carry an `ETag`
and `Cache-Control: public, max-age=86400`, and files are read per request, so edits
show up on refresh without a restart.

**Security boundary:** the resolved path must stay inside `elenvind/static/`; hidden
files/directories are 404; `../`, `%2e%2e`, backslashes and symlinks pointing outside
are all rejected. These assets are **public** by design — never put private content here.

Your own `logo`, `hero` and social icons can stay on Nginx/CDN via plain URLs. When
they're empty the element simply isn't rendered, so a fresh clone never shows a
broken image.

### Architecture: Feature 负责业务，Web Core 负责 Web 安全

```
elenvind/
  core/        Web Core —— 请求/响应、安全头、Cookie、会话、认证、授权、CSRF、
               模板（Jinja2 唯一入口）、Markdown（唯一入口）、数据库连接
  features/    业务 —— blog / pages / auth / users / admin / seo / system
  templates/   Jinja2 模板（Python 只准备数据，HTML 全在这里）
  app.py       装配点：Core App + Feature 注册
```

新增一个页面只需要声明路由，安全由框架默认施加：

```python
@route("/hello", methods=["GET"])
def hello(request):
    return render_template("hello.html", {"greeting": "Hello"})

@route("/settings", methods=["POST"], auth="required")   # 未登录提交 -> 403
def settings(request): ...

@route("/admin", methods=["GET"], auth="required", permission="admin")
def admin_home(request): ...
```

未登录访问受保护页面时，框架会 `302 → /login?next=<原路径>`，登录后自动回到原页面
（`next` 只接受站内路径，防开放重定向）；已登录但权限不足才是 403。
`/user` 与 `GET /logout` 这类"关于你自己"的页面在未登录时渲染友好的提示页，
而状态变更始终只由 POST + CSRF 触发。

Feature 里**不写** CSRF、不拼 Cookie、不加安全头、不检查请求体大小 ——
这些都是 Core 的职责，且由 `tests/test_core_contract.py` 静态 + 运行时双重守卫。
详见 [Feature Development Guide](docs/development/features.md)。

### What it is made of

| Concern | Choice |
|---|---|
| Runtime | Python standard library + Uvicorn (ASGI) |
| Rendering | Jinja2 templates, server-side, Zero-JS |
| Styling | Default stylesheet served by the app (`/css/style.css`); config can override with your own URL |
| Database | SQLite (WAL, `PRAGMA foreign_keys=ON`, `user_version` migrations) |
| Content | Markdown body + TOML front matter (`+++` fence), rendered by `core.markdown` |
| Markup safety | Whitelist HTML sanitiser in `core.markdown` (stdlib `html.parser`) |
| Sessions | Server-side random tokens in SQLite (not JWT) |
| Passwords | `hashlib.scrypt`, self-describing hashes, transparent rehash on login |
| CSRF | Double-submit cookie, enforced in one dispatcher gate |
| AuthZ | Declarative `auth="required"` / `permission="admin"` on the route |
| Cache | In-process file-snapshot caches for articles and custom pages |

## Any Tips

No environment variables. No signing key. No secret to generate and paste into a
systemd unit at 3am. Just:

```bash
python run.py
```

Why it works without one: sessions are **server-side random tokens** in SQLite (the
client only ever holds an unguessable opaque value), CSRF is a **double-submit cookie**
(the token itself is the random value), and passwords are **self-describing scrypt
hashes**. Nothing here derives anything from a shared secret, so requiring one would
have been pure ceremony.

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
python -m unittest tests.test_core_contract -v   # one module
```

Covers the HTTP body parser, CSRF, sessions, auth, comments, the article/page caches,
SEO, config validation, the Markdown renderer + sanitiser (plus a seeded fuzz harness),
the **Core Contract** (features cannot bypass or duplicate security), and an
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
