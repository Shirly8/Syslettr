"""
Systems Design Daily — main runner

CRON (every 3 days at 8am):
  0 8 */3 * * cd /Users/shirleyhuang/Documents/BBGo && python generate.py
"""

import json
import base64
import os
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import smtplib

import anthropic
from playwright.sync_api import sync_playwright

from curriculum import CURRICULUM

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR          = Path(__file__).parent
TOC_FILE          = BASE_DIR / "toc.json"
GENERATED_DIR     = BASE_DIR / "generated"
SENDER            = os.getenv("SENDER", "")
RECIPIENT         = os.getenv("RECEIVER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_SMTP", "")
CLAUDE_MODEL      = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# Tier 3 unlocks after 10 Tier-1 emails; Tier 4 after 20 total
TIER_UNLOCK = {1: 0, 2: 0, 3: 10, 4: 20}

# Color palette for diagram components
COLORS = {
    "client":    "#4A90D9",
    "lb":        "#7B68EE",
    "server":    "#5BA85A",
    "cache":     "#E8944A",
    "db":        "#C0392B",
    "queue":     "#8E44AD",
    "cdn":       "#16A085",
    "external":  "#7F8C8D",
}


# ── TOC ───────────────────────────────────────────────────────────────────────
def load_toc() -> dict:
    if not TOC_FILE.exists():
        return {"generated": [], "stats": {"total_sent": 0, "tier_counts": {"1": 0, "2": 0, "3": 0, "4": 0}}}
    return json.loads(TOC_FILE.read_text())


def save_toc(toc: dict):
    TOC_FILE.write_text(json.dumps(toc, indent=2))


# ── Topic selection ───────────────────────────────────────────────────────────
def pick_next_topic(toc: dict) -> dict:
    total   = toc["stats"]["total_sent"]
    covered = {e["topic"] for e in toc["generated"]}

    for tier in [1, 2, 3, 4]:
        if total < TIER_UNLOCK[tier]:
            continue
        for item in CURRICULUM:
            if item["tier"] == tier and item["topic"] not in covered:
                return item

    # All topics done — cycle back through Tier 1 for variations
    tier1 = [item for item in CURRICULUM if item["tier"] == 1]
    return tier1[total % len(tier1)]


# ── Claude generation ─────────────────────────────────────────────────────────
SYSTEM = """You are a systems design educator writing for a software engineer preparing for FAANG interviews.
Produce clear, accurate, interview-relevant content. Return valid JSON only — no markdown fences."""


def build_prompt(topic: str, tier: int, covered_angles: list[str]) -> str:
    angle_note = f"\nAngles already covered for this topic: {covered_angles}" if covered_angles else ""
    color_guide = "\n".join(f"  - {k}: {v}" for k, v in COLORS.items())
    return f"""Generate a systems design lesson formatted as a realistic mock interview on: "{topic}" (Tier {tier}){angle_note}

Return exactly this JSON structure:
{{
  "subject": "Systems Design: {topic}",
  "angle": "specific angle covered in 8 words or less",
  "intro": "2 sentences — what makes this system surprisingly hard and why it's asked in interviews",

  "opening": [
    {{
      "speaker": "interviewer",
      "text": "Natural opening that sets up the problem and asks where the candidate starts. One or two sentences."
    }},
    {{
      "speaker": "you",
      "text": "First-person, 2-3 direct questions. No filler. Cut straight to what matters. E.g. 'Before diving in — what\\'s the scale we\\'re targeting? Millions of links or billions? And is this purely auto-generated codes or do users get custom aliases?'"
    }},
    {{
      "speaker": "interviewer",
      "text": "Concrete constraints in 2-3 short sentences. Scale, feature scope, SLA. No filler."
    }},
    {{
      "speaker": "you",
      "text": "First-person. State your assumptions and derived numbers directly — no padding. Define jargon inline once. E.g. 'So roughly 1,200 writes/sec and 115K reads/sec. Read-heavy, so caching (keeping hot mappings in memory) is central. I\\'ll plan for ~90TB over five years.'"
    }}
  ],

  "core_components": [
    {{
      "name": "Component Name",
      "interviewer_prompt": "A natural follow-up the interviewer would ask about this specific component. E.g. 'How would you generate the short codes without collisions?'",
      "notes": [
        "WHY: one direct sentence — what breaks without this.",
        "HOW: first-person, 2-3 sentences max. Concrete and specific. Define jargon inline on first use.",
        "TRADEOFF: one sentence — what you give up."
      ],
      "diagram_html": null
    }}
  ],

  "architecture": {{
    "diagram_html": "<full standalone HTML — see DIAGRAM SPEC below>"
  }},

  "bottlenecks": [
    {{
      "problem": "One sentence: what breaks and why.",
      "fix": "First-person, 2-3 sentences. Concrete fix, no language/framework names. Define jargon inline.",
      "tradeoff": "One sentence: what you give up.",
      "diagram_html": null
    }}
  ],

  "api_design": [
    {{"method": "POST", "endpoint": "/api/v1/...", "note": "one-line: what it does and returns"}},
    {{"method": "GET", "endpoint": "/...", "note": "one-line description"}}
  ],

  "db_storage": [
    "DB name + one-line reason why",
    "Schema: table(col TYPE PK, col TYPE, ...) — tight",
    "Index strategy — one line",
    "Key tradeoff — one line"
  ]
}}

JARGON RULE: Define each technical term inline in parentheses the FIRST time it appears — never again after that.
Keep definitions tight (under 10 words). Examples:
  - "p99 latency (99th percentile — the slow tail, not the average)"
  - "sharding (splitting data across multiple DB machines)"
  - "LRU eviction (drops least-recently-used entries when cache is full)"

TONE RULE: First-person interview style throughout. Direct, concise, no padding.
- Cut every sentence that doesn't add information. No wind-up, no summary at the end.
- Never use: "That's a great question", "As I mentioned", "Let me think through this".
- Candidate speaks like someone who knows what they're doing — confident, not verbose.
- Interviewer lines: 1-3 short sentences max.

RULES FOR diagram_html fields:
- The architecture.diagram_html is ALWAYS required — it shows the full system.
- core_components[].diagram_html: Generate a diagram for AT LEAST 2 components per email.
  Good candidates: any component with a multi-step internal flow (e.g. ID generation pipeline,
  cache hit/miss branches, producer→queue→consumer, write path with fan-out).
  Use null only if the component is a single conceptual box with no interesting internal flow.
- bottlenecks[].diagram_html: Generate a diagram for AT LEAST 1 bottleneck per email.
  Good candidates: bottlenecks where the fix changes a flow (e.g. before/after showing
  how requests are distributed differently, or how a queue decouples two components).
  Use null only if the fix is purely a config change with no structural difference to draw.
- All component/bottleneck diagrams: small and focused (SVG width=700). Illustrate ONE thing.
  Do NOT repeat the full architecture — zoom in on just the relevant piece.

DIAGRAM SPEC (applies to all diagram_html fields):
- Complete self-contained HTML file, white background (#ffffff), 32px padding
- Font: -apple-system, BlinkMacSystemFont, sans-serif
- SVG layout rules:
  · Architecture diagram: width=1200, flow LEFT → RIGHT in tier columns
  · Component/bottleneck diagrams: width=700, focused on ONE concept
  · All arrows: HORIZONTAL or 90-degree elbow bends only — NO diagonals
  · Min 160px gap between columns, 80px between rows
  · Group rects drawn FIRST (fill="#f0f4f8", stroke="#d0d8e4", rx=12)
- Component boxes: <rect> rx="10" width=160 height=70 (wider if needed — never clip text)
  · Name: font-size=13 font-weight=700 fill=white, centered
  · Subtitle: font-size=11 fill=rgba(255,255,255,0.85), dy=20 below center
  · Drop shadow: filter="drop-shadow(0 2px 4px rgba(0,0,0,.15))"
- Arrows: elbow <path>, stroke=#666 stroke-width=1.5, arrowhead marker
  · Labels: MAX 3 words, font-size=10 fill=#555, 10px above midpoint, never overlapping
  · Show ONLY the 4-6 most important arrows
- Color palette:
{color_guide}
"""


def generate_content(topic_item: dict, toc: dict) -> dict:
    client = anthropic.Anthropic()
    covered_angles = [e["angle"] for e in toc["generated"] if e["topic"] == topic_item["topic"]]
    prompt = build_prompt(topic_item["topic"], topic_item["tier"], covered_angles)

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = msg.content[0].text.strip()
    # Strip accidental code fences if Claude adds them
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(raw)


# ── Diagram rendering ─────────────────────────────────────────────────────────
def render_html_to_png(html: str, width: int = 1200) -> bytes:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 1})
        page.set_content(html, wait_until="networkidle")
        page.wait_for_timeout(300)
        # Trim SVG height to actual content — Claude often generates oversized canvases
        page.evaluate("""() => {
            const svg = document.querySelector('svg');
            if (!svg) return;
            let maxY = 0;
            svg.querySelectorAll('*').forEach(el => {
                try { const b = el.getBBox(); maxY = Math.max(maxY, b.y + b.height); } catch(e) {}
            });
            if (maxY > 0) svg.setAttribute('height', Math.ceil(maxY + 32));
        }""")
        content_height = page.evaluate("document.documentElement.scrollHeight")
        page.set_viewport_size({"width": width, "height": content_height})
        png = page.screenshot(full_page=False)
        browser.close()
    return png


# ── Email builder ─────────────────────────────────────────────────────────────
IMG_STYLE = "max-width:100%;border-radius:8px;box-shadow:0 2px 16px rgba(0,0,0,.1);"
IMG_WRAP  = '<div style="margin:16px -40px;background:#f7f8fa;padding:20px 40px;text-align:center;"><img src="{src}" style="' + IMG_STYLE + '"></div>'

def inline_diagram(cid: str) -> str:
    return IMG_WRAP.format(src=f"cid:{cid}")

def build_email_html(data: dict, rendered_cids: dict) -> str:
    """rendered_cids maps section keys to their cid strings, e.g. {'arch': 'arch_diagram', 'comp_0': 'comp_0_diagram'}"""
    def li(items): return "".join(f"<li style='margin-bottom:5px;'>{i}</li>" for i in items)
    def note_li(items): return "".join(f"<li style='margin-bottom:7px;color:#444;line-height:1.75;font-size:14px;'>{i}</li>" for i in items)

    S  = "font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#999;margin:32px 0 12px;"
    S2 = "font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:#999;margin:0 0 10px;"

    COMP_COLORS = ["#4A90D9", "#5BA85A", "#E8944A", "#8E44AD", "#16A085", "#C0392B", "#7B68EE"]

    # Component overview pills
    comps = data.get("core_components", [])
    pills = "".join(
        f'<span style="display:inline-block;background:{COMP_COLORS[i % len(COMP_COLORS)]};color:white;'
        f'font-size:11px;font-weight:600;padding:5px 11px;border-radius:20px;margin:3px 5px 3px 0;'
        f'white-space:nowrap;">{c["name"]}</span>'
        for i, c in enumerate(comps)
    )
    overview = f'<div style="margin:0 0 28px;padding:16px 18px;background:#f8f9fa;border-radius:10px;line-height:2;">{pills}</div>'

    # Opening dialogue — alternating interviewer / candidate bubbles
    opening_html = ""
    for msg in data.get("opening", []):
        speaker = msg.get("speaker", "")
        text = msg.get("text", "")
        if speaker == "interviewer":
            opening_html += f"""
            <div style="margin-bottom:16px;">
              <div style="font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:#aaa;margin-bottom:5px;">Interviewer</div>
              <div style="background:#f5f5f7;border-radius:14px;border-top-left-radius:3px;padding:14px 18px;font-size:14px;color:#444;line-height:1.8;max-width:88%;display:inline-block;">{text}</div>
            </div>"""
        else:
            opening_html += f"""
            <div style="margin-bottom:16px;text-align:right;">
              <div style="font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:#4A90D9;margin-bottom:5px;">You</div>
              <div style="background:#eef4ff;border-radius:14px;border-top-right-radius:3px;padding:14px 18px;font-size:14px;color:#1a3a6e;line-height:1.8;display:inline-block;text-align:left;max-width:88%;">{text}</div>
            </div>"""

    # Core components — colored left border cycles through palette, interviewer prompt above each
    components = ""
    for i, c in enumerate(comps):
        cid_key  = f"comp_{i}"
        diag     = inline_diagram(cid_key) if cid_key in rendered_cids else ""
        color    = COMP_COLORS[i % len(COMP_COLORS)]
        prompt   = c.get("interviewer_prompt", "")
        prompt_html = ""
        if prompt:
            prompt_html = (
                f'<div style="margin-bottom:7px;">'
                f'<span style="font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:#aaa;">Interviewer</span>'
                f'<span style="font-size:13px;color:#777;margin-left:8px;font-style:italic;">"{prompt}"</span>'
                f'</div>'
            )
        dot = f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{color};margin-right:7px;vertical-align:middle;"></span>'
        components += (
            f'{prompt_html}'
            f'<div style="margin-bottom:18px;padding:16px 18px;background:#fafafa;border-radius:10px;border-left:3px solid {color};">'
            f'<div style="font-size:13px;font-weight:700;color:#1a1a2e;margin-bottom:8px;">{dot}{c["name"]}</div>'
            f'<ul style="margin:0;padding-left:18px;line-height:1.75;">{note_li(c.get("notes", []))}</ul>'
            f'{diag}'
            f'</div>'
        )

    apis = "".join(
        f'<div style="margin-bottom:9px;font-size:13px;color:#333;line-height:1.6;">'
        f'<span style="background:#4A90D9;color:white;font-size:10px;font-weight:700;padding:2px 7px;border-radius:3px;letter-spacing:.5px;">{a["method"]}</span>'
        f'<code style="background:#f0f0f0;padding:2px 7px;border-radius:3px;font-size:12px;color:#c0392b;margin:0 6px;">{a["endpoint"]}</code>'
        f'<span style="color:#666;">— {a.get("note","")}</span>'
        f'</div>'
        for a in data.get("api_design", [])
    )

    db = li(data.get("db_storage", []))

    # Bottlenecks — numbered cards prefixed with a single interviewer question
    bottlenecks_html = (
        '<div style="margin-bottom:16px;">'
        '<span style="font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:#aaa;">Interviewer</span>'
        '<span style="font-size:13px;color:#777;margin-left:8px;font-style:italic;">"What would break in this design at scale?"</span>'
        '</div>'
    )
    for i, b in enumerate(data.get("bottlenecks", [])):
        cid_key = f"bt_{i}"
        diag    = inline_diagram(cid_key) if cid_key in rendered_cids else ""
        num     = f'<span style="display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:50%;background:#E8944A;color:white;font-size:11px;font-weight:700;margin-right:8px;flex-shrink:0;">{i+1}</span>'
        bottlenecks_html += (
            f'<div style="margin-bottom:16px;padding:16px 18px;background:#fff8f0;border-radius:10px;border-left:3px solid #E8944A;">'
            f'<div style="font-size:13px;font-weight:700;color:#b85a00;margin-bottom:8px;display:flex;align-items:center;">{num}{b["problem"]}</div>'
            f'<div style="font-size:13px;color:#333;margin-bottom:6px;line-height:1.7;"><strong>Fix:</strong> {b["fix"]}</div>'
            f'<div style="font-size:12px;color:#888;line-height:1.65;border-top:1px solid #ffe0c0;margin-top:8px;padding-top:8px;"><strong>Tradeoff:</strong> {b["tradeoff"]}</div>'
            f'{diag}'
            f'</div>'
        )

    arch_diagram = inline_diagram("arch") if "arch" in rendered_cids else ""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:white;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="width:100%;background:white;">

  <div style="background:linear-gradient(135deg,#0f0f1a 0%,#1a1a3e 100%);padding:36px 40px;">
    <div style="font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:#6666aa;margin-bottom:10px;">Systems Design · Tier {data.get('tier','')}</div>
    <h1 style="margin:0;font-size:26px;font-weight:700;color:white;line-height:1.3;">{data['subject'].replace('Systems Design: ','')}</h1>
  </div>

  <div style="padding:36px 40px;">

    <p style="font-size:15px;color:#444;line-height:1.8;margin:0 0 24px;padding:14px 18px;background:#f0f4ff;border-left:4px solid #4A90D9;border-radius:0 8px 8px 0;">{data['intro']}</p>

    {overview}

    <h2 style="{S}">Interview</h2>
    <div style="background:#fafafa;border-radius:12px;padding:20px 22px;margin-bottom:8px;">
      {opening_html}
    </div>

    <h2 style="{S}">Core Components</h2>
    {components}

    <h2 style="{S}">Architecture</h2>
    <div style="margin-bottom:8px;">{arch_diagram}</div>

    <h2 style="{S}">Bottlenecks</h2>
    {bottlenecks_html}

    <div style="margin-top:32px;padding:22px 24px;background:#f8f9fa;border-radius:10px;">
      <h2 style="{S2}">Reference</h2>
      <strong style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.5px;">API</strong>
      <div style="margin:8px 0 18px;">{apis}</div>
      <strong style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.5px;">Database & Storage</strong>
      <ul style="margin:6px 0 0;padding-left:20px;color:#444;line-height:1.9;font-size:14px;">{db}</ul>
    </div>

  </div>

  <div style="background:#f0f0f4;padding:16px 40px;font-size:12px;color:#aaa;text-align:center;border-top:1px solid #e8e8ec;">
    Systems Design
  </div>
</div>
</body>
</html>"""


# ── Archive to disk ──────────────────────────────────────────────────────────
def save_to_disk(data: dict, rendered: dict, email_number: int):
    """rendered: {cid_key: png_bytes}"""
    GENERATED_DIR.mkdir(exist_ok=True)
    slug = data["subject"].replace("Systems Design: ", "").lower()
    slug = "".join(c if c.isalnum() else "_" for c in slug).strip("_")
    filename = f"{email_number:03d}_{slug}.html"

    html = build_email_html(data, rendered)
    for cid_key, png in rendered.items():
        b64 = f"data:image/png;base64,{base64.b64encode(png).decode()}"
        html = html.replace(f"cid:{cid_key}", b64)

    (GENERATED_DIR / filename).write_text(html, encoding="utf-8")
    print(f"Saved : generated/{filename}")


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(subject: str, html_body: str, rendered: dict):
    msg = MIMEMultipart("related")
    msg["to"]      = RECIPIENT
    msg["from"]    = SENDER
    msg["subject"] = subject

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    for cid_key, png in rendered.items():
        img = MIMEImage(png, "png")
        img.add_header("Content-ID", f"<{cid_key}>")
        img.add_header("Content-Disposition", "inline", filename=f"{cid_key}.png")
        msg.attach(img)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER, GMAIL_APP_PASSWORD)
        server.sendmail(SENDER, RECIPIENT, msg.as_bytes())


# ── Main ──────────────────────────────────────────────────────────────────────
def check_smtp():
    """Verify SMTP credentials before doing any expensive work."""
    print("Checking email credentials...")
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(SENDER, GMAIL_APP_PASSWORD)
    print("Email credentials OK")


def main():
    test_mode = "--test" in sys.argv

    if not test_mode:
        check_smtp()

    toc        = load_toc()
    topic_item = pick_next_topic(toc)
    print(f"Topic : {topic_item['topic']} (Tier {topic_item['tier']})")

    print("Generating content...")
    data = generate_content(topic_item, toc)
    data["tier"] = topic_item["tier"]
    print(f"Angle : {data['angle']}")

    # Render all diagrams: arch is always present, comp/bottleneck diagrams are optional
    print("Rendering diagrams...")
    rendered = {}
    arch_html = data.get("architecture", {}).get("diagram_html")
    if arch_html:
        rendered["arch"] = render_html_to_png(arch_html, width=1200)

    for i, c in enumerate(data.get("core_components", [])):
        if c.get("diagram_html"):
            rendered[f"comp_{i}"] = render_html_to_png(c["diagram_html"], width=700)

    for i, b in enumerate(data.get("bottlenecks", [])):
        if b.get("diagram_html"):
            rendered[f"bt_{i}"] = render_html_to_png(b["diagram_html"], width=700)

    email_number = toc["stats"]["total_sent"] + 1
    save_to_disk(data, rendered, email_number)

    if test_mode:
        print("Test mode — skipping email. Open generated/ to preview.")
        return

    print("Sending email...")
    html_body = build_email_html(data, rendered)
    send_email(data["subject"], html_body, rendered)

    toc["generated"].append({
        "topic":        topic_item["topic"],
        "tier":         topic_item["tier"],
        "angle":        data["angle"],
        "sent_at":      datetime.now().strftime("%Y-%m-%d"),
        "email_number": email_number,
    })
    toc["stats"]["total_sent"] += 1
    toc["stats"]["tier_counts"][str(topic_item["tier"])] += 1
    save_toc(toc)

    print(f"Done!  Email #{toc['stats']['total_sent']} sent — {topic_item['topic']}")


if __name__ == "__main__":
    main()
