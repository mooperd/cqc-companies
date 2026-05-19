"""Refresh the committed CQC CSVs from the monthly bulk downloads.

Source: <https://www.cqc.org.uk/about-us/transparency/using-cqc-data> publishes
three bulk files monthly (directory CSV, HSCA Active Locations ODS, Latest
ratings ODS). This module discovers their current URLs by scraping the data
index page, HEADs each URL to check ETag/Last-Modified against a committed
state file, downloads what's changed, maps the new shape onto the existing
`output.csv` / `Locations.csv` schema, and writes the regenerated files.

The deployed pipeline is `python -m cqc_refresh refresh` from a scheduled
GitHub Actions workflow; the same entry point works locally with
`--dry-run` for development. See `docs/plans/cqc-bulk-ingest.md` and
`docs/adr/0007-csvs-checked-into-repo.md` (Amendment 2026-05-19).

Why stdlib-only: this module wants to import cleanly in the PR-time smoke
check without adding HTTP / HTML deps to `requirements.txt`. urllib +
html.parser are sufficient; the ODS parser uses `zipfile` + `xml.etree`.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger(__name__)

# --- Constants ----------------------------------------------------------------

DATA_INDEX_URL = "https://www.cqc.org.uk/about-us/transparency/using-cqc-data"
STATE_FILE = Path("data/cqc-refresh-state.json")
DIRECTORY_CSV = Path("output.csv")
LOCATIONS_CSV = Path("Locations.csv")
USER_AGENT = "cqc-companies-refresh/1.0 (+https://github.com/mooperd/cqc-companies)"

# Stable identifiers for the three bulk files. Used as dict keys in the state
# file and across the pipeline; constants stop a typo from silently misrouting
# a download.
KIND_DIRECTORY = "directory_csv"
KIND_HSCA = "hsca_ods"
KIND_RATINGS = "ratings_ods"
KINDS = (KIND_DIRECTORY, KIND_HSCA, KIND_RATINGS)

# OpenDocument XML namespaces, as they appear in .ods content.xml.
_ODS_TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_ODS_TABLE_TAG = f"{{{_ODS_TABLE_NS}}}table"
_ODS_ROW_TAG = f"{{{_ODS_TABLE_NS}}}table-row"
_ODS_CELL_TAG = f"{{{_ODS_TABLE_NS}}}table-cell"
_ODS_REPEAT_ATTR = f"{{{_ODS_TABLE_NS}}}number-columns-repeated"
_ODS_NAME_ATTR = f"{{{_ODS_TABLE_NS}}}name"


# --- Data classes -------------------------------------------------------------


@dataclass(frozen=True)
class FileMeta:
    """Per-file state we persist between runs."""

    url: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class DiscoveredUrls:
    directory_csv: str
    hsca_ods: str
    ratings_ods: str

    def as_dict(self) -> dict[str, str]:
        return {
            KIND_DIRECTORY: self.directory_csv,
            KIND_HSCA: self.hsca_ods,
            KIND_RATINGS: self.ratings_ods,
        }


# --- WS1: URL discovery -------------------------------------------------------


class _LinkExtractor(HTMLParser):
    """Pull <a href="..."> URLs from the CQC data index page."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append(value)


def discover_urls(index_url: str = DATA_INDEX_URL) -> DiscoveredUrls:
    """Scrape the CQC data index page for the three monthly URLs we need.

    The page links each bulk file by name (CQC_directory.csv, HSCA_Active_
    Locations.ods, Latest_ratings.ods). Day-of-month varies between files, so
    we match on filename pattern rather than templating.

    Raises RuntimeError if any of the three files isn't found.
    """
    html = _http_get(index_url).decode("utf-8", errors="replace")
    parser = _LinkExtractor()
    parser.feed(html)

    patterns = {
        KIND_DIRECTORY: re.compile(r"CQC_directory\.csv$", re.IGNORECASE),
        KIND_HSCA: re.compile(r"HSCA_Active_Locations\.ods$", re.IGNORECASE),
        KIND_RATINGS: re.compile(r"Latest_ratings\.ods$", re.IGNORECASE),
    }
    found: dict[str, str] = {}
    for href in parser.hrefs:
        for kind, pat in patterns.items():
            if kind not in found and pat.search(href):
                found[kind] = href if href.startswith("http") else f"https://www.cqc.org.uk{href}"

    missing = sorted(set(patterns) - set(found))
    if missing:
        raise RuntimeError(
            f"Could not discover bulk URLs from {index_url}: missing {missing}. "
            "CQC may have renamed or removed the published files; fall back to "
            "the API (see docs/adr/0007-csvs-checked-into-repo.md Amendment)."
        )

    return DiscoveredUrls(**found)


# --- WS2: Streaming ODS parser ------------------------------------------------


def stream_ods(path: Path, sheet: str) -> Iterator[dict[str, str]]:
    """Yield rows from a sheet inside an `.ods` file as dicts.

    .ods is a ZIP archive containing OpenDocument XML. The reference path
    (odfpy + pandas.read_excel(engine='odf')) loads the whole DOM and
    consumes >3 GB of RAM on a 26 MB ratings file. We stream content.xml
    via ElementTree.iterparse instead — single-digit MB of memory.

    The first row of the matching <table:table> is treated as the header.
    Subsequent rows are yielded as {header: value} dicts. Empty trailing
    rows (no non-blank cells) are skipped.
    """
    with zipfile.ZipFile(path) as z, z.open("content.xml") as f:
        in_target_sheet = False
        header: list[str] | None = None
        current_row: list[str] = []

        for event, elem in ET.iterparse(f, events=("start", "end")):
            if event == "start" and elem.tag == _ODS_TABLE_TAG:
                in_target_sheet = elem.attrib.get(_ODS_NAME_ATTR) == sheet
                header = None
                current_row = []
                continue

            if not in_target_sheet:
                if event == "end":
                    elem.clear()
                continue

            if event == "end" and elem.tag == _ODS_CELL_TAG:
                # Extract cell text (joins <text:p> children) and respect
                # number-columns-repeated for sparse encoding.
                text = "".join(elem.itertext())
                try:
                    repeat = int(elem.attrib.get(_ODS_REPEAT_ATTR, "1"))
                except ValueError:
                    repeat = 1
                # Cap absurd repeats (some .ods files use huge values for
                # trailing empty cells). 4096 is comfortably above any
                # CQC sheet's actual column count.
                if repeat > 4096:
                    repeat = 0
                current_row.extend([text] * repeat)
                elem.clear()

            elif event == "end" and elem.tag == _ODS_ROW_TAG:
                row, current_row = current_row, []
                elem.clear()

                if header is None:
                    header = [c.strip() for c in row]
                    while header and header[-1] == "":
                        header.pop()
                    continue

                if not any(c.strip() for c in row):
                    continue

                yield {
                    header[i]: (row[i] if i < len(row) else "")
                    for i in range(len(header))
                }

            elif event == "end" and elem.tag == _ODS_TABLE_TAG:
                in_target_sheet = False
                elem.clear()


# --- WS3: ETag / Last-Modified change detection ------------------------------


def load_state(path: Path = STATE_FILE) -> dict[str, FileMeta]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {
        kind: FileMeta(url=meta["url"], etag=meta.get("etag"), last_modified=meta.get("last_modified"))
        for kind, meta in raw.get("files", {}).items()
    }


def save_state(state: dict[str, FileMeta], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_run": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "files": {
            kind: {"url": m.url, "etag": m.etag, "last_modified": m.last_modified}
            for kind, m in state.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def head_with_validators(url: str, prior: FileMeta | None) -> tuple[bool, FileMeta]:
    """HEAD a URL. Returns (changed?, new_meta).

    Sends If-None-Match / If-Modified-Since if we have prior values; a 304
    response means the file is unchanged. Any 2xx response with new ETag /
    Last-Modified counts as changed. A URL change since the prior run is
    treated as a new file (no validators sent).
    """
    if prior and prior.url != url:
        prior = None
    req = urllib.request.Request(url, method="HEAD")
    req.add_header("User-Agent", USER_AGENT)
    if prior and prior.etag:
        req.add_header("If-None-Match", prior.etag)
    if prior and prior.last_modified:
        req.add_header("If-Modified-Since", prior.last_modified)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            etag = resp.headers.get("ETag")
            last_modified = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            assert prior is not None, "got 304 with no prior validators"
            return False, prior
        raise

    new_meta = FileMeta(url=url, etag=etag, last_modified=last_modified)
    changed = (
        prior is None
        or prior.etag != etag
        or prior.last_modified != last_modified
        or prior.url != url
    )
    return changed, new_meta


# --- WS4: Schema mapping ------------------------------------------------------

# Headers of the existing committed CSVs — we preserve these exactly so the
# importers (import_records.py, enrich_locations.py) keep working unchanged.

OUTPUT_CSV_HEADER = [
    "Name", "Address 1", "Address 2", "Town/City", "County", "Postcode",
    "Phone number", "CQC Provider ID (for office use only)",
    "CQC Location ID (for office use only)", "Website", "Local authority",
    "Region", "Report publication date", "URL", "Also known as",
    "Specialisms/services", "Service types", "Provider name",
]

LOCATIONS_CSV_HEADER = [
    "Location Name", "Location ID",
    "Comments within last month / comments last recorded",
    "Registered manager", "Location UPRN", "Location telephone number",
    "Location Web Address", "Location Local Authority",
    "Location ADASS Region", "Primary inspection category", "Service Types",
    "Care homes beds", "Location HSCA start date", "Location HSCA end date",
    "Dormant", "Location Latest Overall Rating", "Publication Date",
    "Service users supported",
    "Size of care home (bands by number of beds)",
    "Location length of service (bands by number of years)",
    "Location safe rating", "Location effective rating",
    "Location caring rating", "Location responsive rating",
    "Location well-led rating", "Provider Name", "Provider ID",
    "Provider HSCA start date",
    "Provider length of service (bands by number of years)",
    "Provider Ownership Type", "Provider Companies House Number",
    "Provider characteristic - number of locations",
    "Provider characteristic - national group size (based on locations)",
    "Provider characteristic - number of beds",
    "Provider characteristic - share of beds (nationally)",
    "Provider characteristic - number of local authority areas where it has locations",
    "Provider characteristic - % of local authority areas where it has locations",
    "Brand Name", "Brand ID",
    "Brand characteristic - number of locations",
    "Brand characteristic -national group size (based on locations)",
    "Brand characteristic - number of beds",
    "Brand characteristic - share of beds (nationally)",
    "Brand characteristic - % of local authority areas where it has locations",
    "Brand characteristic - number of local authority areas where it has locations",
]


def _split_collapsed_address(addr: str) -> tuple[str, str, str, str]:
    """Split CQC's new single 'Address' field into (Addr1, Addr2, Town/City, County).

    The new directory CSV ships addresses as a single comma-separated string
    (e.g. "5 Tainmor Close,Longlevens,Gloucester"). The old format had four
    columns; County is no longer published, so it stays blank. Heuristic:

      N=1: Addr1 = it
      N=2: Addr1, Town/City
      N=3: Addr1, Addr2, Town/City
      N≥4: Addr1, (joined middle) = Addr2, Town/City
    """
    parts = [p.strip() for p in addr.split(",")] if addr else []
    if not parts:
        return "", "", "", ""
    if len(parts) == 1:
        return parts[0], "", "", ""
    if len(parts) == 2:
        return parts[0], "", parts[1], ""
    if len(parts) == 3:
        return parts[0], parts[1], parts[2], ""
    return parts[0], ", ".join(parts[1:-1]), parts[-1], ""


def map_directory_csv(rows: Iterator[dict[str, str]]) -> Iterator[dict[str, str]]:
    """Map the new directory CSV onto the existing `output.csv` shape."""
    for row in rows:
        a1, a2, town, county = _split_collapsed_address(row.get("Address", ""))
        yield {
            "Name": row.get("Name", ""),
            "Address 1": a1,
            "Address 2": a2,
            "Town/City": town,
            "County": county,
            "Postcode": row.get("Postcode", ""),
            "Phone number": row.get("Phone number", ""),
            "CQC Provider ID (for office use only)": row.get("CQC Provider ID (for office use only)", ""),
            "CQC Location ID (for office use only)": row.get("CQC Location ID (for office use only)", ""),
            "Website": row.get("Service's website (if available)", ""),
            "Local authority": row.get("Local authority", ""),
            "Region": row.get("Region", ""),
            "Report publication date": row.get("Date of latest check", ""),
            "URL": row.get("Location URL", ""),
            "Also known as": row.get("Also known as", ""),
            "Specialisms/services": row.get("Specialisms/services", ""),
            "Service types": row.get("Service types", ""),
            "Provider name": row.get("Provider name", ""),
        }


def _make_one_hot_flattener(first_row: dict[str, str], prefix: str):
    """Return a fn that flattens one-hot Y/N columns matching `prefix` to a
    comma-separated string of suffixes.

    HSCA encodes service types and service-user bands as ~30 separate Y/N
    columns like 'Service type - Care home service with nursing'. The
    existing Locations.csv has a single comma-separated string. We precompute
    the matching columns once per parse (~30) instead of scanning all 122
    columns per row × 57k rows × 2 prefixes — saves ~2s on the parse pass.
    """
    cols = [c for c in first_row if c.startswith(prefix)]
    strip_to = len(prefix)
    def flatten(row: dict[str, str]) -> str:
        return ", ".join(
            c[strip_to:].strip()
            for c in cols
            if (row.get(c, "") or "").strip().upper() == "Y"
        )
    return flatten


def map_hsca_ods(rows: Iterator[dict[str, str]]) -> Iterator[dict[str, str]]:
    """Map HSCA Active Locations rows onto the existing `Locations.csv` shape.

    Sub-rating columns (Safe/Effective/Caring/Responsive/Well-led) are left
    blank here — they're populated by `merge_ratings_into_locations` from the
    separate Latest_ratings.ods file.
    """
    # Peek at the first row to precompute one-hot column lists, then iterate.
    first = next(rows, None)
    if first is None:
        return
    flatten_services = _make_one_hot_flattener(first, "Service type - ")
    flatten_bands = _make_one_hot_flattener(first, "Service user band - ")

    for row in itertools.chain([first], rows):
        yield {
            "Location Name": row.get("Location Name", ""),
            "Location ID": row.get("Location ID", ""),
            "Comments within last month / comments last recorded": "",
            "Registered manager": row.get("Registered manager", ""),
            "Location UPRN": row.get("Location UPRN ID", ""),
            "Location telephone number": row.get("Location Telephone Number", ""),
            "Location Web Address": row.get("Location Web Address", ""),
            "Location Local Authority": row.get("Location Local Authority", ""),
            "Location ADASS Region": row.get("Location Region", ""),
            "Primary inspection category": row.get("Location Primary Inspection Category", ""),
            "Service Types": flatten_services(row),
            "Care homes beds": row.get("Care homes beds", ""),
            "Location HSCA start date": row.get("Location HSCA start date", ""),
            "Location HSCA end date": "",
            "Dormant": row.get("Dormant (Y/N)", ""),
            "Location Latest Overall Rating": row.get("Location Latest Overall Rating", ""),
            "Publication Date": row.get("Publication Date", ""),
            "Service users supported": flatten_bands(row),
            "Size of care home (bands by number of beds)": "",
            "Location length of service (bands by number of years)": "",
            "Location safe rating": "",
            "Location effective rating": "",
            "Location caring rating": "",
            "Location responsive rating": "",
            "Location well-led rating": "",
            "Provider Name": row.get("Provider Name", ""),
            "Provider ID": row.get("Provider ID", ""),
            "Provider HSCA start date": row.get("Provider HSCA start date", ""),
            "Provider length of service (bands by number of years)": "",
            "Provider Ownership Type": row.get("Provider Ownership Type", ""),
            "Provider Companies House Number": row.get("Provider Companies House Number", ""),
            "Provider characteristic - number of locations": "",
            "Provider characteristic - national group size (based on locations)": "",
            "Provider characteristic - number of beds": "",
            "Provider characteristic - share of beds (nationally)": "",
            "Provider characteristic - number of local authority areas where it has locations": "",
            "Provider characteristic - % of local authority areas where it has locations": "",
            "Brand Name": row.get("Brand Name", ""),
            "Brand ID": row.get("Brand ID", ""),
            "Brand characteristic - number of locations": "",
            "Brand characteristic -national group size (based on locations)": "",
            "Brand characteristic - number of beds": "",
            "Brand characteristic - share of beds (nationally)": "",
            "Brand characteristic - % of local authority areas where it has locations": "",
            "Brand characteristic - number of local authority areas where it has locations": "",
        }


def pivot_ratings_to_wide(rows: Iterator[dict[str, str]]) -> dict[str, dict[str, str]]:
    """Pivot Latest_ratings.ods long-format rows into per-location sub-ratings.

    Each (Location ID, Domain) row carries a `Latest Rating`. We pivot on
    Domain ∈ {Safe, Effective, Caring, Responsive, Well-led}, filtering to
    'Service / Population Group = Overall' so we get one row per location.

    Returns {location_id: {domain_col: rating}}.
    """
    domain_to_col = {
        "Safe": "Location safe rating",
        "Effective": "Location effective rating",
        "Caring": "Location caring rating",
        "Responsive": "Location responsive rating",
        "Well-led": "Location well-led rating",
    }
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.get("Service / Population Group", "").strip() != "Overall":
            continue
        loc_id = row.get("Location ID", "").strip()
        if not loc_id:
            continue
        domain = row.get("Domain", "").strip()
        col = domain_to_col.get(domain)
        if col is None:
            continue
        out.setdefault(loc_id, {})[col] = row.get("Latest Rating", "")
    return out


def merge_ratings_into_locations(
    location_rows: Iterator[dict[str, str]],
    ratings_by_location: dict[str, dict[str, str]],
) -> Iterator[dict[str, str]]:
    """Join the pivoted ratings into mapped HSCA rows by Location ID."""
    for row in location_rows:
        loc_id = row.get("Location ID", "").strip()
        for col, val in ratings_by_location.get(loc_id, {}).items():
            row[col] = val
        yield row


# --- Pipeline -----------------------------------------------------------------


def write_csv(path: Path, header: list[str], rows: Iterator[dict[str, str]]) -> int:
    count = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _download_to(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=300) as resp, dest.open("wb") as f:
        while chunk := resp.read(1 << 16):
            f.write(chunk)


# --- CLI ----------------------------------------------------------------------


def _cmd_discover(_args: argparse.Namespace) -> int:
    urls = discover_urls()
    print(json.dumps(urls.as_dict(), indent=2))
    return 0


def _cmd_check(_args: argparse.Namespace) -> int:
    urls = discover_urls()
    state = load_state()
    any_changed = False
    for kind, url in urls.as_dict().items():
        changed, _meta = head_with_validators(url, state.get(kind))
        marker = "CHANGED" if changed else "unchanged"
        any_changed = any_changed or changed
        print(f"  {kind:14s} {marker}  {url}")
    return 0 if any_changed else 1  # exit 1 == nothing to do


def _cmd_refresh(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    # On a real refresh we overwrite the committed CSVs in place; on a dry
    # run we write side-by-side in the workdir so the committed files stay
    # untouched. The state file is only persisted on a real refresh.
    directory_target = DIRECTORY_CSV if not args.dry_run else (workdir / "output.csv")
    locations_target = LOCATIONS_CSV if not args.dry_run else (workdir / "Locations.csv")

    urls = discover_urls()
    state = load_state()
    new_state: dict[str, FileMeta] = {}
    downloads: dict[str, Path] = {}

    for kind, url in urls.as_dict().items():
        changed, meta = head_with_validators(url, state.get(kind))
        new_state[kind] = meta
        if changed:
            dest = workdir / Path(url).name
            logger.info("downloading %s → %s", url, dest)
            _download_to(url, dest)
            downloads[kind] = dest
        else:
            logger.info("unchanged: %s", url)

    if not downloads:
        logger.info("nothing changed — exiting clean")
        if not args.dry_run:
            save_state(new_state)
        return 0

    # Always need all three files to regenerate. If only one changed, fetch
    # the others to current versions for consistent output.
    for kind, url in urls.as_dict().items():
        if kind not in downloads:
            dest = workdir / Path(url).name
            logger.info("downloading (for full regenerate) %s → %s", url, dest)
            _download_to(url, dest)
            downloads[kind] = dest

    # --- Regenerate output.csv from the directory CSV ------------------------
    dir_path = downloads[KIND_DIRECTORY]
    logger.info("mapping %s → %s", dir_path.name, directory_target)
    with dir_path.open(encoding="utf-8") as src:
        # Skip the 4-line preamble (title / blank / produced-on / blank).
        for _ in range(4):
            src.readline()
        reader = csv.DictReader(src)
        n = write_csv(directory_target, OUTPUT_CSV_HEADER, map_directory_csv(reader))
    logger.info("wrote %s with %d rows", directory_target, n)

    # --- Regenerate Locations.csv from HSCA + ratings ------------------------
    hsca_path = downloads[KIND_HSCA]
    ratings_path = downloads[KIND_RATINGS]
    logger.info("mapping %s + %s → %s", hsca_path.name, ratings_path.name, locations_target)
    ratings = pivot_ratings_to_wide(stream_ods(ratings_path, "Locations"))
    logger.info("pivoted ratings for %d locations", len(ratings))
    mapped = map_hsca_ods(stream_ods(hsca_path, "HSCA_Active_Locations"))
    n = write_csv(locations_target, LOCATIONS_CSV_HEADER, merge_ratings_into_locations(mapped, ratings))
    logger.info("wrote %s with %d rows", locations_target, n)

    if args.dry_run:
        logger.info("--dry-run: state file NOT updated")
    else:
        save_state(new_state)
        logger.info("updated %s", STATE_FILE)

    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cqc_refresh",
        description="Refresh committed CQC CSVs from the monthly bulk downloads.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="print the three current bulk-file URLs")

    sub.add_parser("check", help="HEAD the URLs; exit 0 if changed, 1 if not")

    refresh = sub.add_parser("refresh", help="download + regenerate CSVs if changed")
    refresh.add_argument("--dry-run", action="store_true", help="don't update the state file")
    refresh.add_argument("--workdir", default=".cqc-refresh-cache", help="download cache dir")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Sanity check per ADR 0007 Amendment: the bulk-download path doesn't
    # use the CQC API. If a key is set as a workflow secret it's almost
    # certainly a misconfiguration — surface it loudly rather than
    # silently ignoring.
    if os.getenv("CQC_PRIMARY_KEY"):
        logger.warning(
            "CQC_PRIMARY_KEY is set but cqc_refresh does not use the API. "
            "If this is the CI environment, unset the secret; the bulk-download "
            "path is unauthenticated and doesn't need it."
        )

    handlers = {"discover": _cmd_discover, "check": _cmd_check, "refresh": _cmd_refresh}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
