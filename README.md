# LinkedIn Jobs - job scraper

*[Русская версия](README.ru.md)*

Django admin + PostgreSQL in Docker. Collects job postings from LinkedIn's public
(guest) pages by search query, stores them together with company profiles, and
exports to Excel/CSV.

## Running it

Docker is the only requirement. On a clean machine:

```powershell
.\build.ps1
```

```bash
./build.sh
```

The script handles everything itself:

1. waits for the Docker engine (up to 4 minutes if it is still starting);
2. creates `.env` from `.env.example` if missing;
3. reads base images straight out of `Dockerfile` and `docker-compose.yml` and
   pulls them **with retries** - right after Docker Desktop starts, its internal
   proxy times out Docker Hub requests for a while and a plain build dies at the
   `load metadata for ...` step;
4. builds the image and brings both containers up, retrying on failure;
5. waits for the admin to answer on `/healthz`.

The script does not start Docker for you - if the engine is down it waits and
tells you. `build.ps1` also runs under `pwsh` on Linux, so nothing here is tied
to Windows.

| | |
|---|---|
| Admin | http://localhost:8000/admin/ |
| Login | `admin` |
| Password | `admin` |

Credentials come from `.env` (`DJANGO_SUPERUSER_USERNAME` /
`DJANGO_SUPERUSER_PASSWORD`) and are created on first start. To change the
password of an existing user, set `DJANGO_SUPERUSER_FORCE_PASSWORD=1` and
restart.

Other script modes:

```powershell
.\build.ps1 -Logs      # logs
.\build.ps1 -Down      # stop (database is kept)
.\build.ps1 -Reset     # wipe the database and start over
.\build.ps1 -Rebuild   # rebuild without cache
```

## What is inside

Two containers:

* `linkedin_jobs_web` - Django + gunicorn, built from `Dockerfile`
* `linkedin_jobs_db` - `postgres:16-alpine`, data in the `pgdata` volume

## Admin sections

### Search queries

What to look for and where. You can create as many queries as you like, with
different countries, keywords and filters.

* **Country** - every country; LinkedIn resolves a location by its name, no
  geoId needed.
* **geoId** - optional override when you need a specific city or region.
* **Max list requests per run** - the total request budget for the whole run,
  including every split task. 1 request = 10 jobs. **0 means no limit.**
* **Timeout** and **delay between requests** - configured per query.
* **Fetch descriptions / company profiles** - turn off if you only want a fast
  list.
* **Max company profiles per run** - `0` for no limit. LinkedIn rate-limits
  company pages harder than anything else and this is the slowest part of a
  crawl; set a small number when the jobs matter more than the profiles.
  Company names and links are always collected - the limit only applies to full
  profiles.

The **▶ Run** button in each row starts the crawl in the background. The same is
available as a bulk action for several queries at once.

The **⤓ Backfill missing descriptions and companies** action picks up whatever
was left after a stopped run, without walking the list again.

Processing is **page by page**: fetch a page (10 cards) → immediately fetch the
descriptions of those same jobs → then their company profiles → and only then
move on to the next page. That way stopping at any moment leaves fully processed
pages behind instead of a pile of jobs with no descriptions.

Descriptions come before companies within a page on purpose: they are the
primary data and the job endpoint is barely rate-limited, whereas LinkedIn holds
back company pages regularly.

### Vacancies

Collected jobs: title, company, location, "reposted N ago", applicant count,
full description, and a **processing status** (`list only` → `description
loaded` / `fetch failed`).

* Search by title, company, location, **description text** and job ID.
* Clicking a company name in the Company column opens that company's **other
  jobs**; the job page itself has a company profile block with a link to all of
  its jobs.
* Filters: status, has description, reposted, query country, the query itself,
  employment type, seniority level, posting date.

Bulk actions:

* **⬇ Export to Excel (.xlsx)** and **⬇ Export to CSV** - 27 columns, including
  industry, size, headquarters, website, follower count and company description.
  CSV is written as `utf-8-sig` with `;` separators so Excel opens it on a
  double click without any dancing.
* **↻ Re-fetch descriptions** - retry failed records.

### Crawl tasks

Getting past the 1000-result ceiling. LinkedIn returns at most 1000 results per
parameter combination: `start=0..990` works, `start=1000` answers HTTP 400. To
collect more, a query is split into sub-tasks and each one gets its own
thousand.

A task is one combination of filters. The task tree *is* "where we left off":
statuses live in the database, so stopping preserves the frontier and resuming
is simply working through the queue further.

Split axes, in order:

1. **By location** - the main axis. Cities are taken **from the jobs that task
   itself already found**, not from a hardcoded list: LinkedIn resolves a
   location string, and the format it hands out is guaranteed to resolve back.
2. **By date** - `past month` → `past week` → `past 24 hours`. The windows are
   nested rather than disjoint (`f_TPR` means "within the last N seconds" from
   now), so this is a way to reach the fresh layer that got pushed past the
   thousandth result in the combined listing.
3. **By refinement keywords** - from the "split keywords" field
   (`django, fastapi, senior`). Empty means the axis is unused.

The axes do **not** have to be disjoint: deduplication by `job_id` makes repeat
passes harmless, so a sloppy split costs extra requests but never corrupts data.

The **Pages done** column doubles as a position in the listing: an interrupted
task resumes from there rather than from the start. Otherwise a task on a small
budget would reread the first pages forever and never reach any depth. The
**↻ Requeue** action resets the position to zero.

Unexplored tasks (position 0) are served first, started ones after - otherwise
the root task with its thousand results would eat every run's budget and
sub-tasks would never get their turn.

### Companies and Parse runs

Reference sections: company profiles (with job counts and links to them) and run
history with counters, a log and a **⏹ Stop** button.

## How the scraper works

Guest endpoints only, **no account cookies**:

```
/jobs-guest/jobs/api/seeMoreJobPostings/search   card list (10 per request)
/jobs-guest/jobs/api/jobPosting/{job_id}         full description
/company/{slug}/                                 company profile (cached in DB)
```

This is deliberate: automated requests from a logged-in LinkedIn account get it
banned, while guest pages are not tied to any profile.

* **Deduplication** - by `job_id` (unique index). Meeting a job again updates
  the record and a "times seen" counter instead of creating duplicates.
  Companies dedupe by slug and their profiles are fetched once.
* **Stopping** - a flag in the database that the worker thread re-reads before
  every HTTP request. Works from any browser tab and any gunicorn worker.
* **Errors** - retries with exponential backoff, with separate handling for 429
  (honouring `Retry-After`), 999 and 5xx. One broken card or page does not kill
  a run: the error goes to the log and a counter and work continues. A run
  interrupted by a container restart is marked as interrupted and can be
  started again.

### LinkedIn's limits

Measured by comparing `job_id` sets in the responses, not assumed:

* **1000 results per parameter combination.** `start=0..990` returns data,
  `start=1000` returns HTTP 400. Worked around by splitting (see *Crawl tasks*).
* **Only `location` and `f_TPR` actually filter.** Location filters for real:
  Kyiv and Kharkiv share no results at all. Posting date works too.
* **`f_WT` (workplace type), `f_E` (experience level) and `f_JT` (job type) are
  ignored.** The guest endpoint accepts them silently and returns the exact same
  listing - a 10-out-of-10 intersection for every value. That is why those
  fields are absent from the admin: there is nothing to split "remote / onsite /
  hybrid" by.
* **A bare city name does not always resolve**: `location=Lviv` returns 0
  results while `location=Lviv, Lviv, Ukraine` returns a full page. That is why
  split locations are taken verbatim from the `location` field of collected
  jobs.
* **An empty page does not mean the end of the listing**: occasionally an empty
  body (26 bytes) arrives mid-listing, and a repeat request at the same offset
  returns all 10 cards. So an empty page is re-checked once.
* Do not set the delay below 1 second - you will get a 429.
* Company pages are rate-limited harder than the rest: 999 shows up regularly.
* Automated access formally violates LinkedIn's terms of service even for public
  pages. For a personal job search or a pet project that is the usual grey area;
  a product needs a properly licensed data source.

## Command line

```bash
docker compose exec web python manage.py run_search --list
docker compose exec web python manage.py run_search --id 1
docker compose exec web python manage.py run_search --id 1 --backfill
docker compose exec web python manage.py run_search --all-active
```

The last one is convenient to put in cron or Task Scheduler for a daily crawl -
combined with the `past 24 hours` date filter it never hits the 1000-result
ceiling at all.
