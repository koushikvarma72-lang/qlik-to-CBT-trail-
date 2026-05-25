from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "docs_assets"
OUTPUT_DIR.mkdir(exist_ok=True)


BG = "#f8fafc"
TEXT = "#0f172a"
BORDER = "#334155"
ARROW = "#64748b"
BLUE = "#dbeafe"
GREEN = "#dcfce7"
AMBER = "#fef3c7"
ROSE = "#ffe4e6"
SLATE = "#e2e8f0"
WHITE = "#ffffff"


def load_font(size, bold=False):
    candidates = []
    if bold:
        candidates.extend(
            [
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/segoeuib.ttf",
                "C:/Windows/Fonts/calibrib.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
                "C:/Windows/Fonts/calibri.ttf",
            ]
        )

    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


TITLE_FONT = load_font(34, bold=True)
SUBTITLE_FONT = load_font(22, bold=True)
TEXT_FONT = load_font(18)
SMALL_FONT = load_font(16)


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_box(draw, box, title, body_lines, fill=WHITE, title_fill=None):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=18, fill=fill, outline=BORDER, width=3)
    if title_fill:
        draw.rounded_rectangle((x1, y1, x2, y1 + 44), radius=18, fill=title_fill, outline=BORDER, width=0)
        draw.rectangle((x1, y1 + 22, x2, y1 + 44), fill=title_fill, outline=title_fill)
    draw.text((x1 + 16, y1 + 10), title, font=SUBTITLE_FONT, fill=TEXT)
    y = y1 + 56
    for line in body_lines:
        draw.text((x1 + 16, y), line, font=TEXT_FONT, fill=TEXT)
        y += 26


def draw_arrow(draw, start, end, label=None):
    sx, sy = start
    ex, ey = end
    draw.line((sx, sy, ex, ey), fill=ARROW, width=4)

    dx = ex - sx
    dy = ey - sy
    length = max((dx * dx + dy * dy) ** 0.5, 1)
    ux = dx / length
    uy = dy / length
    size = 12
    left = (ex - size * ux - size * 0.6 * uy, ey - size * uy + size * 0.6 * ux)
    right = (ex - size * ux + size * 0.6 * uy, ey - size * uy - size * 0.6 * ux)
    draw.polygon([(ex, ey), left, right], fill=ARROW)

    if label:
        mx = (sx + ex) / 2
        my = (sy + ey) / 2
        pad = 6
        bbox = draw.textbbox((0, 0), label, font=SMALL_FONT)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.rounded_rectangle((mx - tw / 2 - pad, my - th / 2 - pad, mx + tw / 2 + pad, my + th / 2 + pad), radius=8, fill=BG, outline=None)
        draw.text((mx - tw / 2, my - th / 2 - 1), label, font=SMALL_FONT, fill=TEXT)


def draw_centered_title(draw, width, title, subtitle):
    title_bbox = draw.textbbox((0, 0), title, font=TITLE_FONT)
    subtitle_bbox = draw.textbbox((0, 0), subtitle, font=TEXT_FONT)
    draw.text(((width - (title_bbox[2] - title_bbox[0])) / 2, 24), title, font=TITLE_FONT, fill=TEXT)
    draw.text(((width - (subtitle_bbox[2] - subtitle_bbox[0])) / 2, 70), subtitle, font=TEXT_FONT, fill="#334155")


def render_architecture_diagram():
    width, height = 2100, 1320
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    draw_centered_title(
        draw,
        width,
        "QVF Decoder Architecture Diagram",
        "Current implementation view: frontend, Flask orchestration, extraction, AI regeneration, dbt integration, and persistence",
    )

    boxes = {
        "frontend": (60, 170, 420, 330),
        "api": (500, 170, 860, 330),
        "server": (940, 145, 1340, 355),
        "extractor": (1420, 70, 2020, 300),
        "sqlgen": (1420, 350, 2020, 570),
        "dbt": (1420, 630, 2020, 870),
        "db": (930, 660, 1340, 880),
        "openrouter": (1420, 940, 2020, 1120),
        "files": (500, 630, 860, 850),
    }

    draw_box(
        draw,
        boxes["frontend"],
        "Frontend UI",
        [
            "Vite app",
            "Pages + components",
            "Global store",
            "File upload and review flow",
        ],
        fill=BLUE,
        title_fill="#bfdbfe",
    )
    draw_box(
        draw,
        boxes["api"],
        "Frontend API Client",
        [
            "frontend/src/api.js",
            "Calls /api/upload",
            "Calls /api/model",
            "Calls /api/regenerate and dbt endpoints",
        ],
        fill=SLATE,
        title_fill="#cbd5e1",
    )
    draw_box(
        draw,
        boxes["server"],
        "Flask App Orchestrator",
        [
            "server.py",
            "Routes, session assembly, job handling",
            "Upload processing and response shaping",
            "Static file serving",
        ],
        fill=GREEN,
        title_fill="#bbf7d0",
    )
    draw_box(
        draw,
        boxes["extractor"],
        "QVF Extraction Engine",
        [
            "qvf_extractor.py",
            "ZIP and binary parsing",
            "Inline table sample extraction",
            "Graph, relationships, script discovery",
        ],
        fill=AMBER,
        title_fill="#fde68a",
    )
    draw_box(
        draw,
        boxes["sqlgen"],
        "SQL Planning and Generation",
        [
            "sql_generation.py",
            "Plan extraction from Qlik script",
            "Prompt building and response parsing",
            "Validation and repair checks",
        ],
        fill=AMBER,
        title_fill="#fde68a",
    )
    draw_box(
        draw,
        boxes["dbt"],
        "dbt Integration",
        [
            "dbt_agent_routes.py",
            "dbt_cloud_agent.py",
            "Command sanitization and run planning",
            "dbt Cloud job trigger and status",
        ],
        fill=ROSE,
        title_fill="#fecdd3",
    )
    draw_box(
        draw,
        boxes["db"],
        "SQLite Persistence",
        [
            "qvf_decoder.db",
            "sessions",
            "uploaded_files",
            "extracted_data",
            "regeneration_history",
        ],
        fill=WHITE,
        title_fill="#e2e8f0",
    )
    draw_box(
        draw,
        boxes["openrouter"],
        "AI Gateway",
        [
            "ai_client.py",
            "OpenRouter chat completions",
            "Used for SQL migration and dbt planning",
        ],
        fill=WHITE,
        title_fill="#e2e8f0",
    )
    draw_box(
        draw,
        boxes["files"],
        "Uploaded and Extracted Files",
        [
            "uploads/",
            "raw QVF files",
            "temporary extracted artifacts",
            "script.qvs, metadata.json, associations.json",
        ],
        fill=WHITE,
        title_fill="#e2e8f0",
    )

    draw_arrow(draw, (420, 250), (500, 250), "HTTP")
    draw_arrow(draw, (860, 250), (940, 250), "REST")
    draw_arrow(draw, (1340, 210), (1420, 185), "extract")
    draw_arrow(draw, (1340, 440), (1420, 460), "plan")
    draw_arrow(draw, (1340, 760), (1420, 750), "dbt")
    draw_arrow(draw, (1135, 355), (1135, 660), "persist")
    draw_arrow(draw, (860, 740), (930, 740), "save")
    draw_arrow(draw, (1720, 570), (1720, 940), "LLM calls")
    draw_arrow(draw, (1720, 870), (1720, 940), "AI plan")
    draw_arrow(draw, (860, 250), (680, 630), "upload")

    note = [
        "Main current characteristic:",
        "server.py is the central orchestration point.",
        "Helper modules provide extraction, SQL logic,",
        "AI access, and dbt Cloud integration.",
    ]
    draw_box(draw, (60, 980, 760, 1180), "Architecture Note", note, fill="#eef2ff", title_fill="#c7d2fe")

    png_path = OUTPUT_DIR / "architecture-diagram.png"
    jpg_path = OUTPUT_DIR / "architecture-diagram.jpg"
    image.save(png_path)
    image.save(jpg_path, quality=92)


def render_layered_view():
    width, height = 1800, 1260
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    draw_centered_title(
        draw,
        width,
        "QVF Decoder Layered View",
        "A simplified layered architecture showing how presentation, API, core logic, integrations, and data are organized today",
    )

    layers = [
        ("Presentation", BLUE, "#bfdbfe", (180, 150, 1620, 310), ["Frontend UI", "Pages, graph, editors, review flow", "Global state store", "frontend/src/api.js"]),
        ("API Layer", GREEN, "#bbf7d0", (180, 360, 1620, 520), ["Flask routes in server.py", "Upload, model load, regenerate, explain, reset", "dbt route registration"]),
        ("Core Logic", AMBER, "#fde68a", (180, 570, 1620, 800), ["qvf_extractor.py", "sql_generation.py", "session assembly", "background regeneration job orchestration"]),
        ("Integrations", ROSE, "#fecdd3", (180, 850, 1620, 1020), ["ai_client.py -> OpenRouter", "dbt_cloud_agent.py -> dbt Cloud API", "requests-based external communication"]),
        ("Data Layer", SLATE, "#cbd5e1", (180, 1070, 1620, 1220), ["SQLite database", "uploads/ directory", "extracted intermediate files and persisted session state"]),
    ]

    centers = []
    for title, fill, title_fill, box, lines in layers:
        draw_box(draw, box, title, lines, fill=fill, title_fill=title_fill)
        centers.append(((box[0] + box[2]) // 2, box[3]))

    for index in range(len(centers) - 1):
        start = centers[index]
        next_box = layers[index + 1][3]
        end = ((next_box[0] + next_box[2]) // 2, next_box[1])
        draw_arrow(draw, start, end, "flows through")

    left_note = [
        "Strength:",
        "Most domain logic is already split into helper modules.",
        "",
        "Current tradeoff:",
        "The API layer and orchestration responsibilities are still concentrated in server.py.",
    ]
    draw_box(draw, (40, 360, 150, 780), "", [], fill=BG)
    y = 390
    for line in left_note:
        if line:
            wrapped = wrap_text(draw, line, TEXT_FONT, 100)
            for item in wrapped:
                draw.text((50, y), item, font=TEXT_FONT, fill=TEXT)
                y += 24
        else:
            y += 18

    png_path = OUTPUT_DIR / "layered-view.png"
    jpg_path = OUTPUT_DIR / "layered-view.jpg"
    image.save(png_path)
    image.save(jpg_path, quality=92)


if __name__ == "__main__":
    render_architecture_diagram()
    render_layered_view()
    print(f"Generated diagram files in: {OUTPUT_DIR}")
