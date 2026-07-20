"""
One-shot: fetch all currently-active Seoul 체험형 인턴 postings from
Linkareer and write them to all_postings.csv + all_postings.json.
Also downloads JD images to ./images/. Does NOT touch postings.csv /
seen_ids.json and does NOT send Telegram.
"""
import asyncio
import csv
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

import scraper

OUT_CSV = Path("all_postings.csv")
OUT_JSON = Path("all_postings.json")


def _build_listing_url(page: int) -> str:
    return re.sub(r"page=\d+", f"page={page}", scraper.LISTING_URL)


async def fetch_page(client, page):
    url = _build_listing_url(page)
    resp = await client.get(url, headers=scraper._browser_headers())
    resp.raise_for_status()
    state = scraper._parse_next_data(resp.text)["props"]["pageProps"]["__APOLLO_STATE__"]
    for k, v in state.get("ROOT_QUERY", {}).items():
        if k.startswith("activities(") and '"field":"RECENT"' in k and isinstance(v, dict):
            ids = []
            for ref in v.get("nodes") or []:
                if isinstance(ref, dict) and (rk := ref.get("__ref")):
                    ids.append(rk.split(":", 1)[1])
            return ids, int(v.get("totalCount") or 0)
    return [], 0


async def fetch_all_ids(client):
    page, all_ids, seen = 1, [], set()
    while True:
        ids, total = await fetch_page(client, page)
        if not ids:
            break
        new = [i for i in ids if i not in seen]
        all_ids.extend(new)
        seen.update(new)
        print(f"page {page}: +{len(new)} (total target {total})", flush=True)
        if len(all_ids) >= total or page > 30:
            break
        page += 1
        await asyncio.sleep(0.5)
    return all_ids


async def main():
    timeout = httpx.Timeout(20.0, connect=10.0)
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    async with httpx.AsyncClient(http2=True, timeout=timeout, limits=limits, follow_redirects=True) as c:
        ids = await fetch_all_ids(c)
        print(f"fetching detail for {len(ids)} postings...", flush=True)
        sem = asyncio.Semaphore(scraper.DETAIL_CONCURRENCY)
        results = await asyncio.gather(*(scraper.fetch_posting_detail(c, pid, sem) for pid in ids))

    postings = [asdict(p) for p in results if p is not None]
    print(f"parsed {len(postings)}/{len(ids)}", flush=True)

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=scraper.CSV_FIELDS)
        w.writeheader()
        w.writerows(postings)

    OUT_JSON.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "filter": "Seoul · 체험형 인턴 · OPEN",
                "count": len(postings),
                "postings": postings,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT_CSV} and {OUT_JSON}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
