# OTS-Citrap-Report

An OpenTAKServer (OTS) plugin implementing the CI-TRAP Report API
(`ci-trap-report-api` tag in TAK Server's OpenAPI spec), confirmed against
the actual spec JSON:

| Operation | Method | Path | clientUid |
|---|---|---|---|
| getReport | GET | `/Marti/api/citrap/{id}` | required |
| putReport | PUT | `/Marti/api/citrap/{id}` (JSON body, base64 bytes) | required |
| deleteReport | DELETE | `/Marti/api/citrap/{id}` | required |
| searchReports | GET | `/Marti/api/citrap` | **optional** |
| postReport | POST | `/Marti/api/citrap` (JSON body, base64 bytes) | required |
| addAttachment | POST | `/Marti/api/citrap/{id}/attachment` (JSON body, base64 bytes) | required |

All responses in the real spec are modeled as a bare `string` with
content-type `*/*` — genuinely opaque, not a defined JSON object — so this
plugin passes bytes through rather than wrapping everything in JSON
metadata. See "Response bodies" below for exactly what each op returns.

## Important: collision with OTS core

**`GET /Marti/api/citrap` (searchReports) is the exact same path and method
OTS core already documents as natively supported.** Flask/Werkzeug won't
error on the duplicate rule, but whichever route got added to the URL map
first wins silently — and core's routes register before plugins load, so
this plugin's `searchReports` would never actually run by default.

This plugin's default behavior: **skip registering `searchReports`
entirely**, log a note explaining why, and let core handle that one path.
The other five operations (`getReport`, `putReport`, `deleteReport`,
`postReport`, `addAttachment`) don't overlap with anything core implements,
so they're always registered.

If you've confirmed your OTS version's core `GET /Marti/api/citrap` is a
stub you actually want to replace, set `OTS_CITRAP_REPORT_OVERRIDE_SEARCH:
true` in config.yml — but whether the override actually wins the route
depends on plugin/blueprint registration order in your OTS version's
`PluginManager`, which isn't guaranteed. Test it before relying on it.

## Ownership / clientUid semantics

The spec requires `clientUid` on every operation except `searchReports`.
This plugin treats it as an **owner** on the required-clientUid operations:
a report can only be fetched, replaced, deleted, or have an attachment
added by the `clientUid` that created it (403 if it doesn't match, 404 if
the report doesn't exist). On `searchReports`, `clientUid` is optional and
used only as a filter — if omitted, search runs across all reports.

If your real deployment's semantics differ (e.g. any client can read any
report), loosen the `_check_owner` calls in `app.py`.

## Response bodies

| Operation | Response |
|---|---|
| getReport | Raw stored bytes (`application/octet-stream`), status 200 |
| putReport | Raw bytes now stored, echoed back, status 200 |
| deleteReport | Empty body, status 200 |
| postReport | The new report's id as plain text, status 200 |
| addAttachment | The new attachment's id as plain text, status 200 |
| searchReports | A JSON array of report summaries, encoded as the response string, status 200 |

`searchReports`'s JSON-array choice is an inference — the spec only says
"string," which a JSON-encoded array literally satisfies, but doesn't
pin down the shape further. Adjust `_register_search_route` in `app.py` if
you learn the real TAK Server response shape differs.

## Remaining genuine ambiguities

1. **`subscribe=true`** on `searchReports` is logged but doesn't push live
   updates yet. Wire it into `on_report_created` below plus OTS's SocketIO
   extension or RabbitMQ if you need real push behavior.
2. **PUT-as-upsert on `putReport`**: the spec doesn't explicitly say
   whether `putReport` on a nonexistent id creates it or 404s. This plugin
   creates it (conventional PUT semantics), since there's no separate
   "create with a caller-chosen id" operation otherwise.

## bbox filtering

Confirmed format: `lon1,lat1,lon2,lat2` — two opposite corners of a
rectangle, e.g. `-122.321777,45.199216,-122.251740,45.234283`. The plugin
takes the min/max of each axis independently, so it works regardless of
which corner comes first.

One thing this required inferring: **reports themselves don't carry a
location field anywhere in the API** (`postReport`/`putReport` only take
opaque bytes — no lat/lon/bbox parameter). Real TAK ecosystem report
payloads are typically CoT (Cursor-on-Target) XML, which does carry
location as `<event><point lat="..." lon="..."/></event>`. So on every
create/update, the plugin best-effort parses the payload as CoT XML and
stores the extracted `lat`/`lon` for filtering (see
`extract_latlon_from_cot()` in `models.py`). A report whose payload isn't
CoT XML (or has no `<point>`) simply has `lat`/`lon = None` and will never
match a bbox filter — it still exists and is fully retrievable by id or by
non-geo search filters, just invisible to `bbox`. If your reports use a
different payload format, swap out `extract_latlon_from_cot()` accordingly.

## Important: package name must be lowercase

OTS's own plugin docs say a plugin's `pyproject.toml` name "must start with
`OTS-`" (capitalized, as a style convention). OTS's actual runtime
validation checks for a lowercase `ots_`/`ots-` prefix instead - and
separately, `PluginManager`'s internal registry keys plugins by the PEP
503-normalized (lowercase) form of whatever gets installed. A mixed-case
name like `OTS-Citrap-Report` satisfies the docs' style guidance but fails
both of those runtime checks: `install_plugin` logs `Invalid Plugin: ...`
and rejects it, and even when a plugin instance manually reports a
mixed-case `self.distro`, enable/disable/uninstall fail with a `KeyError`
because the real dict key is the lowercase-normalized name.

This package is named `ots-citrap-report` (all lowercase) specifically to
avoid both failure modes - `[tool.poetry] name` in `pyproject.toml` and
`DISTRO_NAME` in `app.py` must always match this exactly.

**If you previously installed a build of this plugin under the old
mixed-case name (`OTS-Citrap-Report`)**, clean it up before installing
this version:

```bash
~/.opentakserver_venv/bin/pip uninstall OTS-Citrap-Report -y
```

Also check the OTS admin UI's Plugins page after restarting - if a stale
entry for the old name is still listed (since its DB row may not get
cleaned up automatically given the `KeyError` bug above prevented a normal
disable/uninstall), that's a leftover row in OTS's own `Plugins` table
that a straightforward `pip uninstall` won't touch. Worth checking with
OTS's own admin/support channels for the sanctioned way to remove a stale
plugin DB row if the UI doesn't offer one directly, rather than editing
the database by hand.

## Bundled plugin UI (the official iframe mechanism)

`ots_citrap_report/ui/index.html` implements OTS's documented "plugin UI
iframe" convention - core serves a plugin's `ui/` folder at
`/api/plugins/<distro>/ui` and displays it in an iframe on the Plugins
page. Normally this is a full Mantine/React app built with
`OTS-UI-Plugin-Template` (Vite + TypeScript + Yarn); this is instead a
single self-contained static HTML file with inline CSS and vanilla JS -
no build step, no external CDN dependencies, no separate JS/CSS asset
files to worry about path-resolution for. It fetches from `{prefix}/ui/data`
(a small JSON endpoint on this plugin's own blueprint, same-origin so the
existing OTS session cookie carries over automatically) and renders a
read-only report table, with a link out to the full admin page at
`{prefix}/ui`.

**Where the file lives matters and is confirmed, not guessed**: I checked
the actual `OTS-UI-Plugin-Template` repo's `vite.config.mjs`:

```javascript
build: {
  outDir: '../ots_plugin_template/ui', // <-------- TODO: Change this line
  emptyOutDir: true,
}
```

The build output goes to `../<python_package_name>/ui` - i.e. `ui/` lives
**inside the Python package folder** (`ots_citrap_report/ui/`), not at the
project root next to `pyproject.toml`. This plugin's `ui/index.html` is
placed there accordingly, and `pyproject.toml`'s `include` is set to
`["ots_citrap_report/ui/**/*"]` to match.

If you want the full Mantine/React experience instead of this static page,
clone `OTS-UI-Plugin-Template`, point its `vite.config.mjs` outDir at
`../OTS-Citrap-Report-Plugin/ots_citrap_report/ui` (adjusting the relative
path to wherever this plugin's checkout actually lives), build your own
components against `{prefix}/ui/data`, and `yarn build` will replace this
static file with the real bundled app - no other changes needed on the
Python side.

## Report browser (admin page)

Both pages automatically match whatever dark/light theme is currently set
in OTS's own web UI - no separate toggle to manage. This works by reading
`localStorage.getItem('mantine-color-scheme')` (Mantine's default
persistence key; OTS-UI is built on Mantine per its own docs) via a small
inline script that runs before anything paints, so there's no flash of
the wrong theme. If that key isn't set (theme never explicitly chosen, or
OTS-UI customized the storage key to something not publicly documented),
it falls back to the browser/OS-level `prefers-color-scheme` instead.
Since these pages are separate full-page navigations rather than embedded
in OTS's own React app, this is the only way to read that preference -
there's no live DOM/state to share directly, but `localStorage` is shared
across the whole origin regardless of path, so the persisted value carries
over correctly.

`GET {admin_prefix}` (default `/api/citrap-reports`, e.g.
`https://your-server/api/citrap-reports` - **no port needed, works on the
normal HTTPS port**) is a simple, self-contained read-only page listing
submitted reports. Columns are ordered for human scanning rather than
database identifiers - **Date/Time, Callsign, Title, Type, Importance**
first, then Lat/Lon and attachment count. `id` and `client_uid` aren't
shown as columns at all (still visible on the detail/view page) since
they're not meaningful at a glance; the report's Title is the clickable
link through to its detail page instead.

Every one of those five leading columns is sortable - click a column
header to sort by it (e.g. click "Callsign" to group all reports by which
callsign submitted them), click again to reverse direction. Sort state is
in the URL (`?sort=callsign&order=asc`), so it's bookmarkable/shareable.
"Date/Time" sorts by the report's own embedded submission time (from its
`dateTime` attribute), not by when the server received it.

Delete buttons submit a real CSRF token (via Flask-WTF's `generate_csrf()`,
matching whatever CSRF protection OTS's own Flask app has enabled) - an
earlier version's forms lacked this entirely and got rejected with "The
CSRF token is missing."

The detail/view page also shows every other file bundled in the report's
payload zip beyond `report.xml` itself - photos as an inline gallery
(jpg/jpeg/png/gif/bmp/webp/heic/heif), videos as inline `<video>` players
with playback controls (mp4/mov/m4v/webm/avi/mkv/3gp), and anything else
as a plain download link. Video (and image) responses are served via
Flask's `send_file(..., conditional=True)` rather than a plain `Response`,
specifically so the browser gets HTTP Range request support - without
that, seeking/scrubbing within a video wouldn't work, and some browsers
won't play a video at all depending on its size. There's no confirmed spec
for exactly how ATAK attaches media to a CI-TRAP report (sibling files in
the zip? a subfolder? referenced by name from inside `report.xml`?), so
this deliberately doesn't try to be clever about matching files to
specific report fields - it just surfaces everything else in the package
by file extension. If real traffic turns out to follow a specific
convention, this is the place to make it smarter.

**HEVC/H.265 detection**: real Android-recorded video attached to a
CI-TRAP report was confirmed (via a real uploaded payload, cross-checked
against `ffprobe`) to commonly use H.265/HEVC encoding in an MP4
container - which Safari plays fine but Chrome and Firefox on desktop
generally can't decode at all without OS-level codec support. This is
detected with a small dependency-free MP4/ISO-BMFF box parser (walks
`moov`/`trak`/`mdia`/`minf`/`stbl`/`stsd` to read the codec fourcc directly
- no ffmpeg needed on the server) rather than trying to transcode video
server-side, which would add real complexity and CPU cost this plugin
doesn't take on. When HEVC is detected, the video player still renders
(it'll just work in Safari), plus a clear warning explaining why it may
not play plus a prominent download link so the video is always genuinely
accessible either way.

**Important: this lives at a different prefix than the Marti protocol
routes, on purpose.** Many OTS deployments' nginx config blocks *all*
`/Marti/*` traffic on the default HTTPS port (443) by design:

```nginx
# Do not allow calls to the Marti API on port 443. Only allow them to port 8443 where
# SSL client certificate verification is enabled
location ~ ^/Marti {
    return 404;
}
```

That's intentional - `/Marti` is reserved for cert-authenticated EUD
traffic on a separate port (typically 8443), not for browser/admin access.
An earlier version of this plugin put the admin UI under
`/Marti/api/citrap/ui`, which meant nginx 404'd it before the request ever
reached Flask, regardless of anything the plugin did correctly. The admin
UI now lives under `{admin_prefix}` (config: `OTS_CITRAP_REPORT_ADMIN_UI_PREFIX`,
default `/api/citrap-reports`) specifically so it's reachable on the normal
port. Confirm this is the case on your own server before assuming default
config value from repository:
`sudo grep -B2 -A5 "location.*Marti" /etc/nginx/sites-enabled/*` - if your
port-443 config doesn't block `/Marti`, you don't strictly need this
separation, but it's still good practice not to mix EUD protocol traffic
and browser admin traffic on the same path namespace.

Clicking a report's id (or "view") opens `GET {admin_prefix}/view/<id>` -
a detail page that parses and displays the report's actual content, not
just a raw download link: title/type/callsign/date/location metadata,
plus a generic recursive rendering of the report's `<section>`/`<list>`/
`<option>` structure (checkbox-style fields shown as checked/unchecked
lists). This isn't hardcoded to the "Campsite Information" report type
seen in testing - it walks whatever section/field structure a given
CI-TRAP report type actually contains. Reports whose payload isn't a
parseable data package zip fall back to metadata-only with a note to use
the raw download instead. Attachments are listed with individual download
links, and the delete action is available from here too.

This is plain server-rendered HTML with no external JS/CSS - it doesn't
depend on any CDN being reachable. It's guarded with Flask-Security's
`@auth_required()`, so you need to be logged into OTS's web UI in the same
browser session to view it (unlike the Marti protocol routes, which
authenticate EUDs via client certificate, not a session - mixing session
auth into those would break EUD report submission entirely, which is the
other reason this is a fully separate Flask blueprint from the Marti
routes rather than just different rules on the same one).

This is **not** the officially-supported "plugin UI iframe" mechanism -
see the section below for that, including a known OTS version-skew issue
that currently blocks it regardless of this plugin. This page works
independent of whether that iframe mechanism ever resolves.

Downloaded files get a best-effort extension/mimetype based on the payload's
magic bytes (`.zip` for the data-package zips real ATAK CI-TRAP reports seem
to actually send, `.xml` for bare CoT, `.bin` otherwise).

## Extending it

Domain-level hooks fire on every mutation, so other code (including other
plugins) can react without touching internals:

```python
@my_plugin_instance.on_report_created
def notify(report): ...

@my_plugin_instance.on_report_updated
def notify(report): ...

@my_plugin_instance.on_report_deleted
def notify(report_id, client_uid): ...

@my_plugin_instance.on_attachment_added
def notify(report, attachment): ...
```

## Acting as a proxy instead

If reports should live in a different, authoritative CI-TRAP store rather
than this plugin's own tables, replace the `db.session` calls in each route
in `app.py` with calls to that system, and use the same hooks to keep any
local cache/notifications in sync.

## Install

**Two real bugs were found and fixed here** (not hypothetical - traced to
OTS's actual source at github.com/brian7704/OpenTAKServer):

1. The entry-point group in `pyproject.toml` was registered as
   `"opentakserver.plugins"` (plural). The real group `opentakserver`'s
   `PluginManager` scans is `"opentakserver.plugin"` (**singular** -
   confirmed from `Plugin.py`'s `group = "opentakserver.plugin"` class
   attribute). A mismatched group means `metadata.entry_points(group=...)`
   finds nothing - the plugin installs as a Python package fine, but OTS
   never sees it.
2. `Plugin` requires four abstract methods: `activate(self, app, enabled)`,
   `stop(self)`, `get_info(self)`, `load_metadata(self)`. This plugin
   previously only implemented `activate(self)` with the wrong signature.
   Missing abstract methods make the class impossible to instantiate
   (`TypeError`), which `PluginManager._load_plugin_entry_point` catches
   and only logs server-side (`opentakserver.log`) - from the web UI it
   just looks like the plugin never showed up, and since it never loaded,
   none of its 6 routes existed, so every EUD report submission 404'd.

Both are fixed now: `pyproject.toml` uses the singular group, and `app.py`
implements the full four-method interface (verified by instantiating the
class against a faithful mock of OTS's real `Plugin`/`BasePlugin` classes,
reconstructed from their actual source).

**Through the OTS web UI:** see `PUBLISHING.md` for how to get this
package onto a pip-compatible index your OTS server can reach, so it
installs from the **Plugins → Install Plugin** screen like any other
plugin — no command line on the server needed. `PUBLISHING.md` also covers
direct zip upload, if your OTS build supports that.

**Quick dev/test install via command line**, if you just want to try it
locally before setting up an index:

```bash
~/.opentakserver_venv/bin/pip install git+https://github.com/your-username/OTS-Citrap-Report-Plugin.git
```

Run OTS's DB migration step for the new tables (`ots_citrap_reports`,
`ots_citrap_attachments`) per your OTS version's plugin-DB convention, then
enable the plugin from the web UI or:

```bash
curl -X POST https://your-ots-server/api/plugins/ots_citrap_report/enable
```

## Configuration

Add to `~/ots/config.yml`:

```yaml
OTS_CITRAP_REPORT_ENABLED: true
OTS_CITRAP_REPORT_URL_PREFIX: "/Marti/api/citrap"
OTS_CITRAP_REPORT_ADMIN_UI_PREFIX: "/api/citrap-reports"
OTS_CITRAP_REPORT_OVERRIDE_SEARCH: false
OTS_CITRAP_REPORT_MAX_RESULTS_CEILING: 1000
OTS_CITRAP_REPORT_DEFAULT_MAX_RESULTS: 100
OTS_CITRAP_REPORT_LOG_EVENTS: true
OTS_CITRAP_REPORT_UI_PAGE_SIZE: 50
```

Note: `OTS_CITRAP_REPORT_URL_PREFIX` and `OTS_CITRAP_REPORT_OVERRIDE_SEARCH`
are read at plugin construction time (not live from `app.config`), so
changes to either require a server restart to take effect.
