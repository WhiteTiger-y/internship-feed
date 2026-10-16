# internship-feed

A daily scan of public company career boards for **Summer internships in ML, computer vision, research and data**, built to feed my internship tracker.

## What it scans

- **Greenhouse, Ashby and Lever boards.** These are auto-detected from the company name in `companies.json`, and `slugs` gives hints.
- **Workday career sites** (NVIDIA, Adobe, Salesforce, Intel and others), using the same JSON the public site loads.
- **SmartRecruiters**, **Workable**, **Amazon Jobs** and **Microsoft Careers** search.

Only public, unauthenticated job-board endpoints are used. Requests run once a day, with at most one request per host at a time and a short pause between requests.

## Output

| File | Contents |
|---|---|
| `data/careers.json` | Matching roles in the tracker's compact format (`c` company, `t` title, `l` location, `u` link, `p` posted ms, `d` degree, `k` kind, `a` open, `s` source) |
| `data/report.md` | Per-company result from the last run, with failures listed first |
| `data/state.json` | First and last seen dates, plus the board each company resolved to |

Roles that disappear from a board stay in the feed, marked closed, for 30 days.

## Job descriptions

For roles that pass the title filter, the scanner reads the job description (from the board's API, or the public job page for SimplifyJobs roles) and tags it with:

- **Skills** from a fixed vocabulary (computer vision, PyTorch, LLMs, medical imaging and so on), stored as short ids in `j`.
- **Flags** in `f`: `cit` (US citizenship or a clearance is required), `exp` (export-control wording), `nsp` (employer says it does not sponsor visas).

Nothing about any resume lives in this repo. The tracker compares the tags with a private skills profile in the browser. SimplifyJobs roles are tagged into `data/tags.json`.

The cleaned description text (up to 7,000 characters) is stored in `data/jd/<key>.txt`, where the key is the first 16 hex characters of the SHA-1 of the role id; `data/jd/index.json` maps role ids to keys. The tracker's daily refresh has Claude read these against a resume kept privately in the tracker (never in this repo) to produce a fit score. Files for roles that are no longer listed are deleted.

## LinkedIn (optional)

When the `APIFY_TOKEN` repository secret is set, the scan also runs the Apify actor [`curious_coder/linkedin-jobs-scraper`](https://apify.com/curious_coder/linkedin-jobs-scraper) on LinkedIn's public job search: US internships posted in the past 24 hours for a handful of ML / vision / research queries. Spending is capped three ways: at most 100 results per run, a $0.30 per-run charge limit sent to Apify, and no run once the month's Apify usage reaches $4.25 (the free plan includes $5 a month). Staffing-agency and data-labelling reposts are dropped, and LinkedIn roles stay in the feed for 30 days after posting. No LinkedIn account is used.

## Filters

- **Title:** must mention an internship, co-op or student researcher role and an ML / AI / vision / research / data term. Marketing, sales, UX research and similar are excluded, and so are other terms (Fall, Spring, past years) unless the title also mentions Summer 2027.
- **Location:** US or remote.

## Adding a company

Add `{"name": "Company"}` to `companies.json`. Add `"slugs": ["boardname"]` if you know the board name from its careers URL (for example `boards.greenhouse.io/<slug>`, `jobs.ashbyhq.com/<slug>` or `jobs.lever.co/<slug>`). The next run picks it up, and `data/report.md` shows whether it was found.

## Running manually

Go to the **Actions** tab, open **Scan career boards** and click **Run workflow**. To run it locally, use `python scan.py`.
