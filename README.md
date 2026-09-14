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
- [Ops Guide](docs/OPS_GUIDE.md) — articles, comments, logs, rate limits, FAQ

## Getting started

This is an SSR Python Web App that's both painful and joyful. No external deps except Uvicorn. Just for fun.

```bash
git clone https://codeberg.org/ortzikantu/elenvind.git elenvind-py
cd elenvind-py

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

For CSS, images, and whatnot, you're on your own. Set up Nginx (or equivalent) or a CDN. I can't be bothered.
Static URLs live in `config.toml` (`[static]` + `params.social.icon`). See the Configuration Guide.

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
