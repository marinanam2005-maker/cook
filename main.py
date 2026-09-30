import base64, io, os, subprocess, tempfile
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from PIL import Image, ImageDraw, ImageFont

app = FastAPI()
FONT_BOLD = os.environ.get("FONT_BOLD", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FONT_REG = os.environ.get("FONT_REG", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

def _img(b64):
    if "," in b64[:100]:
        b64 = b64.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(b64)))

def _clean(v):
    s = str(v or "").strip()
    return "" if s.lower() in ("", "не указано", "none", "null") else s

def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines

def build_overlay(p, W, H):
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bg_b64 = (p.get("background") or {}).get("b64")
    if bg_b64:
        layer.alpha_composite(_img(bg_b64).convert("RGBA").resize((W, H), Image.LANCZOS))
    d = ImageDraw.Draw(layer)
    color = p.get("color") or "#000000"
    tsize = int(p.get("title_size") or 48)
    msize = int(p.get("meta_size") or 34)
    max_w = int(p.get("max_title_width") or W - 300)
    ft, fm = ImageFont.truetype(FONT_BOLD, tsize), ImageFont.truetype(FONT_REG, msize)
    y = int(p.get("title_y") if p.get("title_y") is not None else 90)
    for line in _wrap(d, _clean(p.get("title")), ft, max_w)[:3]:
        d.text(((W - d.textlength(line, font=ft)) / 2, y), line, font=ft, fill=color)
        y += int(tsize * 1.2)
    meta = "   •   ".join(x for x in (_clean(p.get("cook_time")), _clean(p.get("servings"))) if x)
    if meta:
        my = int(p["meta_y"]) if p.get("meta_y") is not None else y + int(msize * 0.5)
        d.text(((W - d.textlength(meta, font=fm)) / 2, my), meta, font=fm, fill=color)
    return layer

def render(p):
    W, H = int(p.get("width") or 1600), int(p.get("height") or 900)
    fps, dur = int(p.get("fps") or 25), float(p.get("duration") or 8)
    z0, z1 = float(p.get("zoom_from") or 1.25), float(p.get("zoom_to") or 1.0)
    img_b64 = (p.get("image") or {}).get("b64") or ((p.get("items") or [{}])[0].get("b64"))
    if not img_b64:
        raise HTTPException(400, "image.b64 is required")
    SS = 2
    src = _img(img_b64).convert("RGB")
    k = max(W * SS / src.width, H * SS / src.height)
    src = src.resize((round(src.width * k), round(src.height * k)), Image.LANCZOS)
    l, t = (src.width - W * SS) // 2, (src.height - H * SS) // 2
    src = src.crop((l, t, l + W * SS, t + H * SS))
    frames = max(1, int(round(dur * fps)))
    with tempfile.TemporaryDirectory() as tmp:
        dish_p, over_p, out_p = (os.path.join(tmp, n) for n in ("dish.png", "over.png", "out.mp4"))
        src.save(dish_p)
        build_overlay(p, W, H).save(over_p)
        zoom = f"{z0}+({z1}-{z0})*on/{max(frames - 1, 1)}"
        vf = (f"[0:v]zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
              f":d={frames}:s={W}x{H}:fps={fps},setsar=1[bg];"
              f"[1:v]format=rgba[ov];[bg][ov]overlay=0:0:format=auto,format=yuv420p[v]")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", dish_p,
               "-loop", "1", "-framerate", str(fps), "-i", over_p,
               "-filter_complex", vf, "-map", "[v]", "-frames:v", str(frames),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_p]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise HTTPException(500, "ffmpeg: " + r.stderr[-800:])
        return open(out_p, "rb").read()

@app.get("/health")
def health():
    return {"ok": True, "endpoint": "/render-title"}

@app.post("/render-title")
def render_title(payload: dict):
    return Response(render(payload), media_type="video/mp4",
                    headers={"Content-Disposition": 'attachment; filename="title.mp4"'})
