"""Daily internship scanner for public company career boards.

Reads companies.json, queries each company's public job-board API
(Greenhouse, Lever, Ashby, SmartRecruiters, Workday, plus Amazon and
Microsoft search endpoints), keeps Summer internship roles in ML / CV /
research / data, and writes:

  data/careers.json   feed items in the tracker's compact format
  data/state.json     first/last-seen dates and resolved boards (persisted)
  data/report.md      per-company results and failures

Only public, unauthenticated job-board endpoints are used, one request at
a time per host with a short pause, once per day.
"""
import json, re, time, os, sys, threading, datetime as dt
import urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
UA = "internship-feed/1.0 (personal job tracker; +https://github.com/WhiteTiger-y/internship-feed)"
NOW = int(time.time() * 1000)
DAY = 86400 * 1000
KEEP_CLOSED_DAYS = 30
REPROBE_DAYS = 7

# ---------------------------------------------------------------- HTTP ----
_host_locks = {}
_host_last = {}
_glock = threading.Lock()

def _polite(host, gap=0.4):
    with _glock:
        lock = _host_locks.setdefault(host, threading.Lock())
    lock.acquire()
    wait = _host_last.get(host, 0) + gap - time.time()
    if wait > 0:
        time.sleep(wait)
    return lock

def http(url, body=None, timeout=25):
    """GET (or POST JSON when body is given). Returns parsed JSON or None on 404."""
    host = urllib.parse.urlparse(url).netloc
    lock = _polite(host)
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": UA, "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {})})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1)); continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < 2:
                    time.sleep(2 * (attempt + 1)); continue
                raise
    finally:
        _host_last[host] = time.time()
        lock.release()

# ------------------------------------------------------------ filters ----
RE_INTERN = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b|student researcher|\bresearch (intern|fellow)", re.I)
RE_FIELD = re.compile(
    r"machine learning|\bml\b|\bai\b|artificial intelligence|computer vision|\bvision\b|perception|"
    r"research|scientist|deep learning|data scien|robot|autonom|\b3d\b|graphics|imaging|\bnlp\b|"
    r"language model|\bllm|generative|reinforcement|applied science|\bdata\b|analytics|quant", re.I)
RE_EXCLUDE = re.compile(
    r"\bux\b|user research|market(ing)? research|marketing|sales|legal|counsel|accounting|recruit|"
    r"\bhr\b|human resources|people ops|finance intern|tax|audit|communications|journalis|editorial|"
    r"supply chain|procurement|facilities|real estate|clinical research coordinator", re.I)
RE_OTHER_TERM = re.compile(r"\b(fall|spring|winter|autumn)\b|\b202[0-6]\b", re.I)
RE_SUMMER_27 = re.compile(r"summer\s*'?(20)?27|2027", re.I)

RE_RESEARCH = re.compile(r"research|scientist", re.I)
RE_ML = re.compile(r"machine learning|\bml\b|\bai\b|vision|nlp|deep learning|llm|perception|generative|robot|3d|imaging|autonom", re.I)
RE_DATA = re.compile(r"data|analytics|quant", re.I)

US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
             "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
RE_US = re.compile(r"united states|\busa?\b|u\.s\.|remote|anywhere|"
                   r"new york|san francisco|seattle|boston|austin|chicago|los angeles|bay area|"
                   r"mountain view|palo alto|menlo park|sunnyvale|santa clara|san jose|redmond|cambridge, ma|"
                   r"pittsburgh|atlanta|denver|washington|princeton|jersey|philadelphia|san diego|"
                   r"\b(" + "|".join(US_STATES) + r")\b", re.I)
RE_NON_US = re.compile(r"\b(uk|united kingdom|london|england|canada|toronto|montreal|vancouver|india|"
                       r"bengaluru|bangalore|hyderabad|pune|mumbai|delhi|germany|berlin|munich|france|paris|"
                       r"zurich|switzerland|israel|tel aviv|singapore|japan|tokyo|china|beijing|shanghai|"
                       r"shenzhen|korea|seoul|australia|sydney|netherlands|amsterdam|ireland|dublin|poland|"
                       r"warsaw|spain|madrid|barcelona|italy|taiwan|taipei|hong kong|brazil|mexico|emea|apac|"
                       r"europe|sweden|stockholm|denmark|copenhagen|finland|helsinki|czech|prague|romania)\b", re.I)

def relevant(title):
    if not RE_INTERN.search(title) or not RE_FIELD.search(title) or RE_EXCLUDE.search(title):
        return False
    if RE_OTHER_TERM.search(title) and not RE_SUMMER_27.search(title):
        return False
    return True

def in_us(loc):
    if not loc:
        return True
    if RE_US.search(loc):
        return True
    return not RE_NON_US.search(loc)

def kind(t):
    if RE_RESEARCH.search(t): return "research"
    if RE_ML.search(t): return "ml"
    if RE_DATA.search(t): return "data"
    return "other"

def degree(t):
    tl = t.lower()
    if "phd" in tl or "ph.d" in tl or "doctoral" in tl: return "phd"
    if "undergrad" in tl or "bachelor" in tl: return "bs"
    if "master" in tl or re.search(r"\bm\.?s\.?\b", tl) or "graduate" in tl: return "ms"
    return "?"

def iso_ms(s):
    if not s: return 0
    try:
        s = s.replace("Z", "+00:00")
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return 0

def workday_posted(s):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    s = (s or "").lower()
    if "today" in s: return NOW
    if "yesterday" in s: return NOW - DAY
    m = re.search(r"(\d+)\+?\s*day", s)
    return NOW - int(m.group(1)) * DAY if m else 0

# ------------------------------------------------------------ fetchers ----
# Each fetcher returns a list of dicts: {jid, title, url, loc, posted_ms}

def f_greenhouse(tok):
    j = http(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs")
    if j is None: return None
    return [{"jid": x["id"], "title": x.get("title", ""), "url": x.get("absolute_url", ""),
             "loc": (x.get("location") or {}).get("name", ""),
             "posted_ms": iso_ms(x.get("first_published") or x.get("updated_at"))} for x in j.get("jobs", [])]

def f_lever(tok):
    j = http(f"https://api.lever.co/v0/postings/{tok}?mode=json")
    if j is None or not isinstance(j, list): return None
    out = []
    for x in j:
        cat = x.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        out.append({"jid": x["id"], "title": x.get("text", ""), "url": x.get("hostedUrl", ""),
                    "loc": "; ".join(l for l in locs if l), "posted_ms": x.get("createdAt") or 0})
    return out

def f_ashby(tok):
    j = http(f"https://api.ashbyhq.com/posting-api/job-board/{tok}")
    if j is None: return None
    out = []
    for x in j.get("jobs", []):
        if x.get("isListed") is False: continue
        locs = [x.get("location", "")] + [s.get("location", "") for s in x.get("secondaryLocations") or []]
        if x.get("isRemote"): locs.append("Remote")
        out.append({"jid": x.get("id") or x.get("jobUrl"), "title": x.get("title", ""),
                    "url": x.get("jobUrl") or x.get("applyUrl", ""), "loc": "; ".join(l for l in locs if l),
                    "posted_ms": iso_ms(x.get("publishedAt"))})
    return out

def f_smartrecruiters(tok):
    out, off = [], 0
    while off < 1000:
        j = http(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=100&offset={off}&q=intern")
        if j is None: return None if off == 0 else out
        for x in j.get("content", []):
            l = x.get("location") or {}
            loc = ", ".join(v for v in (l.get("city"), l.get("region"), l.get("country")) if v)
            if l.get("remote"): loc += "; Remote"
            out.append({"jid": x["id"], "title": x.get("name", ""),
                        "url": f"https://jobs.smartrecruiters.com/{tok}/{x['id']}", "loc": loc,
                        "posted_ms": iso_ms(x.get("releasedDate"))})
        off += 100
        if off >= j.get("totalFound", 0): break
    return out

def f_workday(cfg):
    host, tenant, site = cfg["host"], cfg["tenant"], cfg["site"]
    base = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, seen = [], set()
    for q in cfg.get("queries", ["intern"]):
        total, off = None, 0
        while off < 400:
            j = http(base, {"appliedFacets": {}, "limit": 20, "offset": off, "searchText": q})
            if j is None: return None if not out else out
            if total is None: total = j.get("total", 0)
            posts = j.get("jobPostings", [])
            for x in posts:
                path = x.get("externalPath", "")
                if not path or path in seen: continue
                seen.add(path)
                out.append({"jid": path.rsplit("_", 1)[-1] if "_" in path else path, "title": x.get("title", ""),
                            "url": f"https://{host}/en-US/{site}{path}", "loc": x.get("locationsText", ""),
                            "posted_ms": workday_posted(x.get("postedOn"))})
            off += 20
            if len(posts) < 20 or off >= total: break
    return out

def f_amazon(cfg):
    out, seen = [], set()
    for q in cfg.get("queries", ["intern"]):
        for off in range(0, 300, 100):
            u = ("https://www.amazon.jobs/en/search.json?" + urllib.parse.urlencode(
                {"base_query": q, "country": "USA", "result_limit": 100, "offset": off, "sort": "recent"}))
            j = http(u)
            if not j: break
            jobs = j.get("jobs", [])
            for x in jobs:
                jid = x.get("id_icims") or x.get("id")
                if jid in seen: continue
                seen.add(jid)
                try:
                    p = int(dt.datetime.strptime(x.get("posted_date", ""), "%B %d, %Y").timestamp() * 1000)
                except Exception:
                    p = 0
                out.append({"jid": jid, "title": x.get("title", ""),
                            "url": "https://www.amazon.jobs" + x.get("job_path", ""),
                            "loc": x.get("normalized_location") or x.get("location", ""), "posted_ms": p})
            if len(jobs) < 100: break
    return out

def f_microsoft(cfg):
    out, seen = [], set()
    for q in cfg.get("queries", ["intern"]):
        for pg in range(1, 6):
            u = ("https://gcsservices.careers.microsoft.com/search/api/v1/search?" + urllib.parse.urlencode(
                {"q": q, "lc": "United States", "l": "en_us", "pg": pg, "pgSz": 20, "o": "Recent"}))
            j = http(u)
            res = ((j or {}).get("operationResult") or {}).get("result") or {}
            jobs = res.get("jobs") or []
            for x in jobs:
                jid = x.get("jobId")
                if jid in seen: continue
                seen.add(jid)
                locs = (x.get("properties") or {}).get("locations") or []
                out.append({"jid": jid, "title": x.get("title", ""),
                            "url": f"https://jobs.careers.microsoft.com/global/en/job/{jid}",
                            "loc": "; ".join(locs), "posted_ms": iso_ms(x.get("postingDate"))})
            if len(jobs) < 20: break
    return out

SIMPLE = {"greenhouse": f_greenhouse, "lever": f_lever, "ashby": f_ashby, "smartrecruiters": f_smartrecruiters}
CUSTOM = {"workday": f_workday, "amazon": f_amazon, "microsoft": f_microsoft}
LABEL = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "smartrecruiters": "SmartRecruiters",
         "workday": "Workday", "amazon": "Amazon Jobs", "microsoft": "Microsoft Careers"}

# ------------------------------------------------------------ resolving ----
def slug_variants(name):
    base = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    words = base.split()
    joined = "".join(words)
    cands = [joined, "-".join(words), joined + "ai", joined + "inc", joined + "labs", joined + "hq"]
    if words and words[-1] in ("ai", "labs", "inc", "technologies", "robotics"):
        cands.append("".join(words[:-1]))
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out

SUFFIXES = set("inc ai labs lab technologies technology co company corp corporation health industries hq the "
               "usa us llc group holdings robotics therapeutics pharmaceuticals innovation network systems "
               "software capital trading partners international global careers jobs".split())

def _core(name):
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    core = [w for w in words if w not in SUFFIXES] or words
    return "".join(core)

def greenhouse_name_ok(tok, name):
    """Guard against common-word tokens that belong to a different company."""
    j = http(f"https://boards-api.greenhouse.io/v1/boards/{tok}")
    if not j or not j.get("name"): return True
    a, b = _core(j["name"]), _core(name)
    return a == b or (a.startswith(b) or b.startswith(a)) and abs(len(a) - len(b)) <= 3

def resolve(company, state):
    """Return (ats, token) for a probe-only company, caching hits and misses."""
    name = company["name"]
    cache = state["resolved"].get(name)
    if cache and (cache.get("ats") or NOW - cache.get("checked", 0) < REPROBE_DAYS * DAY):
        return (cache["ats"], cache["token"]) if cache.get("ats") else None
    hints = company.get("slugs", [])
    order = [(ats, s) for s in hints for ats in ("greenhouse", "ashby", "lever")]
    order += [(ats, s) for s in slug_variants(name) for ats in ("greenhouse", "ashby", "lever")
              if (ats, s) not in order]
    for ats, s in order:
        try:
            jobs = SIMPLE[ats](s)
        except Exception:
            continue
        if jobs is None:
            continue
        if ats == "greenhouse" and not greenhouse_name_ok(s, name):
            continue
        state["resolved"][name] = {"ats": ats, "token": s, "checked": NOW}
        return ats, s
    state["resolved"][name] = {"ats": None, "token": None, "checked": NOW}
    return None

# ---------------------------------------------------------------- main ----
def scan_company(company, state):
    name = company["name"]
    try:
        if any(k in company for k in CUSTOM):
            ats = next(k for k in CUSTOM if k in company)
            raw, src = CUSTOM[ats](company[ats]), ats
        elif any(k in company for k in SIMPLE):
            ats = next(k for k in SIMPLE if k in company)
            raw, src = SIMPLE[ats](company[ats]), ats
            if raw is None:
                return name, "error", f"{LABEL[ats]} board '{company[ats]}' not found", []
        else:
            hit = resolve(company, state)
            if not hit:
                return name, "unresolved", "no public Greenhouse/Ashby/Lever board found", []
            src = hit[0]
            raw = SIMPLE[src](hit[1])
            if raw is None:
                state["resolved"].pop(name, None)
                return name, "error", f"resolved {LABEL[src]} board disappeared", []
    except Exception as e:
        return name, "error", f"{type(e).__name__}: {str(e)[:120]}", []
    raw = raw or []
    items = []
    for r in raw:
        t = r["title"]
        if not relevant(t) or not in_us(r["loc"]):
            continue
        items.append({"id": f"{src}:{name.lower().replace(' ', '-')}:{r['jid']}", "c": name, "t": t,
                      "l": r["loc"] or "Not listed", "u": r["url"], "p": r["posted_ms"],
                      "d": degree(t), "k": kind(t), "a": 1, "s": LABEL[src]})
    return name, "ok", f"{LABEL[src]}: {len(raw)} postings, {len(items)} matching", items

def main():
    os.makedirs(DATA, exist_ok=True)
    companies = json.load(open(os.path.join(ROOT, "companies.json")))["companies"]
    sp = os.path.join(DATA, "state.json")
    state = json.load(open(sp)) if os.path.exists(sp) else {}
    state.setdefault("seen", {}); state.setdefault("resolved", {}); state.setdefault("items", {})

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda c: scan_company(c, state), companies))

    live = {}
    for _, _, _, items in results:
        for it in items:
            live[it["id"]] = it
    for iid, it in live.items():
        s = state["seen"].setdefault(iid, {"first": NOW})
        s["last"] = NOW
        if not it["p"] or it["p"] > NOW:
            it["p"] = s["first"]
        state["items"][iid] = it
    feed = list(live.values())
    for iid, it in list(state["items"].items()):
        if iid in live:
            continue
        last = state["seen"].get(iid, {}).get("last", 0)
        if NOW - last <= KEEP_CLOSED_DAYS * DAY:
            feed.append({**it, "a": 0})
        else:
            state["items"].pop(iid, None)
            state["seen"].pop(iid, None)
    feed.sort(key=lambda r: -r["p"])

    ok = sum(1 for r in results if r[1] == "ok")
    out = {"generatedAt": NOW, "count": len(feed), "active": sum(r["a"] for r in feed),
           "companies": len(companies), "companiesOk": ok, "items": feed}
    json.dump(out, open(os.path.join(DATA, "careers.json"), "w"), separators=(",", ":"))
    json.dump(state, open(sp, "w"), separators=(",", ":"), sort_keys=True)

    lines = [f"# Scan report: {dt.datetime.fromtimestamp(NOW / 1000, dt.timezone.utc):%Y-%m-%d %H:%M} UTC", "",
             f"{len(companies)} companies, {ok} scanned OK, {out['active']} open matching roles, "
             f"{out['count'] - out['active']} recently closed kept.", "",
             "| Company | Status | Detail |", "|---|---|---|"]
    order = {"error": 0, "unresolved": 1, "ok": 2}
    for name, status, detail, _ in sorted(results, key=lambda r: (order[r[1]], r[0].lower())):
        lines.append(f"| {name} | {status} | {detail} |")
    open(os.path.join(DATA, "report.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines[:4]))
    bad = [r for r in results if r[1] != "ok"]
    for name, status, detail, _ in bad:
        print(f"  {status:10s} {name}: {detail}")

if __name__ == "__main__":
    main()
