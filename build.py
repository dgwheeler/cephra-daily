#!/usr/bin/env python3
"""Build Cephra Signal static site from Company Force execution logs.

Reads daily editions from ~/mneme_data/company/{slug}/news/*.md,
extracts governance data (JSON embedded in HTML comments), converts
narrative to HTML, and renders structured governance panels alongside
the prose.

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
SITE_TITLE = "Cephra Signal"
SITE_URL = "https://newsfeed.cephra.ai"


# =============================================================================
# Data fetching
# =============================================================================

def get_feed_images_for_date(target_date: str, company_workers: list[str] = None) -> list[Path]:
    """Find Clippy feed images generated on a specific date."""
    client = MongoClient("localhost", 27017)
    db = client["mneme"]

    from datetime import datetime as dt, timedelta
    try:
        date_obj = dt.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        return []

    next_day = date_obj + timedelta(days=1)
    images = list(db.generated_images.find({
        "created_at": {"$gte": date_obj, "$lt": next_day},
        "status": "completed",
    }).sort("created_at", -1))

    results = []
    worker_names_lower = [w.lower() for w in (company_workers or [])]

    for img in images:
        project_id = str(img.get("project_id", ""))
        image_id = img.get("image_id", str(img.get("_id", "")))
        prompt = str(img.get("prompt", "")).lower()

        if "_base" in str(image_id):
            continue

        is_meme = str(image_id).startswith("meme_")
        relevance = 0
        if worker_names_lower:
            for name in worker_names_lower:
                if name in prompt:
                    relevance += 10

        img_dir = MNEME_IMAGES / project_id / "images"
        candidates = list(img_dir.glob(f"{image_id}.*")) if img_dir.exists() else []
        if not candidates:
            candidates = list(img_dir.glob(f"*{str(image_id)[-8:]}*.png")) if img_dir.exists() else []

        for f in candidates:
            if f.suffix in (".png", ".jpg", ".webp") and "_base" not in f.name:
                results.append((relevance + (5 if is_meme else 0), f))

    client.close()
    results.sort(key=lambda x: -x[0])
    return [f for _, f in results[:3]]


def get_active_companies() -> list[dict]:
    """Fetch active companies from MongoDB with milestone data."""
    client = MongoClient("localhost", 27017)
    db = client["company_force"]
    companies = []
    for c in db.companies.find({"paused": {"$ne": True}}):
        slug = c.get("name", "").lower().replace(" ", "-").replace("/", "-")
        milestones = c.get("milestones", [])
        current_ms_idx = c.get("current_milestone_idx", 0)
        workers_active = sum(1 for w in c.get("workers", []) if w.get("active"))

        # Count active goals
        comp_id = c["id"]
        goals_active = db.company_goals.count_documents({"company_id": comp_id, "status": "active"})
        goals_completed = db.company_goals.count_documents({"company_id": comp_id, "status": "completed"})

        companies.append({
            "id": comp_id,
            "name": c.get("name", ""),
            "slug": slug,
            "ceo_name": c.get("ceo_name", ""),
            "reporter_name": c.get("reporter_name", ""),
            "milestone_position": f"{current_ms_idx + 1}/{len(milestones)}" if milestones else "",
            "current_milestone": milestones[current_ms_idx].get("title", "") if milestones and current_ms_idx < len(milestones) else "",
            "workers_active": workers_active,
            "goals_active": goals_active,
            "goals_completed": goals_completed,
        })
    client.close()
    return companies


def get_editions(company_slug: str, company_workers: list[str] = None) -> list[dict]:
    """Find all daily edition markdown files for a company."""
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

    today = date.today().isoformat()
    if today not in seen_dates:
        updates_dir = news_dir / "updates"
        if updates_dir.exists() and list(updates_dir.glob(f"{today}_*.md")):
            images = get_feed_images_for_date(today, company_workers)
            editions.insert(0, {
                "date": today,
                "path": None,
                "filename": None,
                "images": images,
            })

    return editions


# =============================================================================
# Governance data extraction & rendering
# =============================================================================

def extract_governance(md_content: str) -> dict | None:
    """Extract governance JSON from <!--governance:{...}--> comment in markdown."""
    match = re.search(r"<!--governance:(.*?)-->", md_content, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, KeyError):
        return None


def render_milestone_track(gov: dict) -> str:
    """Render horizontal milestone progress bar."""
    ms_data = gov.get("milestone", {})
    milestones = ms_data.get("milestones", [])
    if not milestones:
        return ""

    nodes_html = ""
    for i, ms in enumerate(milestones):
        status = ms.get("status", "upcoming")
        if status in ("completed", "done"):
            css_class = "completed"
        elif status in ("active", "current"):
            css_class = "current"
        else:
            css_class = "upcoming"

        # Add connector before each node (except first)
        if i > 0:
            conn_class = "completed" if status in ("completed", "done") or (i > 0 and milestones[i-1].get("status") in ("completed", "done")) else ""
            nodes_html += f'<div class="milestone-connector {conn_class}"></div>'

        title = ms.get("title", "")
        # Truncate long titles
        short_title = title[:20] + "..." if len(title) > 20 else title

        nodes_html += f"""
        <div class="milestone-node {css_class}">
            <div class="milestone-dot"></div>
            <div class="milestone-label">{short_title}</div>
        </div>"""

    gate = ms_data.get("gate_criteria", "")
    gate_html = f'<div class="milestone-gate">Gate: {gate}</div>' if gate else ""
    position = ms_data.get("position", "")
    position_html = f' <span style="color: var(--accent); font-size: 0.75rem;">({position})</span>' if position else ""

    return f"""
    <div class="panel">
        <div class="panel-header">Milestone Roadmap{position_html}</div>
        <div class="milestone-track">{nodes_html}</div>
        {gate_html}
    </div>"""


def render_decisions_panel(gov: dict) -> str:
    """Render color-coded list of decisions made this cycle."""
    decisions = gov.get("decisions", [])
    if not decisions:
        return ""

    items_html = ""
    for d in decisions[:8]:
        dtype = d.get("type", "")
        detail = d.get("detail", "")
        actor = d.get("actor", "")
        items_html += f"""
        <li class="decision-item decision-item--{dtype}">
            <span class="decision-dot"></span>
            <div>
                <div>{detail}</div>
                <span class="decision-actor">{actor}</span>
            </div>
        </li>"""

    return f"""
    <div class="panel">
        <div class="panel-header">Decisions</div>
        <ul class="decision-list">{items_html}</ul>
    </div>"""


def render_execution_summary(gov: dict) -> str:
    """Render stat card grid for execution metrics."""
    ex = gov.get("execution", {})
    completed = ex.get("tasks_completed", 0)
    failed = ex.get("tasks_failed", 0)
    in_progress = ex.get("tasks_in_progress", 0)
    artifacts = ex.get("artifacts_produced", 0)
    code_projects = ex.get("code_projects_modified", [])

    # Only render if there's something to show
    if not any([completed, failed, in_progress, artifacts, code_projects]):
        return ""

    cards = ""
    if completed:
        cards += f'<div class="stat-card"><div class="stat-number success">{completed}</div><div class="stat-label">Tasks Completed</div></div>'
    if failed:
        cards += f'<div class="stat-card"><div class="stat-number danger">{failed}</div><div class="stat-label">Tasks Failed</div></div>'
    if in_progress:
        cards += f'<div class="stat-card"><div class="stat-number info">{in_progress}</div><div class="stat-label">In Progress</div></div>'
    if artifacts:
        cards += f'<div class="stat-card"><div class="stat-number">{artifacts}</div><div class="stat-label">Artifacts</div></div>'
    if code_projects:
        cards += f'<div class="stat-card"><div class="stat-number info">{len(code_projects)}</div><div class="stat-label">Code Projects</div></div>'

    return f"""
    <div class="panel">
        <div class="panel-header">Execution</div>
        <div class="stat-grid">{cards}</div>
    </div>"""


def render_governance_panel(gov: dict) -> str:
    """Render owner directives, manager validations, escalations."""
    g = gov.get("governance", {})
    directives = g.get("owner_directives", [])
    validations = g.get("manager_validations", [])
    escalations = g.get("escalations", [])
    questions = g.get("ceo_forwarded_questions", [])

    if not any([directives, validations, escalations, questions]):
        return ""

    items_html = ""
    for d in directives:
        items_html += f'<div class="governance-item directive"><div class="governance-item-label">Owner Directive</div>{d}</div>'
    for v in validations:
        items_html += f'<div class="governance-item validation"><div class="governance-item-label">Manager Validation</div>{v}</div>'
    for e in escalations:
        items_html += f'<div class="governance-item escalation"><div class="governance-item-label">Escalation</div>{e}</div>'
    for q in questions:
        items_html += f'<div class="governance-item question"><div class="governance-item-label">CEO Question</div>{q}</div>'

    return f"""
    <div class="panel">
        <div class="panel-header">Governance</div>
        {items_html}
    </div>"""


def render_system_state_footer(gov: dict) -> str:
    """Render compact system state bar with platform badges."""
    ss = gov.get("system_state", {})
    platform = gov.get("platform", {})
    components = platform.get("components", [])

    items = []
    workers = ss.get("workers_active", 0)
    if workers:
        idle = ss.get("workers_idle", 0)
        items.append(f'<div class="system-footer-item">Workers <span class="system-footer-value">{workers}</span>{f" ({idle} idle)" if idle else ""}</div>')

    goals_active = ss.get("goals_active", 0)
    goals_total = ss.get("goals_completed_total", 0)
    if goals_active or goals_total:
        items.append(f'<div class="system-footer-item">Goals <span class="system-footer-value">{goals_active} active</span> / {goals_total} completed</div>')

    scores = ss.get("quality_scores", {})
    if scores:
        score_parts = [f"{name}: {score}" for name, score in scores.items()]
        items.append(f'<div class="system-footer-item">Scores <span class="system-footer-value">{", ".join(score_parts)}</span></div>')

    badges_html = ""
    if components:
        badge_items = "".join(f'<span class="platform-badge">{c}</span>' for c in components)
        badges_html = f'<div class="platform-badges">{badge_items}</div>'

    return f"""
    <div class="system-footer">
        {"".join(items)}
        {badges_html}
    </div>"""


# =============================================================================
# Markdown conversion
# =============================================================================

def md_to_html(md_content: str) -> tuple[str, str, str]:
    """Convert markdown to HTML, extracting title, byline, and governance data."""
    # Strip governance comment before processing
    clean = re.sub(r"<!--governance:.*?-->", "", md_content, flags=re.DOTALL).strip()

    lines = clean.split("\n")
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
        parts = f.stem.split("_", 1)
        time_part = parts[1].replace("-", ":") if len(parts) > 1 else "00:00"
        try:
            h, m = time_part.split(":")
            hour = int(h)
            ampm = "AM" if hour < 12 else "PM"
            display_hour = hour % 12 or 12
            mtime_display = f"{display_hour}:{m} {ampm} PST"
        except (ValueError, IndexError):
            mtime_display = f"{time_part} PST"

        # Extract governance from update if present
        gov = extract_governance(content)
        # Strip governance for display
        narrative = re.sub(r"<!--governance:.*?-->", "", content, flags=re.DOTALL).strip()

        updates.append({
            "mtime": mtime_display,
            "content": narrative,
            "governance": gov,
            "filename": f.name,
        })
    return updates


# =============================================================================
# Page rendering
# =============================================================================

def render_edition(company: dict, edition: dict, all_editions: list[dict] = None, output_dir: Path = None) -> str:
    """Render a daily edition as a full HTML page with governance panels."""
    gov = None

    if edition["path"]:
        content = edition["path"].read_text(encoding="utf-8")
        gov = extract_governance(content)
        title, byline, body_html = md_to_html(content)
    else:
        title = f"THE {company['name'].upper()} SIGNAL"
        byline = f"{edition['date']} — Edition pending (9:00 PM PST)"
        body_html = '<p style="color: var(--text-muted); font-style: italic;">Today\'s edition will be published at 9:00 PM PST. Signal dispatches are available below.</p>'

    if not title:
        title = f"THE {company['name'].upper()} SIGNAL"

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

    # Render governance panels (if data available)
    governance_html = ""
    if gov:
        governance_html = '<div class="governance-section">'
        governance_html += render_milestone_track(gov)
        # Decisions and execution side by side on desktop
        decisions = render_decisions_panel(gov)
        execution = render_execution_summary(gov)
        if decisions or execution:
            governance_html += '<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1rem;">'
            governance_html += decisions + execution
            governance_html += '</div>'
        governance_html += render_governance_panel(gov)
        governance_html += '</div>'

    # Get loop updates (wire dispatches)
    updates = get_loop_updates(company["slug"], edition["date"])
    updates_html = ""
    if updates:
        update_items = ""
        for u in reversed(updates):
            update_body = markdown.markdown(u["content"], extensions=["tables"])
            update_items += f"""
                <div style="border-left: 2px solid rgba(160, 120, 48, 0.5); padding-left: 1rem; margin-bottom: 1.5rem;">
                    <div style="font-family: 'Courier New', monospace; font-size: 0.65rem; color: #555; margin-bottom: 0.4rem;">{u['mtime']}</div>
                    <div style="font-size: 0.9rem; color: #d0cec8; line-height: 1.6;">{update_body}</div>
                </div>"""
        updates_html = f"""
            <div style="margin-top: 2.5rem; border-top: 2px solid var(--border); padding-top: 1.5rem;">
                <h2 style="font-family: 'Playfair Display', serif; font-size: 1.1rem; color: var(--accent); margin-bottom: 1.5rem;">Signal Dispatches</h2>
                {update_items}
            </div>"""

    # Images
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
        top_image_html = f'<img src="{img_files[0]}" alt="Signal illustration" style="max-width: 100%; max-height: 400px; object-fit: cover; border-radius: 8px; margin: 0 0 1.5rem; border: 1px solid var(--border); display: block;">\n'
    if len(img_files) >= 2:
        imgs = img_files[1:3]
        bottom_images_html = '<div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.75rem; margin: 1.5rem 0;">\n'
        for name in imgs:
            bottom_images_html += f'  <img src="{name}" alt="Signal illustration" style="width: 100%; max-height: 250px; object-fit: cover; border-radius: 8px; border: 1px solid var(--border);">\n'
        bottom_images_html += '</div>\n'

    # System state footer
    system_footer_html = render_system_state_footer(gov) if gov else ""

    # Narrative section separator (only if we have governance panels)
    narrative_wrapper_open = '<div class="narrative-section">' if gov else ""
    narrative_wrapper_close = '</div>' if gov else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <link rel="stylesheet" href="/style.css">
    <meta property="og:title" content="{title}">
    <meta property="og:description" content="{company['name']} — Autonomous AI Execution Log — {edition['date']}">
    <meta property="og:type" content="article">
</head>
<body>
    <nav class="nav">
        <div class="nav-inner">
            <a href="/" class="site-name">{SITE_TITLE}</a>
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
        {governance_html}
        <div class="article">
            {narrative_wrapper_open}
            {top_image_html}
            {body_html}
            {narrative_wrapper_close}
            {updates_html}
            {bottom_images_html}
        </div>
        {system_footer_html}
        <div class="footer">
            <p>Observed autonomously by the {company['name']} operations correspondent.</p>
            <p style="margin-top: 0.5rem; font-size: 0.7rem;">New editions nightly at 9:00 PM Pacific &middot; Signal dispatches throughout the day</p>
            <p><a href="/{company['slug']}/archive.html">Archive</a> &middot; <a href="/">All companies</a></p>
        </div>
    </div>
</body>
</html>"""


def render_archive(company: dict, editions: list[dict]) -> str:
    """Render the archive page for a company."""
    items = ""
    for ed in editions:
        if ed["path"]:
            content = ed["path"].read_text(encoding="utf-8")
            clean = re.sub(r"<!--governance:.*?-->", "", content, flags=re.DOTALL).strip()
            preview = ""
            for line in clean.split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("**") and not line.startswith("---"):
                    preview = line[:200]
                    break
        else:
            preview = "Signal dispatches available — daily edition pending (9:00 PM PST)"

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
    <title>{company['name']} Signal — Archive</title>
    <link rel="stylesheet" href="/style.css">
</head>
<body>
    <nav class="nav">
        <div class="nav-inner">
            <a href="/" class="site-name">{SITE_TITLE}</a>
            <div style="display: flex; gap: 1rem;">
                <a href="/">Home</a>
                <a href="/{company['slug']}/">{company['name']}</a>
            </div>
        </div>
    </nav>
    <div class="container">
        <div class="masthead">
            <h1>THE {company['name'].upper()} SIGNAL</h1>
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
    """Render the main landing page — positioned as governance showcase."""
    cards = ""
    for co in companies:
        editions = get_editions(co["slug"])
        latest = editions[0]["date"] if editions else "No editions yet"
        count = len(editions)

        # Enriched card with milestone + worker data
        meta_parts = []
        if co.get("milestone_position"):
            meta_parts.append(f'Milestone {co["milestone_position"]}')
        if co.get("workers_active"):
            meta_parts.append(f'{co["workers_active"]} workers')
        if co.get("goals_active"):
            meta_parts.append(f'{co["goals_active"]} active goals')
        meta_line = " &middot; ".join(meta_parts) if meta_parts else ""

        cards += f"""
            <div class="company-card">
                <h3>{co['name']}</h3>
                <p>Latest: {latest} &middot; {count} edition{"s" if count != 1 else ""}</p>
                {f'<p style="margin-top: 0.4rem; font-size: 0.8rem; color: var(--accent-dim);">{meta_line}</p>' if meta_line else ''}
                {f'<p style="margin-top: 0.2rem; font-size: 0.75rem; color: var(--text-muted);">{co["current_milestone"]}</p>' if co.get("current_milestone") else ''}
                <p style="margin-top: 0.75rem;"><a href="/{co['slug']}/">Read the latest &rarr;</a></p>
            </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{SITE_TITLE} — Governed AI Execution</title>
    <link rel="stylesheet" href="/style.css">
    <meta property="og:title" content="{SITE_TITLE} — Governed AI Execution">
    <meta property="og:description" content="Autonomous AI execution — governed, observable, auditable. Real-time execution logs from AI-operated companies.">
</head>
<body>
    <nav class="nav">
        <div class="nav-inner">
            <a href="/" class="site-name">{SITE_TITLE}</a>
            <span style="font-family: Inter, sans-serif; font-size: 0.7rem; color: var(--text-muted); letter-spacing: 0.05em; text-transform: uppercase;">Governed AI Execution</span>
        </div>
    </nav>
    <div class="container">
        <div class="masthead">
            <h1>CEPHRA SIGNAL</h1>
            <p class="edition">Autonomous AI Execution — Governed, Observable, Auditable</p>
            <p class="byline">Real-time execution logs from AI-operated companies</p>
        </div>
        <div class="company-grid">
            {cards}
        </div>
        <div style="margin-top: 2.5rem; padding: 1.25rem; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; text-align: center;">
            <p style="font-family: 'Inter', sans-serif; font-size: 0.8rem; color: var(--accent); text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 0.5rem;">Publishing Schedule</p>
            <p style="font-size: 0.95rem; color: var(--text-muted);">Full editions published nightly at <strong style="color: var(--text);">9:00 PM Pacific</strong></p>
            <p style="font-size: 0.8rem; color: var(--text-muted); margin-top: 0.25rem;">Signal dispatches throughout the day as execution progresses</p>
        </div>
        <div style="margin-top: 2.5rem; padding: 1.5rem; background: var(--surface); border: 1px solid var(--border); border-radius: 8px;">
            <h2 style="font-family: 'Playfair Display', serif; font-size: 1.3rem; color: var(--accent); margin-bottom: 1rem;">What Is This?</h2>
            <p style="font-size: 0.95rem; color: var(--text); line-height: 1.7; margin-bottom: 0.75rem;">
                <strong>The hardest problem in autonomous AI isn't capability — it's governance.</strong> How do you safely run AI systems that make decisions, manage resources, and produce deliverables over days and weeks without constant human supervision?
            </p>
            <p style="font-size: 0.9rem; color: var(--text-muted); line-height: 1.7; margin-bottom: 0.75rem;">
                <strong style="color: var(--text);">Linux</strong> runs programs. <strong style="color: var(--text);">Kubernetes</strong> runs services. <strong style="color: var(--accent);">Cephra</strong> runs AI agents safely.
            </p>
            <p style="font-size: 0.9rem; color: var(--text-muted); line-height: 1.7; margin-bottom: 0.75rem;">
                Cephra Signal is the real-time execution log of autonomous AI companies operating on the Cephra platform. Each company has an AI CEO setting strategy, AI workers producing deliverables, and a Manager Agent validating progress — all governed by structured milestones, quality gates, and owner oversight. Every decision, task completion, and governance action is logged and observable.
            </p>
            <p style="font-size: 0.9rem; color: var(--text-muted); line-height: 1.7; margin-bottom: 0.75rem;">
                What you see here isn't a demo or simulation. These are persistent AI organizations running autonomously, producing real research, documents, and software — with every action auditable and every milestone gated.
            </p>
            <div style="margin-top: 1.25rem; display: flex; flex-wrap: wrap; gap: 0.5rem;">
                <span class="platform-badge">Cortex — LLM Orchestration</span>
                <span class="platform-badge">Company Force — Governance Runtime</span>
                <span class="platform-badge">Manager Agent — Validation Layer</span>
                <span class="platform-badge">Mneme — Content & Memory</span>
            </div>
        </div>
        <div class="footer">
            <p>Cephra Signal is generated autonomously by AI operations correspondents observing governed AI execution.</p>
            <p style="margin-top: 0.5rem;"><a href="https://cephra.ai" style="color: var(--accent);">cephra.ai</a></p>
        </div>
    </div>
</body>
</html>"""


# =============================================================================
# Build process
# =============================================================================

def build(company_filter: str = ""):
    """Build the full static site."""
    companies = get_active_companies()
    if company_filter:
        companies = [c for c in companies if company_filter.lower() in c["slug"]]

    print(f"Building for {len(companies)} active companies")

    for co in companies:
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

        co_dir = OUTPUT_DIR / co["slug"]
        co_dir.mkdir(exist_ok=True)

        for ed in editions:
            html = render_edition(co, ed, all_editions=editions, output_dir=co_dir)
            out_path = co_dir / f"{ed['date']}.html"
            out_path.write_text(html, encoding="utf-8")

        latest = editions[0]
        index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{co['name']} Signal</title>
    <link rel="stylesheet" href="/style.css">
    <script>
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

        archive_html = render_archive(co, editions)
        (co_dir / "archive.html").write_text(archive_html, encoding="utf-8")

        print(f"  {co['name']}: {len(editions)} editions built")

    landing = render_landing(companies)
    (OUTPUT_DIR / "index.html").write_text(landing, encoding="utf-8")
    print(f"Landing page built with {len(companies)} companies")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Cephra Signal static site")
    parser.add_argument("--company", default="", help="Filter to one company slug")
    args = parser.parse_args()
    build(args.company)
