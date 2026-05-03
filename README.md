# Linkareer Intern Watcher

Polls Linkareer's intern listing page (Seoul · 체험형 인턴) every hour, captures full posting details including the **homepage apply URL** (홈페이지 지원 링크) and the **JD poster image**, logs everything to `postings.csv`, and sends Telegram notifications with the image as the photo and the apply link in the caption.

Runs entirely on GitHub Actions' free tier — no server needed.

---

## What you get

For every new posting:

- **`postings.csv`** — append-only log with 18 fields per posting:
  - `id, title, organization, organization_type` (대기업/중소기업/스타트업/etc.)
  - `detail_url` — Linkareer detail page
  - **`apply_url`** — the company's external career page (홈페이지 지원), or an email address, or a Linkareer URL (for 간편지원)
  - `apply_type` — `접수페이지` / `간편지원` / `이메일`
  - `homepage_url` — company general site (not the apply page)
  - `recruit_type, recruit_close_at, salary_info, address`
  - `description_image_urls` — all CDN image URLs from the JD (semicolon-separated)
  - `description_image_local` — path to the downloaded primary JD image
  - `logo_url, description_text` (first 1000 chars of plain text), `view_count, discovered_at`
- **`images/{id}.{png|jpg|webp}`** — downloaded JD poster images, committed to the repo
- **`seen_ids.json`** — dedup state so each posting only triggers one notification ever
- **Telegram message per new posting** — the JD image as a photo, with caption containing title / organization / salary / address / deadline / **clickable apply link**

When 5+ new postings appear at once, you get a one-line summary first ("🔔 N개의 새 인턴 공고") then individual messages.

---

## Setup (one-time, ~10 minutes)

### 1. Create a Telegram bot

1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`. Pick a name (e.g. "Linkareer Watcher") and a username ending in `bot` (e.g. `anu_linkareer_bot`).
3. BotFather replies with a **token** like `7891234567:AAH...xyz`. Copy it.

### 2. Get your chat ID

1. Search for **@userinfobot**, start a chat, send anything.
2. It replies with your numeric ID (e.g. `123456789`). Copy it.
3. **Important:** open a chat with the bot you made in step 1 and send it any message (e.g. `hi`). Bots cannot DM you until you message them first.

### 3. Create the GitHub repo

```bash
cd path/to/this/folder
git init
git add .
git commit -m "initial commit"
gh repo create linkareer-watcher --private --source=. --push
```

(Or use the GitHub UI — make it **private**, then `git push`.)

### 4. Add the secrets

Repo on GitHub → **Settings → Secrets and variables → Actions → New repository secret**

| Name                 | Value                            |
| -------------------- | -------------------------------- |
| `TELEGRAM_BOT_TOKEN` | the token from step 1            |
| `TELEGRAM_CHAT_ID`   | your numeric chat ID from step 2 |

### 5. Allow workflow writes

**Settings → Actions → General → Workflow permissions** → select "Read and write permissions" and check "Allow GitHub Actions to create and approve pull requests". Save.

### 6. Trigger the first run

**Actions tab → Linkareer Watcher → Run workflow** → green button.

The first run will:

- Find ~24 current postings
- Fetch each detail page (5 in parallel, ~10–15 seconds total)
- Download ~14 JD images (the rest have HTML-text descriptions)
- Send you a "24 new postings" summary, then 24 individual Telegram messages over ~25 seconds

After that, hourly runs will only notify on truly new postings.

---

## How it works

1. **Listing**. Fetches the same Linkareer URL your browser uses. The page is a Next.js app that embeds the listing data inside `__NEXT_DATA__` as Apollo cache state — we parse that JSON instead of the rendered HTML, so CSS class changes won't break us.
2. **Diff**. Compares posting IDs against `seen_ids.json`. Only new IDs proceed.
3. **Detail fetch**. Concurrently (5 at a time) fetches each new posting's detail page and pulls: apply URL, deadline, salary, address, description text, embedded image URLs.
4. **Image download**. Downloads the first non-template image from each JD's description into `./images/{id}.{ext}`. Caps at ~9.5MB to stay under Telegram's 10MB photo limit.
5. **Persistence**. Appends to CSV, updates `seen_ids.json`.
6. **Notify**. Sends one Telegram message per new posting — photo + HTML caption with clickable apply link.
7. **Commit**. Workflow commits CSV, seen IDs, and new images back to the repo so state persists.

---

## Apply URL behavior (the part you asked about)

Linkareer postings have three application types:

| Type        | What it means                            | What `apply_url` contains                          |
| ----------- | ---------------------------------------- | -------------------------------------------------- |
| `접수페이지` | 홈페이지 지원 — apply on company's site  | The external URL (career.stclab.com, ...)          |
| `이메일`     | Email the recruiter                      | An email address (becomes a mailto: link)          |
| `간편지원`   | Apply directly on Linkareer              | `https://linkareer.com/activity/{id}`              |

Real distribution from a recent run: 19 × 접수페이지, 4 × 이메일, 1 × 간편지원. So the vast majority give you a direct link to the company's career page — exactly what you wanted.

---

## Configuration

Edit `LISTING_URL` at the top of `scraper.py` to change the filter (e.g. add 채용연계형 인턴, change region to Busan).

Edit the cron in `.github/workflows/scrape.yml` to change frequency:

```yaml
- cron: "17 * * * *"      # every hour (current)
- cron: "*/30 * * * *"    # every 30 min
- cron: "17 9-21 * * *"   # only between 9am-9pm UTC
```

---

## Repo size considerations

Each posting's image is typically 200KB–1.5MB. At ~3 new postings/day average, you're adding ~3MB/day to the repo. After a year that's ~1GB, which git handles fine but is getting chunky. Two options when you want to manage it:

- **Periodic cleanup**: delete `images/` older than 90 days (postings are usually closed by then).
- **External storage**: change `download_image()` to upload to S3/R2/Cloudflare Images and store only the URL.

Don't worry about this for the first year.

---

## Troubleshooting

**"Workflow shows zero postings parsed"**
Linkareer changed their page structure. The workflow uploads `debug_last_page.html` as an artifact on failure (Actions tab → failed run → Artifacts). Open it, find `__NEXT_DATA__`, see what changed.

**"Telegram messages don't include images"**
Image download might have failed (large file, CDN hiccup) or the posting genuinely has no JD image (HTML-text description). The script falls back to text-only messages automatically. Check the workflow log for `[warn] image download failed` lines.

**"sendPhoto fails but text works"**
Telegram occasionally rejects images >10MB or with weird encodings. The script catches this and falls back to a text message with the apply link. You can manually fetch the image from `images/{id}.{ext}` in the repo.

**"403 / 429 errors from Linkareer"**
You got rate-limited. Hourly polling should be safe but if you bumped frequency, back off to every 2-4 hours and wait a day before resuming.

---

## Etiquette

This script:
- Polls once per hour (well below any reasonable rate limit)
- Identifies as a real browser via rotating User-Agents
- Adds random jitter so hourly runs don't hit on the exact same second
- Does not log in, scrape behind auth, or bypass paywalls
- Caches via the `seen_ids.json` dedup so detail pages are fetched at most once per posting

If Linkareer ever publishes an official API or asks you to stop, switch to that or back off. Don't share the CSV publicly — the data is theirs.
