"""
Linkareer intern posting watcher.

Pipeline:
1. Fetch the filtered listing page (Seoul + 체험형 인턴), extract posting IDs
   from embedded Apollo state in __NEXT_DATA__.
2. Diff against seen_ids.json to find new postings.
3. For each new posting, fetch the detail page concurrently and extract:
   apply URL (홈페이지 지원 link), deadline, salary, address, full description,
   description image URLs, logo URL.
4. Download each posting's primary description image to ./images/{id}.{ext}.
5. Append a row to postings.csv.
6. Send a Telegram message per new posting (photo + caption with apply link).

Designed to run hourly via GitHub Actions.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import random
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # only used for local dev; GitHub Actions injects env vars directly

# --- Config -----------------------------------------------------------------

LISTING_URL = (
    "https://linkareer.com/list/intern"
    "?filterBy_activityTypeID=5"
    "&filterBy_internTypeIds=1"
    "&filterBy_jobTypes=INTERN"
    "&filterBy_regionIDs=2"
    "&filterBy_status=OPEN"
    "&orderBy_direction=DESC"
    "&orderBy_field=RECENT"
    "&page=1"
)

CSV_PATH = Path("postings.csv")
SEEN_PATH = Path("seen_ids.json")
IMAGES_DIR = Path("images")
SNAPSHOT_PATH = Path("postings.json")  # live snapshot consumed by the GitHub Pages frontend

# How many detail-page fetches to run in parallel
DETAIL_CONCURRENCY = 5

# Skip absurdly large image downloads (some companies upload 20MB posters).
# Telegram caps photo uploads at 10MB anyway.
MAX_IMAGE_BYTES = 9_500_000

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# --- Data model -------------------------------------------------------------

@dataclass
class Posting:
    id: str
    title: str
    organization: str
    organization_type: str       # 중소기업, 대기업, etc.
    detail_url: str              # linkareer.com/activity/{id}
    apply_url: str               # company external URL OR linkareer URL for 간편지원
    apply_type: str              # 접수페이지 | 간편지원 | (empty)
    homepage_url: str            # company general site, not apply page
    recruit_type: str            # ASAP | DEADLINE
    recruit_close_at: str        # ISO format, "" if rolling
    salary_info: str             # human-readable salary string
    address: str                 # full address if available
    description_image_urls: str  # ;-separated CDN URLs of description images
    description_image_local: str # relative path of downloaded primary image
    logo_url: str
    description_text: str        # plain-text excerpt of the description (first 1000 chars)
    view_count: int
    discovered_at: str           # ISO format

    def telegram_caption(self) -> str:
        close = (
            f"📅 마감: {self.recruit_close_at[:10]}"
            if self.recruit_close_at
            else "📅 마감: 상시채용"
        )
        salary = f"💰 {self.salary_info}\n" if self.salary_info else ""
        addr = f"📍 {_e(self.address)}\n" if self.address else ""

        # Apply link is the most important thing — make it prominent
        if "@" in self.apply_url and not self.apply_url.startswith("http"):
            # Email application — make it a mailto link
            apply_line = f'📧 지원: <a href="mailto:{_e(self.apply_url)}">{_e(self.apply_url)}</a>'
        elif self.apply_type == "접수페이지":
            apply_line = f'🔗 <a href="{_e(self.apply_url)}">지원하기 (홈페이지 지원)</a>'
        elif self.apply_type == "간편지원":
            apply_line = f'🔗 <a href="{_e(self.apply_url)}">Linkareer에서 간편지원</a>'
        else:
            apply_line = f'🔗 <a href="{_e(self.apply_url)}">지원하기</a>'

        return (
            f"🆕 <b>{_e(self.title)}</b>\n"
            f"🏢 {_e(self.organization)}"
            f"{f' · {_e(self.organization_type)}' if self.organization_type else ''}\n"
            f"{salary}"
            f"{addr}"
            f"{close}\n"
            f"{apply_line}\n"
            f'📄 <a href="{self.detail_url}">상세 페이지</a>'
        )


def _e(s: str) -> str:
    """Escape for Telegram HTML parse mode."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- HTTP helpers -----------------------------------------------------------

def _browser_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }


def _parse_next_data(html: str) -> dict[str, Any]:
    """Pull the __NEXT_DATA__ JSON blob out of any Linkareer page."""
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        raise RuntimeError("__NEXT_DATA__ script tag not found")
    return json.loads(m.group(1))


def _resolve(state: dict, ref: dict | None) -> dict:
    """Resolve a {'__ref': 'Type:id'} pointer through Apollo state."""
    if not ref or not isinstance(ref, dict):
        return {}
    key = ref.get("__ref")
    if not key:
        return {}
    return state.get(key, {})


# --- Listing extraction (page 1, just enough to get IDs) --------------------

async def fetch_listing_ids(client: httpx.AsyncClient) -> list[str]:
    """Return ordered list of activity IDs on page 1 of the filtered listing.

    Pulls IDs from the RECENT-ordered query's `nodes` array specifically — not
    from every Activity:* key in the Apollo state, since the page also runs a
    side query for popular postings (SCRAP_COUNT) whose results would otherwise
    pollute our tracking.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            await asyncio.sleep(random.uniform(0, 2))
            resp = await client.get(LISTING_URL, headers=_browser_headers())
            resp.raise_for_status()
            data = _parse_next_data(resp.text)
            state = data["props"]["pageProps"]["__APOLLO_STATE__"]

            recent_query = None
            for key, value in state.get("ROOT_QUERY", {}).items():
                if key.startswith("activities(") and '"field":"RECENT"' in key:
                    recent_query = value
                    break
            if not recent_query:
                raise RuntimeError("RECENT activities query not found in Apollo state")

            ids: list[str] = []
            for ref in recent_query.get("nodes") or []:
                ref_key = ref.get("__ref") if isinstance(ref, dict) else None
                if ref_key and ref_key.startswith("Activity:"):
                    ids.append(ref_key.split(":", 1)[1])
            return ids
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, RuntimeError) as exc:
            last_exc = exc
            wait = 2 ** attempt + random.uniform(0, 1)
            print(f"[warn] listing attempt {attempt + 1} failed: {exc}. retry in {wait:.1f}s", flush=True)
            await asyncio.sleep(wait)

    raise RuntimeError(f"listing fetch failed: {last_exc}")


async def fetch_all_open_ids(client: httpx.AsyncClient) -> list[str]:
    """Paginate through every page of the OPEN listing and return all IDs.

    Used to build the live snapshot for the frontend — we want every currently
    active posting, not just page 1.
    """
    page = 1
    all_ids: list[str] = []
    seen: set[str] = set()
    while page <= 30:  # safety cap
        url = re.sub(r"page=\d+", f"page={page}", LISTING_URL)
        try:
            await asyncio.sleep(random.uniform(0.3, 0.7))
            resp = await client.get(url, headers=_browser_headers())
            resp.raise_for_status()
            state = _parse_next_data(resp.text)["props"]["pageProps"]["__APOLLO_STATE__"]
        except Exception as exc:
            print(f"[warn] snapshot page {page} failed: {exc}", flush=True)
            break

        recent_query = None
        for k, v in state.get("ROOT_QUERY", {}).items():
            if k.startswith("activities(") and '"field":"RECENT"' in k and isinstance(v, dict):
                recent_query = v
                break
        if not recent_query:
            break

        ids: list[str] = []
        for ref in recent_query.get("nodes") or []:
            if isinstance(ref, dict) and (rk := ref.get("__ref")) and rk.startswith("Activity:"):
                ids.append(rk.split(":", 1)[1])
        if not ids:
            break

        new = [i for i in ids if i not in seen]
        all_ids.extend(new)
        seen.update(new)

        total = int(recent_query.get("totalCount") or 0)
        if len(all_ids) >= total:
            break
        page += 1

    return all_ids


# --- Detail extraction ------------------------------------------------------

def _human_salary(activity: dict) -> str:
    """Render a human-readable salary string from the Activity fields."""
    if activity.get("isSalaryDecidedByCompanyPolicy"):
        return "회사 내규에 따름"
    if activity.get("isSalaryDecidedAfterInterview"):
        return "면접 후 결정"

    salary_type = activity.get("salaryType") or ""
    suffix = {"YEARLY": "/년", "MONTHLY": "/월", "HOURLY": "/시간"}.get(salary_type, "")

    mn = activity.get("minSalary")
    mx = activity.get("maxSalary")
    if mn and mx:
        return f"{mn:,}원 ~ {mx:,}원{suffix}"
    if mn:
        return f"{mn:,}원~{suffix}"
    if mx:
        return f"~{mx:,}원{suffix}"
    return ""


def _strip_html(html: str, limit: int = 1000) -> str:
    """Crude HTML→text. Good enough for an excerpt; we aren't writing a sanitizer."""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def parse_detail(html: str, activity_id: str) -> dict[str, Any]:
    """Extract everything we want from a detail page's __NEXT_DATA__."""
    data = _parse_next_data(html)
    state = data["props"]["pageProps"]["__APOLLO_STATE__"]

    a = state.get(f"Activity:{activity_id}")
    if not a:
        raise RuntimeError(f"Activity:{activity_id} not in state")

    # Apply URL — the field you specifically asked about.
    apply_types = a.get("applyTypes", []) or []
    apply_type_names = []
    for ref in apply_types:
        at = _resolve(state, ref)
        if name := at.get("name"):
            apply_type_names.append(name)
    apply_type_str = ", ".join(apply_type_names)

    apply_detail = a.get("applyDetail") or ""
    if apply_detail:
        apply_url = apply_detail  # 접수페이지 (홈페이지 지원) — the company's career page
    else:
        # 간편지원 or unknown — apply via Linkareer itself
        apply_url = f"https://linkareer.com/activity/{activity_id}"

    # Address
    addr_refs = a.get("addresses") or []
    address = ""
    if addr_refs:
        addr = _resolve(state, addr_refs[0])
        # Common address fields on Linkareer's schema
        parts = [
            addr.get("address1") or addr.get("fullAddress") or addr.get("address") or "",
            addr.get("address2") or "",
        ]
        address = " ".join(p for p in parts if p).strip()

    # Description text + embedded images
    description_html = ""
    text_refs = a.get("texts") or []
    for ref in text_refs:
        t = _resolve(state, ref)
        description_html += t.get("text") or ""

    image_urls: list[str] = []
    for src in re.findall(r'<img[^>]+src="([^"]+)"', description_html):
        # Skip inline base64 data URIs — they aren't fetchable URLs
        if src.startswith("data:"):
            continue
        # Skip generic Linkareer template banners — they're decorative, not the JD
        if "/images/activity/banner/" in src:
            continue
        # Make protocol-relative URLs absolute
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://linkareer.com" + src
        image_urls.append(src)

    # Logo (for fallback if there's no description image)
    logo_url = ""
    if logo_ref := a.get("logoImage"):
        logo_url = _resolve(state, logo_ref).get("url") or ""

    # Recruit close timestamp
    close_ms = a.get("recruitCloseAt")
    close_iso = ""
    if isinstance(close_ms, (int, float)) and close_ms > 0:
        try:
            close_iso = datetime.fromtimestamp(
                close_ms / 1000, tz=timezone.utc
            ).isoformat(timespec="seconds")
        except (OSError, ValueError):
            pass

    return {
        "title": (a.get("title") or "").strip(),
        "organization": (a.get("organizationName") or "").strip(),
        "organization_type": (a.get("organizationType") or "").strip(),
        "apply_url": apply_url,
        "apply_type": apply_type_str,
        "homepage_url": (a.get("homepageURL") or "").strip(),
        "recruit_type": (a.get("recruitType") or "").strip(),
        "recruit_close_at": close_iso,
        "salary_info": _human_salary(a),
        "address": address,
        "description_image_urls": image_urls,
        "logo_url": logo_url,
        "description_text": _strip_html(description_html),
        "view_count": int(a.get("viewCount") or 0),
    }


async def fetch_posting_detail(
    client: httpx.AsyncClient,
    activity_id: str,
    sem: asyncio.Semaphore,
) -> Posting | None:
    """Fetch + parse one detail page. Returns None on failure (logged)."""
    async with sem:
        url = f"https://linkareer.com/activity/{activity_id}"
        try:
            await asyncio.sleep(random.uniform(0.3, 1.2))  # gentle stagger
            resp = await client.get(url, headers=_browser_headers())
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[warn] detail fetch {activity_id} failed: {exc}", flush=True)
            return None

        try:
            d = parse_detail(resp.text, activity_id)
        except Exception as exc:
            print(f"[warn] detail parse {activity_id} failed: {exc}", flush=True)
            return None

    # Download the primary description image, if any
    local_image_path = ""
    if d["description_image_urls"]:
        primary = d["description_image_urls"][0]
        local_image_path = await download_image(client, primary, activity_id)

    return Posting(
        id=activity_id,
        title=d["title"],
        organization=d["organization"],
        organization_type=d["organization_type"],
        detail_url=url,
        apply_url=d["apply_url"],
        apply_type=d["apply_type"],
        homepage_url=d["homepage_url"],
        recruit_type=d["recruit_type"],
        recruit_close_at=d["recruit_close_at"],
        salary_info=d["salary_info"],
        address=d["address"],
        description_image_urls=";".join(d["description_image_urls"]),
        description_image_local=local_image_path,
        logo_url=d["logo_url"],
        description_text=d["description_text"],
        view_count=d["view_count"],
        discovered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


# --- Image download ---------------------------------------------------------

def _ext_from_content_type(ct: str) -> str:
    ct = (ct or "").lower().split(";")[0].strip()
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(ct, "img")


async def download_image(client: httpx.AsyncClient, url: str, activity_id: str) -> str:
    """Download an image to ./images/{id}.{ext}. Returns local path or ''."""
    IMAGES_DIR.mkdir(exist_ok=True)
    try:
        resp = await client.get(url, headers={"User-Agent": random.choice(USER_AGENTS)})
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        print(f"[warn] image download failed for {activity_id}: {exc}", flush=True)
        return ""

    if len(resp.content) > MAX_IMAGE_BYTES:
        print(f"[warn] image for {activity_id} too large ({len(resp.content)} bytes); skipping", flush=True)
        return ""

    ext = _ext_from_content_type(resp.headers.get("content-type", ""))
    path = IMAGES_DIR / f"{activity_id}.{ext}"
    path.write_bytes(resp.content)
    return str(path)


# --- Persistence ------------------------------------------------------------

CSV_FIELDS = [
    "id", "title", "organization", "organization_type",
    "detail_url", "apply_url", "apply_type", "homepage_url",
    "recruit_type", "recruit_close_at", "salary_info", "address",
    "description_image_urls", "description_image_local", "logo_url",
    "description_text", "view_count", "discovered_at",
]


def load_seen_ids() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[warn] could not read {SEEN_PATH} ({exc}); treating as empty", flush=True)
        return set()


def save_seen_ids(ids: set[str]) -> None:
    SEEN_PATH.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_to_csv(postings: list[Posting]) -> int:
    file_exists = CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        for p in postings:
            writer.writerow(asdict(p))
    return len(postings)


def load_postings_from_csv() -> dict[str, dict]:
    """Load every previously-seen posting out of the CSV, keyed by ID.

    Used during snapshot building so we can include older postings that are
    still active without re-fetching their detail pages.
    """
    if not CSV_PATH.exists():
        return {}
    out: dict[str, dict] = {}
    with CSV_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pid = row.get("id")
            if pid:
                # Coerce numeric fields that csv stores as strings
                if row.get("view_count"):
                    try:
                        row["view_count"] = int(row["view_count"])
                    except ValueError:
                        row["view_count"] = 0
                else:
                    row["view_count"] = 0
                out[pid] = row
    return out


def write_snapshot(active: list[dict]) -> None:
    """Write the live snapshot consumed by the frontend (postings.json)."""
    SNAPSHOT_PATH.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "filter": "Seoul · 체험형 인턴 · OPEN",
                "count": len(active),
                "postings": active,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# --- Telegram ---------------------------------------------------------------

async def telegram_send_photo(
    client: httpx.AsyncClient,
    photo_path: str,
    caption: str,
) -> bool:
    """Send a photo with caption. Returns False on failure (caller should fall back to text)."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            photo_bytes = f.read()
        files = {"photo": (Path(photo_path).name, photo_bytes)}
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption[:1024],  # Telegram caption limit
            "parse_mode": "HTML",
        }
        resp = await client.post(url, data=data, files=files, timeout=30.0)
        resp.raise_for_status()
        return True
    except (httpx.HTTPError, OSError) as exc:
        print(f"[warn] sendPhoto failed ({exc}); will retry as text", flush=True)
        return False


async def telegram_send_text(client: httpx.AsyncClient, text: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[warn] Telegram credentials not set; skipping notification", flush=True)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = await client.post(url, json=payload, timeout=15.0)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        print(f"[error] Telegram text send failed: {exc}", flush=True)
        return False


async def notify_postings(client: httpx.AsyncClient, new: list[Posting]) -> None:
    if not new:
        return

    # If many new postings, send a one-line summary first so you know the volume
    if len(new) > 5:
        summary = (
            f"🔔 <b>{len(new)}개의 새 인턴 공고</b> (서울 · 체험형)\n"
            f"각 공고를 개별 메시지로 전송합니다."
        )
        await telegram_send_text(client, summary)
        await asyncio.sleep(0.5)

    for p in new:
        caption = p.telegram_caption()
        sent = False
        if p.description_image_local:
            sent = await telegram_send_photo(client, p.description_image_local, caption)
        if not sent:
            await telegram_send_text(client, caption)
        # Telegram allows ~30 msg/sec; 1 msg/sec is safely polite
        await asyncio.sleep(1.0)


# --- Main -------------------------------------------------------------------

async def run() -> int:
    print(f"[info] starting at {datetime.now(timezone.utc).isoformat()}", flush=True)

    timeout = httpx.Timeout(20.0, connect=10.0)
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)

    async with httpx.AsyncClient(http2=True, timeout=timeout, limits=limits, follow_redirects=True) as client:
        # 1. Listing
        try:
            ids = await fetch_listing_ids(client)
        except Exception as exc:
            print(f"[error] listing failed: {exc}", flush=True)
            return 1

        print(f"[info] {len(ids)} postings on page 1", flush=True)
        if not ids:
            print("[warn] zero postings — possible structure change", flush=True)
            return 3

        # 2. Diff
        seen = load_seen_ids()
        new_ids = [pid for pid in ids if pid not in seen]
        print(f"[info] {len(new_ids)} new postings to fetch", flush=True)

        sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
        new_postings: list[Posting] = []

        if new_ids:
            # 3. Detail fetch (concurrent)
            results = await asyncio.gather(
                *(fetch_posting_detail(client, pid, sem) for pid in new_ids),
                return_exceptions=False,
            )
            new_postings = [p for p in results if p is not None]
            print(f"[info] successfully parsed {len(new_postings)}/{len(new_ids)} detail pages", flush=True)

            if new_postings:
                # 4. CSV first — losing notifications is fine, losing data is not
                append_to_csv(new_postings)
                print(f"[info] appended {len(new_postings)} rows to {CSV_PATH}", flush=True)

                # 5. Notify
                await notify_postings(client, new_postings)

        # 6. Update seen with everything we observed (even fetch-failures, so we don't retry forever)
        seen.update(ids)
        save_seen_ids(seen)

        # 7. Build live snapshot for the frontend — runs every time so closed
        #    postings drop off and freshly-listed ones appear on the site
        await build_snapshot(client, new_postings, sem)

    print("[info] done", flush=True)
    return 0


async def build_snapshot(
    client: httpx.AsyncClient,
    just_fetched: list[Posting],
    sem: asyncio.Semaphore,
) -> None:
    """Refresh postings.json — every currently-OPEN posting with full metadata.

    Strategy:
      1. Paginate the OPEN listing → all currently-active IDs
      2. Look each ID up in the CSV cache + the postings we just fetched
      3. Any remaining unknowns (e.g. older postings we never saw because we
         only ever fetched page 1) get a one-time backfill detail fetch
      4. Write postings.json in listing (RECENT-DESC) order
    """
    try:
        all_open_ids = await fetch_all_open_ids(client)
    except Exception as exc:
        print(f"[warn] snapshot pagination failed: {exc}", flush=True)
        return
    print(f"[info] {len(all_open_ids)} currently-open postings", flush=True)
    if not all_open_ids:
        return

    cached = load_postings_from_csv()
    for p in just_fetched:
        cached[p.id] = asdict(p)

    missing = [pid for pid in all_open_ids if pid not in cached]
    if missing:
        print(f"[info] backfilling {len(missing)} postings missing from cache", flush=True)
        extras = await asyncio.gather(
            *(fetch_posting_detail(client, pid, sem) for pid in missing),
            return_exceptions=False,
        )
        backfilled = [p for p in extras if p is not None]
        if backfilled:
            append_to_csv(backfilled)
            for p in backfilled:
                cached[p.id] = asdict(p)
            print(f"[info] backfilled {len(backfilled)}/{len(missing)} → CSV", flush=True)

    snapshot = [cached[pid] for pid in all_open_ids if pid in cached]
    write_snapshot(snapshot)
    print(f"[info] wrote {SNAPSHOT_PATH} with {len(snapshot)} active postings", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
