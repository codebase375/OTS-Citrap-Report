# tests/test_official_routes.py
# Covers the routes added to wire this plugin into OTS core's own
# Settings-tab / UI-iframe convention (/api/plugins/<distro>/{config,ui}),
# which didn't exist before even though the underlying admin page
# (/api/citrap-reports) and static ui/index.html both already worked fine
# on their own. Also covers the "Reports" rename.

from ots_citrap_report.app import DISTRO_NAME, OtsCitrapReport

PREFIX = f"/api/plugins/{DISTRO_NAME}"


def test_display_name_is_plain_reports():
    plugin = OtsCitrapReport()
    plugin.load_metadata()
    info = plugin.get_info()
    assert info["name"] == "Reports"
    # distro (the installed package/entry-point identity) is deliberately
    # unchanged -- renaming that would be a breaking change to an
    # already-installed plugin, not what was asked for.
    assert info["distro"] == "ots-citrap-report"


def test_project_url_is_always_a_list():
    """Same crash class fixed on ots-federation: OTS core's Plugin.tsx does
    about?.project_url.forEach(...) with no null-check on project_url
    itself. This repo already guarded against it independently; this test
    just locks that in."""
    plugin = OtsCitrapReport()
    meta = plugin.load_metadata()
    assert isinstance(meta.get("project_url"), list)


def test_project_url_includes_both_documentation_and_repository():
    """OTS core's Plugin.tsx (About tab) never resets its docUrl/repoUrl
    React state before re-scanning project_url on each plugin fetch:

        useEffect(() => {
            about?.project_url.forEach((value) => {
                if (value.startsWith("Documentation")) setDocUrl(...)
                else if (value.startsWith("Repository")) setRepoUrl(...)
            })
        }, [about]);

    If this plugin's project_url is missing either label, navigating here
    from a DIFFERENT plugin's About tab (e.g. ots-federation, which sets
    both) leaves that field showing the OTHER plugin's stale URL - reported
    in the wild as "the about page references the URL for the federation
    plugin as well as the Reports plugin's actual URL." That root cause is
    a bug in OpenTAKServer-UI itself (missing state reset) and can't be
    fully fixed from either plugin repo, but always supplying both labels
    here means visiting Reports' About tab is guaranteed to overwrite both
    fields with Reports' own URLs regardless of what was viewed before."""
    plugin = OtsCitrapReport()
    meta = plugin.load_metadata()
    if meta["version"] == "unknown":
        import pytest
        pytest.skip("package not installed in this environment (importlib.metadata can't find it)")
    labels = {url.split(",", 1)[0] for url in meta.get("project_url", [])}
    assert "Documentation" in labels, "missing Documentation entry lets another plugin's doc URL linger"
    assert "Repository" in labels, "missing Repository entry lets another plugin's repo URL linger"


def test_about_description_mentions_only_reports_not_citrap():
    """The About tab's Markdown body (Plugin.tsx: about?.description) must
    stay Reports-only wording. This is deliberately sourced from the short
    package Summary header, not the long Description/README payload -
    README.md is protocol-documentation-heavy (Marti API paths, CI-TRAP
    terminology throughout), and showing that verbatim in the About tab
    would defeat the "Reports"-only display name."""
    plugin = OtsCitrapReport()
    meta = plugin.load_metadata()
    description = meta.get("description", "")
    assert description, "About tab description should not be blank"
    assert "citrap" not in description.lower()
    assert "ci-trap" not in description.lower()
    assert "marti" not in description.lower()


def test_official_routes_are_registered(activated_app):
    app, _plugin = activated_app
    rules = {r.rule for r in app.url_map.iter_rules() if PREFIX in r.rule}
    assert f"{PREFIX}/config" in rules
    assert f"{PREFIX}/ui" in rules


def test_ui_and_config_require_login(client):
    for path in (f"{PREFIX}/ui", f"{PREFIX}/config"):
        r = client.get(path)
        assert r.status_code in (302, 401), f"{path} did not require auth (got {r.status_code})"


def test_config_get_returns_current_settings(logged_in_client):
    r = logged_in_client.get(f"{PREFIX}/config")
    assert r.status_code == 200
    body = r.get_json()
    assert body["OTS_CITRAP_REPORT_URL_PREFIX"] == "/Marti/api/citrap"
    assert body["OTS_CITRAP_REPORT_ADMIN_UI_PREFIX"] == "/api/citrap-reports"


def test_config_post_accepts_valid_change(logged_in_client, activated_app):
    app, _plugin = activated_app
    r = logged_in_client.post(f"{PREFIX}/config", json={"OTS_CITRAP_REPORT_DEBUG": True})
    assert r.status_code == 200
    assert r.get_json() == {"success": True, "error": ""}
    assert app.config["OTS_CITRAP_REPORT_DEBUG"] is True


def test_config_post_rejects_invalid_change(logged_in_client):
    r = logged_in_client.post(f"{PREFIX}/config", json={"OTS_CITRAP_REPORT_MAX_RESULTS_CEILING": -5})
    assert r.status_code == 400
    body = r.get_json()
    assert body["success"] is False
    assert "positive integer" in body["error"]


def test_ui_serves_the_static_reports_page(logged_in_client):
    r = logged_in_client.get(f"{PREFIX}/ui")
    assert r.status_code == 200
    assert r.content_type.startswith("text/html")
    assert b"<title>Reports</title>" in r.data
    # Confirms it's still pointed at the existing, already-working data
    # endpoint rather than something new/untested.
    assert b"/api/citrap-reports/data" in r.data


def test_full_admin_page_also_renamed(logged_in_client):
    """The pre-existing /api/citrap-reports list page (unchanged logic,
    just renamed branding) should say "Reports", not "CI-TRAP Reports"."""
    r = logged_in_client.get("/api/citrap-reports")
    assert r.status_code == 200
    assert b"CI-TRAP Reports" not in r.data
    assert b"<h1>Reports</h1>" in r.data
