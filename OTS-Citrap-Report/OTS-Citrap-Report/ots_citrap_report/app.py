"""
OTS-Citrap-Report

Implements the CI-TRAP Report API (tag "ci-trap-report-api" in TAK Server's
OpenAPI spec) as an OpenTAKServer plugin:

    GET    /Marti/api/citrap/{id}             getReport
    PUT    /Marti/api/citrap/{id}             putReport
    DELETE /Marti/api/citrap/{id}             deleteReport
    GET    /Marti/api/citrap                  searchReports
    POST   /Marti/api/citrap                  postReport
    POST   /Marti/api/citrap/{id}/attachment  addAttachment

Confirmed against the real spec (paths, methods, param requiredness,
response codes) - see README for the couple of things the spec leaves
genuinely ambiguous (the "*/*" / bare "string" response bodies don't say
what shape the payload takes beyond "opaque blob").

Known collision with OTS core, and how it's actually resolved
----------------------------------------------------------------
"GET /Marti/api/citrap" (searchReports) is the exact same path+method OTS
core already documents as natively supported, and core's implementation
has no knowledge of this plugin's report data (confirmed: a real EUD's
search returned nothing until this override was in place). Registering a
second Flask route for the identical path does NOT resolve this -
confirmed against real Werkzeug 3.x, whichever rule was added to the URL
map first (core's, since it registers before any plugin loads) always
wins the match; there's no supported way for a plugin to remove or
reorder that from outside Werkzeug's internals (its StateMachineMatcher
builds an internal tree once and doesn't rebuild from the rules list on
demand).

What actually works: `before_app_request` runs before route dispatch for
every request, regardless of which rule Werkzeug matched - returning a
Response from it short-circuits Flask entirely before core's view
function ever runs. See `_register_search_override` below.
OTS_CITRAP_REPORT_OVERRIDE_SEARCH (default True) controls whether this
hook is installed; set it False only if a future OTS version implements
real searchReports behavior you'd rather defer to.

Ownership / clientUid
----------------------
Per the spec, clientUid is REQUIRED on getReport/putReport/deleteReport/
postReport/addAttachment, but OPTIONAL on searchReports. This plugin
enforces clientUid-as-owner on the required-clientUid operations (a report
can only be fetched/replaced/deleted by the clientUid that created it) and
treats searchReports' clientUid as an optional filter, not a hard scope -
if omitted, search runs across all reports. If your deployment's real
semantics differ (e.g. any authenticated client can read any report),
loosen `_check_owner` below.

Response bodies
----------------
The spec models every response as a bare "string" with content-type "*/*" -
i.e. opaque, not a defined JSON object. This plugin passes payload bytes
through mostly untouched instead of wrapping them in JSON metadata:

    getReport            -> raw stored bytes, as originally submitted
    putReport             -> raw bytes now stored (echoed back)
    deleteReport          -> empty body
    postReport            -> the new report's id, as plain text
    addAttachment         -> the new attachment's id, as plain text
    searchReports         -> a JSON array (of report summaries) encoded as
                             the response string - "a string" is still
                             satisfied literally, and this is far more
                             usable than any other reading of "string"
                             for a search result. Adjust if you find out
                             otherwise.
"""

import base64
import importlib.metadata
import io
import json
import re
import traceback
import xml.etree.ElementTree as ET
import zipfile

from flask import Blueprint, request, Response, send_file, current_app as app

from opentakserver.plugins.Plugin import Plugin
from opentakserver.extensions import db, logger

from .models import CitrapReport, CitrapAttachment
from .default_config import (
    OTS_CITRAP_REPORT_URL_PREFIX,
    OTS_CITRAP_REPORT_OVERRIDE_SEARCH,
    OTS_CITRAP_REPORT_ADMIN_UI_PREFIX,
)

# Must match the "name" key on the left of the pyproject.toml entry point
# line under [tool.poetry.plugins."opentakserver.plugin"], and DISTRO must
# match [tool.poetry] name - both are used for metadata/version lookups.
# DISTRO_NAME MUST be the PEP 503-normalized (lowercase) form, matching
# exactly what pip/importlib.metadata report the installed distribution
# as - PluginManager's internal plugin registry is keyed by that
# normalized name, and if self.distro (shown in the admin UI, and echoed
# back on enable/disable/uninstall) doesn't match it exactly, those
# actions fail with a KeyError even though the plugin loaded fine.
PLUGIN_NAME = "ots_citrap_report"
DISTRO_NAME = "ots-citrap-report"


def _debug_enabled() -> bool:
    try:
        return bool(app.config.get("OTS_CITRAP_REPORT_DEBUG", False))
    except RuntimeError:
        # No app context (e.g. called from a test harness) - stay quiet.
        return False


def _decode_byte_body(req, context: str = "") -> bytes:
    """
    putReport/postReport/addAttachment all declare requestBody
    application/json, schema {type: string, format: byte} in the OpenAPI
    spec - but real ATAK traffic (confirmed on the wire) sends raw
    application/x-zip-compressed bytes instead, which lands in the
    req.data branch below. The JSON branches are kept for spec-compliant
    clients, should any exist.

    Verbose wire-format diagnostics (headers, branch taken) only log when
    OTS_CITRAP_REPORT_DEBUG is enabled - they were essential for nailing
    down the real payload format initially, but are noise in normal
    operation.
    """
    if _debug_enabled():
        logger.info(
            f"ots_citrap_report[{context}]: Content-Type={req.content_type!r} "
            f"Content-Length={req.content_length!r} Accept={req.headers.get('Accept')!r} "
            f"User-Agent={req.headers.get('User-Agent')!r} "
            f"all_headers={dict(req.headers)!r}"
        )

    body = req.get_json(silent=True)
    if isinstance(body, str):
        if _debug_enabled():
            logger.info(f"ots_citrap_report[{context}]: parsed as JSON string, len={len(body)}")
        raw = body
    elif isinstance(body, dict) and "data" in body:
        if _debug_enabled():
            logger.info(f"ots_citrap_report[{context}]: parsed as JSON dict with 'data' key")
        raw = body["data"]
    elif req.data:
        if _debug_enabled():
            logger.info(
                f"ots_citrap_report[{context}]: not valid JSON - using raw request body, "
                f"{len(req.data)} bytes, first 16 bytes={req.data[:16]!r}"
            )
        return req.data
    else:
        raise ValueError("Request body must be a base64-encoded JSON string")

    try:
        return base64.b64decode(raw)
    except Exception as e:
        raise ValueError(f"Request body is not valid base64: {e}")


def _parse_iso(value):
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _apply_metadata_to_report(report, metadata: dict):
    """Copy whichever searchable/sortable fields were successfully
    extracted from the payload onto the report row. Deliberately does NOT
    touch report.id - id semantics differ per route (postReport reuses
    the payload's embedded id; putReport's id is URL-authoritative)."""
    if "type" in metadata:
        report.type = metadata["type"]
    if "callsign" in metadata:
        report.callsign = metadata["callsign"]
    if "title" in metadata:
        report.title = metadata["title"]
    if "importance" in metadata:
        report.importance = metadata["importance"]
    if "report_datetime" in metadata:
        report.report_datetime = metadata["report_datetime"]
    if "lat" in metadata and "lon" in metadata:
        report.lat = metadata["lat"]
        report.lon = metadata["lon"]


def _log_zip_contents(payload: bytes, context: str = ""):
    """
    TEMPORARY diagnostic: real CI-TRAP payloads are zip data packages, not
    bare CoT. ATAK data packages conventionally embed a MANIFEST.xml with
    a client-generated uid - if the client is tracking that uid and
    expects our response to echo it back (rather than a new server-
    generated id), that would explain why report creation succeeds
    server-side but the client never recognizes it as successful. This
    logs the zip's contents and any manifest/CoT-looking file so we can
    see definitively rather than guess further. Remove once resolved.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = zf.namelist()
            logger.info(f"ots_citrap_report[{context}]: zip contains {len(names)} entries: {names}")

            for info in zf.infolist():
                if info.is_dir():
                    continue
                try:
                    content = zf.read(info.filename).decode("utf-8", errors="replace")
                except Exception as e:
                    logger.info(f"ots_citrap_report[{context}]: could not read {info.filename}: {e}")
                    continue
                truncated = content if len(content) <= 2000 else content[:2000] + "...(truncated)"
                logger.info(f"ots_citrap_report[{context}]: contents of {info.filename}:\n{truncated}")
    except zipfile.BadZipFile:
        logger.info(f"ots_citrap_report[{context}]: payload is not a valid zip (or not a zip at all)")
    except Exception:
        logger.error(f"ots_citrap_report[{context}]: error inspecting zip contents")
        logger.debug(traceback.format_exc())


_WKT_POINT_RE = re.compile(r"POINT\s*\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", re.IGNORECASE)


def _extract_report_metadata_from_zip(payload: bytes) -> dict:
    """
    Real CI-TRAP payloads are ATAK Mission Package zips containing a
    <report ... id='...' type='...' userCallsign='...' location='POINT
    (lon lat)' ...> XML file. Extract what we can:

        id             - the client-assigned report id. If we generate our
                         own unrelated id instead of reusing this, the
                         client can never correlate our response with its
                         local report and will report "failed to post"
                         even on a 200 response.
        type           - e.g. "Campsite Information" - maps directly to
                         our searchable `type` field.
        callsign       - from the `userCallsign` attribute.
        title          - from the `title` attribute.
        importance     - from the `importance` attribute (e.g. "Routine").
        report_datetime - from the `dateTime` attribute (when the EUD
                         authored/submitted the report), distinct from
                         our own created_at (when we received it).
        lat/lon        - from `location`, WKT "POINT (lon lat)" format -
                         NOT CoT's <point lat= lon=> as originally assumed
                         before real traffic showed the actual shape.

    Returns a dict with whichever keys were found and parseable; missing
    fields are simply absent so callers can dict.get(...) with their own
    fallback/no-op behavior.
    """
    metadata = {}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".xml"):
                    continue
                if "manifest" in info.filename.lower():
                    continue
                try:
                    root = ET.fromstring(zf.read(info.filename))
                except ET.ParseError:
                    continue
                if root.tag != "report":
                    continue

                if root.get("id"):
                    metadata["id"] = root.get("id")
                if root.get("type"):
                    metadata["type"] = root.get("type")
                if root.get("userCallsign"):
                    metadata["callsign"] = root.get("userCallsign")
                if root.get("title"):
                    metadata["title"] = root.get("title")
                if root.get("importance"):
                    metadata["importance"] = root.get("importance")

                report_dt = _parse_iso(root.get("dateTime"))
                if report_dt:
                    metadata["report_datetime"] = report_dt

                location = root.get("location")
                if location:
                    m = _WKT_POINT_RE.search(location)
                    if m:
                        lon, lat = float(m.group(1)), float(m.group(2))
                        metadata["lat"] = lat
                        metadata["lon"] = lon
                break  # only need the first non-manifest report XML
    except zipfile.BadZipFile:
        pass
    except Exception:
        logger.debug("ots_citrap_report: error extracting report metadata from zip", exc_info=True)
    return metadata


def _extract_report_xml_element(payload: bytes):
    """
    Like _extract_report_metadata_from_zip, but returns the full parsed
    <report> XML element (not just a handful of scalar fields), so a
    detail/view page can render the actual section/list/option content -
    e.g. the "Campsite Type"/"Campsite Features" checklists seen in real
    traffic - generically, without hardcoding field names specific to one
    report type. Returns None if the payload isn't a parseable zip
    containing a <report> root element.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".xml"):
                    continue
                if "manifest" in info.filename.lower():
                    continue
                try:
                    root = ET.fromstring(zf.read(info.filename))
                except ET.ParseError:
                    continue
                if root.tag == "report":
                    return root
    except zipfile.BadZipFile:
        return None
    except Exception:
        logger.debug("ots_citrap_report: error parsing report xml from zip", exc_info=True)
        return None
    return None


def _extract_raw_report_attrs(payload: bytes):
    """
    Returns the <report> root element's own attributes as a plain dict -
    id, type, title, userCallsign, dateTime, location, importance,
    status, etc, using ATAK's OWN attribute names exactly as it wrote
    them into report.xml. Used for searchReports: a client's own JSON
    deserializer is far more likely to recognize its own native field
    names than any renamed schema a server might invent. Returns None if
    the payload isn't a parseable data package zip.
    """
    root = _extract_report_xml_element(payload)
    if root is None:
        return None
    return dict(root.attrib)


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".3gp"}


def _image_mimetype(filename: str):
    parts = filename.lower().rsplit(".", 1)
    if len(parts) != 2 or f".{parts[1]}" not in _IMAGE_EXTENSIONS:
        return None
    ext = parts[1]
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "bmp": "image/bmp",
        "webp": "image/webp",
        "heic": "image/heic",
        "heif": "image/heif",
    }.get(ext, "application/octet-stream")


def _mp4_find_codec_fourccs(data: bytes) -> list:
    """
    Best-effort, dependency-free parse of an MP4/ISO-BMFF container's box
    structure to find codec fourccs from stsd boxes (e.g. 'avc1' for
    H.264, 'hev1'/'hvc1' for HEVC/H.265, 'mp4a' for AAC audio). No ffmpeg
    needed - just walks the box tree looking for stsd, descending only
    into known container boxes so it doesn't try to parse actual media
    data (mdat). Returns whatever fourccs it finds; empty list if the
    data isn't a parseable ISO-BMFF container or has no stsd boxes.
    """
    fourccs = []
    container_boxes = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts", b"dinf"}

    def walk(buf: bytes, start: int, end: int, depth: int):
        if depth > 12:  # sanity bound, real files never nest this deep
            return
        pos = start
        while pos + 8 <= end:
            size = int.from_bytes(buf[pos : pos + 4], "big")
            box_type = buf[pos + 4 : pos + 8]
            header_len = 8
            if size == 1:
                if pos + 16 > end:
                    break
                size = int.from_bytes(buf[pos + 8 : pos + 16], "big")
                header_len = 16
            elif size == 0:
                size = end - pos

            if size < header_len:
                break
            box_end = min(pos + size, end)

            if box_type == b"stsd":
                p = pos + header_len + 8  # skip version/flags(4) + entry_count(4)
                if p + 8 <= box_end:
                    fourcc = buf[p + 4 : p + 8]
                    try:
                        fourccs.append(fourcc.decode("ascii", errors="replace"))
                    except Exception:
                        pass
            elif box_type in container_boxes:
                walk(buf, pos + header_len, box_end, depth + 1)

            pos = box_end
            if size <= 0:
                break

    try:
        walk(data, 0, len(data), 0)
    except Exception:
        logger.debug("ots_citrap_report: error parsing MP4 box structure", exc_info=True)
    return fourccs


_HEVC_FOURCCS = {"hev1", "hvc1"}


def _is_likely_hevc(data: bytes) -> bool:
    """
    HEVC/H.265 in an MP4 container plays fine in Safari but generally
    doesn't decode at all in Chrome/Firefox on desktop without OS-level
    codec support - common for phone-recorded video, since it's the
    default on many Android/iOS cameras for storage efficiency. Detecting
    this lets the detail page show a clear explanation + download
    fallback instead of just a silently broken player.
    """
    return bool(_HEVC_FOURCCS & set(_mp4_find_codec_fourccs(data)))


def _video_mimetype(filename: str):
    parts = filename.lower().rsplit(".", 1)
    if len(parts) != 2 or f".{parts[1]}" not in _VIDEO_EXTENSIONS:
        return None
    ext = parts[1]
    return {
        "mp4": "video/mp4",
        "mov": "video/quicktime",
        "m4v": "video/x-m4v",
        "webm": "video/webm",
        "avi": "video/x-msvideo",
        "mkv": "video/x-matroska",
        "3gp": "video/3gpp",
    }.get(ext, "application/octet-stream")


def _find_report_xml_filename(payload: bytes):
    """Same search as _extract_report_xml_element, but returns the zip
    entry's filename instead of the parsed element, so callers can
    exclude it from a general file listing."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for info in zf.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".xml"):
                    continue
                if "manifest" in info.filename.lower():
                    continue
                try:
                    root = ET.fromstring(zf.read(info.filename))
                except ET.ParseError:
                    continue
                if root.tag == "report":
                    return info.filename
    except zipfile.BadZipFile:
        return None
    except Exception:
        return None
    return None


def _list_extra_zip_files(payload: bytes, report_xml_filename=None):
    """
    List every non-manifest, non-report-xml file in the payload zip -
    photos or other attachments a real data package might include. We
    don't know the exact convention ATAK uses to reference these from
    report.xml (sibling files, a subfolder, referenced by name inside the
    XML, etc.), so this deliberately doesn't try to be clever about
    matching them to specific fields - it just shows everything else in
    the package. Returns a list of zip entry filenames.
    """
    files = []
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if "manifest" in info.filename.lower():
                    continue
                if report_xml_filename and info.filename == report_xml_filename:
                    continue
                files.append(info.filename)
    except zipfile.BadZipFile:
        pass
    except Exception:
        logger.debug("ots_citrap_report: error listing zip files", exc_info=True)
    return files


def _read_zip_file(payload: bytes, filename: str):
    """Extract one specific file's raw bytes from the payload zip by its
    exact entry name. Returns None if the zip is unreadable or the file
    isn't present - deliberately exact-match only (no path traversal
    normalization games) since filenames come from our own zip listing,
    not directly from user input."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            if filename not in zf.namelist():
                return None
            return zf.read(filename)
    except zipfile.BadZipFile:
        return None
    except Exception:
        logger.debug(f"ots_citrap_report: error reading {filename} from zip", exc_info=True)
        return None


# Root-level <report> attributes worth surfacing in the metadata card,
# in display order. Anything else on the root tag is skipped rather than
# dumped raw, to keep the card readable.
_REPORT_METADATA_ATTRS = [
    ("title", "Title"),
    ("type", "Type"),
    ("userCallsign", "Callsign"),
    ("dateTime", "Date/Time"),
    ("location", "Location"),
    ("importance", "Importance"),
    ("status", "Status"),
    ("userDescription", "Description"),
]


def _render_report_element_html(elem) -> str:
    """
    Generic recursive renderer for a <report> element's children -
    doesn't hardcode field names, so it should reasonably render whatever
    shape a given CI-TRAP report type's <section>/<list>/<option> (or
    other) structure takes, not just the "Campsite Information" example
    seen in testing.
    """
    parts = []
    for child in elem:
        title = child.get("title") or child.tag
        if child.tag == "list":
            options = []
            for opt in child.findall("option"):
                selected = opt.get("selected") == "true"
                opt_title = _html_escape(opt.get("title") or opt.get("value") or "")
                mark = "&#9745;" if selected else "&#9744;"  # checked/unchecked box
                cls = "opt-selected" if selected else "opt-unselected"
                options.append(f'<li class="{cls}">{mark} {opt_title}</li>')
            parts.append(
                f'<div class="field"><div class="field-title">{_html_escape(title)}</div>'
                f'<ul class="option-list">{"".join(options)}</ul></div>'
            )
        elif len(child):
            # Has its own children (e.g. nested <section>) - recurse.
            parts.append(
                f'<div class="subsection"><div class="field-title">{_html_escape(title)}</div>'
                f'{_render_report_element_html(child)}</div>'
            )
        else:
            value = child.get("value") or (child.text or "").strip()
            if not value and not child.attrib:
                continue
            parts.append(
                f'<div class="field"><div class="field-title">{_html_escape(title)}</div>'
                f'<div class="field-value">{_html_escape(value) or "&mdash;"}</div></div>'
            )
    return "\n".join(parts)


# Shared across both the list and detail pages. Reads OTS's own dark/light
# preference from localStorage (Mantine's default persistence key -
# OTS-UI is built on Mantine per its own docs) so this plugin's pages
# match whatever theme the admin already has set in OTS's own UI, rather
# than always rendering light. Falls back to the OS-level
# prefers-color-scheme if that key isn't set (e.g. the user never
# explicitly chose a theme, or OTS-UI customized the storage key to
# something we don't know). Runs as early as possible in <head>, before
# anything paints, so there's no flash of the wrong theme.
_THEME_DETECT_SCRIPT = """<script>
(function() {
  try {
    var v = localStorage.getItem('mantine-color-scheme');
    var isDark = v === 'dark' || (v !== 'light' &&
      window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    if (isDark) document.documentElement.setAttribute('data-theme', 'dark');
  } catch (e) {}
})();
</script>"""

_THEME_CSS_VARS = """:root {
  --bg: #f7f8fa;
  --card-bg: #ffffff;
  --border: #eaeaea;
  --text: #1a1a2e;
  --muted: #666;
  --accent: #2b6cb0;
  --th-bg: #f0f1f5;
  --th-sorted-bg: #e4e9f5;
  --row-hover: #fafbff;
  --media-placeholder-bg: #f0f1f5;
  --warning-bg: #fef3c7;
  --warning-border: #fde68a;
  --warning-text: #92400e;
}
[data-theme="dark"] {
  --bg: #1a1b1e;
  --card-bg: #25262b;
  --border: #373a40;
  --text: #c1c2c5;
  --muted: #909296;
  --accent: #4dabf7;
  --th-bg: #2c2e33;
  --th-sorted-bg: #1f2733;
  --row-hover: #2c2e33;
  --media-placeholder-bg: #2c2e33;
  --warning-bg: #45330a;
  --warning-border: #78350f;
  --warning-text: #fcd34d;
}"""


def _render_report_detail_html(report, attachments, prefix: str, csrf_token: str) -> str:
    root = _extract_report_xml_element(report.payload)

    if root is not None:
        meta_rows = "".join(
            f'<tr><th>{label}</th><td>{_html_escape(root.get(attr)) or "&mdash;"}</td></tr>'
            for attr, label in _REPORT_METADATA_ATTRS
            if root.get(attr)
        )
        body_html = _render_report_element_html(root)
        content_html = f"""
        <div class="card">
          <table class="meta-table">{meta_rows}</table>
        </div>
        <div class="card content">{body_html or '<p class="empty">No additional content.</p>'}</div>
        """
        report_xml_filename = _find_report_xml_filename(report.payload)
    else:
        content_html = (
            '<div class="card"><p class="empty">'
            "This report's payload isn't a parseable CI-TRAP data package zip - "
            "showing raw metadata only. Use \"Download raw payload\" below to inspect it directly."
            "</p></div>"
        )
        report_xml_filename = None

    # Photos or other files bundled in the payload zip alongside
    # report.xml - we don't know the exact convention ATAK uses to
    # reference these (sibling files, subfolder, referenced by name
    # inside the XML), so this just shows everything else in the
    # package: images/videos as inline media, everything else as a
    # download link.
    from urllib.parse import quote

    extra_files = _list_extra_zip_files(report.payload, report_xml_filename)
    image_files = [f for f in extra_files if _image_mimetype(f)]
    video_files = [f for f in extra_files if _video_mimetype(f)]
    other_files = [f for f in extra_files if not _image_mimetype(f) and not _video_mimetype(f)]

    files_html = ""
    if image_files:
        thumbs = "".join(
            f'<a href="{prefix}/view/{report.id}/file/{quote(f, safe="")}" target="_blank" class="thumb">'
            f'<img src="{prefix}/view/{report.id}/file/{quote(f, safe="")}" alt="{_html_escape(f)}" loading="lazy">'
            f'<span class="thumb-label">{_html_escape(f.rsplit("/", 1)[-1])}</span></a>'
            for f in image_files
        )
        files_html += f'<div class="card"><div class="field-title">Photos</div><div class="gallery">{thumbs}</div></div>'
    if video_files:
        def render_video_item(f):
            url = f'{prefix}/view/{report.id}/file/{quote(f, safe="")}'
            hevc_warning = ""
            try:
                data = _read_zip_file(report.payload, f)
                if data and _is_likely_hevc(data):
                    hevc_warning = (
                        '<div class="codec-warning">This video uses H.265/HEVC encoding, '
                        "which many browsers (Chrome, Firefox) can't play directly - "
                        "Safari usually can. If it doesn't play above, "
                        f'<a href="{url}">download it</a> and open it in a compatible player (e.g. VLC).</div>'
                    )
            except Exception:
                pass  # codec detection is best-effort; never block rendering on it

            return (
                f'<div class="video-item">'
                f'<video controls preload="metadata" src="{url}"></video>'
                f'<span class="thumb-label">{_html_escape(f.rsplit("/", 1)[-1])} '
                f'&middot; <a href="{url}">download</a></span>'
                f'{hevc_warning}'
                f"</div>"
            )

        players = "".join(render_video_item(f) for f in video_files)
        files_html += f'<div class="card"><div class="field-title">Videos</div><div class="gallery">{players}</div></div>'
    if other_files:
        other_rows = "".join(
            f'<tr><td>{_html_escape(f.rsplit("/", 1)[-1])}</td>'
            f'<td><a href="{prefix}/view/{report.id}/file/{quote(f, safe="")}">download</a></td></tr>'
            for f in other_files
        )
        files_html += (
            f'<div class="card"><div class="field-title">Other files</div>'
            f'<table><tbody>{other_rows}</tbody></table></div>'
        )

    attachment_rows = "".join(
        f'<tr><td>{a.id}</td><td>{a.created_at.isoformat(sep=" ", timespec="seconds") if a.created_at else "&mdash;"}</td>'
        f'<td><a href="{prefix}/download/{report.id}/attachment/{a.id}">download</a></td></tr>'
        for a in attachments
    )
    attachments_html = (
        f'<div class="card"><table><thead><tr><th>ID</th><th>Uploaded</th><th></th></tr></thead>'
        f'<tbody>{attachment_rows}</tbody></table></div>'
        if attachments
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
{_THEME_DETECT_SCRIPT}
<title>Report {_html_escape(report.id)}</title>
<style>
{_THEME_CSS_VARS}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; color: var(--text); background: var(--bg); max-width: 900px; }}
  h1 {{ font-size: 1.3rem; margin-bottom: 0.25rem; }}
  .subtitle {{ color: var(--muted); margin-bottom: 1.5rem; font-size: 0.85rem; font-family: ui-monospace, Consolas, monospace; }}
  .actions {{ margin-bottom: 1.5rem; display: flex; gap: 1rem; }}
  .actions a, .actions button {{ color: var(--accent); text-decoration: none; font-size: 0.88rem; background: none; border: none; padding: 0; cursor: pointer; font-family: inherit; }}
  .actions a:hover, .actions button:hover {{ text-decoration: underline; }}
  .card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); padding: 1rem 1.25rem; margin-bottom: 1rem; }}
  .card.content {{ display: flex; flex-direction: column; gap: 1rem; }}
  table.meta-table th {{ text-align: left; color: var(--muted); font-weight: 500; padding: 0.3rem 1rem 0.3rem 0; vertical-align: top; width: 120px; font-size: 0.85rem; }}
  table.meta-table td {{ padding: 0.3rem 0; font-size: 0.9rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 0.4rem 0.6rem; font-size: 0.85rem; border-bottom: 1px solid var(--border); }}
  .field-title {{ font-weight: 600; font-size: 0.85rem; margin-bottom: 0.35rem; }}
  .field {{ margin-bottom: 0.5rem; }}
  .field-value {{ font-size: 0.9rem; }}
  .subsection {{ border-left: 3px solid var(--border); padding-left: 1rem; }}
  .option-list {{ list-style: none; margin: 0; padding: 0; font-size: 0.88rem; }}
  .option-list li {{ padding: 0.15rem 0; }}
  .opt-selected {{ font-weight: 600; }}
  .opt-unselected {{ color: var(--muted); }}
  .empty {{ color: var(--muted); margin: 0; }}
  a.back {{ color: var(--accent); text-decoration: none; font-size: 0.85rem; }}
  .gallery {{ display: flex; flex-wrap: wrap; gap: 0.75rem; margin-top: 0.5rem; }}
  .thumb {{ display: flex; flex-direction: column; align-items: center; width: 140px; text-decoration: none; color: inherit; }}
  .thumb img {{ width: 140px; height: 140px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border); background: var(--media-placeholder-bg); }}
  .thumb-label {{ font-size: 0.75rem; color: var(--muted); margin-top: 0.25rem; text-align: center; word-break: break-all; }}
  .thumb:hover img {{ opacity: 0.85; }}
  .video-item {{ display: flex; flex-direction: column; align-items: center; width: 320px; }}
  .video-item video {{ width: 320px; border-radius: 6px; border: 1px solid var(--border); background: #000; }}
  .codec-warning {{ font-size: 0.78rem; color: var(--warning-text); background: var(--warning-bg); border: 1px solid var(--warning-border); border-radius: 6px; padding: 0.5rem 0.65rem; margin-top: 0.5rem; text-align: left; }}
  .codec-warning a {{ color: var(--warning-text); font-weight: 600; }}
</style>
</head>
<body>
  <a class="back" href="{prefix}">&laquo; back to reports</a>
  <h1>{_html_escape((root.get("title") if root is not None else None) or "Report")}</h1>
  <div class="subtitle">{_html_escape(report.id)} &middot; {_html_escape(report.client_uid)}</div>

  <div class="actions">
    <a href="{prefix}/download/{report.id}">Download raw payload</a>
    <form method="post" action="{prefix}/delete/{report.id}" onsubmit="return confirm('Delete this report? This cannot be undone.');">
      <input type="hidden" name="csrf_token" value="{_html_escape(csrf_token)}">
      <button type="submit">Delete report</button>
    </form>
  </div>

  {content_html}

  {files_html}

  {f'<h2 style="font-size:1rem">Attachments</h2>{attachments_html}' if attachments else ''}
</body>
</html>"""


def _sniff_filename_and_type(base_name: str, data: bytes):
    """
    Real CI-TRAP payloads observed in practice are data-package zips
    (magic bytes PK\\x03\\x04), not bare CoT XML as originally assumed -
    sniff a few common cases so downloads get a sensible name/extension
    instead of a bare blob.
    """
    if not data:
        return f"{base_name}.bin", "application/octet-stream"
    if data[:4] == b"PK\x03\x04":
        return f"{base_name}.zip", "application/zip"
    stripped = data.lstrip()
    if stripped[:1] == b"<":
        return f"{base_name}.xml", "application/xml"
    return f"{base_name}.bin", "application/octet-stream"


def _html_escape(value) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_SORTABLE_COLUMNS = {
    # query-param sort key -> (SQLAlchemy column, display label)
    "datetime": ("report_datetime", "Date/Time"),
    "callsign": ("callsign", "Callsign"),
    "title": ("title", "Title"),
    "type": ("type", "Type"),
    "importance": ("importance", "Importance"),
}


def _render_report_list_html(
    reports, page: int, total_pages: int, total: int, prefix: str, sort: str, order: str, csrf_token: str
) -> str:
    rows = []
    for r in reports:
        latlon = f"{r.lat:.5f}, {r.lon:.5f}" if r.lat is not None and r.lon is not None else "—"
        attachment_count = r.attachments.count()
        when = r.report_datetime or r.created_at
        rows.append(
            f"""<tr>
  <td>{_html_escape(when.isoformat(sep=" ", timespec="seconds")) if when else "—"}</td>
  <td>{_html_escape(r.callsign) or "—"}</td>
  <td><a href="{prefix}/view/{r.id}">{_html_escape(r.title) or "(untitled)"}</a></td>
  <td>{_html_escape(r.type) or "—"}</td>
  <td>{_html_escape(r.importance) or "—"}</td>
  <td>{_html_escape(latlon)}</td>
  <td>{attachment_count}</td>
  <td class="actions">
    <a href="{prefix}/view/{r.id}">view</a>
    <a href="{prefix}/download/{r.id}">download</a>
    <form method="post" action="{prefix}/delete/{r.id}" onsubmit="return confirm('Delete this report? This cannot be undone.');" style="display:inline">
      <input type="hidden" name="csrf_token" value="{_html_escape(csrf_token)}">
      <button type="submit" class="link-btn">delete</button>
    </form>
  </td>
</tr>"""
        )

    rows_html = "\n".join(rows) if rows else '<tr><td colspan="8" class="empty">No reports yet.</td></tr>'

    prev_link = (
        f'<a href="?page={page - 1}&sort={sort}&order={order}">&laquo; prev</a>'
        if page > 1
        else '<span class="disabled">&laquo; prev</span>'
    )
    next_link = (
        f'<a href="?page={page + 1}&sort={sort}&order={order}">next &raquo;</a>'
        if page < total_pages
        else '<span class="disabled">next &raquo;</span>'
    )

    def sort_header(key, label):
        is_active = sort == key
        next_order = "asc" if (is_active and order == "desc") else "desc"
        arrow = (" &#9650;" if order == "asc" else " &#9660;") if is_active else ""
        cls = ' class="sorted"' if is_active else ""
        return f'<th{cls}><a href="?page=1&sort={key}&order={next_order}">{label}{arrow}</a></th>'

    header_cells = "".join(sort_header(key, label) for key, (_col, label) in _SORTABLE_COLUMNS.items())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
{_THEME_DETECT_SCRIPT}
<title>CI-TRAP Reports</title>
<style>
{_THEME_CSS_VARS}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; color: var(--text); background: var(--bg); }}
  h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
  .subtitle {{ color: var(--muted); margin-bottom: 1.5rem; font-size: 0.9rem; }}
  table {{ border-collapse: collapse; width: 100%; background: var(--card-bg); box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  th, td {{ text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); font-size: 0.85rem; }}
  th {{ background: var(--th-bg); font-weight: 600; }}
  th a {{ color: var(--text); text-decoration: none; }}
  th a:hover {{ text-decoration: underline; }}
  th.sorted {{ background: var(--th-sorted-bg); }}
  tr:hover td {{ background: var(--row-hover); }}
  .mono {{ font-family: ui-monospace, Consolas, monospace; font-size: 0.8rem; }}
  .empty {{ text-align: center; color: var(--muted); padding: 2rem; }}
  .actions a, .link-btn {{ color: var(--accent); text-decoration: none; margin-right: 0.75rem; font-size: 0.8rem; }}
  .link-btn {{ background: none; border: none; padding: 0; cursor: pointer; font: inherit; }}
  .link-btn:hover, .actions a:hover {{ text-decoration: underline; }}
  .pagination {{ margin-top: 1rem; display: flex; gap: 1rem; align-items: center; font-size: 0.85rem; }}
  .pagination .disabled {{ color: var(--muted); }}
  .pagination a {{ color: var(--accent); text-decoration: none; }}
</style>
</head>
<body>
  <h1>CI-TRAP Reports</h1>
  <div class="subtitle">{total} total report{"s" if total != 1 else ""} &middot; page {page} of {total_pages} &middot; click a column to sort</div>
  <table>
    <thead>
      <tr>
        {header_cells}<th>Lat, Lon</th><th>Attachments</th><th></th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  <div class="pagination">{prev_link}<span>page {page} / {total_pages}</span>{next_link}</div>
</body>
</html>"""


class OtsCitrapReport(Plugin):
    def __init__(self):
        super().__init__()
        # Plugin.__init__ sets self.name = "" and self.distro = "" - these
        # MUST be set here (they're plain instance attributes read by
        # PluginManager as plugin.name / plugin.distro, not class-level
        # constants), or the plugin registers under an empty name.
        self.name = PLUGIN_NAME
        self.distro = DISTRO_NAME

        self.blueprint = Blueprint(
            "ots_citrap_report", __name__, url_prefix=OTS_CITRAP_REPORT_URL_PREFIX
        )

        # A SEPARATE blueprint for the human-facing admin UI (list/view/
        # download/delete), deliberately NOT nested under /Marti/* - many
        # OTS deployments' nginx config blocks all /Marti/* traffic on the
        # default HTTPS port (443) by design, reserving it for cert-
        # authenticated EUD traffic on a separate port (typically 8443).
        # An admin browsing this in a normal browser on port 443 would
        # get a 404 from nginx before ever reaching Flask, regardless of
        # anything this plugin does. Only Plugin.blueprint (singular) is
        # auto-registered by PluginManager, so this one is registered by
        # hand in activate() below via app.register_blueprint().
        self.admin_blueprint = Blueprint(
            "ots_citrap_report_admin", __name__, url_prefix=OTS_CITRAP_REPORT_ADMIN_UI_PREFIX
        )
        # PluginManager calls activate() every time this plugin's enabled
        # state is toggled via the OTS web UI, not just once at boot -
        # register_blueprint() must only ever be called once per process
        # (Flask raises AssertionError if called after the app has served
        # its first request, which every toggle-after-boot triggers; even
        # setup-phase, Flask doesn't support registering the same
        # blueprint twice). This flag prevents the crash on every
        # subsequent enable/disable click after the first activate() call.
        self._admin_blueprint_registered = False

        # OTS's own per-plugin enabled/disabled state, as passed into
        # activate(enabled=...) by PluginManager - tracked here so route
        # handlers can actually honor it (previously they only checked
        # OTS_CITRAP_REPORT_ENABLED, this plugin's own separate config
        # flag, meaning toggling "Disable" on the Plugins page did
        # nothing to the routes at all). Defaults True since activate()
        # hasn't necessarily run yet when this is read.
        self._ots_enabled = True

        self._created_hooks = []
        self._updated_hooks = []
        self._deleted_hooks = []
        self._attachment_hooks = []

        self._register_routes()

    # ------------------------------------------------------------------
    # Public hook API
    # ------------------------------------------------------------------
    def on_report_created(self, func):
        self._created_hooks.append(func)
        return func

    def on_report_updated(self, func):
        self._updated_hooks.append(func)
        return func

    def on_report_deleted(self, func):
        self._deleted_hooks.append(func)
        return func

    def on_attachment_added(self, func):
        self._attachment_hooks.append(func)
        return func

    def _fire(self, hooks, *args):
        for hook in hooks:
            try:
                hook(*args)
            except Exception:
                logger.error("ots_citrap_report: hook raised an exception")
                logger.debug(traceback.format_exc())

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    def _register_routes(self):
        bp = self.blueprint

        def enabled():
            return self._ots_enabled and app.config.get("OTS_CITRAP_REPORT_ENABLED", True)

        def log_event(msg):
            if app.config.get("OTS_CITRAP_REPORT_LOG_EVENTS", True):
                logger.info(f"[ots_citrap_report] {msg}")

        def _check_owner(report, client_uid):
            return report is not None and report.client_uid == client_uid

        # -- getReport: GET /Marti/api/citrap/{id}?clientUid=... ---------
        @bp.route("/<id>", methods=["GET"], endpoint="citrap_get_report")
        def get_report(id):
            if not enabled():
                return Response("disabled", status=503)

            client_uid = request.args.get("clientUid")
            if not client_uid:
                return Response("clientUid is required", status=400)

            report = db.session.get(CitrapReport, id)
            if report is None:
                return Response("not found", status=404)
            if not _check_owner(report, client_uid):
                return Response("forbidden", status=403)

            return Response(report.payload, status=200, mimetype="application/octet-stream")

        # -- putReport: PUT /Marti/api/citrap/{id}?clientUid=... ---------
        @bp.route("/<id>", methods=["PUT"], endpoint="citrap_put_report")
        def put_report(id):
            if not enabled():
                return Response("disabled", status=503)

            client_uid = request.args.get("clientUid")
            if not client_uid:
                return Response("clientUid is required", status=400)

            try:
                payload = _decode_byte_body(request, context="putReport")
            except ValueError as e:
                return Response(str(e), status=400)

            report = db.session.get(CitrapReport, id)
            if report is None:
                # PUT-as-upsert: spec doesn't say explicitly, but this is
                # the conventional PUT semantic and there's no separate
                # "create with a caller-chosen id" operation otherwise.
                report = CitrapReport(id=id, client_uid=client_uid)
                report.set_payload(payload)
                db.session.add(report)
                created = True
            else:
                if not _check_owner(report, client_uid):
                    return Response("forbidden", status=403)
                report.set_payload(payload)
                created = False

            # Extract searchable/sortable metadata from the payload, same
            # as postReport does - without this, PUT-submitted reports
            # would store fine but show blank title/callsign/type/etc in
            # search and the admin list. Unlike postReport, the id here is
            # URL-authoritative (PUT names its resource), so any embedded
            # id in the payload is deliberately ignored.
            _apply_metadata_to_report(report, _extract_report_metadata_from_zip(payload))

            db.session.commit()
            log_event(f"report {id} {'created' if created else 'updated'} by {client_uid}")
            self._fire(self._created_hooks if created else self._updated_hooks, report)

            return Response(report.payload, status=200, mimetype="application/octet-stream")

        # -- deleteReport: DELETE /Marti/api/citrap/{id}?clientUid=... ---
        @bp.route("/<id>", methods=["DELETE"], endpoint="citrap_delete_report")
        def delete_report(id):
            if not enabled():
                return Response("disabled", status=503)

            client_uid = request.args.get("clientUid")
            if not client_uid:
                return Response("clientUid is required", status=400)

            report = db.session.get(CitrapReport, id)
            if report is None:
                return Response("not found", status=404)
            if not _check_owner(report, client_uid):
                return Response("forbidden", status=403)

            db.session.delete(report)
            db.session.commit()
            log_event(f"report {id} deleted by {client_uid}")
            self._fire(self._deleted_hooks, id, client_uid)

            return Response("", status=200)

        # -- searchReports: GET /Marti/api/citrap -------------------------
        # "GET /Marti/api/citrap" is the exact same path+method OTS core
        # documents as natively supported - and empirically (confirmed via
        # a real EUD reporting empty search results, then verified against
        # actual Werkzeug 3.x behavior), core's rule wins any routing
        # precedence race since it's registered before plugins load.
        # Simply adding a competing @bp.route("") for the same path,
        # gated by OTS_CITRAP_REPORT_OVERRIDE_SEARCH, does NOT work -
        # Werkzeug's StateMachineMatcher matches whichever rule was added
        # first, full stop, and removing/rebuilding that matcher's
        # internal tree from a plugin isn't something Werkzeug exposes a
        # supported way to do.
        #
        # What DOES reliably work (confirmed with real Flask + Werkzeug):
        # before_app_request runs before route dispatch for every request
        # regardless of which rule Werkzeug matched, and returning a
        # Response from it short-circuits Flask entirely - core's view
        # function never even runs. Registered only when
        # OTS_CITRAP_REPORT_OVERRIDE_SEARCH is set (default True, since
        # core's own implementation has been confirmed to return nothing
        # for report data it has no knowledge of - EUD search is broken
        # without this override on every OTS install this plugin runs
        # on). Read statically (not from live app.config) since blueprint
        # registration happens at plugin construction time, before an app
        # context is guaranteed to exist; a config.yml change to this
        # flag takes effect on restart.
        if OTS_CITRAP_REPORT_OVERRIDE_SEARCH:
            self._register_search_override(bp, enabled, log_event)
        else:
            logger.info(
                "ots_citrap_report: OTS_CITRAP_REPORT_OVERRIDE_SEARCH is disabled - "
                "EUD searches (GET /Marti/api/citrap) will hit OTS core's own "
                "implementation instead of this plugin's report data."
            )


        # -- postReport: POST /Marti/api/citrap?clientUid=... ------------
        @bp.route("", methods=["POST"], endpoint="citrap_post_report")
        def post_report():
            if not enabled():
                return Response("disabled", status=503)

            client_uid = request.args.get("clientUid")
            if not client_uid:
                return Response("clientUid is required", status=400)

            try:
                payload = _decode_byte_body(request, context="postReport")
            except ValueError as e:
                return Response(str(e), status=400)

            if _debug_enabled():
                _log_zip_contents(payload, context="postReport")

            metadata = _extract_report_metadata_from_zip(payload)
            extracted_id = metadata.get("id")
            existing = db.session.get(CitrapReport, extracted_id) if extracted_id else None

            if existing is not None and existing.client_uid != client_uid:
                # id collision with a different client's report - don't
                # hijack it, fall back to a server-generated id instead.
                logger.warning(
                    f"ots_citrap_report[postReport]: extracted id {extracted_id} "
                    f"belongs to a different clientUid, generating a new id instead"
                )
                extracted_id = None
                existing = None

            if existing is not None:
                # Client is retrying/resending the same local report -
                # update in place rather than erroring on a duplicate id.
                report = existing
                report.set_payload(payload)
                created = False
            else:
                report = CitrapReport(id=extracted_id, client_uid=client_uid) if extracted_id else CitrapReport(client_uid=client_uid)
                report.set_payload(payload)
                db.session.add(report)
                created = True

            _apply_metadata_to_report(report, metadata)

            db.session.commit()

            if _debug_enabled():
                logger.info(
                    f"ots_citrap_report[postReport]: using "
                    f"{'client-supplied' if extracted_id else 'server-generated'} id {report.id}"
                )
            log_event(f"report {report.id} {'created' if created else 'updated'} by {client_uid}")
            self._fire(self._created_hooks if created else self._updated_hooks, report)

            resp_body = report.id
            location = f"{OTS_CITRAP_REPORT_URL_PREFIX}/{report.id}"
            if _debug_enabled():
                logger.info(
                    f"ots_citrap_report[postReport]: responding status=201 "
                    f"Content-Type=text/plain Location={location!r} body={resp_body!r}"
                )
            resp = Response(resp_body, status=201, mimetype="text/plain")
            resp.headers["Location"] = location
            return resp

        # -- addAttachment: POST /Marti/api/citrap/{id}/attachment --------
        @bp.route("/<id>/attachment", methods=["POST"], endpoint="citrap_add_attachment")
        def add_attachment(id):
            if not enabled():
                return Response("disabled", status=503)

            client_uid = request.args.get("clientUid")
            if not client_uid:
                return Response("clientUid is required", status=400)

            report = db.session.get(CitrapReport, id)
            if report is None:
                return Response("not found", status=404)
            if not _check_owner(report, client_uid):
                return Response("forbidden", status=403)

            try:
                data = _decode_byte_body(request, context="addAttachment")
            except ValueError as e:
                return Response(str(e), status=400)

            attachment = CitrapAttachment(report_id=report.id, client_uid=client_uid, data=data)
            db.session.add(attachment)
            db.session.commit()

            log_event(f"attachment {attachment.id} added to report {id} by {client_uid}")
            self._fire(self._attachment_hooks, report, attachment)

            return Response(str(attachment.id), status=200, mimetype="text/plain")

        self._register_ui_routes(enabled)

    def _register_ui_routes(self, enabled):
        """
        A small read-only admin page for browsing submitted reports, since
        building the full separate Mantine/npm UI-plugin pipeline (a
        distinct repo, Node tooling, an iframe convention in OTS core) is
        a much bigger undertaking than "let me see the reports that came
        in." This is plain server-rendered HTML with no external JS/CSS
        dependencies - it doesn't rely on any CDN being reachable.

        Registered on self.admin_blueprint (a SEPARATE blueprint from the
        Marti protocol routes) specifically because many OTS deployments'
        nginx config blocks all /Marti/* traffic on port 443 by design,
        reserving it for cert-authenticated EUD traffic on a separate port
        - an admin browsing from a normal browser on port 443 would get a
        404 from nginx before ever reaching Flask if this lived under
        /Marti/*, regardless of anything this plugin does correctly.

        Guarded with @auth_required() (Flask-Security-Too, matching what
        OTS's own plugin docs recommend for plugin routes) since this is an
        admin-facing page reached via a logged-in browser session, unlike
        the Marti protocol routes above which authenticate EUDs via client
        certificate instead and must NOT use session auth.
        """
        from flask_security import auth_required

        bp = self.admin_blueprint

        @bp.route("", methods=["GET"], endpoint="citrap_ui_list")
        @auth_required()
        def ui_list_reports():
            if not enabled():
                return Response("disabled", status=503)

            page_size = app.config.get("OTS_CITRAP_REPORT_UI_PAGE_SIZE", 50)
            try:
                page = max(1, int(request.args.get("page", 1)))
            except ValueError:
                page = 1

            sort = request.args.get("sort", "datetime")
            if sort not in _SORTABLE_COLUMNS:
                sort = "datetime"
            order = request.args.get("order", "desc")
            if order not in ("asc", "desc"):
                order = "desc"

            sort_column = getattr(CitrapReport, _SORTABLE_COLUMNS[sort][0])
            # Nulls last regardless of direction, so unset fields (e.g.
            # reports whose payload didn't parse) don't dominate either
            # end of the sort.
            order_clause = sort_column.asc() if order == "asc" else sort_column.desc()

            total = CitrapReport.query.count()
            reports = (
                CitrapReport.query.order_by(sort_column.is_(None), order_clause)
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            total_pages = max(1, (total + page_size - 1) // page_size)

            from flask_wtf.csrf import generate_csrf

            html = _render_report_list_html(
                reports,
                page,
                total_pages,
                total,
                OTS_CITRAP_REPORT_ADMIN_UI_PREFIX,
                sort,
                order,
                generate_csrf(),
            )
            return Response(html, status=200, mimetype="text/html")

        @bp.route("/data", methods=["GET"], endpoint="citrap_ui_data")
        @auth_required()
        def ui_data():
            """
            JSON data source for the bundled ui/index.html (the official
            "plugin UI iframe" convention, served by OTS core - separate
            from this admin_blueprint's own /Marti-avoiding path, and
            unaffected by which prefix this blueprint uses). Same-origin
            as OTS's own web UI, so the browser sends the existing session
            cookie automatically - no separate auth flow needed in the
            static page's JS.
            """
            if not enabled():
                return Response(json.dumps({"error": "disabled"}), status=503, mimetype="application/json")

            page_size = app.config.get("OTS_CITRAP_REPORT_UI_PAGE_SIZE", 50)
            try:
                page = max(1, int(request.args.get("page", 1)))
            except ValueError:
                page = 1

            total = CitrapReport.query.count()
            reports = (
                CitrapReport.query.order_by(CitrapReport.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            total_pages = max(1, (total + page_size - 1) // page_size)

            body = json.dumps(
                {
                    "total": total,
                    "page": page,
                    "totalPages": total_pages,
                    "reports": [r.to_dict(include_payload=False) for r in reports],
                }
            )
            return Response(body, status=200, mimetype="application/json")

        @bp.route("/view/<id>", methods=["GET"], endpoint="citrap_ui_view")
        @auth_required()
        def ui_view_report(id):
            report = db.session.get(CitrapReport, id)
            if report is None:
                return Response("not found", status=404)

            from flask_wtf.csrf import generate_csrf

            attachments = list(report.attachments)
            html = _render_report_detail_html(report, attachments, OTS_CITRAP_REPORT_ADMIN_UI_PREFIX, generate_csrf())
            return Response(html, status=200, mimetype="text/html")

        @bp.route("/view/<id>/file/<path:filename>", methods=["GET"], endpoint="citrap_ui_view_file")
        @auth_required()
        def ui_view_file(id, filename):
            """
            Serves one specific file (photo, video, or otherwise) found
            inside a report's payload zip, for the image gallery / video
            player / "other files" list on the detail page. filename is a
            zip entry name (may contain slashes, hence <path:filename>),
            matched exactly against the payload's own contents - not
            arbitrary filesystem access.

            Uses send_file (not a plain Response) so images/videos get
            HTTP Range request support for free - without it, browsers
            can't seek/scrub within a video, and some won't play it at
            all depending on size.
            """
            report = db.session.get(CitrapReport, id)
            if report is None:
                return Response("not found", status=404)

            data = _read_zip_file(report.payload, filename)
            if data is None:
                return Response("not found", status=404)

            base = filename.rsplit("/", 1)[-1]
            inline_mime = _image_mimetype(filename) or _video_mimetype(filename)

            return send_file(
                io.BytesIO(data),
                mimetype=inline_mime or "application/octet-stream",
                as_attachment=not inline_mime,
                download_name=base,
                conditional=True,
            )

        @bp.route("/download/<id>", methods=["GET"], endpoint="citrap_ui_download")
        @auth_required()
        def ui_download_report(id):
            report = db.session.get(CitrapReport, id)
            if report is None:
                return Response("not found", status=404)

            filename, mimetype = _sniff_filename_and_type(id, report.payload)
            resp = Response(report.payload, status=200, mimetype=mimetype)
            resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            return resp

        @bp.route(
            "/download/<id>/attachment/<int:attachment_id>",
            methods=["GET"],
            endpoint="citrap_ui_download_attachment",
        )
        @auth_required()
        def ui_download_attachment(id, attachment_id):
            attachment = db.session.get(CitrapAttachment, attachment_id)
            if attachment is None or attachment.report_id != id:
                return Response("not found", status=404)

            filename, mimetype = _sniff_filename_and_type(f"{id}-attachment-{attachment_id}", attachment.data)
            resp = Response(attachment.data, status=200, mimetype=mimetype)
            resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
            return resp

        @bp.route("/delete/<id>", methods=["POST"], endpoint="citrap_ui_delete")
        @auth_required()
        def ui_delete_report(id):
            report = db.session.get(CitrapReport, id)
            if report is None:
                return Response("not found", status=404)

            client_uid = report.client_uid
            db.session.delete(report)
            db.session.commit()
            self._fire(self._deleted_hooks, id, client_uid)

            return Response(
                "", status=303, headers={"Location": request.url_root.rstrip("/") + OTS_CITRAP_REPORT_ADMIN_UI_PREFIX}
            )

    def _register_search_override(self, bp, enabled, log_event):
        @bp.before_app_request
        def search_reports_override():
            if request.method != "GET" or request.path != OTS_CITRAP_REPORT_URL_PREFIX:
                return None  # not our path - let normal dispatch continue

            if not enabled():
                return Response("disabled", status=503)

            # clientUid is OPTIONAL here (unlike every other operation) -
            # treat it as a filter, not a required scope.
            client_uid = request.args.get("clientUid")
            query = CitrapReport.query
            if client_uid:
                query = query.filter_by(client_uid=client_uid)

            keywords = request.args.get("keywords")
            if keywords:
                like = f"%{keywords}%"
                query = query.filter(
                    db.or_(
                        CitrapReport.keywords.ilike(like),
                        CitrapReport.callsign.ilike(like),
                    )
                )

            report_type = request.args.get("type")
            if report_type:
                query = query.filter(CitrapReport.type == report_type)

            callsign = request.args.get("callsign")
            if callsign:
                query = query.filter(CitrapReport.callsign == callsign)

            start_time = _parse_iso(request.args.get("startTime"))
            if start_time:
                query = query.filter(CitrapReport.created_at >= start_time)

            end_time = _parse_iso(request.args.get("endTime"))
            if end_time:
                query = query.filter(CitrapReport.created_at <= end_time)

            # bbox format (confirmed): "lon1,lat1,lon2,lat2" - two corners of
            # a rectangle, e.g. "-122.321777,45.199216,-122.251740,45.234283".
            # Take min/max per axis rather than assuming which corner comes
            # first, so this works regardless of corner ordering convention.
            # Only matches reports whose lat/lon were successfully extracted
            # from the payload - a report with an unparseable payload has
            # lat/lon = None and will never match a bbox filter.
            bbox = request.args.get("bbox")
            if bbox:
                try:
                    lon1, lat1, lon2, lat2 = (float(v) for v in bbox.split(","))
                except ValueError:
                    return Response("bbox must be 'lon1,lat1,lon2,lat2'", status=400)
                min_lon, max_lon = min(lon1, lon2), max(lon1, lon2)
                min_lat, max_lat = min(lat1, lat2), max(lat1, lat2)
                query = query.filter(
                    CitrapReport.lat.isnot(None),
                    CitrapReport.lon.isnot(None),
                    CitrapReport.lat >= min_lat,
                    CitrapReport.lat <= max_lat,
                    CitrapReport.lon >= min_lon,
                    CitrapReport.lon <= max_lon,
                )

            ceiling = app.config.get("OTS_CITRAP_REPORT_MAX_RESULTS_CEILING", 1000)
            default_max = app.config.get("OTS_CITRAP_REPORT_DEFAULT_MAX_RESULTS", 100)
            try:
                max_count = int(request.args.get("maxReportCount", default_max))
            except ValueError:
                max_count = default_max
            max_count = max(1, min(max_count, ceiling))

            results = query.order_by(CitrapReport.created_at.desc()).limit(max_count).all()

            if request.args.get("subscribe", "").lower() == "true":
                log_event(
                    f"client {client_uid or '<any>'} requested a subscribed search "
                    "(not yet pushed - wire this into on_report_created if needed)"
                )

            log_event(f"searchReports matched {len(results)} report(s) for client {client_uid or '<any>'}")

            # First attempt returned our own metadata schema (id,
            # clientUid, type, ...) as JSON objects - the client showed
            # "success" but displayed zero results, i.e. it parsed an
            # array of objects fine but didn't recognize our field names.
            # Second attempt returned an array of raw base64 payload
            # strings (matching the opaque-bytes convention every OTHER
            # operation in this API uses) - that CRASHED the client on
            # positive results, consistent with a typed JSON deserializer
            # (e.g. Gson/Jackson) expecting objects and hard-failing a
            # type mismatch on bare strings instead of just ignoring them.
            #
            # This third attempt: an array of objects again (matching
            # what didn't crash), but using ATAK's OWN <report> XML
            # attribute names (id, type, title, userCallsign, dateTime,
            # location, importance, status, ...) instead of an invented
            # schema - since ATAK generated that XML itself, its own
            # deserializer is far more likely to recognize its own native
            # field names. Reports whose payload isn't a parseable data
            # package zip are skipped entirely rather than sent as
            # empty/malformed objects, in case that also risks a crash.
            report_objs = []
            for r in results:
                attrs = _extract_raw_report_attrs(r.payload)
                if attrs:
                    report_objs.append(attrs)

            body = json.dumps(report_objs)

            if _debug_enabled():
                logger.info(
                    f"ots_citrap_report[searchReports]: responding status=200 "
                    f"Content-Type=application/json {len(report_objs)}/{len(results)} report(s) "
                    f"parsed into objects, body preview={body[:500]!r}{'...' if len(body) > 500 else ''}"
                )

            return Response(body, status=200, mimetype="application/json")

    # ------------------------------------------------------------------
    # Plugin lifecycle (required abstract methods from opentakserver's
    # Plugin base class: activate(app, enabled), stop(), get_info(),
    # load_metadata() - all four are mandatory or the class can't be
    # instantiated at all, which is silently swallowed by PluginManager
    # and looks like "the plugin never showed up").
    # ------------------------------------------------------------------
    def activate(self, app, enabled: bool) -> None:
        self._app = app
        self._ots_enabled = enabled
        self.load_metadata()

        # PluginManager only auto-registers self.blueprint (singular) -
        # self.admin_blueprint (the browser-facing admin UI, deliberately
        # NOT under /Marti/* - see its docstring) needs registering by
        # hand. PluginManager calls activate() every time this plugin's
        # enabled state is toggled via the OTS web UI, not just once at
        # boot - only attempt this once per process (Flask forbids
        # calling register_blueprint after the app has served its first
        # request, which every toggle-after-boot triggers, and doesn't
        # support registering the same blueprint twice regardless). If
        # the plugin starts disabled and only gets enabled well after
        # boot, even this first attempt can happen too late - caught and
        # logged clearly rather than crashing the enable action, since a
        # full server restart (not just re-toggling) is the only way to
        # pick up the admin UI routes at that point.
        if not self._admin_blueprint_registered:
            try:
                app.register_blueprint(self.admin_blueprint)
                self._admin_blueprint_registered = True
            except AssertionError:
                logger.error(
                    "ots_citrap_report: could not register the admin UI blueprint - "
                    "the server has already started handling requests. This happens "
                    "when the plugin is left disabled through boot and enabled later "
                    "via the web UI rather than at startup. The Marti protocol routes "
                    "(report upload/search/etc) are unaffected and already active - "
                    "only the admin UI at "
                    f"{OTS_CITRAP_REPORT_ADMIN_UI_PREFIX} needs a full OTS restart "
                    "(not just re-toggling enable/disable) to become reachable."
                )

        # NOTE: previously called self.get_plugin_routes(...) here, a
        # method inherited from Plugin whose actual return shape and
        # side effects on self.routes were never verified against real
        # OTS source. If it returns something that isn't cleanly JSON-
        # serializable (e.g. raw Flask Rule objects), whatever OTS's
        # admin API does with plugin.get_info()'s "routes" field could
        # fail server-side (breaking the JSON response the Plugins page
        # fetches) or fail client-side while rendering it - either of
        # which matches "blank screen after a moment" more plausibly
        # than a clean, JSON-safe error. Building this list ourselves
        # avoids depending on unverified inherited behavior entirely.
        self.routes = [
            f"GET {OTS_CITRAP_REPORT_URL_PREFIX}/<id>",
            f"PUT {OTS_CITRAP_REPORT_URL_PREFIX}/<id>",
            f"DELETE {OTS_CITRAP_REPORT_URL_PREFIX}/<id>",
            f"POST {OTS_CITRAP_REPORT_URL_PREFIX}",
            f"POST {OTS_CITRAP_REPORT_URL_PREFIX}/<id>/attachment",
            f"GET {OTS_CITRAP_REPORT_ADMIN_UI_PREFIX}",
            f"GET {OTS_CITRAP_REPORT_ADMIN_UI_PREFIX}/view/<id>",
        ]

        self._create_tables(app)

        if not enabled:
            logger.info("ots_citrap_report: activated in a disabled state")
            return

        @self.on_report_created
        def _log_created(report):
            logger.info(f"ots_citrap_report: created report {report.id} ({report.client_uid})")

        @self.on_report_deleted
        def _log_deleted(report_id, client_uid):
            logger.info(f"ots_citrap_report: deleted report {report_id} ({client_uid})")

    def _create_tables(self, app) -> None:
        """
        Flask-SQLAlchemy/OTS's own migrations only manage core's tables -
        a plugin's models (CitrapReport, CitrapAttachment) are never
        created automatically just by being defined. Create them here,
        scoped to only these two tables (not a blanket db.create_all(),
        to avoid touching anything unrelated to this plugin) and
        checkfirst=True so this is a safe no-op on every activation after
        the first.

        Also auto-migrates any columns added to the model since a table
        was first created (e.g. title/importance/report_datetime added
        after this plugin was already deployed with real data) - a plain
        ADD COLUMN for whatever's missing, since there's no Alembic
        migration chain available to a plugin.
        """
        with app.app_context():
            try:
                CitrapReport.__table__.create(bind=db.engine, checkfirst=True)
                CitrapAttachment.__table__.create(bind=db.engine, checkfirst=True)
                self._migrate_missing_columns(db.engine)
                logger.info("ots_citrap_report: verified/created database tables")
            except Exception:
                logger.error("ots_citrap_report: failed to create database tables")
                logger.debug(traceback.format_exc())

    def _migrate_missing_columns(self, engine) -> None:
        from sqlalchemy import inspect, text

        inspector = inspect(engine)
        for table in (CitrapReport.__table__, CitrapAttachment.__table__):
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                col_type = col.type.compile(dialect=engine.dialect)
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col_type}'))
                logger.info(f"ots_citrap_report: added missing column {table.name}.{col.name}")

    def stop(self) -> None:
        # No background threads, open sockets, or scheduled jobs to tear
        # down - this plugin only adds request-scoped DB-backed routes.
        # Disabling via the Plugins page calls this (not activate() with
        # enabled=False), so this is where route handlers' enabled()
        # check actually needs to flip - without this, "Disable" on the
        # Plugins page wouldn't stop the routes from working at all.
        self._ots_enabled = False

    def get_info(self) -> dict | None:
        # Every value here is a plain str/list-of-str deliberately - see
        # the note in activate() about why self.routes is built by hand
        # rather than via the inherited get_plugin_routes(). project_url
        # MUST always be a list, even empty - OTS's Plugins page calls
        # .forEach() on it directly with no null-check, so a missing/None
        # value throws an uncaught TypeError that blanks the whole page.
        return {
            "name": str(self.name),
            "distro": str(self.distro),
            "routes": list(self.routes) if self.routes else [],
            "version": str(self.metadata.get("version", "unknown")),
            "author": str(self.metadata.get("author", "unknown")),
            "project_url": list(self.metadata.get("project_url") or []),
        }

    def load_metadata(self) -> dict:
        project_urls = []
        try:
            dist = importlib.metadata.distribution(DISTRO_NAME)
            version = dist.version
            author = dist.metadata.get("Author") or dist.metadata.get("Author-email") or "unknown"

            # Poetry writes [tool.poetry] repository/homepage/etc as
            # "Project-URL: Label, https://..." metadata entries - pull
            # just the URL part out of each.
            for entry in dist.metadata.get_all("Project-URL") or []:
                url = entry.split(",", 1)[1].strip() if "," in entry else entry.strip()
                if url:
                    project_urls.append(url)

            home_page = dist.metadata.get("Home-page")
            if home_page and home_page not in project_urls:
                project_urls.append(home_page)
        except importlib.metadata.PackageNotFoundError:
            # Can happen if installed under a different distro name than
            # DISTRO_NAME, e.g. a zip-upload install with a mismatched
            # pyproject.toml [tool.poetry] name - not fatal, just means
            # get_info()/the Plugins admin page shows placeholder values.
            version = "unknown"
            author = "unknown"

        self.metadata = {
            "name": self.name,
            "distro": self.distro,
            "version": version,
            "author": author,
            "project_url": project_urls,
        }
        return self.metadata
