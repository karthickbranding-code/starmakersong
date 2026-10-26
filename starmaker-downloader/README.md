# Recording Downloader (StarMaker)

A small web tool for saving **your own** StarMaker recordings as audio/video files.
Only use it for recordings you own or have the singer's permission to save.

## What's inside

| File | Purpose |
|---|---|
| `app.py` | Flask backend: reads the share page, finds the media file, streams it as a download |
| `static/index.html` | The web page |
| `static/style.css` | Styling |
| `static/script.js` | Calls the backend and shows the download buttons |
| `blogger-widget.html` | One paste-in block for Blogger (calls your hosted backend) |
| `requirements.txt`, `Procfile` | Hosting setup |

## Why a backend is needed

Browsers can't read another site's page (CORS blocks it), and Blogger can't run Python.
So the work happens on a small server you host, and your Blogger page only shows the form.

```
Blogger page (form)  ->  your backend (Flask)  ->  StarMaker page + media file
```

## Run locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000.

## Host the backend (free options)

**Render.com** (simple):
1. Push this folder to a GitHub repo.
2. New > Web Service > connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app --workers 1 --threads 8 --timeout 120`
5. Add environment variables (below), deploy, and note your URL, e.g. `https://my-sm-tool.onrender.com`.

Railway, Fly.io and PythonAnywhere also work. Keep `--workers 1`, because the rate limiter
is in memory and download links are signed per process.

### Environment variables

| Name | Meaning |
|---|---|
| `SECRET_KEY` | **Set this** to a long random string (signs download links) |
| `ALLOWED_ORIGINS` | Your Blogger address, e.g. `https://yourblog.blogspot.com` (default `*` allows any site) |
| `ALLOWED_PAGE_HOSTS` | Domains accepted as recording links (default covers starmakerstudios.com, starmaker.co, starmaker.com, sm.mk) |
| `RATE_LIMIT_PER_MIN` | Requests per IP per minute (default 20) |

## Use it in Blogger

1. Open `blogger-widget.html`, change `API_BASE` to your backend URL.
2. In Blogger: **Layout > Add a Gadget > HTML/JavaScript**, paste the whole file, save.
   (Or paste into a post/page using **HTML view**.)

## Notes

- StarMaker can change its page layout at any time. If "No downloadable file was found"
  starts appearing, the fix is in `extract_media()` in `app.py`. `yt-dlp` is used as a fallback.
- Private recordings, DRM-protected streams and `.m3u8` playlists can't be downloaded.
- The server blocks private/internal addresses and only accepts links from the allowed domains.
