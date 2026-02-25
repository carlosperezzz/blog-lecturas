#!/usr/bin/env python3
"""
build.py — Fetches Goodreads RSS for user 7001188 and generates index.html
Run locally:  python scripts/build.py
Run in CI:    same command, triggered by GitHub Actions
"""

import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import json
import re
import os
import sys
import time
import hashlib
from datetime import datetime
from pathlib import Path

import csv

GOODREADS_USER_ID = "7001188"
OUTPUT_FILE  = Path(__file__).parent.parent / "index.html"
COVERS_DIR   = Path(__file__).parent.parent / "covers"
CSV_FILE     = Path(__file__).parent.parent / "goodreads_library_export.csv"

# ── CSV LOADER ────────────────────────────────────────────────────────────────

def load_csv():
    """
    Load the Goodreads CSV export as a dict keyed by book_id.
    Used to enrich RSS data with pages, dates and ratings for books
    that the RSS misses (no date_read set).
    """
    if not CSV_FILE.exists():
        print("  INFO: No CSV file found, skipping CSV enrichment.")
        return {}

    books = {}
    with open(CSV_FILE, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Clean book id
            try:
                book_id = int(row.get("Book Id", 0) or 0)
            except ValueError:
                book_id = 0

            # Clean ISBN
            def clean_isbn(val):
                digits = re.sub(r"[^0-9]", "", str(val or ""))
                return digits if len(digits) >= 10 else ""

            isbn = clean_isbn(row.get("ISBN13", "")) or clean_isbn(row.get("ISBN", ""))

            # Pages
            try:
                pages = int(row.get("Number of Pages", 0) or 0)
            except ValueError:
                pages = 0

            # Rating
            try:
                rating = int(row.get("My Rating", 0) or 0)
            except ValueError:
                rating = 0

            # Avg rating
            try:
                avg_rating = float(row.get("Average Rating", 0) or 0)
            except ValueError:
                avg_rating = 0.0

            # Date read
            date_read_raw = row.get("Date Read", "").strip()
            date_read = ""
            date_read_iso = ""
            if date_read_raw:
                try:
                    dt = datetime.strptime(date_read_raw, "%Y/%m/%d")
                    date_read = dt.strftime("%d %b %Y")
                    date_read_iso = dt.strftime("%Y-%m-%d")
                except Exception:
                    date_read_iso = date_read_raw[:10]

            shelf = row.get("Exclusive Shelf", "").strip()

            books[book_id] = {
                "id": book_id,
                "title": row.get("Title", "").strip(),
                "author": row.get("Author", "").strip(),
                "isbn": isbn,
                "rating": rating,
                "avg_rating": avg_rating,
                "pages": pages,
                "date": date_read,
                "date_iso": date_read_iso,
                "date_added": row.get("Date Added", "")[:10],
                "publisher": row.get("Publisher", "").strip(),
                "binding": row.get("Binding", "").strip(),
                "year_pub": row.get("Year Published", "").strip(),
                "review": re.sub(r"<[^>]+>", " ", row.get("My Review", "") or "").strip(),
                "link": f"https://www.goodreads.com/book/show/{book_id}" if book_id else "",
                "image_url": "",
                "shelf": shelf,
            }

    print(f"  Loaded {len(books)} books from CSV")
    return books

def merge_rss_and_csv(rss_books, csv_books):
    """
    Merge RSS books (fresh, has image_url) with CSV books (complete, has pages).
    Strategy:
    - RSS books are authoritative for recent data (image_url, date_read)
    - CSV fills in pages, and adds books missing from RSS (no date_read)
    - Result is deduplicated by book_id
    """
    # Build title index from CSV for fuzzy matching when id=0
    csv_by_title = {}
    for book_id, b in csv_books.items():
        key = b["title"].strip().lower()[:60]
        csv_by_title[key] = book_id

    merged = {}

    # Start with CSV books as the base (all 1372 books)
    for book_id, b in csv_books.items():
        if b["shelf"] == "read":
            merged[book_id] = dict(b)

    print(f"  CSV read books: {len(merged)}")

    # Overlay RSS data: adds image_url and fresher dates
    rss_matched = 0
    rss_new = 0
    for b in rss_books:
        book_id = b["id"]

        # If id=0, try to match by title
        if not book_id:
            title_key = b["title"].strip().lower()[:60]
            book_id = csv_by_title.get(title_key, 0)

        if book_id and book_id in merged:
            # Enrich existing CSV entry with RSS data
            csv_pages = merged[book_id].get("pages", 0)
            csv_review = merged[book_id].get("review", "")
            merged[book_id].update(b)
            merged[book_id]["id"] = book_id
            # Prefer CSV pages (RSS has none) and longer review
            if not b.get("pages") and csv_pages:
                merged[book_id]["pages"] = csv_pages
            if len(csv_review) > len(b.get("review", "")):
                merged[book_id]["review"] = csv_review
            rss_matched += 1
        # If book_id=0 and not in CSV, skip — don't add duplicates

    print(f"  RSS books matched to CSV: {rss_matched}")
    print(f"  New books only in RSS (no date in CSV): {rss_new}")

    result = list(merged.values())
    result.sort(key=lambda b: b.get("date_iso", ""), reverse=True)
    print(f"  Merged total: {len(result)} read books")
    return result

# ── COVER DOWNLOADER ──────────────────────────────────────────────────────────

def cover_filename(book_id, isbn):
    """Stable filename for a book cover."""
    key = str(book_id) if book_id else isbn
    return f"{key}.jpg" if key else None

def download_cover(url, dest_path):
    """Download a single cover image. Returns True on success."""
    if not url or "nophoto" in url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        # Reject tiny placeholder images (< 1KB)
        if len(data) < 1000:
            return False
        dest_path.write_bytes(data)
        return True
    except Exception:
        return False

def ensure_covers(books):
    """
    Download missing covers for all books.
    Skips books whose cover file already exists (cache).
    Returns dict: book_id/isbn -> relative URL path like 'covers/123.jpg'
    """
    COVERS_DIR.mkdir(exist_ok=True)
    cover_map = {}
    total = len(books)

    for i, b in enumerate(books):
        fname = cover_filename(b.get("id"), b.get("isbn", ""))
        if not fname:
            continue

        dest = COVERS_DIR / fname
        rel_path = f"covers/{fname}"

        # Already downloaded → reuse
        if dest.exists() and dest.stat().st_size > 1000:
            cover_map[fname] = rel_path
            continue

        # Try sources in order: Goodreads RSS image → Open Library ISBN
        sources = []
        if b.get("image_url") and "nophoto" not in b.get("image_url", ""):
            sources.append(b["image_url"])
        if b.get("isbn"):
            sources.append(f"https://covers.openlibrary.org/b/isbn/{b['isbn']}-L.jpg")
            sources.append(f"https://covers.openlibrary.org/b/isbn/{b['isbn']}-M.jpg")

        downloaded = False
        for url in sources:
            if download_cover(url, dest):
                cover_map[fname] = rel_path
                downloaded = True
                print(f"  [{i+1}/{total}] ✓ {b.get('title','')[:40]}")
                break

        if not downloaded:
            print(f"  [{i+1}/{total}] – no cover: {b.get('title','')[:40]}")

        # Be polite to external servers
        time.sleep(0.3)

    print(f"  Covers ready: {len(cover_map)} / {total}")
    return cover_map

# ── RSS FETCH ──────────────────────────────────────────────────────────────────

def fetch_rss_page(shelf, page, per_page=200):
    """Fetch a single page from Goodreads RSS feed."""
    url = (f"https://www.goodreads.com/review/list_rss/{GOODREADS_USER_ID}"
           f"?shelf={shelf}&per_page={per_page}&page={page}&sort=date_read&order=d")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        print(f"  WARNING: Could not fetch {shelf} page {page}: {e}")
        return None

def fetch_rss(shelf="read", per_page=200):
    """Fetch ALL books from a shelf by paginating through RSS feed."""
    print(f"Fetching '{shelf}' shelf (paginated)...")
    all_xml_pages = []
    page = 1
    while True:
        xml = fetch_rss_page(shelf, page, per_page)
        if not xml:
            break
        # Count items in this page
        try:
            root = ET.fromstring(xml)
            items = root.findall(".//item")
        except ET.ParseError:
            break
        if not items:
            break
        all_xml_pages.append(xml)
        print(f"  Page {page}: {len(items)} books")
        if len(items) < per_page:
            break  # Last page
        page += 1
        time.sleep(1)  # Be polite between pages
    return all_xml_pages

def parse_rss(xml_pages):
    """Parse list of Goodreads RSS XML pages into list of book dicts."""
    if not xml_pages:
        return []
    # Handle both old single-bytes and new list-of-pages format
    if isinstance(xml_pages, bytes):
        xml_pages = [xml_pages]

    all_books = []
    for xml_bytes in xml_pages:
        if not xml_bytes:
            continue
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as e:
            print(f"  WARNING: XML parse error: {e}")
            continue

        for item in root.findall(".//item"):
            def get(tag, default="", _item=item):
                el = _item.find(tag)
                return (el.text or "").strip() if el is not None else default

            isbn = re.sub(r"[^0-9]", "", get("isbn13") or get("isbn"))
            if len(isbn) < 10:
                isbn = ""
            try:
                rating = int(get("user_rating") or 0)
            except ValueError:
                rating = 0
            try:
                pages = int(get("num_pages") or 0)
            except ValueError:
                pages = 0
            try:
                avg_rating = float(get("average_rating") or 0)
            except ValueError:
                avg_rating = 0.0
            date_read_raw = get("user_read_at") or get("pubDate")
            date_read = ""
            date_read_iso = ""
            try:
                dt = datetime.strptime(date_read_raw[:25].strip(), "%a, %d %b %Y %H:%M:%S")
                date_read = dt.strftime("%d %b %Y")
                date_read_iso = dt.strftime("%Y-%m-%d")
            except Exception:
                date_read = date_read_raw[:10] if date_read_raw else ""
            review_raw = get("user_review")
            review = re.sub(r"<[^>]+>", " ", review_raw).strip()
            review = re.sub(r"\s+", " ", review)
            link = get("link")
            book_id_match = re.search(r"/show/(\d+)", link)
            book_id = int(book_id_match.group(1)) if book_id_match else 0

            all_books.append({
                "id": book_id,
                "title": get("title"),
                "author": get("author_name"),
                "isbn": isbn,
                "rating": rating,
                "avg_rating": avg_rating,
                "pages": pages,
                "date": date_read,
                "date_iso": date_read_iso,
                "date_added": get("user_date_added")[:10] if get("user_date_added") else "",
                "publisher": get("publisher"),
                "binding": get("binding"),
                "year_pub": get("book_published"),
                "review": review[:2000],
                "link": link,
                "image_url": get("book_large_image_url") or get("book_medium_image_url") or get("book_small_image_url"),
            })

    print(f"  Parsed {len(all_books)} books total")
    return all_books

# ── DATA PROCESSING ───────────────────────────────────────────────────────────

def process_data(read_books, current_books, toread_books):
    """Turn raw book lists into structured data for the template."""

    rated = [b for b in read_books if b["rating"] > 0]

    # Stats
    total_pages = sum(b["pages"] for b in read_books if b["pages"])
    avg_rating = round(sum(b["rating"] for b in rated) / len(rated), 2) if rated else 0
    unique_authors = len(set(b["author"] for b in read_books))
    five_stars = sum(1 for b in read_books if b["rating"] == 5)

    stats = {
        "total_read": len(read_books),
        "avg_rating": avg_rating,
        "total_pages": total_pages,
        "to_read": len(toread_books),
        "five_stars": five_stars,
        "unique_authors": unique_authors,
        "updated": datetime.now().strftime("%d %b %Y"),
    }

    # Books per year
    year_counts = {}
    for b in read_books:
        year = b["date_iso"][:4] if b["date_iso"] else None
        if year and year.isdigit():
            year_counts[int(year)] = year_counts.get(int(year), 0) + 1
    years = [{"year": y, "count": c} for y, c in sorted(year_counts.items())]

    # Rating distribution
    rating_counts = {}
    for b in rated:
        rating_counts[b["rating"]] = rating_counts.get(b["rating"], 0) + 1
    ratings = [{"stars": s, "count": rating_counts.get(s, 0)}
               for s in sorted(rating_counts.keys(), reverse=True)]

    # Recent reads: prioritize books with image_url
    recent = sorted(
        [b for b in read_books if b.get("image_url")],
        key=lambda b: b["date_iso"], reverse=True
    )[:9]
    # Fill up to 9 if not enough with images
    if len(recent) < 9:
        seen = {b["id"] for b in recent}
        extra = [b for b in read_books if b["id"] not in seen][:9-len(recent)]
        recent += extra

    # Top 5-star books with images
    top5 = sorted(
        [b for b in read_books if b["rating"] == 5 and b.get("image_url")],
        key=lambda b: b["date_iso"], reverse=True
    )[:12]
    if len(top5) < 6:
        seen = {b["id"] for b in top5}
        extra = [b for b in read_books if b["rating"] == 5 and b["id"] not in seen][:12-len(top5)]
        top5 += extra

    # Top authors (3+ books)
    author_data = {}
    for b in read_books:
        a = b["author"]
        if a not in author_data:
            author_data[a] = {"count": 0, "ratings": [], "pages": 0}
        author_data[a]["count"] += 1
        if b["rating"] > 0:
            author_data[a]["ratings"].append(b["rating"])
        author_data[a]["pages"] += b["pages"]

    top_authors = []
    for name, d in author_data.items():
        if d["count"] >= 3:
            avg = round(sum(d["ratings"]) / len(d["ratings"]), 1) if d["ratings"] else 0
            top_authors.append({"name": name, "count": d["count"], "avg": avg, "pages": d["pages"]})
    top_authors.sort(key=lambda x: x["count"], reverse=True)
    top_authors = top_authors[:10]

    # Shelf (books with images for visual display)
    shelf_books = []
    seen_ids = set()
    for b in (top5 + recent + read_books):
        if b["id"] not in seen_ids and b.get("image_url"):
            shelf_books.append(b)
            seen_ids.add(b["id"])
        if len(shelf_books) >= 24:
            break

    return {
        "stats": stats,
        "years": years,
        "ratings": ratings,
        "recent": recent,
        "top5": top5,
        "top_authors": top_authors,
        "current": current_books[:3],
        "toread": toread_books[:15],
        "shelf": shelf_books,
        "all_books": read_books,  # full list for modals
    }

# ── HTML TEMPLATE ─────────────────────────────────────────────────────────────

def generate_html(data):
    """Inject data into HTML template and return full page string."""

    def js(obj):
        return json.dumps(obj, ensure_ascii=False)

    updated = data["stats"]["updated"]
    total = data["stats"]["total_read"]

    # Inline the JS data
    data_block = f"""
const STATS   = {js(data['stats'])};
const YEARS   = {js(data['years'])};
const RATINGS = {js(data['ratings'])};
const RECENT  = {js(data['recent'])};
const TOP5    = {js(data['top5'])};
const AUTHORS = {js(data['top_authors'])};
const CURRENT = {js(data['current'])};
const TOREAD  = {js(data['toread'])};
const SHELF   = {js(data['shelf'])};
const ALL_BOOKS = {js(data['all_books'])};
"""

    return HTML_TEMPLATE.replace("/* __DATA__ */", data_block).replace(
        "<!-- __UPDATED__ -->", f"Actualizado el {updated} · {total} libros leídos"
    )

# ── HTML TEMPLATE (inline) ────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mi Biblioteca · Diario de Lecturas</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;0,900;1,400;1,700&family=Lora:ital,wght@0,400;0,500;1,400&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#1a1208;--paper:#f7f2e8;--cream:#ede7d5;
  --amber:#c8882a;--amber-light:#e8b060;
  --rust:#8b3a1a;--dusk:#3d3560;--sage:#5a7a5a;
  --muted:#7a6f5e;--line:rgba(26,18,8,.12);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{font-family:'Lora',Georgia,serif;background:var(--paper);color:var(--ink);line-height:1.7;overflow-x:hidden}
body.modal-open{overflow:hidden}
body::before{content:'';position:fixed;inset:0;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.04'/%3E%3C/svg%3E");pointer-events:none;z-index:9999;opacity:.4}
/* HEADER */
header{position:relative;background:var(--ink);color:var(--paper);padding:80px 40px 60px;overflow:hidden}
header::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 80% 60% at 70% 50%,rgba(200,136,42,.18),transparent 70%),radial-gradient(ellipse 50% 80% at 20% 80%,rgba(61,53,96,.4),transparent 60%)}
.header-inner{position:relative;max-width:1100px;margin:0 auto;display:grid;grid-template-columns:1fr auto;align-items:end;gap:40px}
.eyebrow{font-family:'DM Mono',monospace;font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--amber-light);margin-bottom:16px;opacity:0;animation:fadeUp .6s .1s forwards}
header h1{font-family:'Playfair Display',serif;font-size:clamp(52px,7vw,96px);font-weight:900;line-height:.95;letter-spacing:-2px;opacity:0;animation:fadeUp .7s .2s forwards}
header h1 em{font-style:italic;color:var(--amber-light)}
.header-sub{font-family:'Lora',serif;font-size:16px;font-style:italic;color:rgba(247,242,232,.6);margin-top:20px;opacity:0;animation:fadeUp .7s .35s forwards}
.header-pills{display:flex;flex-direction:column;gap:16px;text-align:right;opacity:0;animation:fadeUp .7s .5s forwards}
.pill{background:rgba(247,242,232,.07);border:1px solid rgba(247,242,232,.12);border-radius:4px;padding:12px 20px}
.pill .num{display:block;font-family:'Playfair Display',serif;font-size:30px;font-weight:700;color:var(--amber-light);line-height:1}
.pill .lbl{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:rgba(247,242,232,.5);margin-top:3px}
/* NAV */
nav{background:var(--ink);border-top:1px solid rgba(247,242,232,.08);position:sticky;top:0;z-index:200}
.nav-inner{max-width:1100px;margin:0 auto;padding:0 40px;display:flex;overflow-x:auto}
nav a{font-family:'DM Mono',monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase;color:rgba(247,242,232,.5);text-decoration:none;padding:16px 20px;border-bottom:2px solid transparent;transition:color .2s,border-color .2s;white-space:nowrap}
nav a:hover{color:var(--amber-light);border-bottom-color:var(--amber-light)}
/* MAIN */
main{max-width:1100px;margin:0 auto;padding:0 40px}
section{padding:80px 0;border-bottom:1px solid var(--line)}
section:last-child{border-bottom:none}
.s-label{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:4px;text-transform:uppercase;color:var(--amber);margin-bottom:12px}
.s-title{font-family:'Playfair Display',serif;font-size:clamp(26px,4vw,42px);font-weight:700;line-height:1.1;margin-bottom:48px}
.s-title em{font-style:italic;color:var(--amber)}
.ornament{text-align:center;color:var(--amber);font-size:18px;letter-spacing:12px;margin:0 0 40px;opacity:.5}
.section-note{font-style:italic;color:var(--muted);font-size:14px;margin-bottom:32px;margin-top:-32px}
/* STATS */
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:2px;background:var(--line);border:1px solid var(--line);margin-bottom:60px}
.stat-block{background:var(--paper);padding:36px 24px;position:relative;overflow:hidden;transition:background .3s}
.stat-block::after{content:'';position:absolute;bottom:0;left:0;right:0;height:3px;background:var(--amber);transform:scaleX(0);transform-origin:left;transition:transform .4s}
.stat-block:hover{background:var(--cream)}
.stat-block:hover::after{transform:scaleX(1)}
.stat-block .big{font-family:'Playfair Display',serif;font-size:clamp(32px,4vw,52px);font-weight:900;color:var(--ink);line-height:1;display:block}
.stat-block .unit{font-family:'DM Mono',monospace;font-size:11px;color:var(--amber);letter-spacing:2px;text-transform:uppercase}
.stat-block .desc{font-size:13px;color:var(--muted);margin-top:6px;font-style:italic}
/* CHARTS */
.charts-row{display:grid;grid-template-columns:1fr 1fr;gap:60px}
.chart-label{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:var(--muted);margin-bottom:16px}
.bars-wrap{display:flex;align-items:flex-end;gap:5px;height:130px}
.bar-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:5px;height:100%;justify-content:flex-end}
.bv{font-family:'DM Mono',monospace;font-size:8px;color:var(--muted);opacity:0;transition:opacity .2s}
.bar-col:hover .bv{opacity:1}
.bar{width:100%;background:var(--cream);border:1px solid var(--line);border-radius:2px 2px 0 0;min-height:2px;transform-origin:bottom;animation:growUp 1s cubic-bezier(.34,1.56,.64,1) forwards;transform:scaleY(0);transition:background .3s}
.bar:hover,.bar.hi{background:var(--amber);border-color:var(--amber)}
.bar-yr{font-family:'DM Mono',monospace;font-size:8px;color:var(--muted);transform:rotate(-45deg);white-space:nowrap}
.rating-row{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.r-stars{font-size:13px;width:75px;color:var(--amber);flex-shrink:0}
.r-track{flex:1;height:8px;background:var(--cream);border-radius:4px;overflow:hidden}
.r-fill{height:100%;background:var(--amber);border-radius:4px;transform:scaleX(0);transform-origin:left;animation:scaleIn 1s .5s cubic-bezier(.34,1.56,.64,1) forwards}
.r-count{font-family:'DM Mono',monospace;font-size:11px;color:var(--muted);width:40px;text-align:right}
/* BOOK CARDS */
.books-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:28px}
.book-card{background:var(--paper);border:1px solid var(--line);overflow:hidden;transition:transform .3s,box-shadow .3s;display:flex;flex-direction:column;cursor:pointer}
.book-card:hover{transform:translateY(-6px);box-shadow:0 24px 60px rgba(26,18,8,.14)}
.book-cover-wrap{position:relative;height:220px;overflow:hidden;background:var(--cream);flex-shrink:0}
.book-cover-wrap img{width:100%;height:100%;object-fit:cover;transition:transform .5s}
.book-card:hover .book-cover-wrap img{transform:scale(1.05)}
.cover-ph{width:100%;height:100%;display:flex;align-items:center;justify-content:center;padding:20px;text-align:center}
.cover-ph-text{font-family:'Playfair Display',serif;font-size:13px;font-weight:700;font-style:italic;color:var(--ink);line-height:1.4}
.cover-ph.c0{background:linear-gradient(145deg,#e8dfc8,#d4c8a8)}
.cover-ph.c1{background:linear-gradient(145deg,#d0e0d8,#b8ccc4)}
.cover-ph.c2{background:linear-gradient(145deg,#d8d0e8,#c0b4d4)}
.cover-ph.c3{background:linear-gradient(145deg,#e8d0d0,#d4b8b8)}
.cover-ph.c4{background:linear-gradient(145deg,#d0d8e8,#b8c4d4)}
.book-body{padding:20px 20px 16px;display:flex;flex-direction:column;flex:1}
.book-date{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:1px;margin-bottom:8px}
.book-title{font-family:'Playfair Display',serif;font-size:16px;font-weight:700;line-height:1.3;margin-bottom:5px}
.book-author{font-size:12px;font-style:italic;color:var(--muted);margin-bottom:12px}
.book-review{font-size:13px;color:var(--ink);line-height:1.65;opacity:.75;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;flex:1}
.book-pages{margin-top:12px;padding-top:12px;border-top:1px solid var(--line);font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:1px;text-transform:uppercase}
.book-card-hint{font-family:'DM Mono',monospace;font-size:9px;color:var(--amber);opacity:0;transition:opacity .3s;margin-top:6px;letter-spacing:1px}
.book-card:hover .book-card-hint{opacity:1}
/* FEATURED */
.featured{display:grid;grid-template-columns:220px 1fr;margin-bottom:48px;background:var(--ink);color:var(--paper);position:relative;overflow:hidden;cursor:pointer;transition:box-shadow .3s}
.featured:hover{box-shadow:0 16px 48px rgba(26,18,8,.3)}
.featured::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 60% 80% at 90% 50%,rgba(200,136,42,.15),transparent 70%)}
.featured-img{height:300px;overflow:hidden;flex-shrink:0}
.featured-img img{width:100%;height:100%;object-fit:cover}
.featured-img .cover-ph{height:100%}
.feat-body{padding:44px 52px;position:relative;z-index:1;display:flex;flex-direction:column;justify-content:center}
.feat-badge{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:var(--amber-light);margin-bottom:16px}
.feat-title{font-family:'Playfair Display',serif;font-size:clamp(20px,2.5vw,30px);font-weight:900;line-height:1.2;margin-bottom:8px}
.feat-author{font-style:italic;font-size:15px;color:rgba(247,242,232,.6);margin-bottom:20px}
.feat-review{font-size:14px;line-height:1.75;color:rgba(247,242,232,.8);display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.feat-hint{font-family:'DM Mono',monospace;font-size:9px;color:var(--amber-light);opacity:.6;margin-top:16px;letter-spacing:1px}
/* SHELF */
.shelf-wrap{position:relative}
.shelf-row{display:flex;gap:4px;align-items:flex-end;padding:0 10px 12px;position:relative;flex-wrap:wrap}
.shelf-row::after{content:'';position:absolute;bottom:0;left:0;right:0;height:12px;background:linear-gradient(to bottom,#8b6914,#6b4e10);border-radius:2px;box-shadow:0 4px 12px rgba(0,0,0,.3);z-index:1}
.shelf-item{flex-shrink:0;width:72px;cursor:pointer;position:relative;transition:transform .3s;transform-origin:bottom;z-index:2}
.shelf-item:hover{transform:translateY(-10px)}
.shelf-cover{height:100px;width:72px;overflow:hidden;border-radius:1px 3px 3px 1px;box-shadow:2px 2px 6px rgba(0,0,0,.3)}
.shelf-cover img{width:100%;height:100%;object-fit:cover}
.shelf-cover .cover-ph{height:100%;font-size:9px}
.shelf-tooltip{position:absolute;bottom:115px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--paper);padding:8px 12px;font-size:11px;opacity:0;pointer-events:none;transition:opacity .2s;z-index:10;width:150px;text-align:center;border-radius:2px;line-height:1.3}
.shelf-tooltip::after{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:5px solid transparent;border-top-color:var(--ink)}
.shelf-item:hover .shelf-tooltip{opacity:1}
/* AUTHORS */
.authors-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:2px;background:var(--line);border:1px solid var(--line)}
.author-row{background:var(--paper);padding:18px 24px;display:grid;grid-template-columns:1fr auto auto auto;align-items:center;gap:16px;transition:background .2s;position:relative}
.author-row::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--amber);transform:scaleY(0);transition:transform .3s}
.author-row:hover{background:var(--cream)}
.author-row:hover::before{transform:scaleY(1)}
.a-name{font-family:'Playfair Display',serif;font-size:15px;font-weight:700}
.a-books{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;text-align:right}
.a-rating{font-family:'DM Mono',monospace;font-size:11px;color:var(--amber);min-width:45px;text-align:right}
.a-pages{font-family:'DM Mono',monospace;font-size:10px;color:var(--muted);text-align:right}
/* CURRENTLY READING */
.current-wrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px}
.current-card{background:var(--cream);border:1px solid var(--line);overflow:hidden;display:flex;cursor:pointer;transition:box-shadow .3s}
.current-card:hover{box-shadow:0 8px 30px rgba(26,18,8,.12)}
.current-spine{width:8px;background:var(--amber);flex-shrink:0}
.current-body{padding:24px;flex:1}
.current-lbl{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:var(--amber);margin-bottom:10px}
.current-cover-mini{width:56px;height:76px;overflow:hidden;border-radius:2px;float:right;margin-left:14px;margin-bottom:8px;box-shadow:2px 2px 8px rgba(0,0,0,.2)}
.current-cover-mini img{width:100%;height:100%;object-fit:cover}
.current-title{font-family:'Playfair Display',serif;font-size:18px;font-weight:700;line-height:1.3}
.current-author{font-style:italic;color:var(--muted);font-size:13px;margin-top:4px}
.reading-dot{display:inline-block;width:8px;height:8px;background:var(--amber);border-radius:50%;margin-right:6px;animation:pulse 2s infinite}
/* MODAL */
.modal-overlay{position:fixed;inset:0;background:rgba(26,18,8,.7);z-index:1000;display:flex;align-items:center;justify-content:center;padding:24px;backdrop-filter:blur(6px);opacity:0;pointer-events:none;transition:opacity .3s}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal{background:var(--paper);max-width:780px;width:100%;max-height:90vh;overflow-y:auto;position:relative;animation:modalIn .35s cubic-bezier(.34,1.56,.64,1) forwards;border-radius:2px}
@keyframes modalIn{from{transform:translateY(40px) scale(.96);opacity:0}to{transform:translateY(0) scale(1);opacity:1}}
.modal-close{position:absolute;top:16px;right:16px;background:var(--ink);color:var(--paper);border:none;width:32px;height:32px;border-radius:50%;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;z-index:10;transition:background .2s}
.modal-close:hover{background:var(--amber)}
.modal-header{display:grid;grid-template-columns:160px 1fr;min-height:240px}
.modal-cover{overflow:hidden;background:var(--cream);flex-shrink:0}
.modal-cover img{width:100%;height:100%;object-fit:cover}
.modal-cover .cover-ph{height:240px}
.modal-header-body{background:var(--ink);color:var(--paper);padding:32px 36px;display:flex;flex-direction:column;justify-content:flex-end;position:relative}
.modal-header-body::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 80% 60% at 100% 0%,rgba(200,136,42,.2),transparent 70%)}
.modal-header-body>*{position:relative;z-index:1}
.modal-badge{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:3px;text-transform:uppercase;color:var(--amber-light);margin-bottom:12px}
.modal-title{font-family:'Playfair Display',serif;font-size:clamp(18px,3vw,26px);font-weight:900;line-height:1.2;margin-bottom:8px}
.modal-author{font-style:italic;font-size:15px;color:rgba(247,242,232,.7);margin-bottom:8px}
.modal-body{padding:32px 36px}
.modal-meta-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:2px;background:var(--line);border:1px solid var(--line);margin-bottom:28px}
.meta-item{background:var(--paper);padding:16px 18px}
.meta-key{font-family:'DM Mono',monospace;font-size:9px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
.meta-val{font-family:'Playfair Display',serif;font-size:16px;font-weight:700;color:var(--ink)}
.meta-val.small{font-size:13px;font-family:'Lora',serif;font-weight:400}
.modal-section-title{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:var(--amber);margin-bottom:14px;padding-bottom:8px;border-bottom:1px solid var(--line)}
.modal-review{font-size:14px;line-height:1.85;color:var(--ink);margin-bottom:28px}
.modal-review p{margin-bottom:12px}
.modal-links{display:flex;gap:12px;flex-wrap:wrap}
.modal-link{font-family:'DM Mono',monospace;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--paper);background:var(--ink);padding:10px 18px;text-decoration:none;border-radius:2px;transition:background .2s}
.modal-link:hover{background:var(--amber)}
.modal-link.outline{background:transparent;color:var(--ink);border:1px solid var(--line)}
.modal-link.outline:hover{background:var(--cream)}
/* ANIMATIONS */
@keyframes fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
@keyframes growUp{from{transform:scaleY(0)}to{transform:scaleY(1)}}
@keyframes scaleIn{from{transform:scaleX(0)}to{transform:scaleX(1)}}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.8)}}
.fi{opacity:0;transform:translateY(24px);transition:opacity .7s,transform .7s}
.fi.vis{opacity:1;transform:translateY(0)}
/* FOOTER */
footer{background:var(--ink);color:rgba(247,242,232,.4);text-align:center;padding:48px 40px}
footer p{font-family:'DM Mono',monospace;font-size:11px;letter-spacing:2px;text-transform:uppercase}
footer strong{color:var(--amber-light)}
/* RESPONSIVE */
@media(max-width:768px){
  header{padding:60px 24px 40px}.header-inner{grid-template-columns:1fr}
  .header-pills{flex-direction:row;flex-wrap:wrap;text-align:left}
  .nav-inner{padding:0 24px}
  main{padding:0 24px}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  .charts-row{grid-template-columns:1fr}
  .books-grid{grid-template-columns:1fr}
  .featured{grid-template-columns:1fr}.featured-img{display:none}.feat-body{padding:28px}
  .authors-grid{grid-template-columns:1fr}
  .modal-header{grid-template-columns:1fr}.modal-cover{display:none}
  .modal-meta-grid{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>

<div class="modal-overlay" id="modalOverlay" onclick="closeModalOnBg(event)">
  <div class="modal" id="modal">
    <button class="modal-close" onclick="closeModal()">✕</button>
    <div id="modalContent"></div>
  </div>
</div>

<header>
  <div class="header-inner">
    <div>
      <p class="eyebrow">✦ Diario de lecturas · desde 2011</p>
      <h1>Mi<br><em>Biblioteca</em></h1>
      <p class="header-sub">Un rincón donde los libros dejan huella.</p>
    </div>
    <div class="header-pills" id="headerPills"></div>
  </div>
</header>
<nav>
  <div class="nav-inner">
    <a href="#estadisticas">Estadísticas</a>
    <a href="#ultimas">Últimas lecturas</a>
    <a href="#favoritos">Favoritos</a>
    <a href="#estanteria">Estantería</a>
    <a href="#autores">Autores</a>
    <a href="#leyendo">Leyendo ahora</a>
  </div>
</nav>
<main>
  <section id="estadisticas">
    <p class="s-label">✦ En números</p>
    <h2 class="s-title">Mis <em>estadísticas</em></h2>
    <div class="stats-grid" id="statsGrid"></div>
    <div class="charts-row fi">
      <div><p class="chart-label">Libros leídos por año</p><div class="bars-wrap" id="yearChart"></div></div>
      <div><p class="chart-label">Distribución de puntuaciones</p><div id="ratingBars"></div></div>
    </div>
  </section>
  <section id="ultimas">
    <p class="s-label">✦ Recién cerradas</p>
    <h2 class="s-title">Últimas <em>lecturas</em></h2>
    <div class="ornament">— ✦ —</div>
    <div class="books-grid fi" id="recentGrid"></div>
  </section>
  <section id="favoritos">
    <p class="s-label">✦ Destacados</p>
    <h2 class="s-title">Mis libros <em>favoritos</em></h2>
    <div class="ornament">— ✦ —</div>
    <div id="featuredBook" class="fi"></div>
    <div class="books-grid fi" id="topGrid"></div>
  </section>
  <section id="estanteria">
    <p class="s-label">✦ Visualmente</p>
    <h2 class="s-title">Mi <em>estantería</em></h2>
    <p class="section-note">Pasa el cursor por cada libro para ver el título. Haz clic para abrir la ficha.</p>
    <div class="shelf-wrap fi"><div class="shelf-row" id="shelfRow"></div></div>
  </section>
  <section id="autores">
    <p class="s-label">✦ Mis autores</p>
    <h2 class="s-title">Voces que <em>repito</em></h2>
    <p class="section-note">Autores con 3 o más libros leídos.</p>
    <div class="authors-grid fi" id="authorsGrid"></div>
  </section>
  <section id="leyendo">
    <p class="s-label">✦ Ahora mismo</p>
    <h2 class="s-title">Lo que estoy <em>leyendo</em></h2>
    <div class="current-wrap fi" id="currentGrid"></div>
  </section>
</main>
<footer>
  <p>Blog de lecturas · <strong>Mi Biblioteca</strong></p>
  <p style="margin-top:8px"><!-- __UPDATED__ --></p>
</footer>

<script>
/* __DATA__ */

// ── UTILS ─────────────────────────────────────────────────────
const PLH = ['c0','c1','c2','c3','c4'];
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function cleanTitle(t){return String(t||'').replace(/\s*\([^)]*[Ee]dition[^)]*\)/g,'').replace(/\s*\([^)]*[Ee]dición[^)]*\)/g,'').trim()}

function coverHtml(b, idx=0){
  const title = cleanTitle(b.title);
  const ph = `<div class="cover-ph ${PLH[idx%5]}"><span class="cover-ph-text">${esc(title)}</span></div>`;
  // Use image_url from RSS (Goodreads CDN) — works perfectly on web
  if(b.image_url && !b.image_url.includes('nophoto')){
    return `<img src="${esc(b.image_url)}" alt="${esc(title)}" loading="lazy"
      onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">${ph.replace('style="display:none"','').replace('<div','<div style="display:none"')}`;
  }
  // Fallback: try Open Library by ISBN
  if(b.isbn){
    return `<img src="https://covers.openlibrary.org/b/isbn/${b.isbn}-M.jpg" alt="${esc(title)}" loading="lazy"
      onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">${ph.replace('style="display:none"','').replace('<div','<div style="display:none"')}`;
  }
  return ph;
}

// Build fast lookup index
const bookIndex = {};
ALL_BOOKS.forEach(b => { if(b.id) bookIndex[b.id] = b; });

// ── MODAL ──────────────────────────────────────────────────────
function openBook(b){
  if(!b) return;
  const title = cleanTitle(b.title);
  const review = b.review
    ? b.review.split(/\n+/).filter(p=>p.trim()).map(p=>`<p>${esc(p)}</p>`).join('')
    : '';
  const grUrl = b.link || (b.id ? `https://www.goodreads.com/book/show/${b.id}` : null);

  document.getElementById('modalContent').innerHTML = `
    <div class="modal-header">
      <div class="modal-cover">${coverHtml(b,0)}</div>
      <div class="modal-header-body">
        <p class="modal-badge">✦ Leído</p>
        <h2 class="modal-title">${esc(title)}</h2>
        <p class="modal-author">${esc(b.author)}</p>
      </div>
    </div>
    <div class="modal-body">
      <div class="modal-meta-grid">
        ${b.date?`<div class="meta-item"><p class="meta-key">Fecha leída</p><p class="meta-val small">${esc(b.date)}</p></div>`:''}
        ${b.pages>0?`<div class="meta-item"><p class="meta-key">Páginas</p><p class="meta-val">${b.pages.toLocaleString()}</p></div>`:''}
        ${b.avg_rating>0?`<div class="meta-item"><p class="meta-key">Rating Goodreads</p><p class="meta-val">${b.avg_rating.toFixed(2)} ★</p></div>`:''}
        ${b.year_pub?`<div class="meta-item"><p class="meta-key">Publicación</p><p class="meta-val">${esc(b.year_pub)}</p></div>`:''}
        ${b.publisher?`<div class="meta-item"><p class="meta-key">Editorial</p><p class="meta-val small">${esc(b.publisher)}</p></div>`:''}
        ${b.binding?`<div class="meta-item"><p class="meta-key">Formato</p><p class="meta-val small">${esc(b.binding)}</p></div>`:''}
        ${b.isbn?`<div class="meta-item"><p class="meta-key">ISBN</p><p class="meta-val small" style="font-family:'DM Mono',monospace;font-size:12px">${esc(b.isbn)}</p></div>`:''}
      </div>
      ${review?`<p class="modal-section-title">Mi reseña</p><div class="modal-review">${review}</div>`:''}
      <p class="modal-section-title">Más información</p>
      <div class="modal-links">
        ${grUrl?`<a class="modal-link" href="${esc(grUrl)}" target="_blank" rel="noopener">Ver en Goodreads →</a>`:''}
        <a class="modal-link outline" href="https://www.google.com/search?q=${encodeURIComponent((b.title||'')+' '+(b.author||''))}" target="_blank" rel="noopener">Buscar en Google →</a>
      </div>
    </div>`;

  document.getElementById('modalOverlay').classList.add('open');
  document.body.classList.add('modal-open');
}

function closeModal(){
  document.getElementById('modalOverlay').classList.remove('open');
  document.body.classList.remove('modal-open');
}
function closeModalOnBg(e){ if(e.target===document.getElementById('modalOverlay')) closeModal(); }
document.addEventListener('keydown', e=>{ if(e.key==='Escape') closeModal(); });

// ── RENDERS ────────────────────────────────────────────────────
function renderHeaderPills(){
  document.getElementById('headerPills').innerHTML=[
    {num:STATS.total_read.toLocaleString(),lbl:'libros leídos'},
    {num:Math.round(STATS.total_pages/1000)+'K',lbl:'páginas'},
    {num:STATS.avg_rating+'★',lbl:'puntuación media'},
  ].map(i=>`<div class="pill"><span class="num">${i.num}</span><span class="lbl">${i.lbl}</span></div>`).join('');
}

function renderStats(){
  document.getElementById('statsGrid').innerHTML=[
    {big:STATS.total_read.toLocaleString(),unit:'libros leídos',desc:'Desde 2011 hasta hoy'},
    {big:STATS.total_pages.toLocaleString(),unit:'páginas',desc:'¡Toda una vida en páginas!'},
    {big:STATS.avg_rating+'★',unit:'puntuación media',desc:'Soy bastante generosa'},
    {big:STATS.five_stars,unit:'libros destacados',desc:'Los que más me marcaron'},
  ].map(i=>`<div class="stat-block"><span class="big">${i.big}</span><span class="unit">${i.unit}</span><p class="desc">${i.desc}</p></div>`).join('');
}

function renderYearChart(){
  const el=document.getElementById('yearChart');
  const max=Math.max(...YEARS.map(d=>d.count));
  YEARS.forEach((d,i)=>{
    el.innerHTML+=`<div class="bar-col"><span class="bv">${d.count}</span><div class="bar${d.count===max?' hi':''}" style="height:${(d.count/max)*100}%;animation-delay:${i*.04}s" title="${d.year}: ${d.count}"></div><span class="bar-yr">${d.year}</span></div>`;
  });
}

function renderRatingBars(){
  const total=RATINGS.reduce((s,r)=>s+r.count,0);
  document.getElementById('ratingBars').innerHTML=RATINGS.map((r,i)=>`
    <div class="rating-row">
      <span class="r-stars">${'★'.repeat(r.stars)}</span>
      <div class="r-track"><div class="r-fill" style="width:${(r.count/total)*100}%;animation-delay:${.5+i*.1}s"></div></div>
      <span class="r-count">${r.count}</span>
    </div>`).join('');
}

function bookCard(b,idx){
  const title=cleanTitle(b.title);
  return `<div class="book-card" onclick='openBook(${JSON.stringify(b)})'>
    <div class="book-cover-wrap">${coverHtml(b,idx)}</div>
    <div class="book-body">
      ${b.date?`<p class="book-date">${b.date}</p>`:''}
      <h3 class="book-title">${esc(title)}</h3>
      <p class="book-author">${esc(b.author)}</p>
      ${b.review?`<p class="book-review">${esc(b.review)}</p>`:''}
      ${b.pages>0?`<p class="book-pages">${b.pages} páginas</p>`:''}
      <p class="book-card-hint">✦ ver ficha completa</p>
    </div>
  </div>`;
}

function renderRecent(){ document.getElementById('recentGrid').innerHTML=RECENT.map(bookCard).join(''); }

function renderFeatured(){
  const b=TOP5[0]; if(!b) return;
  const title=cleanTitle(b.title);
  document.getElementById('featuredBook').innerHTML=`
    <div class="featured" onclick='openBook(${JSON.stringify(b)})'>
      <div class="featured-img">${coverHtml(b,0)}</div>
      <div class="feat-body">
        <p class="feat-badge">✦ Último favorito</p>
        <h3 class="feat-title">${esc(title)}</h3>
        <p class="feat-author">${esc(b.author)}</p>
        ${b.review?`<p class="feat-review">${esc(b.review)}</p>`:''}
        <p class="feat-hint">✦ clic para ver ficha completa</p>
      </div>
    </div>`;
}

function renderTop(){ document.getElementById('topGrid').innerHTML=TOP5.slice(1,7).map(bookCard).join(''); }

function renderShelf(){
  const el=document.getElementById('shelfRow');
  SHELF.forEach((b,i)=>{
    const title=cleanTitle(b.title);
    el.innerHTML+=`<div class="shelf-item" onclick='openBook(${JSON.stringify(b)})'>
      <div class="shelf-cover">${coverHtml(b,i)}</div>
      <div class="shelf-tooltip"><strong>${esc(title)}</strong><br><em>${esc(b.author)}</em></div>
    </div>`;
  });
}

function renderAuthors(){
  document.getElementById('authorsGrid').innerHTML=AUTHORS.map(a=>`
    <div class="author-row">
      <span class="a-name">${esc(a.name)}</span>
      <span class="a-books">${a.count} libros</span>
      <span class="a-rating">${a.avg}★</span>
      <span class="a-pages">${(a.pages||0).toLocaleString()} págs</span>
    </div>`).join('');
}

function renderCurrent(){
  document.getElementById('currentGrid').innerHTML=CURRENT.map(b=>{
    const title=cleanTitle(b.title);
    return `<div class="current-card" onclick='openBook(${JSON.stringify(b)})'>
      <div class="current-spine"></div>
      <div class="current-body">
        ${b.image_url?`<div class="current-cover-mini"><img src="${esc(b.image_url)}" alt="${esc(title)}" onerror="this.parentElement.style.display='none'"></div>`:''}
        <p class="current-lbl"><span class="reading-dot"></span> Leyendo ahora</p>
        <p class="current-title">${esc(title)}</p>
        <p class="current-author">${esc(b.author)}</p>
      </div>
    </div>`;
  }).join('');
}

const io=new IntersectionObserver(entries=>entries.forEach(e=>{if(e.isIntersecting)e.target.classList.add('vis');}),{threshold:.08});

document.addEventListener('DOMContentLoaded',()=>{
  renderHeaderPills(); renderStats(); renderYearChart(); renderRatingBars();
  renderRecent(); renderFeatured(); renderTop(); renderShelf();
  renderAuthors(); renderCurrent();
  setTimeout(()=>document.querySelectorAll('.fi').forEach(el=>io.observe(el)),100);
});
</script>
</body>
</html>"""

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Building Mi Biblioteca...")
    print("=" * 60)

    # Fetch all shelves
    read_xml    = fetch_rss("read", per_page=200)
    current_xml = fetch_rss("currently-reading", per_page=10)
    toread_xml  = fetch_rss("to-read", per_page=50)

    read_books    = parse_rss(read_xml)
    current_books = parse_rss(current_xml)
    toread_books  = parse_rss(toread_xml)

    if not read_books:
        print("ERROR: No books fetched. Check that your Goodreads shelves are public.")
        print("Go to: Goodreads → Settings → Privacy → set to Everyone")
        sys.exit(1)

    # Load CSV and merge with RSS for complete book list
    print("\nLoading CSV backup...")
    csv_books = load_csv()
    if csv_books:
        read_books = merge_rss_and_csv(read_books, csv_books)

    # Download covers and replace image_url with local path
    all_books = read_books + current_books + toread_books
    print(f"\nDownloading covers for up to {len(all_books)} books...")
    cover_map = ensure_covers(all_books)
    for b in all_books:
        fname = cover_filename(b.get("id"), b.get("isbn", ""))
        if fname and fname in cover_map:
            b["image_url"] = cover_map[fname]  # local path: "covers/123.jpg"

    # Process data
    data = process_data(read_books, current_books, toread_books)

    print(f"\nStats:")
    print(f"  Read: {data['stats']['total_read']}")
    print(f"  Currently reading: {len(current_books)}")
    print(f"  To read: {data['stats']['to_read']}")
    print(f"  Total pages: {data['stats']['total_pages']:,}")
    print(f"  5-star books: {data['stats']['five_stars']}")

    # Generate HTML
    html = generate_html(data)
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"\n✓ Generated: {OUTPUT_FILE} ({len(html)//1024}KB)")
    print("=" * 60)

if __name__ == "__main__":
    main()
