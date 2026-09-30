import base64
import io
import os
import shutil
import subprocess
import tempfile
import threading

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from starlette.background import BackgroundTask

# ---------- Настройки (можно менять через переменные окружения Railway) ----------
DEF_W = int(os.getenv("VIDEO_W", "1600"))
DEF_H = int(os.getenv("VIDEO_H", "900"))
FPS = int(os.getenv("VIDEO_FPS", "25"))
CRF = os.getenv("VIDEO_CRF", "23")
DEFAULT_DURATION = float(os.getenv("DEFAULT_DURATION", "3"))
TITLE_DURATION = float(os.getenv("TITLE_DURATION", "4"))
MAX_DURATION = float(os.getenv("MAX_DURATION", "30"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "30"))
SHOW_TEXT = os.getenv("SHOW_TEXT", "1") == "1"
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT", "240"))

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

render_lock = threading.Semaphore(1)  # один рендер за раз — экономия памяти
app = FastAPI()


# ---------- Вспомогательные функции ----------
def decode_image(b64, target=None):
    if not b64 or not isinstance(b64, str):
        raise ValueError("empty image b64")
    if b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    if target and img.format == "JPEG":
        img.draft("RGB", target)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white, img)
    return img.convert("RGB")


def as_text(v):
    return "" if v is None else str(v).strip()


def even(v):
    v = int(v)
    return v - (v % 2)


def wrap(draw, text, font, max_w):
    words = str(text).split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_lines(draw, text, font_path, max_w, start, min_size, max_lines):
    if not text:
        return None, []
    for size in range(start, min_size - 1, -2):
        font = ImageFont.truetype(font_path, size)
        lines = wrap(draw, text, font, max_w)
        if len(lines) <= max_lines:
            return font, lines
    font = ImageFont.truetype(font_path, min_size)
    lines = wrap(draw, text, font, max_w)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" ,.") + "…"
    return font, lines


def balanced_two_lines(draw, text, font, max_w):
    """Делит название на 2 строки примерно равной длины."""
    words = str(text).split()
    if len(words) < 2:
        return [text] if draw.textlength(text, font=font) <= max_w else None
    best = None
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        wa, wb = draw.textlength(a, font=font), draw.textlength(b, font=font)
        if wa <= max_w and wb <= max_w:
            score = abs(wa - wb)
            if best is None or score < best[0]:
                best = (score, [a, b])
    return best[1] if best else None


def draw_box(frame, box, radius, alpha=150):
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(box, radius=radius, fill=(0, 0, 0, alpha))
    return Image.alpha_composite(frame, overlay)


def draw_shadow_text(d, xy, text, font, fill, shadow):
    x, y = xy
    d.text((x + shadow, y + shadow), text, font=font, fill=(0, 0, 0, 200))
    d.text((x, y), text, font=font, fill=fill)


def parse_duration(v, default):
    try:
        d = float(v)
    except (TypeError, ValueError):
        return default
    if d <= 0:
        return default
    return min(d, MAX_DURATION)


# ---------- Кадр-заставка: название сверху по центру, время и порции справа внизу ----------
def make_title_frame(bg, card, title, cook_time, servings, W, H, path):
    s = H / 900.0
    margin = int(50 * s)
    frame = bg.copy().convert("RGBA")
    d = ImageDraw.Draw(frame)

    # Название — 2 строки, по центру сверху
    title_bottom = margin
    title = as_text(title)
    if title:
        max_w = int(W * 0.84)
        font, lines = None, None
        for size in range(int(74 * s), int(34 * s) - 1, -2):
            f = ImageFont.truetype(FONT_BOLD, size)
            ls = balanced_two_lines(d, title, f, max_w)
            if ls:
                font, lines = f, ls
                break
        if not lines:
            font, lines = fit_lines(d, title, FONT_BOLD, max_w, int(40 * s), int(30 * s), 2)
        lh = int(font.size * 1.22)
        pad = int(26 * s)
        tw = max(d.textlength(l, font=font) for l in lines)
        box_w = int(tw + 2 * pad)
        box_h = lh * len(lines) + 2 * pad
        bx = (W - box_w) // 2
        by = margin
        frame = draw_box(frame, [bx, by, bx + box_w, by + box_h], int(28 * s))
        d = ImageDraw.Draw(frame)
        y = by + pad
        for l in lines:
            lw = d.textlength(l, font=font)
            draw_shadow_text(d, ((W - lw) / 2, y), l, font, (255, 255, 255), max(2, int(2 * s)))
            y += lh
        title_bottom = by + box_h

    # Время и порции — правый нижний угол
    info = []
    if cook_time:
        info.append("Время: " + as_text(cook_time))
    if servings:
        sv = as_text(servings)
        info.append(sv if "порц" in sv.lower() else "Порции: " + sv)
    if info:
        f = ImageFont.truetype(FONT_BOLD, int(40 * s))
        max_w = int(W * 0.4)
        lines = []
        for t in info:
            lines += fit_lines(d, t, FONT_BOLD, max_w, int(40 * s), int(26 * s), 2)[1]
        lh = int(f.size * 1.3)
        pad = int(22 * s)
        tw = max(d.textlength(l, font=f) for l in lines)
        box_w = int(tw + 2 * pad)
        box_h = lh * len(lines) + 2 * pad
        bx = W - margin - box_w
        by = H - margin - box_h
        frame = draw_box(frame, [bx, by, bx + box_w, by + box_h], int(22 * s))
        d = ImageDraw.Draw(frame)
        y = by + pad
        for l in lines:
            d.text((bx + pad, y), l, font=f, fill=(255, 214, 120))
            y += lh

    # Фото блюда — по центру между названием и нижним краем
    if card is not None:
        gap = int(30 * s)
        top = title_bottom + gap
        bottom = H - margin
        avail_h = max(int(200 * s), bottom - top)
        avail_w = int(W * 0.5)
        c = ImageOps.contain(card, (avail_w, avail_h), Image.LANCZOS)
        cx = (W - c.width) // 2
        cy = top + (avail_h - c.height) // 2
        frame.paste(c, (cx, cy))

    frame.convert("RGB").save(path, "JPEG", quality=90)


# ---------- Обычный кадр: карточка слева, подпись справа ----------
def make_card_frame(bg, card, name, amount, W, H, path):
    s = H / 900.0
    margin = int(70 * s)
    card_size = int(600 * s)
    frame = bg.copy().convert("RGBA")
    has_text = SHOW_TEXT and (name or amount)

    text_x = margin
    if card is not None:
        c = ImageOps.contain(card, (card_size, card_size), Image.LANCZOS)
        cx = margin if has_text else (W - c.width) // 2
        cy = (H - c.height) // 2
        frame.paste(c, (cx, cy))
        text_x = cx + c.width + margin

    if has_text:
        box_w = W - text_x - margin
        if box_w > int(180 * s):
            pad = int(36 * s)
            d = ImageDraw.Draw(frame)
            nf, nl = fit_lines(d, name, FONT_BOLD, box_w - 2 * pad, int(80 * s), int(36 * s), 3)
            af, al = fit_lines(d, amount, FONT_REG, box_w - 2 * pad, int(56 * s), int(28 * s), 3)
            nlh = int(nf.size * 1.25) if nf else 0
            alh = int(af.size * 1.25) if af else 0
            gap = int(24 * s) if (nl and al) else 0
            box_h = nlh * len(nl) + gap + alh * len(al) + 2 * pad
            by = (H - box_h) // 2
            frame = draw_box(frame, [text_x, by, text_x + box_w, by + box_h], int(28 * s))
            d = ImageDraw.Draw(frame)
            y = by + pad
            for line in nl:
                d.text((text_x + pad, y), line, font=nf, fill=(255, 255, 255))
                y += nlh
            y += gap
            for line in al:
                d.text((text_x + pad, y), line, font=af, fill=(255, 214, 120))
                y += alh

    frame.convert("RGB").save(path, "JPEG", quality=90)


# ---------- Эндпоинты ----------
@app.get("/health")
def health():
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None,
            "default_size": f"{DEF_W}x{DEF_H}", "fps": FPS, "layouts": ["cards", "title"]}


@app.post("/render")
def render(payload: dict = Body(...)):
    bg_b64 = (payload.get("background") or {}).get("b64")
    if not bg_b64:
        raise HTTPException(400, "background.b64 is required")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(400, "items[] is required")
    if len(items) > MAX_ITEMS:
        raise HTTPException(400, f"too many items (max {MAX_ITEMS})")

    try:
        W = even(min(max(int(payload.get("width") or DEF_W), 320), 1920))
        H = even(min(max(int(payload.get("height") or DEF_H), 240), 1080))
    except (TypeError, ValueError):
        raise HTTPException(400, "width/height must be numbers")
    layout = as_text(payload.get("layout")) or "cards"

    workdir = tempfile.mkdtemp(prefix="render_")
    out = os.path.join(workdir, "out.mp4")
    try:
        with render_lock:
            bg = ImageOps.fit(decode_image(bg_b64, (W, H)), (W, H), Image.LANCZOS)
            entries = []

            if layout == "title":
                it = items[0] or {}
                card = decode_image(it["b64"], (W, H)) if it.get("b64") else None
                p = os.path.join(workdir, "f000.jpg")
                make_title_frame(bg, card, payload.get("title") or it.get("name"),
                                 payload.get("cook_time"), payload.get("servings"), W, H, p)
                entries.append((p, parse_duration(it.get("duration") or payload.get("duration"), TITLE_DURATION)))
            else:
                for i, it in enumerate(items):
                    it = it or {}
                    card = decode_image(it["b64"], (W, H)) if it.get("b64") else None
                    p = os.path.join(workdir, f"f{i:03d}.jpg")
                    make_card_frame(bg, card, as_text(it.get("name")), as_text(it.get("amount")), W, H, p)
                    del card
                    entries.append((p, parse_duration(it.get("duration"), DEFAULT_DURATION)))
            del bg

            list_path = os.path.join(workdir, "list.txt")
            with open(list_path, "w", encoding="utf-8") as f:
                for p, d in entries:
                    f.write(f"file '{p}'\nduration {d}\n")
                f.write(f"file '{entries[-1][0]}'\n")

            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", list_path,
                "-vf", f"scale={W}:{H},fps={FPS},format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
                "-crf", CRF, "-threads", "1",
                "-movflags", "+faststart",
                out,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
            if r.returncode != 0 or not os.path.exists(out):
                raise HTTPException(500, f"ffmpeg failed (code {r.returncode}): {r.stderr[-1500:]}")
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except (ValueError, UnidentifiedImageError) as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(400, f"bad image: {e}")
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(504, "ffmpeg timeout")
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(500, f"render error: {e}")

    return FileResponse(out, media_type="video/mp4", filename="video.mp4",
                        background=BackgroundTask(shutil.rmtree, workdir, True))
