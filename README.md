# internship-feed

A daily scan of public company career boards for **Summer internships in ML, computer vision, research and data**, built to feed my internship tracker.

## What it scans

- **Greenhouse, Ashby and Lever boards.** These are auto-detected from the company name in `companies.json`, and `slugs` gives hints.
- **Workday career sites** (NVIDIA, Adobe, Salesforce, Intel and others), using the same JSON the public site loads.
- **SmartRecruiters**, **Amazon Jobs** and **Microsoft Careers** search.

Only public, unauthenticated job-board endpoints are used. Requests run once a day, with at most one request per host at a time and a short pause between requests.

## Output

| File | Contents |
|---|---|
| `data/careers.json` | Matching roles in the tracker's compact format (`c` company, `t` title, `l` location, `u` link, `p` posted ms, `d` degree, `k` kind, `a` open, `s` source) |
| `data/report.md` | Per-company result from the last run, with failures listed first |
| `data/state.json` | First and last seen dates, plus the board each company resolved to |

Roles that disappear from a board stay in the feed, marked closed, for 30 days.

## Filters

- **Title:** must mention an internship, co-op or student researcher role and an ML / AI / vision / research / data term. Marketing, sales, UX research and similar are excluded, and so are other terms (Fall, Spring, past years) unless the title also mentions Summer 2027.
- **Location:** US or remote.

## Adding a company

Add `{"name": "Company"}` to `companies.json`. Add `"slugs": ["boardname"]` if you know the board name from its careers URL (for example `boards.greenhouse.io/<slug>`, `jobs.ashbyhq.com/<slug>` or `jobs.lever.co/<slug>`). The next run picks it up, and `data/report.md` shows whether it was found.

## Running manually

Go to the **Actions** tab, open **Scan career boards** and click **Run workflow**. To run it locally, use `python scan.py`.
