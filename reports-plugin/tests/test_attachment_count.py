# tests/test_attachment_count.py
# Bug report: a report with two photos bundled directly in its submitted
# payload zip (no separate addAttachment calls) showed "Attachments: 0" on
# the list page and the compact iframe page, even though the detail page
# correctly showed both photos in its gallery. Root cause: the count only
# ever queried CitrapAttachment rows (the addAttachment API), never looked
# at files bundled inside the report's own zip -- which is how EUDs
# actually attach photos in practice for many CI-TRAP submissions.

import io
import zipfile

from ots_citrap_report.app import DISTRO_NAME, OtsCitrapReport, _count_payload_attachments
from ots_citrap_report.models import CitrapReport

PREFIX = "/api/citrap-reports"


def _make_payload_with_photos(n_photos: int, report_xml: str = None) -> bytes:
    report_xml = report_xml or '<report type="Campsite" callsign="Alpha1"></report>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("report.xml", report_xml)
        for i in range(n_photos):
            zf.writestr(f"photo{i}.jpg", f"FAKEJPEGDATA{i}".encode())
    return buf.getvalue()


def _activate(app):
    plugin = OtsCitrapReport()
    plugin.activate(app, True)
    app.register_blueprint(plugin.blueprint)
    return plugin


def test_count_payload_attachments_counts_embedded_photos():
    payload = _make_payload_with_photos(2)
    assert _count_payload_attachments(payload) == 2


def test_count_payload_attachments_excludes_report_xml_and_manifest():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("report.xml", '<report type="Campsite"></report>')
        zf.writestr("MANIFEST/manifest.xml", "<manifest/>")
        zf.writestr("photo0.jpg", b"data")
    assert _count_payload_attachments(buf.getvalue()) == 1


def test_count_payload_attachments_empty_zip_is_zero():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("report.xml", '<report type="Campsite"></report>')
    assert _count_payload_attachments(buf.getvalue()) == 0


def test_count_payload_attachments_handles_bad_zip_gracefully():
    assert _count_payload_attachments(b"not a zip file at all") == 0
    assert _count_payload_attachments(b"") == 0


def test_list_page_shows_embedded_photos_not_zero(logged_in_client, activated_app):
    app, _plugin = activated_app
    payload = _make_payload_with_photos(2)

    with app.app_context():
        from opentakserver.extensions import db

        report = CitrapReport(client_uid="test-eud-1", payload=payload)
        report.type = "Campsite"
        report.callsign = "Alpha1"
        db.session.add(report)
        db.session.commit()

    r = logged_in_client.get(PREFIX)
    assert r.status_code == 200
    # The row's Attachments cell should show 2, not 0 -- this was the bug.
    assert b">2<" in r.data or b">2</td>" in r.data


def test_ui_data_json_reflects_embedded_photos(logged_in_client, activated_app):
    app, _plugin = activated_app
    payload = _make_payload_with_photos(3)

    with app.app_context():
        from opentakserver.extensions import db

        report = CitrapReport(client_uid="test-eud-2", payload=payload)
        db.session.add(report)
        db.session.commit()

    r = logged_in_client.get(f"{PREFIX}/data")
    assert r.status_code == 200
    body = r.get_json()
    matches = [rep for rep in body["reports"] if rep["clientUid"] == "test-eud-2"]
    assert len(matches) == 1
    assert matches[0]["attachmentCount"] == 3


def test_combines_real_attachments_with_embedded_photos(activated_app):
    """A report could plausibly have BOTH: photos bundled in its own zip
    AND separate files added via the addAttachment API. Total should be
    the sum of both, not just one or the other."""
    from ots_citrap_report.models import CitrapAttachment

    app, _plugin = activated_app
    payload = _make_payload_with_photos(2)

    with app.app_context():
        from opentakserver.extensions import db

        report = CitrapReport(client_uid="test-eud-3", payload=payload)
        db.session.add(report)
        db.session.commit()

        attachment = CitrapAttachment(report_id=report.id, client_uid="test-eud-3", data=b"extra-file-data")
        db.session.add(attachment)
        db.session.commit()

        total = report.attachments.count() + _count_payload_attachments(report.payload)
        assert total == 3
