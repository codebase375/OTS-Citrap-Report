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
# and method OTS core already documents as natively supported. Werkzeug
# won't error on the duplicate rule, but whichever view got registered
# first (core's, since it loads before plugins) wins - this plugin's
# searchReports route would silently never run. Default to NOT registering
# it, so core's existing behavior is untouched, and only add the 5
# operations core doesn't implement at all (get/put/delete/post/attachment).
# Set this True only if you've confirmed (e.g. by testing) that your OTS
# version's core citrap route is a stub/no-op you actually want overridden -
# and even then, whether the override wins depends on plugin/blueprint
# registration order in PluginManager, which isn't guaranteed.
OTS_CITRAP_REPORT_OVERRIDE_SEARCH = False

# Cap for maxReportCount on searchReports, even if the caller asks for more.
OTS_CITRAP_REPORT_MAX_RESULTS_CEILING = 1000

# Default number of results for searchReports when maxReportCount is omitted.
OTS_CITRAP_REPORT_DEFAULT_MAX_RESULTS = 100

# Log every CI-TRAP report create/update/delete/attachment at INFO level.
OTS_CITRAP_REPORT_LOG_EVENTS = True

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
