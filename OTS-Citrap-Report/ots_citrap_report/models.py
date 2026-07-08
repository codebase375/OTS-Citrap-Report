import base64
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from opentakserver.extensions import db


def _now():
    return datetime.now(timezone.utc)


def _new_uid():
    return str(uuid.uuid4())


def extract_latlon_from_cot(payload: bytes):
    """
    CI-TRAP report payloads are opaque per the spec, but in practice
    TAK ecosystem reports are CoT (Cursor-on-Target) XML, which carries
    location as <event ...><point lat="..." lon="..." .../></event>.

    Best-effort extraction for bbox search filtering: returns (lat, lon)
    as floats, or None if the payload isn't parseable CoT XML or has no
    point element. Non-CoT payloads simply won't be matchable by bbox
    search - see README.
    """
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None

    point = root.find("point")
    if point is None:
        return None

    try:
        return float(point.get("lat")), float(point.get("lon"))
    except (TypeError, ValueError):
        return None


class CitrapReport(db.Model):
    """
    One row per CI-TRAP report. `id` is the report's own uid (used as the
    {id} path parameter on getReport/putReport/deleteReport/addAttachment),
    NOT the DB primary key surrogate - it's exactly what clients pass around.

    `payload` stores the raw report bytes as submitted by postReport/putReport
    (OpenAPI type "string <byte>", i.e. base64-encoded in transit). We store
    the decoded bytes and re-encode to base64 only when serializing a
    response, so the DB always holds the canonical bytes.

    `lat`/`lon` are best-effort, extracted from the payload if it parses as
    CoT XML (see extract_latlon_from_cot) - used only for bbox filtering in
    searchReports. Reports whose payload isn't CoT XML (or has no <point>)
    simply have lat/lon = None and won't match any bbox filter.
    """

    __tablename__ = "ots_citrap_reports"

    id = db.Column(db.String(255), primary_key=True, default=_new_uid)
    client_uid = db.Column(db.String(255), nullable=False, index=True)

    # Searchable metadata (populated from putReport/postReport body if the
    # caller includes it, or left null - the CI-TRAP spec doesn't dictate
    # that the server parses the payload, only that search can filter on
    # these fields, so callers are expected to supply them via query params
    # on subsequent searchReports calls; we index what we're given).
    type = db.Column(db.String(255), nullable=True, index=True)
    callsign = db.Column(db.String(255), nullable=True, index=True)
    title = db.Column(db.String(500), nullable=True, index=True)
    importance = db.Column(db.String(100), nullable=True, index=True)
    # The report's OWN embedded dateTime (when the EUD authored/submitted
    # it), distinct from created_at (when our server received it). Falls
    # back to created_at for display when a payload isn't parseable.
    report_datetime = db.Column(db.DateTime, nullable=True, index=True)
    keywords = db.Column(db.Text, nullable=True)
    lat = db.Column(db.Float, nullable=True, index=True)
    lon = db.Column(db.Float, nullable=True, index=True)
    start_time = db.Column(db.DateTime, nullable=True)
    end_time = db.Column(db.DateTime, nullable=True)

    payload = db.Column(db.LargeBinary, nullable=False)

    created_at = db.Column(db.DateTime, default=_now)
    updated_at = db.Column(db.DateTime, default=_now, onupdate=_now)

    attachments = db.relationship(
        "CitrapAttachment",
        backref="report",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def set_payload(self, payload: bytes):
        """Store payload bytes and (re-)derive lat/lon from it."""
        self.payload = payload
        latlon = extract_latlon_from_cot(payload)
        if latlon:
            self.lat, self.lon = latlon

    def to_dict(self, include_payload: bool = True) -> dict:
        d = {
            "id": self.id,
            "clientUid": self.client_uid,
            "type": self.type,
            "callsign": self.callsign,
            "title": self.title,
            "importance": self.importance,
            "reportDateTime": self.report_datetime.isoformat() if self.report_datetime else None,
            "keywords": self.keywords,
            "lat": self.lat,
            "lon": self.lon,
            "startTime": self.start_time.isoformat() if self.start_time else None,
            "endTime": self.end_time.isoformat() if self.end_time else None,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "attachmentCount": self.attachments.count(),
        }
        if include_payload:
            d["payload"] = base64.b64encode(self.payload or b"").decode("ascii")
        return d


class CitrapAttachment(db.Model):
    __tablename__ = "ots_citrap_attachments"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    report_id = db.Column(
        db.String(255), db.ForeignKey("ots_citrap_reports.id"), nullable=False, index=True
    )
    client_uid = db.Column(db.String(255), nullable=False)
    data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(db.DateTime, default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "reportId": self.report_id,
            "clientUid": self.client_uid,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "data": base64.b64encode(self.data or b"").decode("ascii"),
        }
