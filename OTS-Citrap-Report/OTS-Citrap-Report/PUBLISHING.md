# Making this installable through the OTS web UI

There are two separate ways to get this plugin onto an OTS server without
a developer running pip commands by hand:

## Option 0: Direct zip upload (simplest, if your OTS build supports it)

Some OTS installs let an admin upload a `.zip` directly and run
`pip install <the zip>` on it server-side. If yours does, you don't need
an index at all - just zip this project's contents **with `pyproject.toml`
at the top level of the zip** (not nested inside an extra wrapper folder)
and upload it. `pyproject.toml` needs to be visible the moment pip
extracts the zip, or you'll get "does not appear to be a Python project."

```bash
zip -r OTS-Citrap-Report.zip pyproject.toml README.md ots_citrap_report scripts
```

This plugin's `pyproject.toml` uses a plain static version (no
`poetry-dynamic-versioning`, no git tag required) specifically so a
zip-upload install - which has no `.git` directory for dynamic versioning
to read - works cleanly.

## Options A/B/C: pip index (for the web UI's "Install Plugin by name" screen)

OTS's "Install Plugin" screen runs `pip install <name> -i
<OTS_PLUGIN_REPO_URL>`. `OTS_PLUGIN_REPO_URL` defaults to the official repo
(`https://repo.opentakserver.io/brian/prod/`), but any admin can repoint it
at another pip-compatible index. Three ways to get there, easiest first:

### Option A: Static index on GitHub Pages (no server to run)

1. Build the package (needs network, which this sandbox doesn't
   have - run this on your own machine):

   ```bash
   pip install poetry
   poetry build
   # -> dist/ots_citrap_report-1.0.0-py3-none-any.whl
   # -> dist/ots_citrap_report-1.0.0.tar.gz
   ```

2. Turn that into a static package index:

   ```bash
   python scripts/build_static_index.py dist/ docs/
   ```

   This writes a PEP 503-compliant `docs/` folder (`docs/index.html` +
   `docs/ots-citrap-report/index.html` + the actual files) — the same
   layout pip expects when it talks to PyPI or devpi, just as flat files.

3. Commit `docs/` and push to GitHub, then enable **GitHub Pages** for
   that branch/folder in the repo's Settings → Pages.

4. On your OTS server, in `~/ots/config.yml`:

   ```yaml
   OTS_PLUGIN_REPO_URL: "https://<you>.github.io/<repo>/"
   ```

   Restart OTS.

5. In the OTS web UI's **Plugins → Install Plugin** screen, enter the
   package name `OTS-Citrap-Report` (any casing — pip normalizes it) and
   install. No command line needed on the server itself.

Rebuilding for a new version: bump `version` in `pyproject.toml` and
`ots_citrap_report/__init__.py`, `poetry build`, re-run the index script,
commit, push — GitHub Pages picks it up automatically.

## Option B: Self-hosted devpi (what the OTS docs officially suggest)

This is the option OTS's own documentation for `OTS_PLUGIN_REPO_URL`
mentions by name. More setup than Option A (a devpi server process to run
and keep up), but gives you a real writable index with auth, multiple
users, etc. if you're distributing more than one plugin internally.

```bash
pip install devpi-server devpi-client
devpi-server --host 0.0.0.0 --port 3141 --init  # run this as a persistent service
devpi use http://localhost:3141
devpi user -c yourname password=yourpassword
devpi login yourname --password=yourpassword
devpi index -c prod
devpi use yourname/prod
poetry build
devpi upload dist/*
```

Then point `OTS_PLUGIN_REPO_URL` at `http://<devpi-host>:3141/yourname/prod/`
and install by name from the web UI, same as Option A step 5.

## Option C: Submit to the official public repo

Distributing to *other* OTS admins with zero config on their end means
getting accepted into `repo.opentakserver.io` — that's a manual review
process run by the OTS maintainer (code review for correctness and no
malicious code), not something automatable from here. Worth doing once
you're happy with the plugin and want it broadly discoverable, but Option
A or B is the right move for "install this on my own server via the UI"
in the meantime.

## Note on this sandbox

I don't have network access in this environment, so I couldn't actually
run `poetry build` or set up a live index to hand you a working URL —
everything above is accurate to OTS's documented mechanism, but the
`poetry build` / `devpi upload` / GitHub push steps need to happen on a
machine with network access (yours).
