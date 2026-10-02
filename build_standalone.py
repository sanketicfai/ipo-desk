#!/usr/bin/env python3
"""Build the offline single-file dashboard.

Produces ipo-desk-offline.html: the exact dashboard, with every payload (the IPO list, the
comparison rows, the market strip and all 428 detail pages) packed into one gzip+base64 block
inside the file. Open it by double-click / share it on WhatsApp - no server, no internet.

    python3 build_standalone.py            # -> ipo-desk-offline.html
"""
import base64
import glob
import gzip
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")
OUT = os.path.join(HERE, "ipo-desk-offline.html")


def payload():
    """Everything the page would otherwise fetch, keyed by the path it asks for."""
    files = {"data/ipos.json", "data/market.json", "data/version.json",
             "data/day1.json", "data/subhist.json", "data/compare.json"}
    body = {}
    for rel in sorted(files):
        p = os.path.join(SITE, rel)
        if os.path.exists(p):
            body[rel] = json.load(open(p, encoding="utf-8"))
    for f in sorted(glob.glob(os.path.join(SITE, "data", "ipo", "*.json"))):
        body["data/ipo/" + os.path.basename(f)] = json.load(open(f, encoding="utf-8"))
    return body


MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}


def inline_logos(body):
    """A single file must carry its own images: swap every logo/xxx path for a data URI."""
    import re
    cache = {}

    def uri(rel):
        if rel in cache:
            return cache[rel]
        path = os.path.join(SITE, rel)
        try:
            with open(path, "rb") as f:
                blob = f.read()
            ext = os.path.splitext(rel)[1].lower()
            val = "data:%s;base64,%s" % (MIME.get(ext, "image/png"), base64.b64encode(blob).decode("ascii"))
        except Exception:
            val = ""
        cache[rel] = val
        return val

    pat = re.compile(r"logo/[A-Za-z0-9_.-]+\.(?:png|jpe?g|webp|gif|svg)", re.I)
    missing = 0
    seen = set()

    def walk(o):
        nonlocal missing
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        if isinstance(o, str):
            if "logo/" in o:
                def sub(m):
                    nonlocal missing
                    u = uri(m.group(0))
                    if not u:
                        missing += 1
                    seen.add(m.group(0))
                    return u
                return pat.sub(sub, o)
            return o
        return o

    body = walk(body)
    print(f"linked inline images: {len(seen)} (missing {missing})")
    return body


def main():
    html = open(os.path.join(SITE, "index.html"), encoding="utf-8").read()
    body = inline_logos(payload())
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False, default=str).encode()
    b64 = base64.b64encode(gzip.compress(raw, 9)).decode("ascii")

    boot = """<script id="offlinePayload" type="application/octet-stream">%s</script>
<script>
/* Offline build: the data below is the live snapshot, packed and compressed into this file. */
window.__STANDALONE = true;
window.__INLINE = null;
window.__INLINE_READY = (async () => {
  try {
    const b64 = document.getElementById('offlinePayload').textContent.trim();
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    let txt;
    if (typeof DecompressionStream === 'function') {
      const ds = new DecompressionStream('gzip');
      const stream = new Blob([bytes]).stream().pipeThrough(ds);
      txt = await new Response(stream).text();
    } else {                                    /* very old browser: ask for nothing, stay empty */
      document.documentElement.setAttribute('data-offline-unsupported', '1');
      window.__INLINE = {};
      return window.__INLINE;
    }
    window.__INLINE = JSON.parse(txt);
  } catch (e) {
    window.__INLINE = {};
    console.warn('offline payload failed', e);
  }
  return window.__INLINE;
})();
</script>
""" % b64

    # 1 - the packed data block goes in first so nothing else can run before it exists
    html = html.replace("<script>", boot + "<script>", 1)

    # 2 - every data read consults the packed copy before touching the network
    old_j = ("async function j(url, opt){ const r = await fetch(url, Object.assign({cache:'no-store'}, opt || {}));"
             " if(!r.ok) throw new Error(r.status); return r.json(); }")
    new_j = ("async function j(url, opt){\n"
             "  if (window.__STANDALONE){\n"
             "    await window.__INLINE_READY;\n"
             "    const k = String(url).split('?')[0];\n"
             "    if (window.__INLINE && window.__INLINE[k] !== undefined) return window.__INLINE[k];\n"
             "    throw new Error('not in offline copy');\n"
             "  }\n"
             "  const r = await fetch(url, Object.assign({cache:'no-store'}, opt || {}));"
             " if(!r.ok) throw new Error(r.status); return r.json(); }")
    assert old_j in html
    html = html.replace(old_j, new_j, 1)

    # 3 - offline mode: no API probe, no CSV/refresh buttons, honest wording
    old_mode = "async function detectMode(){\n  if (MODE_READY) return MODE_READY;"
    new_mode = ("async function detectMode(){\n"
                "  if (window.__STANDALONE){\n"
                "    MODE_READY = Promise.resolve(true); STATIC = true;\n"
                "    try{ const c = document.getElementById('btnCsv'); if (c) c.style.display = 'none';\n"
                "         const b = document.getElementById('btnRefresh'); if (b) b.style.display = 'none'; }catch(e){}\n"
                "    return MODE_READY;\n"
                "  }\n"
                "  if (MODE_READY) return MODE_READY;")
    assert old_mode in html
    html = html.replace(old_mode, new_mode, 1)
    html = html.replace("cloud snapshot", "offline copy")

    # 3b - the day-1 spreadsheet travels inside the file too, so the download button still works
    for name, mime in (("day1.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                       ("day1.csv", "text/csv")):
        path = os.path.join(SITE, name)
        if not os.path.exists(path):
            print(f"  ! {name} missing from the site - download button will not work offline")
            continue
        blob = base64.b64encode(open(path, "rb").read()).decode("ascii")
        html = html.replace(f'href="{name}"', f'href="data:{mime};base64,{blob}"', 1)
        print(f"  inlined {name}: {len(blob)/1024:.0f} KB base64")

    # 4 - a footer line that says what this file is
    html = html.replace(
        '<div class="note legal">',
        '<div class="note legal" style="border-style:dashed">'
        '<b>Offline copy.</b> This single file carries its own data snapshot (packed inside it) - '
        'no server and no internet are needed. Figures are frozen at the generation time shown in the '
        'header. For the live version open the dashboard on GitHub Pages.<br><br>', 1)

    open(OUT, "w", encoding="utf-8").write(html)
    print(f"{OUT}  {os.path.getsize(OUT)/1e6:.2f} MB  ({len(body)} payloads, {len(raw)/1e6:.2f} MB raw)")


if __name__ == "__main__":
    main()
