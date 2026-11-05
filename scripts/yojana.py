#!/usr/bin/env python3
"""
Vavetra Yojana – scheme watcher (Python standard library only, runs on the Windows PC runner). No AI.

    python scripts/yojana.py <data_dir> [auto|seen] [ids]

<data_dir> is a checkout of the "data" branch. Everything lives in <data_dir>/yojana/:

    central.json, GJ.json   the detailed schemes, written and checked by a person (never changed by this script)
    index.json              states, categories, myScheme category links, news file (the app reads this first)
    schemes.json            central + Gujarat in one file, for app version 1.0.0
    news.json               farm news from PIB's official RSS feed (title + link + date, never rewritten)
    STATUS.md, status.json  what this run did and what needs a person (read STATUS.md on GitHub)
    state.json              page fingerprints and alerts between runs

What it does every run (auto):
  1. Checks the scheme files: valid JSON, every field, dates (openDate <= lastDate), official links only.
     A broken file is NOT published: the last good copy stays and STATUS.md says what is wrong.
  2. Opens the official page of every scheme. When a page changed since the last run, or our last date is no
     longer written on it, or the last date is near/past, STATUS.md lists it with the dates now on the page.
     A person then checks the page and updates the scheme file. The alert clears when the scheme's
     "verified" date is on/after the change, or with mode "seen" (ids = id1,id2 or all).
  3. Reads PIB's official press-release RSS and keeps farm news (title, link, date) in news.json.
  4. Reads the i-Khedut home-page notices (the department's own public JSON) into ikhedut.json: title as published,
     date, application window from the title, topics. The app notifies farmers of new ones.
  5. Checks help.json (guides, status links, helplines written by a person) and that its official links still open.
"""
import codecs
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(HERE, "yojana_sources.json"), encoding="utf-8"))
TODAY = date.today()
TODAY_S = TODAY.isoformat()
SOON_DAYS = 7          # warn this many days before a last date
STALE_DAYS = 120       # warn when a person has not verified a scheme for this long
FAIL_WARN = 3          # warn after this many runs in a row where a page did not open
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) vavetra-yojana/2.0 (+https://github.com/chhatrolasmit/vavetra-mandi-data)"
CATEGORIES = CONFIG["categories"]
ICONS = {"money", "portal", "shield", "bank", "drop", "sun", "tractor", "cow", "leaf", "doc"}
STATUSES = {"always", "window", "check", "info"}
RESERVED = {"head", "quiz", "chips", "ad", "title", "none", "updated", "all", "news"}


# ---------------------------------------------------------------- files

def jload(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jsave(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def scopes():
    return ["central"] + list(CONFIG["states"])


def scope_file(d, scope):
    return os.path.join(d, "central.json" if scope == "central" else f"{scope}.json")


# ---------------------------------------------------------------- web pages

class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in ("p", "br", "li", "tr", "h1", "h2", "h3", "h4", "div", "td"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _host_in(url, domains):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def allowed(url):
    """Official page the pipeline may open and the app may link to."""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and _host_in(url, CONFIG["allow"]) and not _host_in(url, CONFIG.get("deny", []))


def linkable(url):
    """Links in scheme files: official domains, including myScheme (linking is allowed there, reading is not)."""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and _host_in(url, CONFIG["allow"])


class _OfficialOnly(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an allowed official page (never to myScheme or any other site)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed(newurl):
            raise urllib.error.URLError("redirected off the official domains: " + newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_OfficialOnly)


def get(url, accept="text/html,application/xhtml+xml,application/xml,*/*"):
    """(text, None) or (None, error). Never opens a page outside the official domains."""
    if not allowed(url):
        return None, "not an allowed official domain"
    err = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with _OPENER.open(req, timeout=60) as r:
                if not allowed(r.geturl()):
                    return None, "redirected off the official domains"
                raw = r.read(3_000_000)
                ctype = r.headers.get("Content-Type", "")
            enc = (re.search(r"charset=([\w-]+)", ctype) or [None, "utf-8"])[1]
            try:
                codecs.lookup(enc)
            except LookupError:
                enc = "utf-8"
            return raw.decode(enc, errors="replace"), None
        except Exception as e:  # network, TLS, HTTP errors: try again, then report
            err = f"{type(e).__name__}: {e}"[:200]
            time.sleep(4 * (attempt + 1))
    return None, err


def page_text(url):
    raw, err = get(url)
    if raw is None:
        return None, err
    p = _Text()
    p.feed(raw)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
    return re.sub(r"\n\s*\n+", "\n", text).strip(), None


def fingerprint(text):
    # Visitor counters and "last updated" lines change daily without any real change; ignore them.
    lines = [l for l in text.splitlines() if not re.search(r"(?i)visitor|last updated|updated on|hits|counter|page views", l)]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- dates written on a page

DIGITS = str.maketrans("૦૧૨૩૪૫૬૭૮૯०१२३४५६७८९", "01234567890123456789")
MONTHS = {
    1: ["january", "jan", "જાન્યુઆરી", "જાન્યુ", "जनवरी", "जन"],
    2: ["february", "feb", "ફેબ્રુઆરી", "ફેબ્રુ", "फरवरी", "फ़रवरी", "फ़र", "फर"],
    3: ["march", "mar", "માર્ચ", "मार्च"],
    4: ["april", "apr", "એપ્રિલ", "अप्रैल", "अप्रेल"],
    5: ["may", "મે", "मई"],
    6: ["june", "jun", "જૂન", "જુન", "जून"],
    7: ["july", "jul", "જુલાઈ", "જુલાઇ", "जुलाई"],
    8: ["august", "aug", "ઑગસ્ટ", "ઓગસ્ટ", "ઓગષ્ટ", "ઑગષ્ટ", "अगस्त", "अग"],
    9: ["september", "sept", "sep", "સપ્ટેમ્બર", "સપ્ટેંબર", "સપ્ટે", "सितंबर", "सितम्बर", "सित"],
    10: ["october", "oct", "ઑક્ટોબર", "ઓક્ટોબર", "ઑક્ટો", "ઓક્ટો", "अक्टूबर", "अक्तूबर", "अक्टू"],
    11: ["november", "nov", "નવેમ્બર", "નવેંબર", "નવે", "नवंबर", "नवम्बर", "नव"],
    12: ["december", "dec", "ડિસેમ્બર", "ડિસેંબર", "ડિસે", "दिसंबर", "दिसम्बर", "दिस"],
}
_NAME = {unicodedata.normalize("NFC", n): m for m, ns in MONTHS.items() for n in ns}
_NAMES = "|".join(re.escape(n) for n in sorted(_NAME, key=len, reverse=True))
_SEP = r"[\s,.\-/]*"
_ORD = r"(?:st|nd|rd|th)?"
_PATTERNS = [
    # 05-11-2026, 05.11.2026, 5/11/26 (a 2-digit year only with / or -, so "Version 2.5.26" is not a date)
    (re.compile(r"(?<!\d)(?<!\d\.)(\d{1,2})\s*([-/.])\s*(\d{1,2})\s*\2\s*(\d{4})(?![\d.])"), "dmy"),
    (re.compile(r"(?<!\d)(?<!\d\.)(\d{1,2})\s*([-/])\s*(\d{1,2})\s*\2\s*(\d{2})(?![\d.\-/])"), "dmy"),
    # 2026-11-05
    (re.compile(r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)"), "ymd"),
    # 5 November 2026, 5th Nov, 2026, ૫ નવેમ્બર ૨૦૨૬, 5 नवंबर 2026
    (re.compile(r"(?<!\d)(\d{1,2})" + _ORD + _SEP + r"(?:of\s+)?(" + _NAMES + r")(?![a-z])" + _SEP + r"(\d{4})(?!\d)"), "dny"),
    # November 5, 2026
    (re.compile(r"(?<![a-z])(" + _NAMES + r")" + _SEP + r"(\d{1,2})" + _ORD + _SEP + r"(\d{4})(?!\d)"), "ndy"),
]


def find_dates(text):
    """Every full date (day, month, year) written on a page, as ISO strings."""
    t = unicodedata.normalize("NFC", text).translate(DIGITS).lower()
    found = set()
    for rx, kind in _PATTERNS:
        for m in rx.finditer(t):
            try:
                if kind == "dmy":
                    d, mo, y = int(m[1]), int(m[3]), int(m[4])
                elif kind == "ymd":
                    y, mo, d = int(m[1]), int(m[2]), int(m[3])
                elif kind == "dny":
                    d, mo, y = int(m[1]), _NAME[m[2]], int(m[3])
                else:
                    mo, d, y = _NAME[m[1]], int(m[2]), int(m[3])
                if y < 100:
                    y += 2000
                if 2015 <= y <= TODAY.year + 3:
                    found.add(date(y, mo, d).isoformat())
            except (ValueError, KeyError):
                pass
    return found


# ---------------------------------------------------------------- scheme files (written by a person)

def txt_ok(t):
    return isinstance(t, dict) and all(isinstance(t.get(l), str) and t[l].strip() for l in ("gu", "hi", "en"))


def iso(v):
    if v in (None, ""):
        return None
    if not (isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)):
        raise ValueError
    return date.fromisoformat(v)


def check_scheme(s):
    """List of problems of one scheme (empty = fine)."""
    p = []
    if not isinstance(s, dict):
        return ["not an object"]
    sid = s.get("id")
    if not (isinstance(sid, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", sid)) or sid in RESERVED:
        p.append("bad id")
    if s.get("group") not in CATEGORIES:
        p.append(f"unknown group {s.get('group')}")
    if s.get("icon") not in ICONS:
        p.append(f"unknown icon {s.get('icon')}")
    if s.get("status") not in STATUSES:
        p.append("bad status")
    for k in ("key", "by", "name", "short", "benefit", "dateNote"):
        if not txt_ok(s.get(k)):
            p.append(f"{k} needs gu, hi and en")
    for k in ("eligible", "documents", "steps"):
        v = s.get(k)
        if not isinstance(v, list) or not v or not all(txt_ok(x) for x in v):
            p.append(f"{k} needs items with gu, hi and en")
    v = s.get("notEligible", [])
    if not isinstance(v, list) or not all(txt_ok(x) for x in v):
        p.append("notEligible items need gu, hi and en")
    v = s.get("rates", [])
    if not isinstance(v, list) or not all(isinstance(x, dict) and isinstance(x.get("when"), str) and txt_ok(x.get("text")) for x in v):
        p.append("rates items need when + text (gu, hi, en)")
    links = s.get("links")
    if not isinstance(links, list) or not links:
        p.append("no official link")
    else:
        for l in links:
            if not isinstance(l, dict) or not txt_ok(l.get("label")) or not linkable(str(l.get("url", ""))):
                p.append(f"bad link {l.get('url') if isinstance(l, dict) else l}")
    for u in s.get("sources") or []:
        if not (isinstance(u, str) and allowed(u)):
            p.append(f"source not official/readable: {u}")
    try:
        od, ld, vd = iso(s.get("openDate")), iso(s.get("lastDate")), iso(s.get("verified"))
        if od and ld and od > ld:
            p.append("openDate is after lastDate")
        if vd and vd > TODAY + timedelta(days=1):
            p.append("verified is in the future")
        if (od or ld) and s.get("status") != "window":
            p.append('dates need status "window"')
    except ValueError:
        p.append("dates must be YYYY-MM-DD")
    for k, ok in (("needs", {"owner", "notTaxGovt", "water", "well", "noGrid"}),):
        if not isinstance(s.get(k, []), list) or not set(s.get(k, [])) <= ok:
            p.append(f"bad {k}")
    if not isinstance(s.get("except", []), list):
        p.append("bad except")
    return p


def load_books(d, rep):
    """Valid scheme files by scope. A broken file keeps its last good copy (last_good/), never an empty list."""
    books = {}
    seen = set()
    for scope in scopes():
        path = scope_file(d, scope)
        good = os.path.join(d, "last_good", os.path.basename(path))
        book = jload(path, None)
        problems = []
        if not isinstance(book, dict) or not isinstance(book.get("schemes"), list):
            problems = ["file is missing or not valid JSON"]
        else:
            for s in book["schemes"]:
                for pr in check_scheme(s):
                    problems.append(f"{s.get('id') if isinstance(s, dict) else '?'}: {pr}")
                if isinstance(s, dict):
                    if s.get("id") in seen:
                        problems.append(f"{s.get('id')}: same id twice")
                    seen.add(s.get("id"))
        if problems:
            rep["broken"].append({"file": os.path.basename(path), "problems": problems[:20]})
            # Keep the person's broken edit next to it, so no work is lost when the good copy is put back.
            if os.path.exists(path):
                os.makedirs(os.path.join(d, "last_bad"), exist_ok=True)
                shutil.copyfile(path, os.path.join(d, "last_bad", os.path.basename(path)))
            old = jload(good, None)
            if old:
                jsave(path, old)       # put the last good copy back, so the app never reads a broken file
                book = old
            else:
                book = None
        else:
            jsave(good, book)
        if book:
            books[scope] = book
    return books


# ---------------------------------------------------------------- watching the official pages

def watch(books, state, rep):
    hashes = state.setdefault("hashes", {})
    fails = state.setdefault("fails", {})
    alerts = state.setdefault("alerts", {})
    live_ids = set()
    for scope, book in books.items():
        for s in book["schemes"]:
            sid = s["id"]
            live_ids.add(sid)
            urls = [u for u in (s.get("sources") or [l["url"] for l in s["links"]]) if allowed(u)][:4]
            texts, failed = [], []
            for u in urls:
                t, err = page_text(u)
                if t is None:
                    failed.append((u, err))
                else:
                    texts.append(t)
            rep["checked"] += 1
            if texts:
                rep["opened"] += 1
            if failed:
                fails[sid] = fails.get(sid, 0) + 1
                if fails[sid] >= FAIL_WARN:
                    rep["failed"].append({"id": sid, "runs": fails[sid], "pages": [f"{u} ({e})" for u, e in failed]})
            else:
                fails.pop(sid, None)
            dates = set().union(*map(find_dates, texts)) if texts else set()
            future = sorted(x for x in dates if x >= TODAY_S)[:8]
            verified = s.get("verified") or ""
            # 1. the page changed (compared only when every page opened)
            if texts and not failed:
                fp = "|".join(fingerprint(t) for t in texts)
                old = hashes.get(sid)
                hashes[sid] = fp
                if old and old != fp:
                    alerts[sid] = {"kind": "changed", "since": TODAY_S, "dates": future, "urls": urls, "new": True}
            a = alerts.get(sid)
            # Cleared when the scheme was verified on/after the change day, but never in the run that found the change
            # (a check made the same morning, before an afternoon change, must not hide it).
            if a and not a.pop("new", False) and verified >= a["since"]:
                alerts.pop(sid)           # a person checked the scheme after the change
                a = None
            if a:
                a["dates"] = future
                rep["changed"].append({"id": sid, "scope": scope, **{k: v for k, v in a.items() if k != "new"}})
            # 2. our dates are no longer written on the official page
            for k in ("openDate", "lastDate"):
                v = s.get(k)
                if v and texts and not failed and v not in dates and v >= TODAY_S:
                    rep["dates"].append({"id": sid, "scope": scope, "field": k, "value": v, "page": future, "urls": urls})
            # 3. the last date is near or has passed (a new round may have opened)
            ld = s.get("lastDate")
            if ld:
                left = (date.fromisoformat(ld) - TODAY).days
                if 0 <= left <= SOON_DAYS:
                    rep["soon"].append({"id": sid, "scope": scope, "lastDate": ld, "days": left})
                elif -30 <= left < 0:
                    rep["ended"].append({"id": sid, "scope": scope, "lastDate": ld, "page": future})
            # 4. not verified by a person for a long time
            if not verified or (TODAY - date.fromisoformat(verified)).days > STALE_DAYS:
                rep["stale"].append({"id": sid, "scope": scope, "verified": verified or "—"})
    for k in list(hashes):
        if k not in live_ids:
            hashes.pop(k)
    for k in list(alerts):
        if k not in live_ids:
            alerts.pop(k)


# ---------------------------------------------------------------- news (PIB RSS, never rewritten)

def news(d, rep):
    cfg = CONFIG["news"]
    path = os.path.join(d, "news.json")
    old = jload(path, {"items": []}).get("items", [])
    # English words must start a word ("mandi" not in "demanding"); Hindi words need a space or punctuation after them.
    words = [(r"\b" + re.escape(w.lower())) if re.match(r"[a-z]", w.lower()) else
             (r"(?<![\u0900-\u097F])" + re.escape(w) + r"(?=[\s,.:;!?()'\"-]|ों|ें|$)") for w in cfg["words"]]
    have = {i["url"]: i for i in old if isinstance(i, dict) and i.get("url")}
    opened = 0
    for feed in cfg["feeds"]:
        raw, err = get(feed, "application/rss+xml,application/xml,text/xml,*/*")
        if raw is None:
            rep["news_failed"].append(f"{feed} ({err})")
            continue
        opened += 1
        for block in re.findall(r"<item>(.*?)</item>", raw, re.S):
            def tag(n):
                m = re.search(rf"<{n}>(.*?)</{n}>", block, re.S)
                return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m[1])).strip() if m else ""
            title, link = " ".join(tag("title").split()), re.sub(r"^http://", "https://", tag("link"))
            if not title or not allowed(link) or link in have:
                continue
            low = " " + title.lower() + " "
            if not any(re.search(w, low) for w in words):
                continue
            lang = "hi" if re.search("[ऀ-ॿ]", title) else "en"
            have[link] = {"title": title, "url": link, "lang": lang, "date": TODAY_S}
            rep["news_new"] += 1
    keep_from = (TODAY - timedelta(days=cfg["days"])).isoformat()
    items = sorted((i for i in have.values() if i.get("date", "") >= keep_from), key=lambda i: i["date"], reverse=True)[:cfg["keep"]]
    if opened or not os.path.exists(path):
        jsave(path, {"v": 1, "updated": TODAY_S, "source": cfg["source"], "items": items})
    rep["news_total"] = len(items)


# ---------------------------------------------------------------- i-Khedut notices (Gujarat agriculture department)

def ikhedut(d, rep, state):
    """
    The notices of the i-Khedut home page come from a public JSON file of the department (the same file the site itself loads).
    Kept as published (title + date); dates in a title ("તા:૧૬/૧૦/૨૦૨૬ થી તા:૩૦/૧૦/૨૦૨૬") give the application window.
    """
    cfg = CONFIG["ikhedut"]
    path = os.path.join(d, "ikhedut.json")
    old = {i["id"]: i for i in jload(path, {"items": []}).get("items", []) if isinstance(i, dict) and i.get("id")}
    raw, err = get(cfg["data"], "application/json,*/*")
    items = None
    if raw is not None:
        try:
            items = json.loads(raw)["data"]["News"]
            if not isinstance(items, list) or not items:
                raise ValueError("News is empty or not a list")
        except (ValueError, KeyError, TypeError) as e:
            err = f"unexpected file: {e}"[:200]
            items = None
    if items is None:
        rep["ikhedut_failed"] = err or "?"
        state["ikhedut_fails"] = state.get("ikhedut_fails", 0) + 1
        rep["ikhedut_total"] = len(old)
        return
    state.pop("ikhedut_fails", None)
    out = {}
    for n in items:
        if not isinstance(n, dict):
            continue
        title = " ".join(str(n.get("newstitle") or "").split())
        if not title:
            continue
        nid = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
        if nid in out:
            continue          # the same notice in two languages of the site
        try:
            pub = datetime.strptime(str(n.get("publisheddate", "")).strip(), "%d %b, %Y").date().isoformat()
        except ValueError:
            pub = ""
        dates = sorted(find_dates(title))
        frm = to = None
        if len(dates) >= 2:
            frm, to = dates[0], dates[-1]
        elif len(dates) == 1:
            # One date: the word after it says whether applications start ("થી") or end ("સુધી") then; otherwise unknown.
            m = re.search(r"(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2,4})\s*(\S+)", title.translate(DIGITS))
            word = (m[4] if m else "").lower()
            if word.startswith(("થી", "से", "from")):
                frm = dates[0]
            elif word.startswith(("સુધી", "तक", "till", "upto", "up")):
                to = dates[0]
        opens = any(w in title for w in cfg["applyWords"]) and any(w in title for w in cfg["openWords"])
        topics = [k for k, ws in cfg["topics"].items() if any(w in title for w in ws)]
        # First seen: today for a notice that appears now; on the very first run, its publish date (not "new" for all).
        first = old.get(nid, {}).get("first") or (TODAY_S if old else (pub or TODAY_S))
        out[nid] = {"id": nid, "title": title, "date": pub, "from": frm, "to": to, "apply": opens, "topics": topics, "first": first}
        if nid not in old:
            rep["ikhedut_new"].append(title[:120])
    keep = sorted(out.values(), key=lambda i: (i["date"] or i["first"]), reverse=True)[:cfg["keep"]]
    jsave(path, {"v": 1, "updated": TODAY_S, "url": cfg["site"], "source": cfg["source"], "topics": cfg["topicNames"], "items": keep})
    rep["ikhedut_total"] = len(keep)
    rep["ikhedut_open"] = sum(1 for i in keep if i["apply"] and i["to"] and i["to"] >= TODAY_S)


# ---------------------------------------------------------------- help.json (guides, status links, contacts – written by a person)

def help_file(d, rep, state):
    path = os.path.join(d, "help.json")
    good = os.path.join(d, "last_good", "help.json")
    h = jload(path, None)
    problems = []
    if not isinstance(h, dict):
        problems = ["file is missing or not valid JSON"]
    elif not all(isinstance(h.get(k, []), list) and all(isinstance(x, dict) for x in h.get(k, [])) for k in ("guides", "status", "contacts")):
        problems = ["guides, status and contacts must be lists of objects"]
    elif any(not isinstance(l, dict) for g in h.get("guides", []) for l in (g.get("links") or [])):
        problems = ["guide links must be objects"]
    else:
        ids = set()
        for g in h.get("guides", []):
            gid = g.get("id") if isinstance(g, dict) else None
            if not gid or gid in ids:
                problems.append(f"guide id missing or repeated: {gid}")
                continue
            ids.add(gid)
            for k in ("title", "short"):
                if not txt_ok(g.get(k)):
                    problems.append(f"{gid}: {k} needs gu, hi and en")
            for k in ("steps",):
                if not isinstance(g.get(k), list) or not g[k] or not all(txt_ok(x) for x in g[k]):
                    problems.append(f"{gid}: steps need gu, hi and en")
            if not all(txt_ok(x) for x in g.get("documents", [])):
                problems.append(f"{gid}: documents need gu, hi and en")
        for sec in ("guides", "status", "contacts"):
            for item in h.get(sec, []):
                for l in (item.get("links", []) if sec == "guides" else [item]):
                    u = l.get("url") if isinstance(l, dict) else None
                    if u and not (linkable(u) or _host_in(u, CONFIG["extraLinks"])):
                        problems.append(f"link not official: {u}")
        for sec in ("status", "contacts"):
            seen_ids = set()
            for x in h.get(sec, []):
                if not txt_ok(x.get("title")) or not x.get("id") or x.get("id") in seen_ids:
                    problems.append(f"{sec}: {x.get('id')}: needs a unique id and a title in gu, hi and en")
                seen_ids.add(x.get("id"))
        for c in h.get("contacts", []):
            if c.get("phone") and not re.fullmatch(r"[0-9 \-+]{3,20}", str(c["phone"])):
                problems.append(f"bad phone {c.get('phone')}")
    if problems:
        rep["broken"].append({"file": "help.json", "problems": problems[:20]})
        if os.path.exists(path):
            os.makedirs(os.path.join(d, "last_bad"), exist_ok=True)
            shutil.copyfile(path, os.path.join(d, "last_bad", "help.json"))
        old = jload(good, None)
        if old:
            jsave(path, old)
        return
    jsave(good, h)
    # Official links of the help file must keep opening (status pages move now and then).
    fails = state.setdefault("helpfails", {})
    urls = sorted({l["url"] for g in h.get("guides", []) for l in g.get("links", []) if allowed(l.get("url", ""))} |
                  {x["url"] for x in h.get("status", []) if allowed(x.get("url", ""))})
    for u in urls:
        t, err = get(u)
        if t is None:
            fails[u] = fails.get(u, 0) + 1
            if fails[u] >= FAIL_WARN:
                rep["help_failed"].append(f"{u} ({err}, {fails[u]} run)")
        else:
            fails.pop(u, None)
    for u in list(fails):
        if u not in urls:
            fails.pop(u)


# ---------------------------------------------------------------- outputs

def write_index(d, books):
    states = {k: {"name": v["name"], "file": f"{k}.json"} for k, v in CONFIG["states"].items() if k in books}
    jsave(os.path.join(d, "index.json"), {
        "v": 3, "updated": TODAY_S, "central": "central.json", "states": states,
        "categories": CATEGORIES, "note": CONFIG["note"], "myscheme": CONFIG["myscheme"], "news": "news.json",
        "help": "help.json", "ikhedut": "ikhedut.json",
    })
    gj = books["GJ"]["schemes"] if "GJ" in books else []
    central = [s for s in books.get("central", {}).get("schemes", []) if "GJ" not in s.get("except", [])]
    jsave(os.path.join(d, "schemes.json"), {"v": 1, "updated": TODAY_S, "note": CONFIG["note"], "schemes": central + gj})


def name_of(books, scope, sid):
    for s in books.get(scope, {}).get("schemes", []):
        if s["id"] == sid:
            return s["name"]["gu"]
    return sid


def write_status(d, books, rep, state, mode):
    now = datetime.now(timezone.utc)
    ist = now + timedelta(hours=5, minutes=30)
    total = sum(len(b["schemes"]) for b in books.values())
    ik_down = bool(rep["ikhedut_failed"]) and state.get("ikhedut_fails", 0) >= 2
    need = len(rep["broken"]) + len(rep["changed"]) + len(rep["dates"]) + len(rep["ended"]) + len(rep["failed"]) + \
        len(rep["help_failed"]) + (1 if ik_down else 0)
    ok = not rep["broken"] and rep["checked"] > 0 and len(rep["unreachable"]) == 0
    summary = {
        "run": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": mode, "ok": ok, "needsPerson": need,
        "schemes": {sc: len(b["schemes"]) for sc, b in books.items()}, "news": rep["news_total"],
        "newNews": rep["news_new"], "ikhedut": rep["ikhedut_total"], "ikhedutOpen": rep["ikhedut_open"],
        "ikhedutNew": len(rep["ikhedut_new"]),
    }
    hist = [h for h in (jload(os.path.join(d, "status.json"), {}).get("history") or []) if isinstance(h, dict) and "run" in h][:39]
    jsave(os.path.join(d, "status.json"), {**summary, "report": rep, "history": [summary] + hist})

    G = lambda r: f"**{name_of(books, r['scope'], r['id'])}** (`{r['id']}`, {'central.json' if r['scope'] == 'central' else r['scope'] + '.json'})"
    L = [f"# Vavetra Yojana – સ્થિતિ", "",
         f"**છેલ્લો run:** {ist:%d-%m-%Y %H:%M} (ભારતનો સમય) · **mode:** {mode}", "",
         ("✅ **બધું બરાબર.** તમારે કંઈ કરવાનું નથી." if need == 0 and ok else
          "❌ **Run અધૂરો રહ્યો** (નીચે કારણ). એપમાં જૂની ચકાસેલી માહિતી ચાલુ છે." if need == 0 else
          f"⚠️ **{need} બાબત તમારે જોવાની છે** (નીચે). જ્યાં સુધી તમે સુધારો નહીં ત્યાં સુધી એપમાં જૂની ચકાસેલી માહિતી જ રહે છે."), "",
         f"- યોજના: {total} (" + ", ".join(f"{'કેન્દ્ર' if k == 'central' else k}: {len(v['schemes'])}" for k, v in books.items()) + ")",
         f"- સત્તાવાર પાનાં તપાસ્યાં: {rep['checked']} યોજનાનાં",
         f"- સમાચાર (PIB): {rep['news_total']} ({rep['news_new']} નવા)",
         f"- i-Khedut સૂચના: {rep['ikhedut_total']} (હાલ ખુલ્લાં ફોર્મ: {rep['ikhedut_open']}, નવી: {len(rep['ikhedut_new'])})", ""]
    if rep["ikhedut_new"]:
        L += ["## 🔔 i-Khedut ની નવી સૂચના (ખેડૂતોને notification જશે)", ""] + [f"- {t}" for t in rep["ikhedut_new"]] + [""]
    if ik_down:
        L += ["## 🟠 i-Khedut ની સૂચનાઓ વંચાઈ નહીં", "", f"- {state.get('ikhedut_fails')} run થી: {rep['ikhedut_failed']}",
              "- સાઇટની ફાઈલ બદલાઈ હશે: `yojana_sources.json` → `ikhedut.data` ચકાસો. એપમાં જૂની સૂચનાઓ ચાલુ છે.", ""]
    if rep["help_failed"]:
        L += ["## 🟠 મદદ (help.json) ની સત્તાવાર link ખૂલતી નથી", ""] + [f"- {x}" for x in rep["help_failed"]] + [""]
    if rep["unreachable"]:
        L += ["## ❌ Run અધૂરો", ""] + [f"- {x}" for x in rep["unreachable"]] + [""]
    if rep["broken"]:
        L += ["## 🔴 ફાઈલમાં ભૂલ (એપને જૂની સાચી નકલ મળે છે)", "",
              "તમારો ભૂલવાળો સુધારો `yojana/last_bad/` માં સાચવ્યો છે: ત્યાંથી નકલ કરી, ભૂલ સુધારી, ફરી મૂકો.", ""]
        for b in rep["broken"]:
            L += [f"- **{b['file']}**"] + [f"  - {p}" for p in b["problems"]]
        L += [""]
    if rep["dates"]:
        L += ["## 🔴 આપણી તારીખ સત્તાવાર પાના પર હવે લખેલી નથી", ""]
        for r in rep["dates"]:
            L += [f"- {G(r)}: `{r['field']}` = {r['value']}. પાના પરની આવનારી તારીખો: {', '.join(r['page']) or '—'}. "
                  + " · ".join(f"[પાનું]({u})" for u in r["urls"])]
        L += [""]
    if rep["changed"]:
        L += ["## 🟠 સત્તાવાર પાનું બદલાયું, ચકાસો", "",
              "પાનું ખોલો, ફેરફાર જુઓ, જરૂર હોય તો યોજના સુધારો અને `verified` આજની તારીખ કરો. ફેરફાર કામનો ન હોય તો mode `seen` થી ચલાવો.", ""]
        for r in rep["changed"]:
            L += [f"- {G(r)}: {r['since']} થી. પાના પરની આવનારી તારીખો: {', '.join(r['dates']) or '—'}. "
                  + " · ".join(f"[પાનું]({u})" for u in r["urls"])]
        L += [""]
    if rep["ended"]:
        L += ["## 🟠 છેલ્લી તારીખ પૂરી થઈ (નવો રાઉન્ડ ખૂલ્યો હોય તો તારીખ બદલો)", ""]
        L += [f"- {G(r)}: છેલ્લી તારીખ {r['lastDate']}. પાના પરની આવનારી તારીખો: {', '.join(r['page']) or '—'}" for r in rep["ended"]] + [""]
    if rep["failed"]:
        L += ["## 🟠 સત્તાવાર પાનું ખૂલતું નથી (link બદલાઈ હશે)", ""]
        L += [f"- {G({'scope': next((sc for sc, b in books.items() if any(s['id'] == r['id'] for s in b['schemes'])), 'central'), **r})}: "
              f"{r['runs']} run થી. " + "; ".join(r["pages"]) for r in rep["failed"]] + [""]
    if rep["soon"]:
        L += ["## 🟡 છેલ્લી તારીખ નજીક (એપ આપમેળે બતાવે છે)", ""]
        L += [f"- {G(r)}: {r['lastDate']} ({r['days']} દિવસ બાકી)" for r in rep["soon"]] + [""]
    if rep["stale"]:
        L += [f"## 🟡 {STALE_DAYS} દિવસથી ચકાસી નથી", ""]
        L += [f"- {G(r)}: છેલ્લે ચકાસી {r['verified']}" for r in rep["stale"]] + [""]
    if rep["news_failed"]:
        L += ["## 🟡 સમાચાર feed ખૂલી નહીં (જૂના સમાચાર ચાલુ)", ""] + [f"- {x}" for x in rep["news_failed"]] + [""]
    L += ["## છેલ્લા runs", "", "| સમય (UTC) | બરાબર | જોવાનું | સમાચાર |", "|---|---|---|---|"]
    for h in [summary] + hist[:9]:
        L.append(f"| {str(h.get('run', '')).replace('T', ' ')[:16]} | {'✅' if h.get('ok') else '❌'} | {h.get('needsPerson', '—')} | {h.get('news', '—')} |")
    with open(os.path.join(d, "STATUS.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    return ok


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    d = os.path.join(sys.argv[1], "yojana")
    os.makedirs(d, exist_ok=True)
    mode = (sys.argv[2] if len(sys.argv) > 2 else "auto").strip() or "auto"
    ids = [x.strip() for x in (sys.argv[3] if len(sys.argv) > 3 else "").split(",") if x.strip()]
    state = jload(os.path.join(d, "state.json"), {})
    for old in ("drafts", "REVIEW.md"):          # left over from the earlier AI version
        p = os.path.join(d, old)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)
    for k in ("discovered", "rejected"):
        state.pop(k, None)
    rep = {"broken": [], "changed": [], "dates": [], "ended": [], "failed": [], "soon": [], "stale": [],
           "unreachable": [], "news_failed": [], "news_new": 0, "news_total": 0, "checked": 0, "opened": 0,
           "ikhedut_new": [], "ikhedut_failed": "", "ikhedut_total": 0, "ikhedut_open": 0, "help_failed": []}
    if mode == "seen":
        alerts = state.setdefault("alerts", {})
        for i in (list(alerts) if ids == ["all"] else ids):
            alerts.pop(i, None)
        mode = "seen+auto"
    books = load_books(d, rep)
    if not books:
        rep["unreachable"].append("કોઈ યોજના ફાઈલ વાંચી શકાઈ નહીં")
    watch(books, state, rep)
    if rep["checked"] and not rep["opened"]:
        rep["unreachable"].append("એક પણ સત્તાવાર પાનું ખૂલ્યું નહીં (internet / PC તપાસો)")
    news(d, rep)
    ikhedut(d, rep, state)
    help_file(d, rep, state)
    write_index(d, books)
    jsave(os.path.join(d, "state.json"), state)
    ok = write_status(d, books, rep, state, mode)
    print(open(os.path.join(d, "STATUS.md"), encoding="utf-8").read())
    # 3 = the files were written but the run is not healthy: the workflow still saves STATUS.md, then shows red.
    sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
