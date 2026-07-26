# Config options for OTS-Citrap-Report, prefixed to avoid collisions with
# core OTS settings or other plugins.

# Master on/off switch.
OTS_CITRAP_REPORT_ENABLED = True

# Confirmed against the real TAK Server OpenAPI spec (ci-trap-report-api tag):
#   GET/PUT/DELETE {prefix}/{id}
#   GET  {prefix}            (searchReports)
#   POST {prefix}            (postReport)
#   POST {prefix}/{id}/attachment
OTS_CITRAP_REPORT_URL_PREFIX = "/Marti/api/citrap"

# The human-facing admin browser (list/view/download/delete reports) lives
# at a DIFFERENT prefix than the Marti protocol routes above, deliberately.
# Many OTS deployments' nginx config blocks all /Marti/* traffic on the
# default HTTPS port (443) by design - that path is reserved for cert-
# authenticated EUD traffic on a separate port (typically 8443). Putting
# the admin UI under /Marti/* would make it unreachable from a normal
# browser on port 443 regardless of anything this plugin does correctly.
OTS_CITRAP_REPORT_ADMIN_UI_PREFIX = "/api/citrap-reports"

# IMPORTANT: "GET /Marti/api/citrap" (searchReports) is the exact same path
# and method OTS core already documents as natively supported - and
# empirically confirmed (a real EUD's search returned nothing) to not know
# about this plugin's report data, since core has no way to know this
# plugin's table exists. Default is True: without this, EUD search is
# broken on every install of this plugin. Implemented via a
# before_app_request hook (see app.py) rather than a competing same-path
# route - registering a second Flask route for the identical path does NOT
# work, confirmed against real Werkzeug 3.x: whichever rule was added first
# (core's, since it registers before any plugin loads) always wins the
# match, with no supported way for a plugin to remove or override that from
# outside Werkzeug's internals. before_app_request runs before route
# dispatch for every request regardless of which rule Werkzeug matched, so
# it reliably wins instead - this is no longer a "best effort, might not
# take priority" situation.
OTS_CITRAP_REPORT_OVERRIDE_SEARCH = True

# Cap for maxReportCount on searchReports, even if the caller asks for more.
OTS_CITRAP_REPORT_MAX_RESULTS_CEILING = 1000

# Default number of results for searchReports when maxReportCount is omitted.
OTS_CITRAP_REPORT_DEFAULT_MAX_RESULTS = 100

# Log every CI-TRAP report create/update/delete/attachment at INFO level.
OTS_CITRAP_REPORT_LOG_EVENTS = True

# Verbose diagnostics: logs full request headers and dumps the contents
# of every file inside uploaded payload zips. Invaluable when debugging
# client/wire-format issues; expensive and noisy in normal operation
# (decompresses and logs multi-MB media files on every upload) - leave
# off unless actively troubleshooting.
OTS_CITRAP_REPORT_DEBUG = False

# Rows per page on the /ui report browser (GET {prefix}/ui).
OTS_CITRAP_REPORT_UI_PAGE_SIZE = 50


def validate(config: dict) -> (bool, str):
    """
    Called by OpenTAKServer when the plugin's config is loaded/changed.
    Return (True, "") if valid, otherwise (False, "reason").
    """
    ceiling = config.get(
        "OTS_CITRAP_REPORT_MAX_RESULTS_CEILING", OTS_CITRAP_REPORT_MAX_RESULTS_CEILING
    )
    default = config.get(
        "OTS_CITRAP_REPORT_DEFAULT_MAX_RESULTS", OTS_CITRAP_REPORT_DEFAULT_MAX_RESULTS
    )
    if not isinstance(ceiling, int) or ceiling <= 0:
        return False, "OTS_CITRAP_REPORT_MAX_RESULTS_CEILING must be a positive integer"
    if not isinstance(default, int) or default <= 0:
        return False, "OTS_CITRAP_REPORT_DEFAULT_MAX_RESULTS must be a positive integer"
    if default > ceiling:
        return False, "OTS_CITRAP_REPORT_DEFAULT_MAX_RESULTS cannot exceed the ceiling"

    prefix = config.get("OTS_CITRAP_REPORT_URL_PREFIX", OTS_CITRAP_REPORT_URL_PREFIX)
    if not isinstance(prefix, str) or not prefix.startswith("/"):
        return False, "OTS_CITRAP_REPORT_URL_PREFIX must be a string starting with /"

    admin_prefix = config.get("OTS_CITRAP_REPORT_ADMIN_UI_PREFIX", OTS_CITRAP_REPORT_ADMIN_UI_PREFIX)
    if not isinstance(admin_prefix, str) or not admin_prefix.startswith("/"):
        return False, "OTS_CITRAP_REPORT_ADMIN_UI_PREFIX must be a string starting with /"
    if admin_prefix.startswith("/Marti"):
        return False, "OTS_CITRAP_REPORT_ADMIN_UI_PREFIX must not start with /Marti - that path is blocked on port 443 in most OTS nginx configs"

    return True, ""
