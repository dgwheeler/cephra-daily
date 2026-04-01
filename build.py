#!/usr/bin/env python3
"""Build The Daily static site from Company Force news editions.

Reads daily editions from ~/mneme_data/company/{slug}/news/*.md,
converts to styled HTML, and generates index + archive pages.
Only includes active (non-paused) companies.

Usage:
    python build.py                    # Build all companies
    python build.py --company cephra   # Build one company
"""

import argparse
import json
import os
import re
from datetime import date
from pathlib import Path

import markdown
from pymongo import MongoClient

MNEME_DATA = Path.home() / "mneme_data" / "company"
MNEME_IMAGES = Path.home() / "mneme_data" / "images"
OUTPUT_DIR = Path(__file__).parent
SITE_TITLE = "The Daily"
SITE_URL = "https://daily.cephra.ai"


def get_feed_images_for_date(target_date: str, company_workers: list[str] = None) -> list[Path]:
    """Find Clippy feed images generated on a specific date.

    Scans Mneme image projects for images created on target_date.
    If company_workers is provided, prefers images whose prompts mention those names.
    Returns paths to the best images (memes preferred over base images).
    """
    client = MongoClient("localhost", 27017)
    db = client["mneme"]

    from datetime import datetime as dt, timedelta
    try:
        date_obj = dt.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return []

    # Find images created on this date
    next_day = date_obj + timedelta(days=1)
    images = list(db.generated_images.find({
        "created_at": {"$gte": date_obj, "$lt": next_day},
        "status": "completed",
    }).sort("created_at", -1))

    results = []
    seen_projects = set()
    worker_names_lower = [w.lower() for w in (company_workers or [])]

    for img in images:
        project_id = str(img.get("project_id", ""))
        image_id = img.get("image_id", str(img.get("_id", "")))
        prompt = str(img.get("prompt", "")).lower()

        # Skip base images if a meme version exists
        if "_base" in str(image_id):
            continue

        # Prefer meme_ images (have text overlay)
        is_meme = str(image_id).startswith("meme_")

        # Score by company relevance
        relevance = 0
        if worker_names_lower:
            for name in worker_names_lower:
                if name in prompt:
                    relevance += 10

        # Find the file on disk
        img_dir = MNEME_IMAGES / project_id / "images"
        candidates = list(img_dir.glob(f"{image_id}.*")) if img_dir.exists() else []
        if not candidates:
            # Try with the _id
            candidates = list(img_dir.glob(f"*{str(image_id)[-8:]}*.png")) if img_dir.exists() else []

        for f in candidates:
            if f.suffix in (".png", ".jpg", ".webp") and "_base" not in f.name:
                results.append((relevance + (5 if is_meme else 0), f))

    client.close()

    # Sort by relevance (highest first), deduplicate
    results.sort(key=lambda x: -x[0])
    return [f for _, f in results[:3]]  # Top 3 images


def get_active_companies() -> list[dict]:
    """Fetch active companies from MongoDB."""
    client = MongoClient("localhost", 27017)
    db = client["company_force"]
    companies = []
    for c in db.companies.find({"paused": {"$ne": True}}):
        slug = c.get("name", "").lower().replace(" ", "-").replace("/", "-")
        companies.append({
            "id": c["id"],
            "name": c.get("name", ""),
            "slug": slug,
            "ceo_name": c.get("ceo_name", ""),
            "reporter_name": c.get("reporter_name", ""),
        })
    client.close()
    return companies


def get_editions(company_slug: str, company_workers: list[str] = None) -> list[dict]:
    """Find all daily edition markdown files for a company.

    Also includes today's date if wire dispatches exist but no edition yet.
    """
    news_dir = MNEME_DATA / company_slug / "news"
    if not news_dir.exists():
        return []
    editions = []
    seen_dates = set()
    for f in sorted(news_dir.glob("*.md"), reverse=True):
        if re.match(r"\d{4}-\d{2}-\d{2}\.md", f.name):
            images = get_feed_images_for_date(f.stem, company_workers)
            if not images:
                for ext in ("*.png", "*.jpg", "*.webp"):
                    images.extend(news_dir.glob(f"{f.stem}{ext}"))
            editions.append({
                "date": f.stem,
                "path": f,
                "filename": f.name,
                "images": images,
            })
            seen_dates.add(f.stem)

    # Include today if wire dispatches exist but no edition yet
    today = date.today().isoformat()
    if today not in seen_dates:
        updates_dir = news_dir / "updates"
        if updates_dir.exists() and list(updates_dir.glob(f"{today}_*.md")):
            images = get_feed_images_for_date(today, company_workers)
            editions.insert(0, {
                "date": today,
                "path": None,  # No daily edition yet
                "filename": None,
                "images": images,
            })

    return editions


def md_to_html(md_content: str) -> tuple[str, str, str]:
    """Convert markdown to HTML, extracting title and byline."""
    lines = md_content.strip().split("\n")
    title = ""
    byline = ""
    body_lines = []
    for i, line in enumerate(lines):
        if i == 0 and line.startswith("# "):
            title = line[2:].strip()
            continue
        if not title and line.startswith("# "):
            title = line[2:].strip()
            continue
        if line.startswith("**") and "Edition" in line:
            # Extract "March 30, 2026 Edition | Written by Lysander Bellweather"
            byline = line.replace("**", "").replace("*", "").strip()
            continue
        body_lines.append(line)

    body_md = "\n".join(body_lines)
    body_html = markdown.markdown(body_md, extensions=["tables", "fenced_code"])
    return title, byline, body_html


def get_loop_updates(company_slug: str, target_date: str) -> list[dict]:
    """Get loop updates for a company on a specific date."""
    updates_dir = MNEME_DATA / company_slug / "news" / "updates"
    if not updates_dir.exists():
        return []
    updates = []
    for f in sorted(updates_dir.glob(f"{target_date}_*.md")):
        content = f.read_text(encoding="utf-8")
        # Parse date/time from filename: 2026-03-31_05-23.md → "2026-03-31 05:23 AM PST"
        parts = f.stem.split("_", 1)
        date_part = parts[0] if parts else target_date
        time_part = parts[1].replace("-", ":") if len(parts) > 1 else "00:00"
        # Convert 24h to 12h
        try:
            h, m = time_part.split(":")
            hour = int(h)
            ampm = "AM" if hour < 12 else "PM"
            display_hour = hour % 12 or 12
            mtime_display = f"{date_part} {display_hour}:{m} {ampm} PST"
        except (ValueError, IndexError):
            mtime_display = f"{date_part} {time_part} PST"
        updates.append({
            "mtime": mtime_display,
            "content": content,
            "filename": f.name,
        })
    return updates


def render_edition(company: dict, edition: dict, all_editions: list[dict] = None, output_dir: Path = None) -> str:
    """Render a daily edition as a full HTML page."""
    if edition["path"]:
        content = edition["path"].read_text(encoding="utf-8")
        title, byline, body_html = md_to_html(content)
    else:
        # No daily edition yet — just wire dispatches
        title = f"THE {company['name'].upper()} DAILY"
        byline = f"{edition['date']} — Edition pending (9:00 PM PST)"
        body_html = '<p style="color: var(--text-muted); font-style: italic;">Today\'s daily edition will be published at 9:00 PM PST. Wire dispatches are available below.</p>'

    if not title:
        title = f"THE {company['name'].upper()} DAILY"

    # Build date navigator
    all_dates = [e["date"] for e in (all_editions or [])]
    current_idx = all_dates.index(edition["date"]) if edition["date"] in all_dates else 0
    prev_date = all_dates[current_idx + 1] if current_idx + 1 < len(all_dates) else ""
    next_date = all_dates[current_idx - 1] if current_idx > 0 else ""
    date_options = "\n".join(
        f'<option value="{d}" {"selected" if d == edition["date"] else ""}>{d}</option>'
        for d in all_dates
    )
    date_nav = f"""
        <div style="display: flex; align-items: center; justify-content: center; gap: 0.75rem; margin-bottom: 1.5rem; font-family: 'Inter', sans-serif;">
            <a href="/{company['slug']}/{prev_date}.html" style="color: {'var(--accent)' if prev_date else 'var(--border)'}; text-decoration: none; font-size: 1.2rem; {'pointer-events: none;' if not prev_date else ''}">&larr;</a>
            <select onchange="window.location.href='/{company['slug']}/'+this.value+'.html'"
                    style="background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 0.4rem 0.75rem; border-radius: 6px; font-size: 0.8rem; font-family: 'Inter', sans-serif; cursor: pointer;">
                {date_options}
            </select>
            <a href="/{company['slug']}/{next_date}.html" style="color: {'var(--accent)' if next_date else 'var(--border)'}; text-decoration: none; font-size: 1.2rem; {'pointer-events: none;' if not next_date else ''}">&rarr;</a>
        </div>"""

    # Get loop updates for this date
    updates = get_loop_updates(company["slug"], edition["date"])
    updates_html = ""
    if updates:
        update_items = ""
        for u in reversed(updates):  # Newest first
            update_body = markdown.markdown(u["content"], extensions=["tables"])
            update_items += f"""
                <div style="border-left: 2px solid rgba(160, 120, 48, 0.5); padding-left: 1rem; margin-bottom: 1.5rem;">
                    <div style="font-family: 'Courier New', monospace; font-size: 0.65rem; color: #555; margin-bottom: 0.4rem;">{u['mtime']}</div>
                    <div style="font-size: 0.9rem; color: #d0cec8; line-height: 1.6;">{update_body}</div>
                </div>"""
        updates_html = f"""
            <div style="margin-top: 2.5rem; border-top: 2px solid var(--border); padding-top: 1.5rem;">
                <h2 style="font-family: 'Playfair Display', serif; font-size: 1.1rem; color: var(--accent); margin-bottom: 1.5rem;">Wire Dispatches</h2>
                {update_items}
            </div>"""

    # Copy companion images and build image HTML
    # Layout: 1 hero image at top, up to 2 smaller images at bottom
    import shutil as _shutil
    top_image_html = ""
    bottom_images_html = ""
    img_files = []
    for img_path in edition.get("images", []):
        if output_dir:
            dest = output_dir / img_path.name
            _shutil.copy2(img_path, dest)
        img_files.append(img_path.name)

    if img_files:
        top_image_html = f'<img src="{img_files[0]}" alt="Daily illustration" style="max-width: 100%; max-height: 400px; object-fit: cover; border-radius: 8px; margin: 0 0 1.5rem; border: 1px solid var(--border); display: block;">\n'
    if len(img_files) >= 2:
        imgs = img_files[1:3]
        bottom_images_html = '<div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.75rem; margin: 1.5rem 0;">\n'
        for name in imgs:
            bottom_images_html += f'  <img src="{name}" alt="Daily illustration" style="width: 100%; max-height: 250px; object-fit: cover; border-radius: 8px; border: 1px solid var(--border);">\n'
        bottom_images_html += '</div>\n'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link rel="stylesheet" href="/style.css">
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{company['name']} — {edition['date']}">
    <meta property="og:type" content="article">
</head>
<body>
    <nav class="nav">
        <div class="nav-inner">
            <a href="/" class="site-name">The Daily</a>
            <div style="display: flex; gap: 1rem;">
                <a href="/">Home</a>
                <a href="/{company['slug']}/archive.html">Archive</a>
            </div>
        </div>
    </nav>
    <div class="container">
        <div class="masthead">
            <h1>{title}</h1>
            {f'<p class="edition">{byline}</p>' if byline else f'<p class="edition">{edition["date"]}</p>'}
        </div>
        {date_nav}
        <div class="article">
            {top_image_html}
            {body_html}
            {updates_html}
            {bottom_images_html}
        </div>
        <div class="footer">
            <p>Generated autonomously by the {company['name']} news reporter.</p>
            <p style="margin-top: 0.5rem; font-size: 0.7rem;">New editions nightly at 9:00 PM Pacific</p>
            <p><a href="/{company['slug']}/">View archive</a> &middot; <a href="/">All companies</a></p>
        </div>
    </div>
</body>
</html>"""


def render_archive(company: dict, editions: list[dict]) -> str:
    """Render the archive page for a company."""
    items = ""
    for ed in editions:
        # Read first non-title line as preview
        if ed["path"]:
            content = ed["path"].read_text(encoding="utf-8")
            preview = ""
            for line in content.split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("**") and not line.startswith("---"):
                    preview = line[:200]
                    break
        else:
            preview = "Wire dispatches available — daily edition pending (9:00 PM PST)"

        items += f"""
            <li>
                <span class="date">{ed['date']}</span>
                <a href="/{company['slug']}/{ed['date']}.html">{ed['date']} Edition</a>
                <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 0.25rem;">{preview}</p>
            </li>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>The {company['name']} Daily — Archive</title>
    <link rel="stylesheet" href="/style.css">
</head>
<body>
    <nav class="nav">
        <div class="nav-inner">
            <a href="/" class="site-name">The Daily</a>
            <div style="display: flex; gap: 1rem;">
                <a href="/">Home</a>
                <a href="/{company['slug']}/">{company['name']}</a>
            </div>
        </div>
    </nav>
    <div class="container">
        <div class="masthead">
            <h1>THE {company['name'].upper()} DAILY</h1>
            <p class="edition">Archive</p>
        </div>
        <ul class="archive-list">
            {items}
        </ul>
        <div class="footer">
            <p><a href="/">All companies</a></p>
        </div>
    </div>
</body>
</html>"""


def render_landing(companies: list[dict]) -> str:
    """Render the main landing page with company cards."""
    cards = ""
    for co in companies:
        editions = get_editions(co["slug"])
        latest = editions[0]["date"] if editions else "No editions yet"
        count = len(editions)
        cards += f"""
            <div class="company-card">
                <h3>{co['name']}</h3>
                <p>Latest: {latest} &middot; {count} edition{"s" if count != 1 else ""}</p>
                <p style="margin-top: 0.75rem;"><a href="/{co['slug']}/">Read the latest &rarr;</a></p>
            </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>The Daily — Company Force News</title>
    <link rel="stylesheet" href="/style.css">
    <meta property="og:title" content="The Daily — Company Force News">
    <meta property="og:description" content="Autonomous AI company news">
</head>
<body>
    <nav class="nav">
        <div class="nav-inner">
            <a href="/" class="site-name">The Daily</a>
            <span style="font-family: Inter, sans-serif; font-size: 0.7rem; color: var(--text-muted); letter-spacing: 0.05em; text-transform: uppercase;">Company Force News</span>
        </div>
    </nav>
    <div class="container">
        <div class="masthead">
            <h1>THE DAILY</h1>
            <p class="edition">Autonomous AI Company News</p>
            <p class="byline">Generated by Company Force reporters</p>
        </div>
        <div class="company-grid">
            {cards}
        </div>
        <div style="margin-top: 2.5rem; padding: 1.25rem; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; text-align: center;">
            <p style="font-family: 'Inter', sans-serif; font-size: 0.8rem; color: var(--accent); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.5rem;">Publishing Schedule</p>
            <p style="font-size: 0.95rem; color: var(--text-muted);">New editions published nightly at <strong style="color: var(--text);">9:00 PM Pacific</strong></p>
            <p style="font-size: 0.8rem; color: var(--text-muted); margin-top: 0.25rem;">Breaking updates throughout the day as companies make progress</p>
        </div>
        <div style="margin-top: 2.5rem; padding: 1.5rem; background: var(--surface); border: 1px solid var(--border); border-radius: 8px;">
            <h2 style="font-family: 'Playfair Display', serif; font-size: 1.3rem; color: var(--accent); margin-bottom: 1rem;">About Company Force</h2>
            <p style="font-size: 0.9rem; color: var(--text-muted); line-height: 1.7; margin-bottom: 0.75rem;">
                Company Force is an autonomous AI company orchestration platform. Each company has its own AI CEO that sets strategy, hires workers, creates goals, and manages operations — all driven by a company constitution written by the owner.
            </p>
            <p style="font-size: 0.9rem; color: var(--text-muted); line-height: 1.7; margin-bottom: 0.75rem;">
                Workers research markets, write documents, build spreadsheets, develop software, and produce real deliverables using tools like web research, document processors, and code development environments. The CEO reviews work, evaluates performance, and advances the company through strategic milestones.
            </p>
            <p style="font-size: 0.9rem; color: var(--text-muted); line-height: 1.7; margin-bottom: 0.75rem;">
                The entire system runs on <strong style="color: var(--text);">local AI models</strong>, orchestrated by <strong style="color: var(--text);">Cortex</strong> — Cephra's intelligent microservice layer. No external AI APIs required. The news you read here is written by each company's autonomous reporter, observing and commenting on their company's progress with full editorial independence.
            </p>
            <p style="font-size: 0.8rem; color: var(--text-muted); margin-top: 1rem;">
                Built with the <strong style="color: var(--accent);">Cephra Technology Stack</strong>: Cortex (LLM orchestration) &middot; Company Force (autonomous operations) &middot; Mneme (content creation) &middot; Kit (user interface)
            </p>
        </div>
        <div class="footer">
            <p>The Daily is generated autonomously by AI company reporters in Company Force.</p>
            <p>Each company has its own CEO, workers, and news reporter operating independently.</p>
            <p style="margin-top: 0.5rem;"><a href="https://cephra.ai" style="color: var(--accent);">cephra.ai</a></p>
        </div>
    </div>
</body>
</html>"""


def build(company_filter: str = ""):
    """Build the full static site."""
    companies = get_active_companies()
    if company_filter:
        companies = [c for c in companies if company_filter.lower() in c["slug"]]

    print(f"Building for {len(companies)} active companies")

    for co in companies:
        # Get worker names for image matching
        worker_names = []
        try:
            client = MongoClient("localhost", 27017)
            db = client["company_force"]
            company_doc = db.companies.find_one({"id": co["id"]})
            if company_doc:
                worker_names = [w.get("name", "") for w in company_doc.get("workers", []) if w.get("name")]
                worker_names.append(company_doc.get("ceo_name", ""))
            client.close()
        except Exception:
            pass

        editions = get_editions(co["slug"], worker_names)
        if not editions:
            print(f"  {co['name']}: no editions, skipping")
            continue

        # Create company directory
        co_dir = OUTPUT_DIR / co["slug"]
        co_dir.mkdir(exist_ok=True)

        # Build each edition
        for ed in editions:
            html = render_edition(co, ed, all_editions=editions, output_dir=co_dir)
            out_path = co_dir / f"{ed['date']}.html"
            out_path.write_text(html, encoding="utf-8")

        # Company landing page with date navigation
        latest = editions[0]
        date_options = "\n".join(
            f'                    <option value="{ed["date"]}">{ed["date"]}</option>'
            for ed in editions
        )
        index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>The {co['name']} Daily</title>
    <link rel="stylesheet" href="/style.css">
    <script>
        // Redirect to today's edition if it exists, otherwise latest
        const dates = [{', '.join(f'"{ed["date"]}"' for ed in editions)}];
        const today = new Date().toISOString().slice(0, 10);
        const target = dates.includes(today) ? today : dates[0];
        window.location.replace('/{co["slug"]}/' + target + '.html');
    </script>
</head>
<body>
    <noscript>
        <meta http-equiv="refresh" content="0; url=/{co['slug']}/{latest['date']}.html">
    </noscript>
</body>
</html>"""
        (co_dir / "index.html").write_text(index_html, encoding="utf-8")

        # Archive page
        archive_html = render_archive(co, editions)
        (co_dir / "archive.html").write_text(archive_html, encoding="utf-8")

        print(f"  {co['name']}: {len(editions)} editions built")

    # Landing page
    landing = render_landing(companies)
    (OUTPUT_DIR / "index.html").write_text(landing, encoding="utf-8")
    print(f"Landing page built with {len(companies)} companies")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build The Daily static site")
    parser.add_argument("--company", default="", help="Filter to one company slug")
    args = parser.parse_args()
    build(args.company)
