#!/usr/bin/env python3
"""
WP Cache Warmer — single self-contained file (engine + web UI, no imports
between them, so nothing can collide).

    pip install flask
    python warmer.py                       # web UI at http://127.0.0.1:5000
    python warmer.py --host 0.0.0.0 --port 8080
    python warmer.py cli https://example.com --verify --insecure   # CLI mode

Warms W3 Total Cache, WP Super Cache, WP Rocket, LiteSpeed and Cloudflare by
requesting every page of a site (from a sitemap, auto-discovered or supplied,
or by crawling internal links). Engine is pure stdlib; only the web UI needs
Flask.
"""

import argparse
import concurrent.futures as cf
import gzip
import json
import math
import queue
import re
import ssl
import statistics
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zlib
from argparse import Namespace
from collections import Counter, deque, namedtuple

__author__ = "BeforeMyCompileFails"
GITHUB_URL = "https://github.com/BeforeMyCompileFails"
REPO_URL = "https://github.com/BeforeMyCompileFails/WP-Cache-Warmer"
BMC_URL = "https://www.buymeacoffee.com/beforemycompilefails"
BMC_BUTTON = ("https://img.buymeacoffee.com/button-api/?text=Buy me a beer"
              "&emoji=\U0001F37A&slug=beforemycompilefails&button_colour=FFDD00"
              "&font_colour=000000&font_family=Cookie&outline_colour=000000"
              "&coffee_colour=ffffff")

# =========================================================================== #
# ENGINE  (pure standard library — no Flask needed)
# =========================================================================== #

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")

COMMON_SITEMAP_PATHS = [
    "/sitemap_index.xml", "/sitemap.xml", "/wp-sitemap.xml",
    "/sitemap-index.xml", "/sitemap.xml.gz",
]

MAX_BYTES = 8_000_000
ASSET_RE = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|ico|css|js|pdf|zip|gz|rar|7z|"
    r"mp[34]|m4a|mov|avi|webm|woff2?|ttf|eot|otf|wasm|map)(?:\?|#|$)", re.I)

Result = namedtuple(
    "Result", "url ua_label status elapsed size plugin cstatus final_url error")


def vlog(args, msg):
    if not args.quiet:
        print(msg)


def human(n):
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def short_path(url, maxlen=60):
    p = urllib.parse.urlparse(url)
    s = p.path or "/"
    if p.query:
        s += "?" + p.query
    if len(s) > maxlen:
        s = s[:maxlen - 1] + "\u2026"
    return s


def normalize_target(t):
    if not re.match(r"(?i)^https?://", t):
        t = "https://" + t
    return t


def site_root(url):
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def looks_like_sitemap(t):
    low = t.lower()
    return low.endswith(".xml") or low.endswith(".xml.gz") or "sitemap" in low


def dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def build_opener(insecure):
    handlers = []
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def maybe_decompress(raw, encoding):
    encoding = (encoding or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(raw)
        if "deflate" in encoding:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except OSError:
        return raw
    return raw


def read_capped(resp, cap):
    chunks, total = [], 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total >= cap:
            break
    return b"".join(chunks)


def fetch_raw(url, args, ua=None, read_body=True):
    """Return (status, headers_lower, body_bytes, final_url). Raises on error."""
    opener = build_opener(args.insecure)
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", ua or args.user_agent or DESKTOP_UA)
    req.add_header("Accept",
                   "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Encoding", "gzip, deflate")
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    req.add_header("Connection", "close")
    resp = opener.open(req, timeout=args.timeout)
    try:
        status = resp.getcode()
        headers = {k.lower(): v for k, v in resp.headers.items()}
        raw = read_capped(resp, MAX_BYTES) if read_body else b""
        final_url = resp.geturl()
    finally:
        resp.close()
    body = maybe_decompress(raw, headers.get("content-encoding", "")) if read_body else b""
    return status, headers, body, final_url


def _localname(tag):
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(data):
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            return [], []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return [], []
    root_name = _localname(root.tag)
    pages, children = [], []
    for child in root:
        cname = _localname(child.tag)
        loc = None
        for sub in child:
            if _localname(sub.tag) == "loc" and sub.text:
                loc = sub.text.strip()
                break
        if not loc:
            continue
        if cname == "sitemap":
            children.append(loc)
        elif cname == "url":
            pages.append(loc)
        else:
            (children if root_name == "sitemapindex" else pages).append(loc)
    return pages, children


def discover_from_sitemap(start_url, args, seen=None, depth=0):
    if seen is None:
        seen = set()
    if start_url in seen or depth > 10:
        return []
    seen.add(start_url)
    try:
        status, _h, body, _f = fetch_raw(start_url, args)
    except Exception as e:
        vlog(args, f"  ! sitemap fetch failed: {start_url} ({e})")
        return []
    if status != 200 or not body:
        return []
    pages, children = parse_sitemap(body)
    if children:
        vlog(args, f"  + sitemap index: {short_path(start_url)} "
                   f"-> {len(children)} child sitemap(s)")
    urls = list(pages)
    for c in children:
        urls.extend(discover_from_sitemap(c, args, seen, depth + 1))
    if pages:
        vlog(args, f"  + {short_path(start_url)} -> {len(pages)} URL(s)")
    return urls


def autodiscover(root, args):
    found = []
    try:
        status, _h, body, _f = fetch_raw(root + "/robots.txt", args)
        if status == 200 and body:
            for line in body.decode("utf-8", "replace").splitlines():
                m = re.match(r"(?i)\s*sitemap:\s*(\S+)", line)
                if m:
                    found.append(m.group(1).strip())
    except Exception:
        pass
    if found:
        vlog(args, f"  robots.txt advertises {len(found)} sitemap(s)")
        return found
    for path in COMMON_SITEMAP_PATHS:
        url = root + path
        try:
            status, _h, body, _f = fetch_raw(url, args)
        except Exception:
            continue
        head = body[:5000]
        if status == 200 and (b"<urlset" in head or b"<sitemapindex" in head
                              or body[:2] == b"\x1f\x8b"):
            vlog(args, f"  found sitemap at {path}")
            return [url]
    return []


def crawl(root, args):
    host = urllib.parse.urlparse(root).netloc
    href_re = re.compile(rb"""href=["']([^"'#]+)["']""", re.I)
    seen, out = set(), []
    q = deque([(root.rstrip("/") + "/", 0)])
    while q and len(out) < args.max_pages:
        url, depth = q.popleft()
        if url in seen:
            continue
        seen.add(url)
        try:
            status, headers, body, _f = fetch_raw(url, args)
        except Exception:
            continue
        if status != 200 or "html" not in headers.get("content-type", "").lower():
            continue
        out.append(url)
        if depth >= args.max_depth:
            continue
        for raw in href_re.findall(body or b""):
            link = urllib.parse.urljoin(url, raw.decode("utf-8", "replace"))
            p = urllib.parse.urlparse(link)
            if p.scheme not in ("http", "https") or p.netloc != host:
                continue
            clean = p._replace(fragment="").geturl()
            if ASSET_RE.search(clean):
                continue
            if clean not in seen:
                q.append((clean, depth + 1))
    return out


_HIT_WORDS = ("hit", "miss", "dynamic", "expired", "stale", "updating", "revalidated")


def detect_cache(headers, body_text):
    h = headers
    if "x-litespeed-cache" in h:
        return "LiteSpeed", h["x-litespeed-cache"].strip().lower()
    if "cf-cache-status" in h:
        return "Cloudflare", h["cf-cache-status"].strip().lower()
    if "x-rocket-nginx-serving-static" in h:
        return "WP Rocket", h["x-rocket-nginx-serving-static"].strip().lower()
    proxy = h.get("x-cache", "").strip().lower() or None
    if body_text:
        low = body_text.lower()
        if "w3 total cache" in low:
            return "W3 Total Cache", "hit" if "served from:" in low else "?"
        if "wp-super-cache" in low or ("super cache" in low and "cached" in low):
            return "WP Super Cache", "hit"
        if "wp rocket" in low or "wp-rocket" in low:
            return "WP Rocket", "hit"
        if "litespeed" in low:
            return "LiteSpeed", "?"
    if proxy:
        return "proxy", proxy
    return None, "?"


def warm_one(url, ua_label, ua, args):
    attempts = args.retries + 1
    last_err = None
    for i in range(attempts):
        start = time.perf_counter()
        try:
            status, headers, body, final_url = fetch_raw(url, args, ua=ua)
            elapsed = time.perf_counter() - start
            text = body.decode("utf-8", "replace") if body else ""
            plugin, cstatus = detect_cache(headers, text)
            if args.delay:
                time.sleep(args.delay)
            return Result(url, ua_label, status, elapsed, len(body),
                          plugin, cstatus, final_url, None)
        except urllib.error.HTTPError as e:
            elapsed = time.perf_counter() - start
            if e.code >= 500 and i < attempts - 1:
                last_err = e
                time.sleep(min(1.5 * (i + 1), 4.0))
                continue
            if args.delay:
                time.sleep(args.delay)
            return Result(url, ua_label, e.code, elapsed, 0, None, "?", url, f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(min(1.5 * (i + 1), 4.0))
                continue
    if args.delay:
        time.sleep(args.delay)
    reason = getattr(last_err, "reason", last_err)
    return Result(url, ua_label, 0, 0.0, 0, None, "?", url, str(reason))


def collect_urls(args):
    if args.urls_file:
        with open(args.urls_file, encoding="utf-8") as f:
            urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        vlog(args, f"Loaded {len(urls)} URL(s) from {args.urls_file}")
        return urls
    if args.sitemap:
        urls = []
        for sm in args.sitemap:
            urls.extend(discover_from_sitemap(normalize_target(sm), args))
        return urls
    if not args.target:
        return []
    target = normalize_target(args.target)
    if args.crawl:
        vlog(args, f"Crawling {target} (max {args.max_pages} pages, depth {args.max_depth})")
        return crawl(site_root(target), args)
    if looks_like_sitemap(target):
        return discover_from_sitemap(target, args)
    root = site_root(target)
    vlog(args, f"Auto-discovering sitemap for {root}")
    sitemaps = autodiscover(root, args)
    if sitemaps:
        urls = []
        for sm in sitemaps:
            urls.extend(discover_from_sitemap(sm, args))
        if urls:
            return urls
    vlog(args, "  no usable sitemap - falling back to link crawl")
    return crawl(root, args)


# ---- CLI output (only used by `python warmer.py cli ...`) ------------------ #

def _cli_line(done, total, r):
    w = len(str(total))
    idx = f"[{done:>{w}}/{total}]"
    if r.error:
        code, timing, size, cache, extra = "ERR", "  --  ", "    --", "--", f"  ({r.error})"
    else:
        code = str(r.status)
        timing = f"{r.elapsed:5.2f}s"
        size = human(r.size)
        cache = f"{r.plugin}/{r.cstatus}" if r.plugin else "-"
        extra = ""
    tag = "" if r.ua_label == "desktop" else " [m]"
    print(f"{idx} {code:>3} {timing} {size:>7} {cache:<18}{tag} "
          f"{short_path(r.final_url or r.url)}{extra}")


def cli_run_pass(jobs, args, label):
    total = len(jobs)
    results, done = [], 0
    vlog(args, f"\n[{label}] {total} request(s), concurrency={args.concurrency}")
    start = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(warm_one, u, lab, ua, args) for (u, lab, ua) in jobs]
        for fut in cf.as_completed(futs):
            r = fut.result()
            done += 1
            results.append(r)
            if not args.quiet:
                _cli_line(done, total, r)
    return results, time.perf_counter() - start


def cli_summary(results, wall, args, n_urls, n_uas):
    ok = [r for r in results if r.error is None and 200 <= r.status < 400]
    errs = [r for r in results if r.error is not None or r.status == 0]
    status_counts = Counter(r.status for r in results if r.error is None)
    times = [r.elapsed for r in ok if r.elapsed > 0]
    print("\n" + "=" * 66)
    rps = len(results) / wall if wall else 0
    print(f"Warmed {n_urls} URL(s) x {n_uas} UA(s) = {len(results)} request(s) "
          f"in {wall:.1f}s ({rps:.1f} req/s)")
    sc = ", ".join(f"{c}: {n}" for c, n in sorted(status_counts.items()))
    print(f"Status: {sc}" + (f", errors: {len(errs)}" if errs else ""))
    if times:
        print(f"Timing (OK): min {min(times):.2f}s  median {statistics.median(times):.2f}s  "
              f"p95 {percentile(times, 0.95):.2f}s  max {max(times):.2f}s")
    plug = Counter((r.plugin or "none") for r in results if r.error is None)
    if any(k != "none" for k in plug):
        print("Cache plugin: " + ", ".join(f"{k}: {n}" for k, n in plug.items()))
    hm = Counter(f"{r.plugin}/{r.cstatus}" for r in results
                 if r.plugin and r.cstatus in _HIT_WORDS)
    if hm:
        print("Cache status: " + ", ".join(f"{k}: {n}" for k, n in hm.items()))
    slow = sorted([r for r in ok if r.elapsed > 0], key=lambda r: -r.elapsed)[:args.slowest]
    if slow:
        print(f"\nSlowest {len(slow)}:")
        for r in slow:
            print(f"  {r.elapsed:6.2f}s  {short_path(r.final_url or r.url, 70)}")
    if errs:
        print(f"\nFailed ({len(errs)}):")
        for r in errs[:30]:
            print(f"  {(r.error or 'HTTP %s' % r.status):<24} {short_path(r.url, 70)}")
        if len(errs) > 30:
            print(f"  ... and {len(errs) - 30} more")
    print("=" * 66)


def cli_verify(p1, p2):
    m1 = {(r.url, r.ua_label): r.elapsed for r in p1 if r.error is None and r.elapsed > 0}
    m2 = {(r.url, r.ua_label): r.elapsed for r in p2 if r.error is None and r.elapsed > 0}
    common = [k for k in m1 if k in m2]
    print("\n" + "-" * 66)
    if not common:
        print("[verify] no comparable successful requests.")
        print("-" * 66)
        return
    t1 = sum(m1[k] for k in common) / len(common)
    t2 = sum(m2[k] for k in common) / len(common)
    if t2:
        print(f"[verify] 2nd pass avg {t2:.2f}s vs 1st {t1:.2f}s ({t1 / t2:.1f}x faster)")
    else:
        print(f"[verify] 2nd pass avg {t2:.2f}s")
    threshold = max(0.5, t2 * 3)
    still = sorted([(k, m2[k]) for k in common if m2[k] > threshold], key=lambda x: -x[1])
    if still:
        print("Still slow on 2nd pass (likely uncached / dynamic):")
        for (url, _lab), t in still[:15]:
            print(f"  {t:6.2f}s  {short_path(url, 70)}")
    print("-" * 66)


def cli_parse_args(argv):
    p = argparse.ArgumentParser(prog="warmer.py cli",
                                description="Warm a WordPress site's page cache.")
    p.add_argument("target", nargs="?")
    p.add_argument("--sitemap", action="append", default=[])
    p.add_argument("--urls-file")
    p.add_argument("--crawl", action="store_true")
    p.add_argument("--max-pages", type=int, default=500)
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("-c", "--concurrency", type=int, default=5)
    p.add_argument("-t", "--timeout", type=float, default=30)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--delay", type=float, default=0)
    p.add_argument("--user-agent")
    p.add_argument("--mobile", action="store_true")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--include", action="append", default=[])
    p.add_argument("--exclude", action="append", default=[])
    p.add_argument("--limit", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--slowest", type=int, default=10)
    p.add_argument("-q", "--quiet", action="store_true")
    return p.parse_args(argv)


def cli_main(argv):
    args = cli_parse_args(argv)
    if not (args.target or args.sitemap or args.urls_file):
        print("error: provide a target URL, --sitemap, or --urls-file.", file=sys.stderr)
        return 2
    urls = dedupe(collect_urls(args))
    if args.include or args.exclude:
        inc = [re.compile(p, re.I) for p in args.include]
        exc = [re.compile(p, re.I) for p in args.exclude]
        urls = [u for u in urls
                if (not inc or any(r.search(u) for r in inc))
                and not any(r.search(u) for r in exc)]
    if args.limit:
        urls = urls[:args.limit]
    if not urls:
        print("error: no URLs to warm.", file=sys.stderr)
        return 1
    print(f"Discovered {len(urls)} URL(s).")
    if args.dry_run:
        for u in urls:
            print(u)
        return 0
    uas = [("desktop", args.user_agent or DESKTOP_UA)]
    if args.mobile:
        uas.append(("mobile", MOBILE_UA))
    jobs = [(u, lab, ua) for u in urls for (lab, ua) in uas]
    results, wall = cli_run_pass(jobs, args, "warm")
    cli_summary(results, wall, args, len(urls), len(uas))
    if args.verify:
        results2, _w = cli_run_pass(jobs, args, "verify")
        cli_verify(results, results2)
    print(f"\nMade by {__author__}  ·  {GITHUB_URL}")
    print(f"Enjoying it? Buy me a beer \U0001F37A  {BMC_URL}")
    return 0


# =========================================================================== #
# WEB UI  (Flask)
# =========================================================================== #

from flask import Flask, Response, jsonify, request  # noqa: E402

app = Flask(__name__)
JOBS = {}
JOB_TTL = 3600


@app.errorhandler(Exception)
def _json_errors(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify(error=e.description, code=e.code), (e.code or 500)
    app.logger.exception("Unhandled error in request")
    return jsonify(error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), 500


def clampi(v, lo, hi, default):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def clampf(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def splitlist(s):
    if not s:
        return []
    return [p.strip() for p in re.split(r"[\n,]+", s) if p.strip()]


def warm_defaults():
    return Namespace(
        target=None, sitemap=[], urls_file=None, crawl=False,
        max_pages=500, max_depth=3, concurrency=5, timeout=30.0,
        retries=1, delay=0.0, user_agent=None, mobile=False, insecure=False,
        include=[], exclude=[], limit=None, dry_run=False, verify=False,
        slowest=15, quiet=True)


def row_dict(r):
    return {
        "url": short_path(r.final_url or r.url, 80),
        "full_url": r.url,
        "status": r.status,
        "elapsed": round(r.elapsed, 3),
        "size": r.size,
        "size_h": human(r.size) if r.size else "",
        "plugin": r.plugin or "",
        "cstatus": r.cstatus,
        "ua": r.ua_label,
        "error": r.error or "",
        "ok": r.error is None and 200 <= r.status < 400,
    }


def web_summary(results, wall, n_urls, n_uas, slowest):
    ok = [r for r in results if r.error is None and 200 <= r.status < 400]
    errs = [r for r in results if r.error is not None or r.status == 0]
    status_counts = Counter(r.status for r in results if r.error is None)
    times = [r.elapsed for r in ok if r.elapsed > 0]
    plug = Counter((r.plugin or "none") for r in results if r.error is None)
    hm = Counter(f"{r.plugin}/{r.cstatus}" for r in results
                 if r.plugin and r.cstatus in _HIT_WORDS)
    slow = sorted([r for r in ok if r.elapsed > 0], key=lambda r: -r.elapsed)[:slowest]
    return {
        "n_urls": n_urls, "n_uas": n_uas, "total": len(results),
        "wall": round(wall, 1),
        "rps": round(len(results) / wall, 1) if wall else 0,
        "status_counts": dict(sorted(status_counts.items())),
        "errors": len(errs),
        "timing": ({
            "min": round(min(times), 2),
            "median": round(statistics.median(times), 2),
            "p95": round(percentile(times, 0.95), 2),
            "max": round(max(times), 2),
        } if times else None),
        "plugins": dict(plug),
        "cache_status": dict(hm),
        "slowest": [{"elapsed": round(r.elapsed, 2),
                     "url": short_path(r.final_url or r.url, 80)} for r in slow],
        "failures": [{"label": r.error or f"HTTP {r.status}",
                      "url": short_path(r.url, 80)} for r in errs[:50]],
    }


def web_verify(p1, p2):
    m1 = {(r.url, r.ua_label): r.elapsed for r in p1 if r.error is None and r.elapsed > 0}
    m2 = {(r.url, r.ua_label): r.elapsed for r in p2 if r.error is None and r.elapsed > 0}
    common = [k for k in m1 if k in m2]
    if not common:
        return {"comparable": 0}
    t1 = sum(m1[k] for k in common) / len(common)
    t2 = sum(m2[k] for k in common) / len(common)
    threshold = max(0.5, t2 * 3)
    still = sorted([(k, m2[k]) for k in common if m2[k] > threshold],
                   key=lambda x: -x[1])[:15]
    return {
        "comparable": len(common), "t1": round(t1, 3), "t2": round(t2, 3),
        "speedup": round(t1 / t2, 1) if t2 else None,
        "still_slow": [{"elapsed": round(t, 2), "url": short_path(u, 80)}
                       for (u, _lab), t in still],
    }


def warm_pass(jobs, args, cancel, emit, phase):
    total = len(jobs)
    results, done = [], 0
    emit({"type": "phase", "phase": phase, "total": total})
    start = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(warm_one, u, lab, ua, args) for (u, lab, ua) in jobs]
        for fut in cf.as_completed(futs):
            if cancel.is_set():
                for f in futs:
                    f.cancel()
                break
            r = fut.result()
            done += 1
            results.append(r)
            emit({"type": "progress", "phase": phase, "done": done,
                  "total": total, "row": row_dict(r)})
    return results, time.perf_counter() - start


def run_job(job_id, args, dry):
    job = JOBS[job_id]
    q, cancel = job["queue"], job["cancel"]
    emit = q.put
    try:
        emit({"type": "status", "message": "Discovering URLs\u2026"})
        urls = dedupe(collect_urls(args))
        if args.include or args.exclude:
            inc = [re.compile(p, re.I) for p in args.include]
            exc = [re.compile(p, re.I) for p in args.exclude]
            urls = [u for u in urls
                    if (not inc or any(r.search(u) for r in inc))
                    and not any(r.search(u) for r in exc)]
        if args.limit:
            urls = urls[:args.limit]
        if not urls:
            emit({"type": "error",
                  "message": "No URLs found — check the target, sitemap or filters."})
            return
        emit({"type": "discovered", "count": len(urls)})
        if dry:
            emit({"type": "urls", "urls": urls})
            return
        uas = [("desktop", args.user_agent or DESKTOP_UA)]
        if args.mobile:
            uas.append(("mobile", MOBILE_UA))
        warm_jobs = [(u, lab, ua) for u in urls for (lab, ua) in uas]
        results, wall = warm_pass(warm_jobs, args, cancel, emit, "warm")
        emit({"type": "summary", "phase": "warm",
              "data": web_summary(results, wall, len(urls), len(uas), args.slowest)})
        job["results"] = results
        if args.verify and not cancel.is_set():
            results2, _w2 = warm_pass(warm_jobs, args, cancel, emit, "verify")
            emit({"type": "verify", "data": web_verify(results, results2)})
        if cancel.is_set():
            emit({"type": "status", "message": "Cancelled."})
    except Exception as e:
        emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        emit({"type": "done"})
        q.put(None)


def prune_jobs():
    now = time.time()
    for jid in [k for k, v in JOBS.items()
                if now - v["created"] > JOB_TTL and v.get("finished")]:
        JOBS.pop(jid, None)


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/start", methods=["POST"])
def start():
    prune_jobs()
    data = request.get_json(force=True, silent=True) or {}
    target = (data.get("target") or "").strip()
    if not target:
        return jsonify(error="A target URL is required."), 400
    args = warm_defaults()
    args.target = target
    args.concurrency = clampi(data.get("concurrency"), 1, 64, 5)
    args.timeout = clampf(data.get("timeout"), 1, 300, 30)
    args.retries = clampi(data.get("retries"), 0, 5, 1)
    args.delay = clampf(data.get("delay"), 0, 10, 0)
    args.mobile = bool(data.get("mobile"))
    args.verify = bool(data.get("verify"))
    args.insecure = bool(data.get("insecure"))
    args.crawl = bool(data.get("crawl"))
    args.max_pages = clampi(data.get("max_pages"), 1, 100000, 500)
    args.max_depth = clampi(data.get("max_depth"), 1, 20, 3)
    args.include = splitlist(data.get("include"))
    args.exclude = splitlist(data.get("exclude"))
    lim = data.get("limit")
    args.limit = clampi(lim, 1, 1000000, None) if lim not in (None, "", 0) else None
    ua = (data.get("user_agent") or "").strip()
    args.user_agent = ua or None
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"queue": queue.Queue(), "cancel": threading.Event(),
                    "created": time.time(), "results": None, "finished": False}
    threading.Thread(target=_run_and_flag, args=(job_id, args, bool(data.get("dry"))),
                     daemon=True).start()
    return jsonify(job_id=job_id)


def _run_and_flag(job_id, args, dry):
    try:
        run_job(job_id, args, dry)
    finally:
        if job_id in JOBS:
            JOBS[job_id]["finished"] = True


@app.route("/stream/<job_id>")
def stream(job_id):
    job = JOBS.get(job_id)
    if not job:
        return "no such job", 404
    q = job["queue"]

    def gen():
        yield ": connected\n\n"
        while True:
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if ev is None:
                yield "event: end\ndata: {}\n\n"
                break
            yield f"data: {json.dumps(ev)}\n\n"

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    job = JOBS.get(job_id)
    if job:
        job["cancel"].set()
    return jsonify(ok=True)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WP Cache Warmer</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c232c; --line:#30363d;
    --txt:#e6edf3; --muted:#8b949e; --accent:#3fb950; --accent2:#58a6ff;
    --warn:#d29922; --err:#f85149; --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:1040px;margin:0 auto;padding:22px 18px 60px}
  h1{font-size:19px;margin:0 0 2px;font-weight:650}
  h1 span{color:var(--accent)}
  .sub{color:var(--muted);font-size:12.5px;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
        padding:16px;margin-bottom:14px}
  label{display:block;color:var(--muted);font-size:11.5px;text-transform:uppercase;
        letter-spacing:.04em;margin-bottom:5px}
  input[type=text],input[type=number]{width:100%;background:#0d1117;border:1px solid var(--line);
        color:var(--txt);border-radius:7px;padding:9px 10px;font-size:13.5px;font-family:var(--mono)}
  input:focus{outline:none;border-color:var(--accent2)}
  .grid{display:grid;gap:12px}
  .row4{grid-template-columns:repeat(4,1fr)}
  .checks{display:flex;flex-wrap:wrap;gap:16px;margin-top:14px}
  .chk{display:flex;align-items:center;gap:7px;color:var(--txt);font-size:13px;cursor:pointer}
  .chk input{width:15px;height:15px;accent-color:var(--accent)}
  .adv{margin-top:12px}
  details summary{cursor:pointer;color:var(--muted);font-size:12.5px;margin-top:6px;user-select:none}
  .btns{display:flex;gap:10px;margin-top:16px}
  button{border:1px solid var(--line);background:var(--panel2);color:var(--txt);
         padding:9px 18px;border-radius:7px;font-size:13.5px;cursor:pointer;font-weight:550}
  button.go{background:var(--accent);border-color:var(--accent);color:#06210e}
  button.go:hover{filter:brightness(1.08)}
  button:disabled{opacity:.45;cursor:not-allowed}
  .stats{display:flex;flex-wrap:wrap;gap:18px;margin-bottom:10px;font-size:13px}
  .stat b{font-family:var(--mono);font-size:16px}
  .stat span{color:var(--muted);font-size:11px;display:block;text-transform:uppercase;letter-spacing:.04em}
  .bar{height:7px;background:#0d1117;border:1px solid var(--line);border-radius:5px;overflow:hidden;margin-bottom:12px}
  .bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .15s}
  #log{font-family:var(--mono);font-size:12.2px;background:#0a0d12;border:1px solid var(--line);
       border-radius:8px;padding:10px;height:340px;overflow:auto;white-space:pre}
  #log .l{padding:1px 0}
  .c-ok{color:var(--accent)} .c-redir{color:var(--accent2)} .c-err{color:var(--err)}
  .c-dim{color:var(--muted)} .c-tag{color:var(--warn)}
  .sumgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .kv{font-size:13px;margin:3px 0} .kv b{font-family:var(--mono)}
  .list{font-family:var(--mono);font-size:12px;color:var(--muted);max-height:170px;overflow:auto}
  .list div{padding:1px 0}
  .pill{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:20px;
        padding:2px 10px;margin:2px 4px 2px 0;font-size:12px;font-family:var(--mono)}
  .hidden{display:none}
  .err-banner{background:#3a1416;border:1px solid var(--err);color:#ffb4ae;padding:10px 12px;border-radius:8px;margin-bottom:12px;white-space:pre-wrap;max-height:280px;overflow:auto;font-family:var(--mono);font-size:12.5px;line-height:1.4}
  .footer{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:18px;margin-top:28px;padding-top:18px;border-top:1px solid var(--line)}
  .footer a{color:var(--muted);text-decoration:none;font-size:13px;display:inline-flex;align-items:center;gap:6px}
  .footer a:hover{color:var(--accent2)}
  .footer img{display:block;border-radius:8px}
  a{color:var(--accent2)}
</style>
</head>
<body>
<div class="wrap">
  <h1>WP <span>Cache Warmer</span></h1>
  <div class="sub">Flush your cache, then warm every page so the first real visitor never hits a cold miss.</div>

  <div class="card">
    <label>Target — site root or sitemap URL</label>
    <input id="target" type="text" placeholder="https://example.com  or  https://example.com/sitemap_index.xml" autofocus>

    <div class="grid row4" style="margin-top:12px">
      <div><label>Concurrency</label><input id="concurrency" type="number" value="5" min="1" max="64"></div>
      <div><label>Timeout (s)</label><input id="timeout" type="number" value="30" min="1" max="300"></div>
      <div><label>Retries</label><input id="retries" type="number" value="1" min="0" max="5"></div>
      <div><label>Limit (blank = all)</label><input id="limit" type="number" min="1" placeholder="all"></div>
    </div>

    <div class="checks">
      <label class="chk"><input type="checkbox" id="mobile"> Also warm mobile</label>
      <label class="chk"><input type="checkbox" id="verify"> Verify (2nd pass)</label>
      <label class="chk"><input type="checkbox" id="insecure"> Skip TLS verify (expired / self-signed)</label>
      <label class="chk"><input type="checkbox" id="crawl"> Crawl links (no sitemap)</label>
      <label class="chk"><input type="checkbox" id="dry"> Discover only</label>
    </div>

    <details class="adv">
      <summary>Advanced filters</summary>
      <div class="grid" style="grid-template-columns:1fr 1fr;margin-top:10px">
        <div><label>Include (regex, one per line / comma)</label><input id="include" type="text" placeholder="/blog|/product"></div>
        <div><label>Exclude (regex, one per line / comma)</label><input id="exclude" type="text" placeholder="/cart|/checkout|\?add-to-cart"></div>
      </div>
      <div class="grid" style="grid-template-columns:1fr 1fr;margin-top:10px">
        <div><label>Crawl max pages</label><input id="max_pages" type="number" value="500" min="1"></div>
        <div><label>Crawl depth</label><input id="max_depth" type="number" value="3" min="1" max="20"></div>
      </div>
      <div style="margin-top:10px"><label>User-Agent override (desktop)</label><input id="user_agent" type="text" placeholder="(default Chrome desktop UA)"></div>
    </details>

    <div class="btns">
      <button class="go" id="startBtn" onclick="start()">Start warming</button>
      <button id="cancelBtn" onclick="cancelJob()" disabled>Cancel</button>
    </div>
  </div>

  <div id="errBanner" class="err-banner hidden"></div>

  <div class="card" id="liveCard">
    <div class="stats">
      <div class="stat"><b id="sDisc">0</b><span>Discovered</span></div>
      <div class="stat"><b id="sDone">0</b><span id="sDoneLbl">Done</span></div>
      <div class="stat"><b id="sOk" class="c-ok">0</b><span>OK</span></div>
      <div class="stat"><b id="sErr" class="c-err">0</b><span>Errors</span></div>
      <div class="stat"><b id="sRps">–</b><span>req/s</span></div>
      <div class="stat"><b id="sPhase" class="c-dim">idle</b><span>Phase</span></div>
    </div>
    <div class="bar"><i id="barFill"></i></div>
    <div id="log"></div>
  </div>

  <div class="card hidden" id="urlsCard">
    <label>Discovered URLs</label>
    <div class="list" id="urlsList"></div>
  </div>

  <div class="card hidden" id="sumCard">
    <label>Summary</label>
    <div id="sumTop" style="margin-bottom:10px"></div>
    <div class="sumgrid">
      <div><div class="kv" style="margin-bottom:6px"><b>Slowest pages</b></div><div class="list" id="sumSlow"></div></div>
      <div><div class="kv" style="margin-bottom:6px"><b>Failures</b></div><div class="list" id="sumFail"></div></div>
    </div>
  </div>

  <div class="card hidden" id="verCard">
    <label>Verify — second pass</label>
    <div id="verTop"></div>
    <div id="verSlowWrap" class="hidden"><div class="kv" style="margin:8px 0 4px"><b>Still slow on 2nd pass (likely uncached / dynamic)</b></div><div class="list" id="verSlow"></div></div>
  </div>

  <div class="footer">
    <a href="https://github.com/BeforeMyCompileFails" target="_blank" rel="noopener">⌨ GitHub @BeforeMyCompileFails</a>
    <a href="https://github.com/BeforeMyCompileFails/WP-Cache-Warmer" target="_blank" rel="noopener">★ Source</a>
    <a href="https://www.buymeacoffee.com/beforemycompilefails" target="_blank" rel="noopener" title="Buy me a beer 🍺">
      <img src="https://img.buymeacoffee.com/button-api/?text=Buy me a beer&amp;emoji=🍺&amp;slug=beforemycompilefails&amp;button_colour=FFDD00&amp;font_colour=000000&amp;font_family=Cookie&amp;outline_colour=000000&amp;coffee_colour=ffffff" alt="Buy me a beer" height="40">
    </a>
  </div>
</div>

<script>
let es=null, jobId=null, ok=0, err=0;
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function reset(){
  ok=0; err=0; jobId=null;
  $('sDisc').textContent='0'; $('sDone').textContent='0';
  $('sOk').textContent='0'; $('sErr').textContent='0';
  $('sRps').textContent='–'; $('sPhase').textContent='starting'; $('sDoneLbl').textContent='Done';
  $('barFill').style.width='0%';
  $('log').innerHTML=''; $('urlsList').innerHTML='';
  ['urlsCard','sumCard','verCard'].forEach(i=>$(i).classList.add('hidden'));
  $('errBanner').classList.add('hidden');
}

function start(){
  const target=$('target').value.trim();
  if(!target){ banner('Enter a target URL first.'); return; }
  reset();
  const body={
    target, concurrency:+$('concurrency').value, timeout:+$('timeout').value,
    retries:+$('retries').value, limit:$('limit').value, mobile:$('mobile').checked,
    verify:$('verify').checked, insecure:$('insecure').checked, crawl:$('crawl').checked,
    dry:$('dry').checked, include:$('include').value, exclude:$('exclude').value,
    max_pages:+$('max_pages').value, max_depth:+$('max_depth').value,
    user_agent:$('user_agent').value
  };
  $('startBtn').disabled=true; $('cancelBtn').disabled=false;
  fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(async r=>{
      const txt=await r.text(); let d;
      try{ d=JSON.parse(txt); }
      catch(e){ throw new Error('Server returned HTTP '+r.status+' (not JSON):\n'+txt.slice(0,500)); }
      if(!r.ok || d.error){ throw new Error((d.error||('HTTP '+r.status))+(d.trace?('\n\n'+d.trace):'')); }
      return d;
    })
    .then(d=>{ jobId=d.job_id; listen(jobId); })
    .catch(e=>{ finish(); banner(String(e.message||e)); });
}

function listen(id){
  es=new EventSource('/stream/'+id);
  es.onmessage=ev=>{ try{ handle(JSON.parse(ev.data)); }catch(e){} };
  es.addEventListener('end',()=>finish());
  es.onerror=()=>{ if(es){ es.close(); es=null; } if(jobId){ banner('Stream connection lost — check the server console.'); finish(); } };
}

function handle(m){
  switch(m.type){
    case 'status': $('sPhase').textContent=m.message.replace(/\u2026/,'…'); break;
    case 'discovered': $('sDisc').textContent=m.count; break;
    case 'urls':
      $('urlsCard').classList.remove('hidden');
      $('urlsList').innerHTML=m.urls.map(u=>'<div>'+esc(u)+'</div>').join('');
      $('sPhase').textContent='done'; break;
    case 'phase':
      $('sPhase').textContent=m.phase; $('sDoneLbl').textContent=(m.phase==='verify'?'Done (verify)':'Done');
      $('sDone').textContent='0'; $('barFill').style.width='0%';
      if(m.phase==='verify'){ ok=0; err=0; $('sOk').textContent='0'; $('sErr').textContent='0'; }
      window._total=m.total; break;
    case 'progress': progress(m); break;
    case 'summary': summary(m.data); break;
    case 'verify': verify(m.data); break;
    case 'error': banner(m.message); break;
    case 'done': finish(); break;
  }
}

function progress(m){
  $('sDone').textContent=m.done;
  $('barFill').style.width=(100*m.done/m.total).toFixed(1)+'%';
  const r=m.row;
  if(r.ok){ ok++; $('sOk').textContent=ok; } else { err++; $('sErr').textContent=err; }
  const w=String(m.total).length;
  const idx=('['+String(m.done).padStart(w)+'/'+m.total+']');
  let cls, code, timing, size, cache;
  if(r.error){ cls='c-err'; code='ERR'; timing=' --- '; size='   --'; cache='--  ('+esc(r.error)+')'; }
  else{
    code=String(r.status);
    cls = r.status<300?'c-ok':(r.status<400?'c-redir':'c-err');
    timing=r.elapsed.toFixed(2)+'s'; size=(r.size_h||'').padStart(7);
    cache=r.plugin?(r.plugin+'/'+r.cstatus):'-';
  }
  const tag=r.ua==='mobile'?' [m]':'';
  const line=document.createElement('div'); line.className='l';
  line.innerHTML='<span class="c-dim">'+idx+'</span> <span class="'+cls+'">'+code.padStart(3)+'</span> '
    +'<span class="c-dim">'+timing+'</span> <span class="c-dim">'+size+'</span> '
    +'<span class="c-tag">'+esc(cache.padEnd(18))+tag+'</span> '+esc(r.url);
  const log=$('log'); const atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;
  log.appendChild(line); if(atBottom) log.scrollTop=log.scrollHeight;
}

function summary(d){
  $('sRps').textContent=d.rps;
  const sc=Object.entries(d.status_counts).map(([k,v])=>k+': '+v).join(', ');
  let h='<div class="kv">Warmed <b>'+d.n_urls+'</b> URL(s) × '+d.n_uas+' UA = <b>'+d.total
       +'</b> req in <b>'+d.wall+'s</b> ('+d.rps+' req/s)</div>';
  h+='<div class="kv">Status: '+esc(sc)+(d.errors?(', <span class="c-err">errors: '+d.errors+'</span>'):'')+'</div>';
  if(d.timing) h+='<div class="kv">Timing (OK): min '+d.timing.min+'s · median '+d.timing.median
       +'s · p95 '+d.timing.p95+'s · max '+d.timing.max+'s</div>';
  const plug=Object.entries(d.plugins).filter(([k])=>k!=='none').map(([k,v])=>'<span class="pill">'+esc(k)+': '+v+'</span>').join('');
  if(plug) h+='<div class="kv" style="margin-top:6px">Cache plugin: '+plug+'</div>';
  const cs=Object.entries(d.cache_status).map(([k,v])=>'<span class="pill">'+esc(k)+': '+v+'</span>').join('');
  if(cs) h+='<div class="kv">Cache status: '+cs+'</div>';
  $('sumTop').innerHTML=h;
  $('sumSlow').innerHTML=d.slowest.map(s=>'<div>'+s.elapsed.toFixed(2).padStart(6)+'s  '+esc(s.url)+'</div>').join('')||'<div>–</div>';
  $('sumFail').innerHTML=d.failures.map(f=>'<div class="c-err">'+esc(f.label)+'  '+esc(f.url)+'</div>').join('')||'<div class="c-dim">none</div>';
  $('sumCard').classList.remove('hidden');
}

function verify(d){
  let h;
  if(!d.comparable){ h='<div class="kv c-dim">No comparable successful requests.</div>'; }
  else if(d.speedup){ h='<div class="kv">2nd pass avg <b>'+d.t2+'s</b> vs 1st <b>'+d.t1+'s</b> — <b class="c-ok">'+d.speedup+'× faster</b></div>'; }
  else { h='<div class="kv">2nd pass avg <b>'+d.t2+'s</b></div>'; }
  $('verTop').innerHTML=h;
  if(d.still_slow && d.still_slow.length){
    $('verSlowWrap').classList.remove('hidden');
    $('verSlow').innerHTML=d.still_slow.map(s=>'<div>'+s.elapsed.toFixed(2).padStart(6)+'s  '+esc(s.url)+'</div>').join('');
  } else $('verSlowWrap').classList.add('hidden');
  $('verCard').classList.remove('hidden');
}

function finish(){
  if(es){ es.close(); es=null; }
  jobId=null;
  $('startBtn').disabled=false; $('cancelBtn').disabled=true;
  if($('sPhase').textContent!=='done') $('sPhase').textContent='done';
}
function cancelJob(){ if(jobId) fetch('/cancel/'+jobId,{method:'POST'}); }
function banner(msg){ const b=$('errBanner'); b.textContent=msg; b.classList.remove('hidden'); }
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("cli", "warm"):
        sys.exit(cli_main(sys.argv[2:]))
    p = argparse.ArgumentParser(description="WP Cache Warmer — web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--debug", action="store_true")
    a = p.parse_args()
    print(f"WP Cache Warmer UI -> http://{a.host}:{a.port}")
    print(f"  by {__author__}  ·  {GITHUB_URL}  ·  beer: {BMC_URL}")
    print("  CLI mode:  python warmer.py cli <url> [--verify --insecure --mobile ...]")
    app.run(host=a.host, port=a.port, threaded=True, debug=a.debug)
