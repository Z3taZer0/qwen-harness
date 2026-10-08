"""Web tools: search, fetch, download. Purpose-built so the model doesn't have to
improvise with curl/sleep loops (which is slow and error-prone)."""
from __future__ import annotations

import html
import re
import tempfile
import urllib.parse
from pathlib import Path

import httpx

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) qh/0.1"}
MAX_DOWNLOAD = 150 * 1024 * 1024


def _client() -> httpx.Client:
    return httpx.Client(headers=UA, follow_redirects=True, timeout=30)


def web_search(query: str) -> str:
    with _client() as c:
        r = c.post("https://html.duckduckgo.com/html/", data={"q": query})
    if r.status_code != 200:
        return f"Error: search failed with HTTP {r.status_code}"
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S):
        url, title, snip = m.groups()
        if "uddg=" in url:
            url = urllib.parse.unquote(url.split("uddg=")[1].split("&")[0])
        strip = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        out.append(f"{strip(title)}\n  {url}\n  {strip(snip)[:200]}")
        if len(out) >= 8:
            break
    return "\n".join(out) or "(no results)"


def fetch_url(url: str, max_chars: int = 8000) -> str:
    """GET a URL. JSON/text returned as-is, HTML reduced to readable text."""
    with _client() as c:
        r = c.get(url)
    ct = r.headers.get("content-type", "")
    if r.status_code != 200:
        return f"Error: HTTP {r.status_code}\n{r.text[:500]}"
    if ct.startswith("image/") or "octet-stream" in ct:
        return f"Error: binary content ({ct}, {len(r.content)} bytes). Use download instead."
    text = r.text
    if "html" in ct:
        text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text))
    return text[:max_chars] + (f"\n[truncated, {len(text)} chars total]" if len(text) > max_chars else "")


def download(url: str, path: str) -> str:
    """Stream a file to `path`; reports size and image resolution."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    with _client() as c, c.stream("GET", url) as r:
        if r.status_code != 200:
            return f"Error: HTTP {r.status_code}"
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    tmp.unlink(missing_ok=True)
                    return "Error: file larger than 150MB, aborted"
                f.write(chunk)
    tmp.rename(dest)
    info = f"Saved {dest} ({total / 1e6:.1f} MB)"
    try:
        from PIL import Image

        with Image.open(dest) as im:
            info += f", image {im.size[0]}x{im.size[1]}"
    except Exception:
        pass
    return info


def fetch_to_temp(url: str) -> str:
    """Download a (small) remote image to a temp file, e.g. a thumbnail for view_image."""
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".jpg"
    p = Path(tempfile.mkdtemp(prefix="qh_")) / f"img{suffix}"
    res = download(url, str(p))
    if res.startswith("Error"):
        raise RuntimeError(res)
    return str(p)
