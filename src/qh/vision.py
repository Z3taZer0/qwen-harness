"""Image preparation. Keeps visual-token cost bounded and inside the server's
--mm-processor-kwargs window (min 65536 px, max 2097152 px)."""
from __future__ import annotations

import base64
import io
import math

from PIL import Image, ImageOps

from .config import Config


def load_image_part(path: str, cfg: Config, max_pixels: int | None = None) -> tuple[dict, str]:
    img = ImageOps.exif_transpose(Image.open(path))
    w, h = img.size
    px = w * h
    scale = 1.0
    limit = max_pixels or cfg.image_max_pixels
    if px > limit:
        scale = math.sqrt(limit / px)
    elif px < cfg.image_min_pixels:
        scale = math.sqrt(cfg.image_min_pixels / px)
    if scale != 1.0:
        nw = max(28, round(w * scale / 28) * 28)
        nh = max(28, round(h * scale / 28) * 28)
        # BILINEAR is ~10x faster than LANCZOS on multi-megapixel wallpapers
        img = img.resize((nw, nh), Image.BILINEAR)

    # Always encode as compact JPEG for model vision evaluation:
    # Avoids PNG optimize=True which caused 6s freezes on large RGBA images
    buf = io.BytesIO()
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(buf, "JPEG", quality=82)
    mime = "image/jpeg"
    url = f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()
    note = f"{path} ({w}x{h} -> {img.size[0]}x{img.size[1]})"
    return {"type": "image_url", "image_url": {"url": url}}, note


def count_images(messages: list[dict]) -> int:
    return sum(
        1
        for m in messages
        if isinstance(m.get("content"), list)
        for p in m["content"]
        if p.get("type") == "image_url"
    )


def drop_old_images(messages: list[dict], keep: int) -> bool:
    """Replace all but the newest `keep` images with a text placeholder."""
    slots = [
        (i, j)
        for i, m in enumerate(messages)
        if isinstance(m.get("content"), list)
        for j, p in enumerate(m["content"])
        if p.get("type") == "image_url"
    ]
    changed = False
    for i, j in slots[: max(0, len(slots) - keep)]:
        parts = messages[i]["content"]
        prev = parts[j - 1].get("text", "") if j > 0 else ""
        name = prev[len("[image: "):-1] if prev.startswith("[image: ") else ""
        parts[j] = {"type": "text", "text": f"(image {name} removed to save context; view_image it again if needed)"
                    if name else "[older image removed to save context]"}
        changed = True
    return changed


def image_in_context(messages: list[dict], label: str) -> bool:
    """True if the image labeled `label` is still present (not evicted) in the history."""
    tag = f"[image: {label}]"
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for a, b in zip(c, c[1:]):
                if a.get("text") == tag and b.get("type") == "image_url":
                    return True
    return False


def contact_sheet(paths: list[str], out: str, cols: int = 0, tile_w: int = 480) -> list[str]:
    """Grid of numbered thumbnails (one image instead of N). Returns the labels drawn."""
    from PIL import ImageDraw, ImageFont

    cols = cols or (2 if len(paths) <= 4 else 3)
    tile_h = tile_w * 9 // 16
    rows = math.ceil(len(paths) / cols)
    pad, bar = 6, 26
    sheet = Image.new("RGB", (cols * (tile_w + pad) + pad, rows * (tile_h + bar + pad) + pad), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    labels = []
    for i, p in enumerate(paths):
        x = pad + (i % cols) * (tile_w + pad)
        y = pad + (i // cols) * (tile_h + bar + pad)
        try:
            im = ImageOps.exif_transpose(Image.open(p)).convert("RGB")
            dims = f"{im.size[0]}x{im.size[1]}"
            im = ImageOps.contain(im, (tile_w, tile_h), Image.BILINEAR)
            sheet.paste(im, (x + (tile_w - im.size[0]) // 2, y + bar + (tile_h - im.size[1]) // 2))
        except Exception as e:
            dims = f"unreadable: {type(e).__name__}"
        name = p.rsplit("/", 1)[-1]
        label = f"{i + 1}. {name} ({dims})"
        draw.text((x + 4, y + 3), label if len(label) < 48 else label[:46] + "…", fill=(235, 235, 235), font=font)
        labels.append(label)
    sheet.save(out, "JPEG", quality=85)
    return labels