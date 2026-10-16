"""Daily internship scanner for public company career boards.

Reads companies.json, queries each company's public job-board API
(Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Workday, plus Amazon and
Microsoft search endpoints), keeps Summer internship roles in ML / CV /
research / data, and writes:

  data/careers.json   feed items in the tracker's compact format
  data/state.json     first/last-seen dates and resolved boards (persisted)
  data/report.md      per-company results and failures
  data/tags.json      skill tags for SimplifyJobs roles
  data/jd/            plain-text job descriptions (for the tracker's Claude fit score)

Only public, unauthenticated job-board endpoints are used, one request at
a time per host with a short pause, once per day. LinkedIn roles come from an
Apify actor when the APIFY_TOKEN secret is set, with a monthly spending stop.
"""
import json, re, time, os, sys, threading, hashlib, textwrap, html as _html, datetime as dt
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

HOST_GAP = {"apply.careers.microsoft.com": 3.0}  # stricter rate limits

def _polite(host, gap=None):
    gap = HOST_GAP.get(host, 0.4) if gap is None else gap
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
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    return None
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    try:
                        ra = int(e.headers.get("Retry-After") or 0)
                    except ValueError:
                        ra = 0
                    time.sleep(min(60, max(ra, (10 if e.code == 429 else 2) * (attempt + 1)))); continue
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


# ------------------------------------------------------ JD analysis ----
# Skills are tagged from a fixed vocabulary so the tracker can score roles against
# a private profile. Nothing about the user's resume is stored in this repo.
# (id, label, weight, regex): weight 3 = domain, 2 = framework/skill, 1 = general tool.
VOCAB = [
    ("cv", "Computer vision", 3, r"computer vision|image (processing|recognition|understanding|classification|segmentation)|object detection|semantic segmentation|visual perception|video understanding|(?-i:\bML ?/ ?CV\b|\bCV ?/ ?ML\b)"),
    ("mm", "Multimodal / VLM", 3, r"multi-?modal|vision[- ]language|\bvlms?\b|\bmllm"),
    ("med", "Medical imaging", 3, r"medical imaging|radiolog|\bdicom\b|\bmri\b|\bct (scan|imaging)|pathology|biomedical|healthcare|clinical"),
    ("3d", "3D vision", 3, r"3d (vision|reconstruction|perception|scene|geometry)|point clouds?|\bnerf\b|gaussian splatting|\bslam\b|\blidar\b|photogrammetry"),
    ("gen", "Generative models", 3, r"diffusion|generative (ai|model)|\bgans?\b|text-to-image|image generation|video generation"),
    ("llm", "LLMs", 3, r"\bllms?\b|large language models?|foundation models?|fine-?tuning|post-?training|\brlhf\b"),
    ("nlp", "NLP", 3, r"\bnlp\b|natural language|text classification|language understanding"),
    ("rag", "RAG / agents", 3, r"\brag\b|retrieval[- ]augmented|agentic|\bai agents?\b"),
    ("rl", "Reinforcement learning", 3, r"reinforcement learning|\brl\b"),
    ("prob", "Probabilistic ML", 3, r"probabilistic|bayesian|gaussian process|uncertainty (estimation|quantification)|variational"),
    ("ml", "Machine learning", 2, r"machine learning|\bml\b|deep learning|neural networks?"),
    ("robo", "Robotics / autonomy", 3, r"robotic|autonomous (vehicle|driving|systems)|self-driving|perception stack"),
    ("speech", "Speech / audio", 3, r"speech|\basr\b|audio"),
    ("rec", "Recommendation / ranking", 3, r"recommend(ation|er)|ranking|search relevance|\bads\b|advertising"),
    ("ts", "Time series", 2, r"time[- ]series|forecasting"),
    ("data", "Data analysis", 1, r"data (analysis|analytics|pipelines?|engineering)|\betl\b|dashboards?"),
    ("stats", "Statistics", 2, r"statistic|hypothesis test|a/b test|experimentation|causal inference"),
    ("res", "Research / publications", 2, r"publications?|peer-reviewed|neurips|\bicml\b|\bcvpr\b|\biccv\b|\beccv\b|\biclr\b|research (experience|papers?|projects?)"),
    ("pytorch", "PyTorch", 2, r"pytorch|\btorch\b"),
    ("tf", "TensorFlow", 2, r"tensorflow|keras"),
    ("jax", "JAX", 2, r"\bjax\b"),
    ("hf", "Hugging Face", 2, r"hugging ?face"),
    ("sklearn", "scikit-learn", 1, r"scikit-?learn|sklearn"),
    ("opencv", "OpenCV", 2, r"opencv"),
    ("cuda", "CUDA / GPU", 2, r"\bcuda\b|gpu (programming|kernels?|optimi[sz]ation)|tensorrt|triton|\bnccl\b"),
    ("dist", "Distributed training", 2, r"distributed (training|systems)|deepspeed|\bfsdp\b|megatron"),
    ("mlops", "MLOps / deployment", 2, r"mlops|model (serving|deployment)|inference (optimi[sz]ation|latency)|\bvllm\b|\bonnx\b"),
    ("python", "Python", 1, r"\bpython\b"),
    ("cpp", "C++", 1, r"c\+\+"),
    ("java", "Java", 1, r"\bjava\b(?!script)"),
    ("js", "JavaScript / TypeScript", 1, r"javascript|typescript|\breact\b|node\.?js"),
    ("sql", "SQL", 1, r"\bsql\b"),
    ("docker", "Docker / Kubernetes", 1, r"docker|kubernetes|\bk8s\b"),
    ("linux", "Linux", 1, r"\blinux\b|\bunix\b"),
    ("git", "Git", 1, r"\bgit\b|github"),
    ("cloud", "Cloud (AWS/GCP/Azure)", 1, r"\baws\b|\bgcp\b|google cloud|\bazure\b|amazon web services"),
    ("spark", "Spark / big data", 1, r"\bspark\b|hadoop|\bhive\b|big data|databricks"),
    ("dsa", "Data structures & algorithms", 1, r"data structures|algorithms"),
]
_VC = [(i, re.compile(rx, re.I)) for i, _, _, rx in VOCAB]
VOCAB_OUT = [[i, lbl, w] for i, lbl, w, _ in VOCAB]

# Hard stop: the role requires US citizenship / a US-person status / a clearance.
RE_CIT = re.compile(
    r"(must|need to|required to|have to)\s+be\s+(a\s+)?(u\.?s\.?|united states)\s+(citizen|person)|"
    r"(u\.?s\.?|united states)\s+citizen(ship)?\s+(status\s+)?(is\s+|are\s+)?(required|needed|mandatory|necessary)|"
    r"requires?\s+(u\.?s\.?|united states)\s+citizen|(u\.?s\.?|united states)\s+citizens?\s+only|"
    r"only\s+(u\.?s\.?|united states)\s+citizens|citizenship\s+(is\s+)?required|"
    r"(top[- ]secret|ts/sci|secret|dod|public trust)\s+(security\s+)?clearance|"
    r"(security\s+)?clearance\s+(is\s+)?(required|needed)|"
    r"ability to obtain (and maintain )?(a |an )?(u\.?s\.? )?(government )?(security )?clearance", re.I)
# Soft caution: export-control wording (often boilerplate, sometimes a real restriction).
RE_EXPORT = re.compile(r"export[- ]control|\bitar\b|export administration|deemed export", re.I)
# Soft caution: employer says it will not sponsor visas.
RE_NOSPON = re.compile(
    r"(will|does|do|can|could)\s*(not|n't)\s+(provide\s+|offer\s+)?(visa\s+|work\s+)?sponsor|"
    r"unable to (provide\s+)?(visa\s+)?sponsor|not (able|eligible|available) (to|for) (visa )?sponsor|"
    r"without (requiring |the need for |needing )?(visa |employer |current or future )?sponsorship|"
    r"no (visa )?sponsorship|cannot (provide )?(visa )?sponsor", re.I)
RE_PHD_ONLY = re.compile(r"(currently|actively)\s+(enrolled in|pursuing|working toward)\s+(a\s+)?(ph\.?d|doctoral|doctorate)|ph\.?d\.?\s+(student|candidate)s?|(enrolled|pursuing)\s+(in\s+)?(a\s+)?(ph\.?d|doctorate|doctoral)", re.I)

def clean(s):
    """HTML (possibly entity-escaped) -> plain text."""
    if not s:
        return ""
    s = _html.unescape(str(s))
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", _html.unescape(s)).strip()

def clean_lines(s):
    """HTML or plain text -> readable plain text that keeps paragraph and bullet breaks."""
    if not s:
        return ""
    s = _html.unescape(str(s))
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<\s*li\b[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)<\s*(br|/p|/li|/h[1-6]|/div|/tr|/ul|/ol|p|h[1-6])\b[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    out = []
    for ln in s.splitlines():
        ln = re.sub(r"[ \t\r\f\v ​]+", " ", ln).strip()
        if ln and ln != "-":
            out.extend(textwrap.wrap(ln, 160) or [ln])
    return "\n".join(out)

# Job description text store: data/jd/<key>.txt, key = sha1(role id)[:16], plus data/jd/index.json.
JD_DIR = os.path.join(DATA, "jd")
JD_MAX = 7000

def jd_key(iid):
    return hashlib.sha1(iid.encode()).hexdigest()[:16]

def jd_path(iid):
    return os.path.join(JD_DIR, jd_key(iid) + ".txt")

def jd_has(iid):
    return os.path.exists(jd_path(iid))

def jd_save(iid, text):
    os.makedirs(JD_DIR, exist_ok=True)
    t = text[:JD_MAX]
    p = jd_path(iid)
    if not os.path.exists(p) or open(p, encoding="utf-8").read() != t:
        open(p, "w", encoding="utf-8").write(t)

def jd_index(ids, prune=True):
    """Write index.json for roles that have a stored description; drop files for roles no longer listed."""
    os.makedirs(JD_DIR, exist_ok=True)
    idx = {i: jd_key(i) for i in sorted(ids) if jd_has(i)}
    if prune:
        keep = set(idx.values())
        for fn in os.listdir(JD_DIR):
            if fn.endswith(".txt") and fn[:-4] not in keep:
                os.remove(os.path.join(JD_DIR, fn))
    json.dump(idx, open(os.path.join(JD_DIR, "index.json"), "w"), separators=(",", ":"), sort_keys=True)
    return len(idx)

def jd_degree(text):
    tl = text.lower()
    ms = re.search(r"master|\bm\.?s\.?\b|graduate (student|degree|program)", tl)
    bs = re.search(r"bachelor|undergrad|\bb\.?s\.?\b", tl)
    if RE_PHD_ONLY.search(text) and not ms and not bs:
        return "phd"
    return "ms" if ms else "bs" if bs else "?"

def tag_text(title, text):
    blob = title + " . " + text
    f = []
    if RE_CIT.search(text): f.append("cit")
    if RE_EXPORT.search(text): f.append("exp")
    if RE_NOSPON.search(text): f.append("nsp")
    return {"j": [i for i, rx in _VC if rx.search(blob)], "f": f, "dj": jd_degree(text)}

def _strings(o, out):
    if isinstance(o, str):
        if len(o) > 80: out.append(o)
    elif isinstance(o, dict):
        for v in o.values(): _strings(v, out)
    elif isinstance(o, list):
        for v in o: _strings(v, out)
    return out

# Per-job detail fetchers, used only for roles that already passed the title filter.
def d_greenhouse(tok, jid):
    return (http(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs/{jid}") or {}).get("content")

def d_sr(tok, jid):
    j = http(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings/{jid}") or {}
    return " ".join((s or {}).get("text", "") for s in ((j.get("jobAd") or {}).get("sections") or {}).values())

def d_workday(host, tenant, site, path):
    j = http(f"https://{host}/wday/cxs/{tenant}/{site}{path}") or {}
    return (j.get("jobPostingInfo") or {}).get("jobDescription")

def d_ms(pid):
    j = http("https://apply.careers.microsoft.com/api/pcsx/position_details?" + urllib.parse.urlencode(
        {"position_id": pid, "domain": "microsoft.com", "hl": "en"})) or {}
    t = " ".join(_strings(j.get("data") or {}, []))
    m = RE_MS_TAIL.search(t)   # the response also carries Microsoft's blog feed; drop it
    return t[:m.start()] if m else t

RE_MS_TAIL = re.compile(r"\d{2,3},000 employees empowering|https://blogs\.microsoft\.com")

def tags_for(iid, title, r, state):
    """Tags for one role: reuse what an earlier run stored, else read the job description."""
    prev = state["items"].get(iid) or {}
    cached = ({k: prev[k] for k in ("j", "f") if k in prev} | ({"dj": prev["dj"]} if "dj" in prev else {})) if "j" in prev else {}
    if jd_has(iid):   # re-tag from the stored text so rule changes reach older roles; no network
        return tag_text(title, open(jd_path(iid), encoding="utf-8").read())
    text = r.get("desc")
    if text is None and callable(r.get("detail")):
        try:
            text = r["detail"]()
        except Exception:
            text = None
    text = clean_lines(text)
    if len(text) < 200:
        return cached
    jd_save(iid, text)
    return tag_text(title, text)

# ------------------------------------------------------------ fetchers ----
# Each fetcher returns a list of dicts: {jid, title, url, loc, posted_ms}

def f_greenhouse(tok):
    j = http(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs")
    if j is None: return None
    return [{"jid": x["id"], "title": x.get("title", ""), "url": x.get("absolute_url", ""),
             "loc": (x.get("location") or {}).get("name", ""),
             "posted_ms": iso_ms(x.get("first_published") or x.get("updated_at")),
             "detail": (lambda i=x["id"]: d_greenhouse(tok, i))} for x in j.get("jobs", [])]

def f_lever(tok):
    j = http(f"https://api.lever.co/v0/postings/{tok}?mode=json")
    if j is None or not isinstance(j, list): return None
    out = []
    for x in j:
        cat = x.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        out.append({"jid": x["id"], "title": x.get("text", ""), "url": x.get("hostedUrl", ""),
                    "loc": "; ".join(l for l in locs if l), "posted_ms": x.get("createdAt") or 0,
                    "desc": " ".join([x.get("descriptionPlain") or "",
                                      " ".join((l.get("text", "") + " " + l.get("content", "")) for l in x.get("lists") or []),
                                      x.get("additionalPlain") or ""])})
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
                    "posted_ms": iso_ms(x.get("publishedAt")),
                    "desc": x.get("descriptionPlain") or x.get("descriptionHtml")})
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
                        "posted_ms": iso_ms(x.get("releasedDate")),
                        "detail": (lambda i=x["id"]: d_sr(tok, i))})
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
                            "posted_ms": workday_posted(x.get("postedOn")),
                            "detail": (lambda pth=path: d_workday(host, tenant, site, pth))})
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
                            "loc": x.get("normalized_location") or x.get("location", ""), "posted_ms": p,
                            "desc": " ".join(str(x.get(k) or "") for k in ("description", "basic_qualifications", "preferred_qualifications"))})
            if len(jobs) < 100: break
    return out

def f_microsoft(cfg):
    """Microsoft's Eightfold-based careers search (apply.careers.microsoft.com)."""
    out, seen = [], set()
    for q in cfg.get("queries", ["intern"]):
        start = 0
        while start < 300:
            u = ("https://apply.careers.microsoft.com/api/pcsx/search?" + urllib.parse.urlencode(
                {"domain": "microsoft.com", "query": q, "location": "United States", "start": start}))
            j = http(u) or {}
            data = j.get("data") or {}
            pos = data.get("positions") or []
            for x in pos:
                jid = x.get("displayJobId") or x.get("id")
                if jid in seen: continue
                seen.add(jid)
                path = x.get("positionUrl") or f"/careers/job/{x.get('id')}"
                ts = x.get("postedTs") or x.get("creationTs") or 0
                out.append({"jid": jid, "title": x.get("name", ""),
                            "url": "https://apply.careers.microsoft.com" + path,
                            "loc": "; ".join(x.get("standardizedLocations") or x.get("locations") or []),
                            "posted_ms": int(ts) * 1000 if ts and ts < 10**11 else int(ts or 0),
                            "detail": (lambda i=x.get("id"): d_ms(i))})
            start += len(pos)
            if not pos or start >= (data.get("count") or 0): break
    return out

def f_workable(tok):
    j = http(f"https://apply.workable.com/api/v1/widget/accounts/{tok}")
    if j is None: return None
    out = []
    for x in j.get("jobs", []):
        locs = x.get("locations") or [x]
        loc = "; ".join(", ".join(v for v in (l.get("city"), l.get("region") or l.get("state"), l.get("country")) if v)
                        for l in locs)
        if x.get("telecommuting"): loc += "; Remote"
        sc = x.get("shortcode", "")
        out.append({"jid": sc or x.get("url"), "title": x.get("title", ""),
                    "url": x.get("url") or x.get("shortlink") or f"https://apply.workable.com/{tok}/j/{sc}/",
                    "loc": loc.strip("; "), "posted_ms": iso_ms(x.get("published_on") or x.get("created_at")),
                    "desc": x.get("description")})
    return out

SIMPLE = {"greenhouse": f_greenhouse, "lever": f_lever, "ashby": f_ashby, "smartrecruiters": f_smartrecruiters,
          "workable": f_workable}
CUSTOM = {"workday": f_workday, "amazon": f_amazon, "microsoft": f_microsoft}
LABEL = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "smartrecruiters": "SmartRecruiters",
         "workday": "Workday", "amazon": "Amazon Jobs", "microsoft": "Microsoft Careers", "workable": "Workable"}

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


# --------------------------------------------- SimplifyJobs enrichment ----
SJ_URL = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json"
SJ_ML = {"AI/ML/Data", "Data Science, AI & Machine Learning"}
SJ_SW = {"Software", "Software Engineering"}
SJ_KW = re.compile(r"research|machine learning|\bml\b|\bai\b|computer vision|\bvision\b|nlp|scientist|deep learning|llm|perception|generative", re.I)

def sj_wanted(x):
    """Same selection the tracker's feed build uses for SimplifyJobs roles (open ones only)."""
    if not x.get("is_visible", True) or not x.get("active"): return False
    if not any("Summer" in t and "2027" in t for t in (x.get("terms") or [])): return False
    cat, title = x.get("category"), x.get("title") or ""
    return cat in SJ_ML or (cat in SJ_SW and bool(SJ_KW.search(title)))

def fetch_page(url):
    host = urllib.parse.urlparse(url).netloc
    lock = _polite(host)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(1_200_000).decode("utf-8", "replace")
    except Exception:
        return ""
    finally:
        _host_last[host] = time.time()
        lock.release()

def page_text(raw):
    """Job description text from a public job page: JSON-LD first, embedded JSON next, then visible text."""
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', raw, re.S | re.I):
        try:
            j = json.loads(m.group(1))
        except Exception:
            continue
        for o in (j if isinstance(j, list) else [j]):
            if isinstance(o, dict) and "JobPosting" in str(o.get("@type")):
                t = clean_lines("\n".join(str(o.get(k) or "") for k in ("description", "responsibilities", "qualifications", "skills")))
                if len(t) >= 200:
                    return t
    m = re.search(r'"descriptionHtml":\s*("(?:[^"\\]|\\.)*")', raw)
    if m:
        try:
            t = clean_lines(json.loads(m.group(1)))
            if len(t) >= 200:
                return t
        except Exception:
            pass
    t = clean_lines(re.sub(r"(?is)<(script|style|noscript|nav|footer|header)[^>]*>.*?</\1>", " ", raw))
    return trim_to_jd(t) if len(t) >= 500 and re.search(r"responsibilit|qualification|requirement|what you|you will", t, re.I) else ""

RE_JD_START = re.compile(r"^(about (the|this) (role|team|job|position|opportunity)|job (description|summary|overview)|"
                         r"(key )?responsibilities|what you('|\u2019)?ll do|the role|your role|the opportunity|"
                         r"(position|role) (overview|summary)|overview|team intro(duction)?)\b", re.I)

def trim_to_jd(t):
    """Drop site navigation before the posting: start a few lines above the first heading or full sentence."""
    lines = t.splitlines()
    for i, ln in enumerate(lines):
        if RE_JD_START.search(ln.strip()):
            return "\n".join(lines[max(0, i - 8):])   # keep the title / location block just above
    for i, ln in enumerate(lines):
        if len(ln.split()) >= 12 and re.search(r"[.!?:]$", ln.strip()):
            return "\n".join(lines[max(0, i - 3):])
    return t

def enrich_simplify(budget_s=600):
    tp = os.path.join(DATA, "tags.json")
    old = json.load(open(tp)) if os.path.exists(tp) else {}
    tags, miss = old.get("tags", {}), old.get("miss", {})
    try:
        data = http(SJ_URL, timeout=90)
    except Exception as e:
        print("simplify listings unavailable:", type(e).__name__, str(e)[:100])
        return None
    if not isinstance(data, list) or len(data) < 100:
        print("simplify listings looked wrong; skipping enrichment")
        return None
    wanted = {x["id"]: x for x in data if sj_wanted(x)}
    # New roles, plus already-tagged roles whose description text isn't stored yet.
    todo = [x for i, x in wanted.items() if x.get("url") and (
        (i not in tags and NOW - miss.get(i, 0) > REPROBE_DAYS * DAY) or (i in tags and not jd_has(i) and NOW - miss.get(i, 0) > REPROBE_DAYS * DAY))]
    todo.sort(key=lambda x: x["id"] in tags)  # brand-new roles first
    deadline = time.time() + budget_s
    stats = {"ok": 0, "miss": 0, "kept": 0, "skipped": 0}

    def one(x):
        if time.time() > deadline:
            return x["id"], "skipped", None
        txt = page_text(fetch_page(x["url"]))
        if len(txt) < 300:
            return x["id"], ("kept" if x["id"] in tags else "miss"), None
        jd_save(x["id"], txt)
        tg = tag_text(x.get("title") or "", txt)
        return x["id"], "ok", {"j": tg["j"], "f": tg["f"]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        for iid, st, tg in ex.map(one, todo):
            stats[st] += 1
            if st == "ok": tags[iid] = tg; miss.pop(iid, None)
            elif st in ("miss", "kept"): miss[iid] = NOW
    for i, x in wanted.items():   # re-tag from stored text so rule changes reach older roles
        if jd_has(i):
            tg = tag_text(x.get("title") or "", open(jd_path(i), encoding="utf-8").read())
            tags[i] = {"j": tg["j"], "f": tg["f"]}
    tags = {i: t for i, t in tags.items() if i in wanted}
    miss = {i: t for i, t in miss.items() if i in wanted}
    json.dump({"generatedAt": NOW, "vocab": VOCAB_OUT, "tags": tags, "miss": miss},
              open(tp, "w"), separators=(",", ":"), sort_keys=True)
    return {"wanted": len(wanted), "tagged": len(tags), "no_jd": len(wanted) - len(tags), "ids": set(wanted), **stats}

# ------------------------------------------------- LinkedIn via Apify ----
# Runs only when the APIFY_TOKEN secret is set. Uses LinkedIn's public (logged-out)
# job search through an Apify actor; no LinkedIn account is involved.
APIFY_ACTOR = "curious_coder~linkedin-jobs-scraper"
LI_QUERIES = ["machine learning intern", "computer vision intern", "AI research intern",
              "research scientist intern", "applied scientist intern", "deep learning intern"]
LI_MAX_ITEMS = 100          # results paid for per day (pay-per-result actor)
LI_MAX_CHARGE_USD = 0.30    # per-run hard cap passed to Apify
APIFY_MONTHLY_STOP = 4.25   # skip runs once this month's Apify usage reaches this (free plan: $5)
LI_KEEP_DAYS = 30
LI_SPAM_CO = re.compile(r"lensa|jobs via dice|^dice$|jobright|talentify|outlier|data ?annotation|mercor|"
                        r"crossing hurdles|jobot|actalent|insight global|robert half|teksystems|wiraa|hirenza|"
                        r"patterned learning|ladders|ziprecruiter|cybercoders|motion recruitment|jooble|"
                        r"hiring ?cafe|swooped|remotehunter|joinrs|handshake", re.I)
LI_SPAM_TITLE = re.compile(r"ai trainer|annotat|\btutor\b|\brater\b|evaluator|freelance", re.I)

def apify(method, path, body=None, timeout=60):
    req = urllib.request.Request("https://api.apify.com/v2" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + os.environ.get("APIFY_TOKEN", "").strip(),
                                          "Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def apify_usage():
    d = apify("GET", "/users/me/limits")["data"]
    return float(d["current"]["monthlyUsageUsd"]), float(d["limits"]["maxMonthlyUsageUsd"])

def li_search_url(q):
    # US, posted in the past 24 hours, job type = internship
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(
        {"keywords": q, "location": "United States", "geoId": "103644278", "f_TPR": "r86400", "f_JT": "I"})

def fetch_linkedin():
    """Returns (status dict, raw actor items)."""
    if not os.environ.get("APIFY_TOKEN", "").strip():
        return {"status": "off", "detail": "APIFY_TOKEN secret not set"}, []
    try:
        used, cap = apify_usage()
    except urllib.error.HTTPError as e:
        return {"status": "error", "detail": f"Apify rejected the token (HTTP {e.code})" if e.code in (401, 403)
                else f"Apify usage check failed (HTTP {e.code})"}, []
    except Exception as e:
        return {"status": "error", "detail": f"Apify usage check failed: {type(e).__name__}"}, []
    stop = min(APIFY_MONTHLY_STOP, cap - 0.5)
    if used >= stop:
        return {"status": "paused", "detail": f"Apify usage ${used:.2f} of ${cap:.2f} this month; paused until it resets",
                "usedUsd": round(used, 2), "capUsd": cap}, []
    inp = {"urls": [li_search_url(q) for q in LI_QUERIES], "count": LI_MAX_ITEMS, "scrapeCompany": False}
    qs = urllib.parse.urlencode({"maxItems": LI_MAX_ITEMS, "maxTotalChargeUsd": LI_MAX_CHARGE_USD, "timeout": 900})
    try:
        run = apify("POST", f"/acts/{APIFY_ACTOR}/runs?{qs}", inp)["data"]
        deadline = time.time() + 960
        while run.get("status") in ("READY", "RUNNING") and time.time() < deadline:
            time.sleep(15)
            run = apify("GET", f"/actor-runs/{run['id']}")["data"]
        raw = apify("GET", f"/datasets/{run['defaultDatasetId']}/items?clean=true&format=json&limit=1000", timeout=120)
    except urllib.error.HTTPError as e:
        msg = ""
        try:
            msg = json.loads(e.read().decode())["error"]["message"][:160]
        except Exception:
            pass
        return {"status": "error", "detail": f"Apify run failed (HTTP {e.code}) {msg}".strip()}, []
    except Exception as e:
        return {"status": "error", "detail": f"Apify run failed: {type(e).__name__}: {str(e)[:120]}"}, []
    try:
        used2, _ = apify_usage()
    except Exception:
        used2 = used
    st = {"status": "ok" if run.get("status") == "SUCCEEDED" else "partial",
          "detail": f"run {run.get('status', '?').lower()}, {len(raw)} results",
          "usedUsd": round(used2, 2), "capUsd": cap, "results": len(raw)}
    return st, raw if isinstance(raw, list) else []

def li_posted(x):
    ts = x.get("postedAtTimestamp")
    if isinstance(ts, (int, float)) and ts > 0:
        return int(ts if ts > 1e12 else ts * 1000)
    p = iso_ms(str(x.get("postedAt") or ""))
    return p if 0 < p <= NOW else NOW

def li_merge(raw, state):
    """Fold today's LinkedIn results into the persisted LinkedIn set; returns (items, new_count)."""
    li = state.setdefault("li", {})
    new = 0
    for x in raw:
        jid, title, comp = str(x.get("id") or "").strip(), (x.get("title") or "").strip(), (x.get("companyName") or "").strip()
        if not jid or not title or not comp:
            continue
        if LI_SPAM_CO.search(comp) or LI_SPAM_TITLE.search(title):
            continue
        loc = x.get("location") or ""
        if not relevant(title) or not in_us(loc):
            continue
        iid = "linkedin:" + jid
        item = {"id": iid, "c": comp, "t": title, "l": loc or "Not listed",
                "u": x.get("link") or f"https://www.linkedin.com/jobs/view/{jid}",
                "p": li.get(iid, {}).get("p") or li_posted(x), "d": degree(title), "k": kind(title), "a": 1, "s": "LinkedIn"}
        text = clean_lines(x.get("descriptionText") or x.get("descriptionHtml") or "")
        if len(text) >= 200:
            jd_save(iid, text)
            tg = tag_text(title, text)
            item["j"], item["f"], item["dj"] = tg["j"], tg["f"], tg["dj"]
            if item["d"] == "?" and tg["dj"] != "?":
                item["d"] = tg["dj"]
        if iid not in li:
            new += 1
        li[iid] = item
    for iid, it in list(li.items()):
        if NOW - it["p"] > LI_KEEP_DAYS * DAY:
            li.pop(iid)
    return sorted(li.values(), key=lambda r: -r["p"]), new

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
        iid = f"{src}:{name.lower().replace(' ', '-')}:{r['jid']}"
        tg = tags_for(iid, t, r, state)
        d = degree(t)
        if d == "?" and tg.get("dj") not in (None, "?"):
            d = tg["dj"]
        item = {"id": iid, "c": name, "t": t, "l": r["loc"] or "Not listed", "u": r["url"], "p": r["posted_ms"],
                "d": d, "k": kind(t), "a": 1, "s": LABEL[src]}
        if "j" in tg:
            item["j"], item["f"], item["dj"] = tg["j"], tg["f"], tg.get("dj", "?")
        items.append(item)
    return name, "ok", f"{LABEL[src]}: {len(raw)} postings, {len(items)} matching", items

def main():
    os.makedirs(DATA, exist_ok=True)
    companies = json.load(open(os.path.join(ROOT, "companies.json")))["companies"]
    sp = os.path.join(DATA, "state.json")
    state = json.load(open(sp)) if os.path.exists(sp) else {}
    state.setdefault("seen", {}); state.setdefault("resolved", {}); state.setdefault("items", {})

    # LinkedIn (Apify) runs in the background while the boards are scanned.
    li_box = {}
    li_thread = threading.Thread(target=lambda: li_box.update(r=fetch_linkedin()), daemon=True)
    li_thread.start()

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

    try:
        sj = enrich_simplify()
    except Exception as e:
        sj = None
        print("simplify enrichment failed:", type(e).__name__, str(e)[:120])

    li_thread.join(timeout=1200)
    li_st, li_raw = li_box.get("r") or ({"status": "error", "detail": "LinkedIn fetch did not finish in time"}, [])
    li_items, li_new = li_merge(li_raw, state)
    li_st.update(kept=len(li_items), new=li_new)

    ok = sum(1 for r in results if r[1] == "ok")
    out = {"generatedAt": NOW, "count": len(feed), "active": sum(r["a"] for r in feed),
           "companies": len(companies), "companiesOk": ok, "vocab": VOCAB_OUT, "linkedin": li_st,
           # company-board roles first so they win the tracker's duplicate check over LinkedIn copies
           "items": feed + li_items}
    json.dump(out, open(os.path.join(DATA, "careers.json"), "w"), separators=(",", ":"))
    json.dump(state, open(sp, "w"), separators=(",", ":"), sort_keys=True)
    n_jd = jd_index({r["id"] for r in feed} | {r["id"] for r in li_items} | (sj["ids"] if sj else set()),
                    prune=sj is not None)

    lines = [f"# Scan report: {dt.datetime.fromtimestamp(NOW / 1000, dt.timezone.utc):%Y-%m-%d %H:%M} UTC", "",
             f"{len(companies)} companies, {ok} scanned OK, {out['active']} open matching roles, "
             f"{out['count'] - out['active']} recently closed kept.", "",
             "| Company | Status | Detail |", "|---|---|---|"]
    order = {"error": 0, "unresolved": 1, "ok": 2}
    for name, status, detail, _ in sorted(results, key=lambda r: (order[r[1]], r[0].lower())):
        lines.append(f"| {name} | {status} | {detail} |")
    tagged = sum(1 for r in feed if "j" in r)
    li_line = {"off": "LinkedIn: off (add the APIFY_TOKEN secret to turn it on).",
               "paused": f"LinkedIn: paused. {li_st.get('detail', '')}.",
               "error": f"LinkedIn: error. {li_st.get('detail', '')}."}.get(
        li_st["status"], f"LinkedIn via Apify: {li_st.get('detail', '')}; {li_st['new']} new roles kept, "
                         f"{li_st['kept']} LinkedIn roles in the feed (last {LI_KEEP_DAYS} days); "
                         f"Apify usage ${li_st.get('usedUsd', 0):.2f} of ${li_st.get('capUsd', 5):.2f} this month.")
    lines[4:4] = [f"Job descriptions read: {tagged} of {len(feed)} company-board roles"
                      + (f"; SimplifyJobs roles {sj['tagged']} of {sj['wanted']} ({sj['no_jd']} pages had no readable description)." if sj else "; SimplifyJobs enrichment unavailable this run."),
                  "", li_line, "", f"Description texts stored for Claude fit scoring: {n_jd}.", ""]
    open(os.path.join(DATA, "report.md"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines[:4]))
    bad = [r for r in results if r[1] != "ok"]
    for name, status, detail, _ in bad:
        print(f"  {status:10s} {name}: {detail}")

if __name__ == "__main__":
    main()
