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
W = int(os.getenv("VIDEO_W", "1280"))
H = int(os.getenv("VIDEO_H", "720"))
FPS = int(os.getenv("VIDEO_FPS", "25"))
CRF = os.getenv("VIDEO_CRF", "23")                 # качество: меньше = лучше, больше файл
DEFAULT_DURATION = float(os.getenv("DEFAULT_DURATION", "3"))
MAX_DURATION = float(os.getenv("MAX_DURATION", "30"))
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "30"))
CARD_SIZE = int(os.getenv("CARD_SIZE", "480"))
SHOW_TEXT = os.getenv("SHOW_TEXT", "1") == "1"     # подписи name/amount на кадре
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT", "240"))
MARGIN = 60

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
        img.draft("RGB", target)  # быстрое уменьшение JPEG при чтении
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        white = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(white, img)
    return img.convert("RGB")


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
    return font, wrap(draw, text, font, max_w)[:max_lines]


def as_text(v):
    return "" if v is None else str(v).strip()


def make_frame(bg, card, name, amount, path):
    frame = bg.copy().convert("RGBA")
    has_text = SHOW_TEXT and (name or amount)

    text_x = MARGIN
    if card is not None:
        c = ImageOps.contain(card, (CARD_SIZE, CARD_SIZE), Image.LANCZOS)
        cx = MARGIN if has_text else (W - c.width) // 2
        cy = (H - c.height) // 2
        frame.paste(c, (cx, cy))
        text_x = cx + c.width + MARGIN

    if has_text:
        box_w = W - text_x - MARGIN
        if box_w > 150:
            pad = 30
            d = ImageDraw.Draw(frame)
            nf, nl = fit_lines(d, name, FONT_BOLD, box_w - 2 * pad, 64, 30, 3)
            af, al = fit_lines(d, amount, FONT_REG, box_w - 2 * pad, 46, 24, 2)
            nlh = int(nf.size * 1.25) if nf else 0
            alh = int(af.size * 1.25) if af else 0
            gap = 20 if (nl and al) else 0
            box_h = nlh * len(nl) + gap + alh * len(al) + 2 * pad
            by = (H - box_h) // 2

            overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
            ImageDraw.Draw(overlay).rounded_rectangle(
                [text_x, by, text_x + box_w, by + box_h], radius=24, fill=(0, 0, 0, 150)
            )
            frame = Image.alpha_composite(frame, overlay)
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


def parse_duration(v):
    try:
        d = float(v)
    except (TypeError, ValueError):
        return DEFAULT_DURATION
    if d <= 0:
        return DEFAULT_DURATION
    return min(d, MAX_DURATION)


# ---------- Эндпоинты ----------
@app.get("/health")
def health():
    return {"ok": True, "ffmpeg": shutil.which("ffmpeg") is not None, "size": f"{W}x{H}", "fps": FPS}


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

    workdir = tempfile.mkdtemp(prefix="render_")
    out = os.path.join(workdir, "out.mp4")
    try:
        with render_lock:
            bg = ImageOps.fit(decode_image(bg_b64, (W, H)), (W, H), Image.LANCZOS)

            entries = []
            for i, it in enumerate(items):
                it = it or {}
                card = decode_image(it["b64"], (CARD_SIZE, CARD_SIZE)) if it.get("b64") else None
                p = os.path.join(workdir, f"f{i:03d}.jpg")
                make_frame(bg, card, as_text(it.get("name")), as_text(it.get("amount")), p)
                del card
                entries.append((p, parse_duration(it.get("duration"))))
            del bg

            list_path = os.path.join(workdir, "list.txt")
            with open(list_path, "w", encoding="utf-8") as f:
                for p, d in entries:
                    f.write(f"file '{p}'\nduration {d}\n")
                f.write(f"file '{entries[-1][0]}'\n")  # особенность concat: повтор последнего кадра

            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", list_path,
                "-vf", f"fps={FPS},format=yuv420p",
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

    return FileResponse(
        out,
        media_type="video/mp4",
        filename="video.mp4",
        background=BackgroundTask(shutil.rmtree, workdir, True),
    )
