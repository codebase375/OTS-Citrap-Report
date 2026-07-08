# OTS-Citrap-Report — Project Summary

## What it is

An OpenTAKServer plugin implementing the CI-TRAP Report API (the
`ci-trap-report-api` used by ATAK's Reports tool): six endpoints
(get/put/delete/search/post report, plus attachments), backed by its own
Postgres tables, installable through OTS's normal plugin flow.

## Key points

- **Real protocol, not the documented one.** The official OpenAPI spec
  claimed JSON request bodies; real ATAK traffic sends raw
  `application/x-zip-compressed` data-package zips instead. The plugin
  follows the real wire format, confirmed against live traffic.
- **Client-assigned report IDs.** Reports carry their own `id` inside an
  embedded `report.xml`; the server reuses that id rather than minting
  its own, which is what let EUD uploads actually succeed.
- **Admin UI at `https://yourserver.whatever/api/citrap-reports`**, deliberately *not* under
  `/Marti/*` — that path is blocked on port 443 by design in most OTS
  nginx configs (reserved for cert-authenticated EUD traffic on 8443).
  Browse, sort, read parsed report content, view photos/video inline
  (with HEVC detection and a fallback download), download, delete — all
  self-contained HTML/CSS/JS, no build step, no external dependencies.
  Matches OTS's own dark/light theme automatically.


## This project heavily leveraged Claude for implementation and is still not perfect. ##

Help is appreciated for a further and properly fleshed out implementation. 


# Right now as it sits the plugin will: #

* Accept reports from EUD's for storage on the server with attachments
* List all reports submitted to the server on the admin UI
* Further dig down to individual reports for viewing/download/deletion

Thanks for looking, using, and contributing!

