"""One-off probe: find the real job board for companies scan.py couldn't resolve.

Fetches each company's careers pages, looks for links to a known applicant
tracking system, and tries likely board names on the public ATS APIs.
Prints a JSON summary. Not part of the daily scan.
"""
import json, re, sys, urllib.request, urllib.error, urllib.parse

UA = "Mozilla/5.0 (X11; Linux x86_64) internship-feed-probe/1.0 (+https://github.com/WhiteTiger-y/internship-feed)"

def get(url, body=None, accept="*/*"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": accept, **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(400000).decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, "", url
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", url

PATTERNS = {
    "greenhouse": r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)",
    "greenhouse_js": r"greenhouse\.io/embed/job_board/js\?for=([A-Za-z0-9_-]+)",
    "ashby": r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)",
    "lever": r"jobs\.lever\.co/([A-Za-z0-9_-]+)",
    "workable": r"apply\.workable\.com/([A-Za-z0-9_-]+)",
    "smartrecruiters": r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
    "workday": r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)",
    "icims": r"([a-z0-9-]+)\.icims\.com",
    "eightfold": r"([a-z0-9-]+)\.eightfold\.ai",
    "jobvite": r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)",
    "rippling": r"ats\.rippling\.com/([A-Za-z0-9_-]+)",
    "bamboohr": r"([a-z0-9-]+)\.bamboohr\.com",
    "teamtailor": r"([a-z0-9-]+)\.teamtailor\.com",
}
SKIP = {"embed", "js", "v1", "api", "jobs", "job", "en-US", "en", "static", "assets"}

def scan_html(html):
    found = {}
    for ats, pat in PATTERNS.items():
        for m in re.findall(pat, html):
            v = "/".join(m) if isinstance(m, tuple) else m
            if v.split("/")[0] in SKIP: continue
            found.setdefault(ats.replace("_js", ""), set()).add(v)
    return {k: sorted(v)[:5] for k, v in found.items()}

def try_slug(ats, s):
    try:
        return _try_slug(ats, s)
    except Exception as e:
        return f"err {type(e).__name__}"

def _try_slug(ats, s):
    if ats == "greenhouse":
        c, b, _ = get(f"https://boards-api.greenhouse.io/v1/boards/{s}/jobs", accept="application/json")
        return len(json.loads(b).get("jobs", [])) if c == 200 else None
    if ats == "ashby":
        c, b, _ = get(f"https://api.ashbyhq.com/posting-api/job-board/{s}", accept="application/json")
        return len(json.loads(b).get("jobs", [])) if c == 200 else None
    if ats == "lever":
        c, b, _ = get(f"https://api.lever.co/v0/postings/{s}?mode=json", accept="application/json")
        return len(json.loads(b)) if c == 200 and b.startswith("[") else None
    if ats == "workable":
        c, b, _ = get(f"https://apply.workable.com/api/v1/widget/accounts/{s}", accept="application/json")
        return len(json.loads(b).get("jobs", [])) if c == 200 else None
    if ats == "smartrecruiters":
        c, b, _ = get(f"https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1", accept="application/json")
        return json.loads(b).get("totalFound") if c == 200 else None

def try_workday(host_tenant_site):
    try:
        return _try_workday(host_tenant_site)
    except Exception as e:
        return f"err {type(e).__name__}"

def _try_workday(host_tenant_site):
    tenant, wd, site = host_tenant_site.split("/")
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    c, b, _ = get(url, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "intern"}, "application/json")
    return json.loads(b).get("total") if c == 200 else None

def probe(name, domains, slugs):
    res = {"name": name, "pages": {}, "links": {}, "slugs": {}}
    for d in domains:
        for path in (d,) if d.startswith("http") else (f"https://{d}/careers", f"https://{d}/careers/", f"https://{d}/jobs",
                                                        f"https://{d}/company/careers", f"https://careers.{d}/", f"https://{d}/"):
            c, html, final = get(path)
            res["pages"][path] = c if c != 200 else f"200 -> {final}"
            if c == 200:
                for k, v in scan_html(html).items():
                    res["links"].setdefault(k, [])
                    res["links"][k] = sorted(set(res["links"][k]) | set(v))[:6]
    for k, vals in res["links"].items():
        for v in vals:
            if k == "workday":
                res["slugs"][f"workday:{v}"] = try_workday(v)
            elif k in ("greenhouse", "ashby", "lever", "workable", "smartrecruiters"):
                res["slugs"][f"{k}:{v}"] = try_slug(k, v)
    for s in slugs:
        for ats in ("greenhouse", "ashby", "lever", "workable"):
            key = f"{ats}:{s}"
            if key not in res["slugs"]:
                n = try_slug(ats, s)
                if n is not None: res["slugs"][key] = n
    res["pages"] = {k: v for k, v in res["pages"].items() if v not in (404, 0)}
    return res

def microsoft():
    out = {}
    tests = {
        "gcs": "https://gcsservices.careers.microsoft.com/search/api/v1/search?q=intern&lc=United%20States&l=en_us&pg=1&pgSz=20&o=Recent",
        "eightfold_v2": "https://apply.careers.microsoft.com/api/apply/v2/jobs?domain=microsoft.com&query=intern&location=United%20States&start=0&num=10&sort_by=timestamp",
        "pcsx": "https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com&query=intern&location=United%20States&start=0",
        "careers_page": "https://careers.microsoft.com/v2/global/en/search?q=intern",
        "jobs_page": "https://jobs.careers.microsoft.com/global/en/search?q=intern",
    }
    for k, u in tests.items():
        c, b, final = get(u, accept="application/json")
        info = {"status": c, "final": final}
        try:
            j = json.loads(b)
            info["keys"] = list(j.keys())[:12] if isinstance(j, dict) else f"list[{len(j)}]"
            data = j.get("data") if isinstance(j, dict) else None
            pos = (j.get("positions") if isinstance(j, dict) else None) or ((data or {}).get("positions") if isinstance(data, dict) else None)
            if pos:
                info["count"] = j.get("count") or (data or {}).get("count")
                info["sample"] = {kk: pos[0].get(kk) for kk in list(pos[0].keys())[:25]}
        except Exception:
            info["head"] = b[:300]
        out[k] = info
    return out

if __name__ == "__main__":
    targets = json.load(open(sys.argv[1]))
    results = []
    for t in targets:
        try:
            results.append(probe(t["name"], t.get("domains", []), t.get("slugs", [])))
        except Exception as e:
            results.append({"name": t["name"], "error": f"{type(e).__name__}: {e}"})
    try:
        ms = microsoft()
    except Exception as e:
        ms = {"error": f"{type(e).__name__}: {e}"}
    with open(sys.argv[2], "w") as f:
        json.dump({"microsoft": ms, "companies": results}, f, indent=1, default=str)
