#!/usr/bin/env python3
"""
Build a static PEP 503 ("simple") package index from `poetry build` output,
so this plugin can be installed through OTS's web UI "Install Plugin"
screen (which runs `pip install <name> -i <OTS_PLUGIN_REPO_URL>`) without
running a real index server like devpi.

Usage:
    poetry build                       # produces dist/*.whl and dist/*.tar.gz
    python scripts/build_static_index.py dist/ docs/

Then:
    1. Commit and push the output dir (e.g. docs/) to GitHub.
    2. Enable GitHub Pages for that branch/folder in the repo settings.
    3. On your OTS server, set in config.yml:
           OTS_PLUGIN_REPO_URL: "https://<you>.github.io/<repo>/"
    4. Restart OTS, then use the web UI's Install Plugin screen with the
       package name "OTS-Citrap-Report".

Any static file host works the same way (S3, Cloudflare Pages, a plain
nginx directory listing is NOT enough - it needs this exact index.html
structure, which is what this script generates) - GitHub Pages is just
free and requires no server process, unlike devpi.
"""

import re
import sys
from pathlib import Path


def normalize(name: str) -> str:
    # PEP 503 normalization: lowercase, runs of -_. collapsed to a single -
    return re.sub(r"[-_.]+", "-", name).lower()


def project_name_from_filename(filename: str) -> str:
    # wheel: {name}-{version}-{python tag}-{abi tag}-{platform tag}.whl
    # sdist: {name}-{version}.tar.gz
    stem = filename
    for suffix in (".whl", ".tar.gz", ".zip"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = stem.split("-")
    # For wheels, name is everything before the version component; version
    # is the first part that starts with a digit.
    name_parts = []
    for part in parts:
        if part[:1].isdigit():
            break
        name_parts.append(part)
    return "-".join(name_parts) if name_parts else stem


def build_index(dist_dir: Path, out_dir: Path):
    files = sorted(
        p for p in dist_dir.iterdir() if p.suffix in (".whl", ".gz", ".zip") and p.is_file()
    )
    if not files:
        print(f"No .whl/.tar.gz/.zip files found in {dist_dir}", file=sys.stderr)
        sys.exit(1)

    by_project = {}
    for f in files:
        proj = normalize(project_name_from_filename(f.name))
        by_project.setdefault(proj, []).append(f)

    out_dir.mkdir(parents=True, exist_ok=True)

    root_links = []
    for proj, proj_files in sorted(by_project.items()):
        proj_dir = out_dir / proj
        proj_dir.mkdir(parents=True, exist_ok=True)

        proj_links = []
        for f in proj_files:
            dest = proj_dir / f.name
            dest.write_bytes(f.read_bytes())
            proj_links.append(f'<a href="{f.name}">{f.name}</a><br/>')

        (proj_dir / "index.html").write_text(
            "<!DOCTYPE html>\n<html><body>\n" + "\n".join(proj_links) + "\n</body></html>\n"
        )
        root_links.append(f'<a href="{proj}/">{proj}</a><br/>')

    (out_dir / "index.html").write_text(
        "<!DOCTYPE html>\n<html><body>\n" + "\n".join(root_links) + "\n</body></html>\n"
    )

    print(f"Static index written to {out_dir}/")
    for proj in sorted(by_project):
        print(f"  {proj}/  ({len(by_project[proj])} file(s))")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <dist_dir> <output_dir>", file=sys.stderr)
        sys.exit(1)
    build_index(Path(sys.argv[1]), Path(sys.argv[2]))
