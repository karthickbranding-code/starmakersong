"""Recording downloader - Flask backend.

Intended for saving YOUR OWN StarMaker recordings (or ones the singer has
given you permission to save). It:
  1. takes a public recording/share link,
  2. reads the page and finds the audio/video file URL,
  3. streams that file back to the browser as a download.

Run locally:   python app.py
Production:    gunicorn app:app --workers 1 --threads 8 --timeout 120
"""
import ipaddress
import os
import re
import socket
import time
from collections import defaultdict, deque
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from itsdangerous import BadSignature, URLSafeTimedSerializer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")

# ---------------------------------------------------------------- settings
SECRET_KEY = os.environ.get("SECRET_KEY") or os.urandom(32).hex()
# Only links from these domains (and their subdomains) are accepted.
ALLOWED_PAGE_HOSTS = [
    h.strip().lower()
    for h in os.environ.get(
        "ALLOWED_PAGE_HOSTS", "starmakerstudios.com,starmaker.co,starmaker.com,sm.mk"
    ).split(",")
    if h.strip()
]
# CORS: "*" or a comma-separated list, e.g. "https://yourblog.blogspot.com"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "20"))
# Testing only: lets the server fetch from localhost. Keep OFF in production.
ALLOW_LOCAL = os.environ.get("ALLOW_LOCAL_FOR_TESTING") == "1"

MAX_PAGE_BYTES = 3 * 1024 * 1024
TOKEN_MAX_AGE = 3600  # download links stay valid for 1 hour
UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
MEDIA_EXTS = {"mp3": "audio", "m4a": "audio", "aac": "audio", "mp4": "video"}
MEDIA_RE = re.compile(
    r"https?://[^\s\"'<>\\)]+?\.(?:mp3|m4a|aac|mp4)(?:\?[^\s\"'<>\\)]*)?", re.I
)
EXT_BY_CTYPE = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "video/mp4": "mp4",
}

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="recording-download")
session = requests.Session()


# ----------------------------------------------------------------- helpers
def error(message, status=400):
    return jsonify({"error": message}), status


def host_allowed(host):
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_PAGE_HOSTS)


def is_public_host(host):
    """Block localhost / private networks so the server can't be used to probe them."""
    if ALLOW_LOCAL:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return True


def safe_get(url, *, stream=False, headers=None, timeout=15, max_redirects=5):
    """GET that validates every hop (including redirects) before connecting."""
    for _ in range(max_redirects + 1):
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Invalid URL")
        if not is_public_host(parts.hostname):
            raise ValueError("Blocked address")
        resp = session.get(
            url, stream=stream, headers=headers, timeout=timeout, allow_redirects=False
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                raise ValueError("Bad redirect")
            url = urljoin(url, location)
            continue
        return resp
    raise ValueError("Too many redirects")


_hits = defaultdict(deque)


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.remote_addr or "unknown"


def rate_limited(ip):
    now = time.time()
    if len(_hits) > 10000:
        _hits.clear()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        return True
    q.append(now)
    return False


def clean_title(title):
    title = re.sub(r"\s+", " ", title or "").strip()
    return title[:120] or "recording"


# -------------------------------------------------------------- extraction
def extract_media(html, base_url):
    """Return (title, thumbnail, [(url, kind)]) found in a recording page."""
    html = (
        html.replace("\\/", "/")
        .replace("\\u002F", "/")
        .replace("\\u0026", "&")
        .replace("&amp;", "&")
    )
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names):
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find(
                "meta", attrs={"name": name}
            )
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    title = meta("og:title", "twitter:title")
    if not title and soup.title and soup.title.string:
        title = soup.title.string
    thumbnail = meta("og:image", "twitter:image")

    raw = []  # (url, kind-hint or None)
    for name in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        value = meta(name)
        if value:
            raw.append((value, "video"))
    for name in ("og:audio", "og:audio:url", "og:audio:secure_url"):
        value = meta(name)
        if value:
            raw.append((value, "audio"))
    for tag in soup.find_all(["audio", "video", "source"]):
        if tag.get("src"):
            hint = "video" if tag.name == "video" else "audio" if tag.name == "audio" else None
            raw.append((tag["src"], hint))
    for match in MEDIA_RE.findall(html):
        raw.append((match, None))

    found, seen = [], set()
    for value, hint in raw:
        url = urljoin(base_url, value)
        if urlparse(url).scheme not in ("http", "https") or url in seen:
            continue
        ext = os.path.splitext(urlparse(url).path)[1].lower().lstrip(".")
        if ext == "m3u8":
            continue  # streaming playlists can't be saved as one file
        kind = MEDIA_EXTS.get(ext) or hint or "audio"
        seen.add(url)
        found.append((url, kind))
    return clean_title(title), thumbnail, found


def ytdlp_fallback(url):
    """Optional fallback if yt-dlp is installed and understands the page."""
    try:
        import yt_dlp
    except ImportError:
        return None
    try:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return None
    if not info:
        return None
    found = []
    for fmt in sorted(info.get("formats") or [], key=lambda f: f.get("tbr") or 0, reverse=True):
        if fmt.get("protocol") not in ("http", "https") or not fmt.get("url"):
            continue
        if fmt.get("acodec") in (None, "none"):
            continue
        kind = "audio" if fmt.get("vcodec") in (None, "none") else "video"
        found.append((fmt["url"], kind))
    if not found and info.get("url") and info.get("protocol") in ("http", "https"):
        found.append((info["url"], "video" if info.get("vcodec") not in (None, "none") else "audio"))
    if not found:
        return None
    return clean_title(info.get("title")), info.get("thumbnail"), found[:4]


# ------------------------------------------------------------------ routes
@app.after_request
def add_cors(resp):
    origin = request.headers.get("Origin")
    if "*" in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/resolve")
def resolve():
    if rate_limited(client_ip()):
        return error("Too many requests. Please wait a minute and try again.", 429)

    data = request.get_json(silent=True) or {}
    if not data.get("confirm"):
        return error("Please confirm you own this recording or have permission to save it.")

    page_url = (data.get("url") or "").strip()
    parts = urlparse(page_url)
    if len(page_url) > 600 or parts.scheme not in ("http", "https") or not parts.hostname:
        return error("Please paste a full recording link starting with https://")
    if not host_allowed(parts.hostname):
        return error("That link isn't from a supported StarMaker domain.")

    try:
        resp = safe_get(
            page_url,
            stream=True,
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"},
        )
    except (ValueError, requests.RequestException):
        return error("Couldn't open that link. Check it and try again.", 502)

    try:
        if resp.status_code != 200:
            return error("The recording page didn't load (it may be private or removed).", 502)
        chunks, size = [], 0
        for chunk in resp.iter_content(65536):
            size += len(chunk)
            chunks.append(chunk)
            if size > MAX_PAGE_BYTES:
                break
        html = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
        final_url = resp.url
    finally:
        resp.close()

    title, thumbnail, found = extract_media(html, final_url)
    if not found:
        fallback = ytdlp_fallback(page_url)
        if fallback:
            title, thumbnail, found = fallback
    if not found:
        return error(
            "No downloadable file was found on that page. Make sure it's a public recording link.",
            404,
        )

    formats, counts = [], defaultdict(int)
    for media_url, kind in found[:6]:
        ext = os.path.splitext(urlparse(media_url).path)[1].lower().lstrip(".")
        ext = ext if ext in MEDIA_EXTS else ""
        label = ("Audio" if kind == "audio" else "Video") + (f" ({ext.upper()})" if ext else "")
        counts[label] += 1
        if counts[label] > 1:
            label += f" #{counts[label]}"
        token = serializer.dumps({"u": media_url, "e": ext, "n": title, "r": final_url})
        formats.append(
            {"kind": kind, "label": label, "download_url": "/api/download?t=" + quote(token)}
        )

    if thumbnail and not thumbnail.startswith("https://"):
        thumbnail = None
    return jsonify({"title": title, "thumbnail": thumbnail, "formats": formats})


@app.get("/api/download")
def download():
    try:
        payload = serializer.loads(request.args.get("t", ""), max_age=TOKEN_MAX_AGE)
    except BadSignature:
        return error("This download link has expired. Please fetch the recording again.", 410)

    try:
        upstream = safe_get(
            payload["u"],
            stream=True,
            headers={"User-Agent": UA, "Referer": payload.get("r", "")},
            timeout=20,
        )
    except (ValueError, requests.RequestException):
        return error("Couldn't reach the file.", 502)

    if upstream.status_code != 200:
        upstream.close()
        return error("The file couldn't be downloaded right now.", 502)

    ctype = upstream.headers.get("Content-Type", "").split(";")[0].strip().lower()
    allowed = ctype.startswith(("audio/", "video/")) or ctype in (
        "application/octet-stream",
        "binary/octet-stream",
    )
    if not allowed:
        upstream.close()
        return error("That link doesn't point to an audio or video file.", 415)

    ext = payload.get("e") or EXT_BY_CTYPE.get(ctype) or ("mp4" if ctype.startswith("video/") else "mp3")
    base = re.sub(r'[\\/:*?"<>|\r\n]+', "", payload.get("n") or "recording").strip() or "recording"
    ascii_name = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 _-]+", "", base)).strip() or "recording"

    def generate():
        try:
            for chunk in upstream.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    headers = {
        "Content-Type": ctype if ctype.startswith(("audio/", "video/")) else "application/octet-stream",
        "Content-Disposition": (
            f'attachment; filename="{ascii_name}.{ext}"; '
            f"filename*=UTF-8''{quote(base)}.{ext}"
        ),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    length = upstream.headers.get("Content-Length")
    if length and not upstream.headers.get("Content-Encoding"):
        headers["Content-Length"] = length
    return Response(stream_with_context(generate()), headers=headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
