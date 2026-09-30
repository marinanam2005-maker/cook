import base64, io, math, os, re, shutil, subprocess, tempfile
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps
from starlette.background import BackgroundTask

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FONT_BOLD = os.environ.get("FONT_BOLD", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FONT_REG = os.environ.get("FONT_REGULAR", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
API_KEY = os.environ.get("API_KEY", "")
app = FastAPI(title="recipe-ffmpeg")

def font(size, bold=True):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, max(8, int(size)))

def decode_img(b64):
    return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")

def wrap(draw, text, fnt, max_w):
    words = str(text or "").split(); lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if not cur or draw.textlength(t, font=fnt) <= max_w: cur = t
        else: lines.append(cur); cur = w
    if cur: lines.append(cur)
    return lines

def fit_text(text, max_w, max_h, start, min_size, bold):
    d = ImageDraw.Draw(Image.new("RGBA", (4, 4))); size = int(start)
    while size >= min_size:
        f = font(size, bold); lines = wrap(d, text, f, max_w); lh = int(size * 1.18)
        if len(lines) * lh <= max_h and all(d.textlength(l, font=f) <= max_w for l in lines):
            return f, lines, lh
        size -= 2
    f = font(min_size, bold); lines = wrap(d, text, f, max_w); lh = int(min_size * 1.18)
    maxn = max(1, int(max_h // lh))
    if len(lines) > maxn:
        lines = lines[:maxn]; lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    return f, lines, lh

def draw_text(img, text, box, start, color, bold=True, min_size=14, valign="center"):
    if not text: return
    x, y, w, h = box
    f, lines, lh = fit_text(text, w, h, start, min_size, bold)
    d = ImageDraw.Draw(img); total = len(lines) * lh
    cy = y + (h - total) / 2 if valign == "center" else y
    stroke = max(1, f.size // 16)
    for line in lines:
        tw = d.textlength(line, font=f)
        d.text((x + (w - tw) / 2, cy), line, font=f, fill=color, stroke_width=stroke, stroke_fill=(0, 0, 0, 190))
        cy += lh

def fit_image(im, w, h, cover=False, radius=0):
    w, h = max(1, int(w)), max(1, int(h))
    im = ImageOps.fit(im, (w, h), Image.LANCZOS) if cover else ImageOps.contain(im, (w, h), Image.LANCZOS)
    if radius:
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.size[0] - 1, im.size[1] - 1], int(radius), fill=255)
        im.putalpha(ImageChops.multiply(mask, im.getchannel("A")))
    return im

def split_grid(im, cols, rows):
    W, H = im.size
    return [im.crop((c * W // cols, r * H // rows, (c + 1) * W // cols, (r + 1) * H // rows))
            for r in range(rows) for c in range(cols)]

def probe(path):
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    return dur, ("Audio:" in p.stderr)

def build(job, tmp):
    W = int(job.get("width", 1600)); H = int(job.get("height", 900)); fps = int(job.get("fps", 30))
    s = W / 1600.0; white = (255, 255, 255, 255)
    bg = job.get("background") or {}
    if not bg.get("b64"): raise ValueError("background.b64 is required")
    ext = (bg.get("ext") or "png").lower().strip(".")
    bg_path = os.path.join(tmp, "bg." + ext)
    with open(bg_path, "wb") as fh: fh.write(base64.b64decode(bg["b64"]))
    is_video = ext in ("mp4", "mov", "webm", "mkv")
    bg_dur, bg_audio = probe(bg_path) if is_video else (None, False)
    dur = float(job.get("duration") or bg_dur or 20)
    overlays = []; mode = job.get("mode", "cards")

    header_h = int((230 if mode == "title" else 170) * s)
    head = Image.new("RGBA", (W, header_h), (0, 0, 0, 0))
    draw_text(head, job.get("title", ""), (int(60*s), int(25*s), W - int(120*s), header_h - int(40*s)),
              (86 if mode == "title" else 72) * s, white, True, int(28*s))
    hp = os.path.join(tmp, "head.png"); head.save(hp); overlays.append((hp, 0, 0, 0.2))

    dish_path = None
    if mode == "title":
        dish = job.get("dish") or {}
        if not dish.get("b64"): raise ValueError("dish.b64 is required for title mode")
        zf = float(job.get("zoom_from", 1.25))
        box_w, box_h = W - int(200*s), H - header_h - int(40*s)
        im = fit_image(decode_img(dish["b64"]), box_w*zf, box_h*zf, radius=int(28*s*zf))
        dish_path = os.path.join(tmp, "dish.png"); im.save(dish_path)
        dish_cy = header_h + box_h / 2 + int(10*s)
    else:
        items = job.get("items") or []
        if not items: raise ValueError("items are required for cards mode")
        frames = []; col = job.get("collage")
        if col and col.get("b64"):
            n = len(items); cols = int(col.get("cols") or math.ceil(math.sqrt(n)))
            rows = int(col.get("rows") or math.ceil(n / cols))
            frames = split_grid(decode_img(col["b64"]), cols, rows)
        layout = job.get("layout", "ingredients"); per_row = int(job.get("per_row", 5))
        n = len(items); rows = math.ceil(n / per_row)
        margin, gap = int(40*s), int(18*s); top = header_h; area_h = H - top - int(30*s)
        cols_max = min(per_row, n)
        cell_w = (W - 2*margin - (cols_max-1)*gap) / cols_max
        cell_h = (area_h - (rows-1)*gap) / rows
        img_ratio = 0.66 if layout == "ingredients" else 0.50
        step = min(1.0, max(0.25, (dur * 0.45) / n))
        for i, it in enumerate(items):
            r, c = divmod(i, per_row); in_row = min(per_row, n - r*per_row)
            row_w = in_row*cell_w + (in_row-1)*gap
            x0 = (W - row_w)/2 + c*(cell_w+gap); y0 = top + r*(cell_h+gap)
            cw, ch = int(cell_w), int(cell_h)
            card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0)); ih = int(ch * img_ratio)
            src = None
            if it.get("image_b64"): src = decode_img(it["image_b64"])
            elif it.get("collage_index") is not None and int(it["collage_index"]) < len(frames):
                src = frames[int(it["collage_index"])]
            if src is not None:
                pic = fit_image(src, cw - int(8*s), ih - int(8*s), cover=(layout != "ingredients"), radius=int(18*s))
                card.alpha_composite(pic, ((cw - pic.size[0])//2, (ih - pic.size[1])//2))
            rest = ch - ih
            if layout == "ingredients":
                draw_text(card, it.get("name", ""), (0, ih, cw, int(rest*0.55)), 34*s, white, True, int(14*s))
                draw_text(card, it.get("sub", ""), (0, ih + int(rest*0.55), cw, int(rest*0.45)), 28*s, white, False, int(12*s))
            else:
                draw_text(card, it.get("name", ""), (0, ih + int(4*s), cw, int(rest*0.28)), 30*s, white, True, int(14*s))
                draw_text(card, it.get("sub", ""), (int(4*s), ih + int(rest*0.30), cw - int(8*s), int(rest*0.68)),
                          24*s, white, False, int(12*s), valign="top")
            p = os.path.join(tmp, f"item{i}.png"); card.save(p)
            overlays.append((p, int(x0), int(y0), round(0.6 + i*step, 3)))

    out = os.path.join(tmp, "out.mp4")
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    cmd += ["-stream_loop", "-1", "-i", bg_path] if is_video else ["-loop", "1", "-framerate", str(fps), "-i", bg_path]
    for p, *_ in overlays: cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", p]
    if dish_path: cmd += ["-loop", "1", "-framerate", str(fps), "-i", dish_path]
    f = [f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={fps},format=yuv420p[b0]"]
    last = "b0"
    if dish_path:
        di = len(overlays) + 1; zf = float(job.get("zoom_from", 1.25))
        f.append(f"[{di}:v]format=rgba,scale=w='trunc(iw*(1-(1-1/{zf})*min(t/{dur},1))/2)*2':h=-2:eval=frame,"
                 f"fade=t=in:st=0:d=0.6:alpha=1[dish]")
        f.append(f"[{last}][dish]overlay=x='(W-w)/2':y='{dish_cy}-h/2':eval=frame[bd]"); last = "bd"
    for idx, (p, x, y, st) in enumerate(overlays, start=1):
        f.append(f"[{idx}:v]format=yuva420p,fade=t=in:st={st}:d=0.5:alpha=1[o{idx}]")
        f.append(f"[{last}][o{idx}]overlay={x}:{y}:format=yuv420:shortest=0[v{idx}]"); last = f"v{idx}"
    f.append(f"[{last}]format=yuv420p[vout]")
    cmd += ["-filter_complex_threads", "1", "-filter_complex", ";".join(f), "-map", "[vout]"]
    if bg_audio: cmd += ["-map", "0:a", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-t", f"{dur:.3f}", "-r", str(fps), "-c:v", "libx264", "-preset", job.get("preset", "veryfast"),
            "-crf", str(job.get("crf", 20)), "-threads", os.environ.get("FFMPEG_THREADS", "0"),
            "-movflags", "+faststart", out]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0: raise RuntimeError(f"ffmpeg failed (code {p.returncode}): " + p.stderr[-2000:])
    return out

@app.get("/health")
def health(): return {"ok": True}

@app.post("/render")
async def render(req: Request):
    if API_KEY and req.headers.get("x-api-key") != API_KEY: raise HTTPException(401, "bad api key")
    try: job = await req.json()
    except Exception: raise HTTPException(400, "body must be JSON")
    tmp = tempfile.mkdtemp()
    try: out = await run_in_threadpool(build, job, tmp)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True); raise HTTPException(400, str(e))
    return FileResponse(out, media_type="video/mp4", filename=job.get("output_name", "video.mp4"),
                        background=BackgroundTask(shutil.rmtree, tmp, True))
