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

    from datetime import datetime as dt
    try:
        date_obj = dt.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return []

    # Find images created on this date
    next_day = dt(date_obj.year, date_obj.month, date_obj.day + 1) if date_obj.day < 28 else dt(date_obj.year, date_obj.month + 1, 1)
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
    """Find all daily edition markdown files for a company."""
    news_dir = MNEME_DATA / company_slug / "news"
    if not news_dir.exists():
        return []
    editions = []
    for f in sorted(news_dir.glob("*.md"), reverse=True):
        if re.match(r"\d{4}-\d{2}-\d{2}\.md", f.name):
            # Pull images from Mneme's image projects for this date
            images = get_feed_images_for_date(f.stem, company_workers)
            # Also check local companion images as fallback
            if not images:
                for ext in ("*.png", "*.jpg", "*.webp"):
                    images.extend(news_dir.glob(f"{f.stem}{ext}"))
            editions.append({
                "date": f.stem,
                "path": f,
                "filename": f.name,
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


def render_edition(company: dict, edition: dict, output_dir: Path = None) -> str:
    """Render a daily edition as a full HTML page."""
    content = edition["path"].read_text(encoding="utf-8")
    title, byline, body_html = md_to_html(content)

    if not title:
        title = f"THE {company['name'].upper()} DAILY"

    # Copy companion images and build image HTML
    images_html = ""
    for img_path in edition.get("images", []):
        if output_dir:
            import shutil
            dest = output_dir / img_path.name
            shutil.copy2(img_path, dest)
        images_html += f'<img src="{img_path.name}" alt="Daily illustration" style="max-width: 100%; border-radius: 8px; margin: 1.5rem 0; border: 1px solid var(--border);">\n'

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
            <div>
                <a href="/{company['slug']}/">Archive</a>
            </div>
        </div>
    </nav>
    <div class="container">
        <div class="masthead">
            <h1>{title}</h1>
            {f'<p class="edition">{byline}</p>' if byline else f'<p class="edition">{edition["date"]}</p>'}
        </div>
        <div class="article">
            {images_html}
            {body_html}
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
        content = ed["path"].read_text(encoding="utf-8")
        preview = ""
        for line in content.split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("**") and not line.startswith("---"):
                preview = line[:200]
                break

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
            <a href="/{company['slug']}/">{company['name']}</a>
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
                The entire system runs on local AI models via <strong style="color: var(--text);">Ollama</strong> and <strong style="color: var(--text);">Ollama Cloud</strong>, orchestrated by <strong style="color: var(--text);">Cortex</strong> — Cephra's intelligent microservice layer. No external AI APIs required. The news you read here is written by each company's autonomous reporter, observing and commenting on their company's progress with full editorial independence.
            </p>
            <p style="font-size: 0.8rem; color: var(--text-muted); margin-top: 1rem;">
                Built with the <strong style="color: var(--accent);">Cephra Technology Stack</strong>: Cortex (LLM orchestration) &middot; Company Force (autonomous operations) &middot; Mneme (content creation) &middot; Kit (user interface) &middot; Ollama Cloud (inference)
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
            html = render_edition(co, ed, output_dir=co_dir)
            out_path = co_dir / f"{ed['date']}.html"
            out_path.write_text(html, encoding="utf-8")

        # Latest redirect
        latest = editions[0]
        index_html = f"""<!DOCTYPE html>
<html><head><meta http-equiv="refresh" content="0; url=/{co['slug']}/{latest['date']}.html"></head></html>"""
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
