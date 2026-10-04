"""
Footage sources with a strict license gate (docs/COMPLIANCE.md section 1).

Only Internet Archive items whose own `licenseurl` is a public-domain mark,
a CC0/public-domain dedication, or plain CC BY get through. Everything else —
non-commercial, no-derivatives, share-alike, or no license at all — is
rejected and recorded with the reason, so every decision is auditable.
"""
import datetime as dt
import json
import re

import requests

from db.database import get_conn

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{name}"
DETAILS_URL = "https://archive.org/details/{identifier}"
HEADERS = {"User-Agent": "ClipForge-Autopilot/1.0 (archive research; contact via site)"}

# Rights notes shown in the provenance record for well-documented collections.
COLLECTION_RIGHTS = {
    "universal_newsreels": (
        "Universal Newsreel (1929-1967): MCA/Universal deeded the collection's rights "
        "to the US Government in 1974; held by the National Archives."
    ),
}

MIN_DURATION = 30
MAX_DURATION = 20 * 60

# Topics we never automate — graphic or distressing footage risks YouTube's
# "emotionally manipulative / distressing" rule and isn't worth the channel.
SENSITIVE = re.compile(
    r"assassinat|execut|lynch|massacre|atrocit|corpse|dead bod|bodies|murder|"
    r"electrocut|hanging|genocide|holocaust|concentration camp|war crime|suicide|"
    r"autopsy|mutilat|torture|crash victims|death camp",
    re.I,
)


def classify_license(url: str):
    """Return (allowed, license_name, needs_attribution)."""
    u = (url or "").strip().lower().replace("https://", "http://")
    if not u:
        return False, "No license metadata", False
    if "creativecommons.org/publicdomain/mark/" in u:
        return True, "Public Domain Mark 1.0", False
    if "creativecommons.org/publicdomain/zero/" in u:
        return True, "CC0 1.0 (public domain dedication)", False
    if "creativecommons.org/licenses/publicdomain" in u:
        return True, "Public Domain (Creative Commons dedication)", False
    m = re.search(r"creativecommons\.org/licenses/([a-z\-]+)/([\d.]+)?", u)
    if m:
        kind, ver = m.group(1), m.group(2) or ""
        if kind == "by":
            return True, f"CC BY {ver}".strip(), True
        return False, f"CC {kind.upper()} {ver} — not allowed (NC/ND/SA)".strip(), False
    return False, f"Unrecognized license: {url}", False


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def search_collection(collection: str, rows: int = 200) -> list:
    params = {
        "q": f"collection:({collection}) AND mediatype:(movies)",
        "fl[]": ["identifier", "title", "date", "licenseurl", "downloads"],
        "sort[]": "downloads desc",
        "rows": rows,
        "output": "json",
    }
    r = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r.json().get("response", {}).get("docs", [])


def refresh_candidates(collections: list) -> int:
    """Add newly found items to archive_sources. Returns how many were added."""
    added = 0
    conn = get_conn()
    try:
        for col in collections:
            for doc in search_collection(col):
                ident = doc.get("identifier")
                if not ident:
                    continue
                allowed, lic_name, _ = classify_license(_first(doc.get("licenseurl")))
                title = _first(doc.get("title"))
                status, reason = "new", None
                if not allowed:
                    status, reason = "rejected", lic_name
                elif SENSITIVE.search(title):
                    status, reason = "rejected", "Sensitive topic (auto-skipped)"
                cur = conn.execute(
                    """INSERT OR IGNORE INTO archive_sources
                       (provider, identifier, title, date, collection, license_url, license_name,
                        source_url, status, reject_reason)
                       VALUES ('internet_archive',?,?,?,?,?,?,?,?,?)""",
                    (ident, title, _first(doc.get("date")), col,
                     _first(doc.get("licenseurl")), lic_name,
                     DETAILS_URL.format(identifier=ident), status, reason),
                )
                added += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return added


def _date_from_identifier(ident: str):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", ident or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def candidate_score(ident: str, today: dt.date) -> float:
    """Prefer items from this calendar week in past years ("this week in 1941")."""
    d = _date_from_identifier(ident)
    if not d:
        return 0.0
    try:
        anniv = d.replace(year=today.year)
    except ValueError:
        anniv = d.replace(year=today.year, day=28)
    days = min(abs((anniv - today).days), 365 - abs((anniv - today).days))
    return max(0.0, 14 - days)


def pick_next_candidates(limit: int = 10, today: dt.date = None) -> list:
    today = today or dt.date.today()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM archive_sources WHERE status='new' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    ranked = sorted(rows, key=lambda r: (-candidate_score(r["identifier"], today), r["id"]))
    return [dict(r) for r in ranked[:limit]]


def choose_video_file(files: list):
    """Pick the best h.264 MP4 derivative (largest resolution under 1080p)."""
    mp4s = []
    for f in files:
        name = f.get("name", "")
        if not name.lower().endswith(".mp4"):
            continue
        try:
            h = int(f.get("height") or 0)
            length = float(f.get("length") or 0)
            size = int(f.get("size") or 0)
        except ValueError:
            continue
        mp4s.append((h if h <= 1080 else 0, size, length, name))
    if not mp4s:
        return None
    mp4s.sort(reverse=True)
    h, size, length, name = mp4s[0]
    return {"name": name, "length": length, "height": h, "size": size}


def resolve_item(source: dict) -> dict:
    """Fetch full metadata, re-check the license on the item itself, and pick a
    downloadable file. Updates the DB row; returns the updated source dict or
    raises ValueError with the rejection reason."""
    ident = source["identifier"]
    r = requests.get(METADATA_URL.format(identifier=ident), headers=HEADERS, timeout=60)
    r.raise_for_status()
    data = r.json()
    meta = data.get("metadata", {})

    lic_url = _first(meta.get("licenseurl"))
    allowed, lic_name, needs_attr = classify_license(lic_url)
    reason = None
    if not allowed:
        reason = lic_name
    elif _first(meta.get("mediatype")) != "movies":
        reason = "Not a video item"
    text = f"{_first(meta.get('title'))} {_first(meta.get('description'))}"
    if not reason and SENSITIVE.search(text):
        reason = "Sensitive topic (auto-skipped)"
    chosen = choose_video_file(data.get("files", []))
    if not reason and not chosen:
        reason = "No MP4 file available"
    if not reason and not (MIN_DURATION <= chosen["length"] <= MAX_DURATION):
        reason = f"Duration {int(chosen['length'])}s outside {MIN_DURATION}-{MAX_DURATION}s"

    collections = _as_list(meta.get("collection"))
    rights = "; ".join(COLLECTION_RIGHTS[c] for c in collections if c in COLLECTION_RIGHTS)
    rights_basis = f"Item license: {lic_name} ({lic_url})." + (f" {rights}" if rights else "")
    if needs_attr:
        creator = _first(meta.get("creator")) or "the uploader"
        rights_basis += f" Attribution required: {creator}."

    description = re.sub(r"<[^>]+>", " ", _first(meta.get("description")))
    description = re.sub(r"\s+", " ", description).strip()

    updates = {
        "title": _first(meta.get("title")) or source.get("title"),
        "description": description[:4000],
        "date": _first(meta.get("date")) or source.get("date"),
        "license_url": lic_url,
        "license_name": lic_name,
        "rights_basis": rights_basis,
        "file_url": DOWNLOAD_URL.format(identifier=ident, name=chosen["name"]) if chosen else None,
        "duration": chosen["length"] if chosen else 0,
        "metadata_json": json.dumps({
            "fetched_at": dt.datetime.utcnow().isoformat() + "Z",
            "metadata": meta,
            "file": chosen,
        })[:200000],
        "status": "rejected" if reason else "new",
        "reject_reason": reason,
    }
    conn = get_conn()
    try:
        sets = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE archive_sources SET {sets} WHERE id=?", (*updates.values(), source["id"]))
        conn.commit()
    finally:
        conn.close()
    if reason:
        raise ValueError(reason)
    source.update(updates)
    return source


def set_source_status(source_id: int, status: str, reason: str = None):
    conn = get_conn()
    try:
        conn.execute("UPDATE archive_sources SET status=?, reject_reason=? WHERE id=?",
                     (status, reason, source_id))
        conn.commit()
    finally:
        conn.close()


def download(file_url: str, dest: str):
    with requests.get(file_url, headers=HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


def credit_line(source: dict) -> str:
    year = (source.get("date") or "")[:4]
    return (
        f"Footage: \"{source.get('title')}\"{f' ({year})' if year else ''} — "
        f"{source.get('license_name')}, via the Internet Archive: {source.get('source_url')}"
    )
