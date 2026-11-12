"""AI Reader: zbiera artykuły z RSS, punktuje słowami kluczowymi, deduplikuje i wysyła na Discord."""
from __future__ import annotations

import argparse
import calendar
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
TRACKING_PARAMS = re.compile(r"^(utm_|ref$|ref_src$|source$|fbclid$|gclid$|mc_|s$|r$)", re.I)
USER_AGENT = "ai-reader/1.0 (+https://github.com)"


@dataclass
class Article:
    title: str
    url: str
    source: str
    summary: str
    published: datetime | None
    score: int = 0
    matched: list[str] = field(default_factory=list)


# ---------- normalizacja i deduplikacja ----------

def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", title.lower()).strip()


def is_duplicate(article: Article, seen: dict, already_picked: list[Article], threshold: float) -> bool:
    url = normalize_url(article.url)
    if url in seen:
        return True
    title = normalize_title(article.title)
    candidates = [v["title"] for v in seen.values()] + [normalize_title(a.title) for a in already_picked]
    return any(SequenceMatcher(None, title, t).ratio() >= threshold for t in candidates if t)


def load_seen(path: Path, retention_days: int) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8") or "{}")
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    return {k: v for k, v in data.items() if datetime.fromisoformat(v["sent_at"]) >= cutoff}


def save_seen(path: Path, seen: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------- pobieranie ----------

def clean_text(raw: str, limit: int = 600) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def entry_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def fetch_feed(feed: dict) -> list[Article]:
    resp = requests.get(feed["url"], timeout=20, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    return parse_entries(parsed, feed["name"])


def parse_entries(parsed, source: str) -> list[Article]:
    out = []
    for e in parsed.entries:
        link, title = e.get("link"), e.get("title")
        if not link or not title:
            continue
        out.append(Article(
            title=clean_text(title, 300),
            url=link,
            source=source,
            summary=clean_text(e.get("summary") or e.get("description") or ""),
            published=entry_date(e),
        ))
    return out


# ---------- punktacja ----------

def score_article(article: Article, feed: dict, keywords: dict, negative: dict) -> Article:
    score = int(feed.get("weight", 0))
    positive_hit = False
    for pattern, weight in {**keywords, **negative}.items():
        rx = re.compile(pattern, re.I)
        in_title = bool(rx.search(article.title))
        in_summary = bool(rx.search(article.summary))
        if not (in_title or in_summary):
            continue
        points = weight * 2 if in_title else weight
        score += points
        if weight > 0:
            positive_hit = True
        label = rx.search(article.title) or rx.search(article.summary)
        article.matched.append(label.group(0).lower())
    if feed.get("require_keyword") and not positive_hit:
        score = -999
    article.score = score
    return article


# ---------- Discord ----------

def build_payloads(articles: list[Article]) -> list[dict]:
    if not articles:
        return []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    embeds = []
    for a in articles:
        matched = ", ".join(dict.fromkeys(a.matched))[:200]
        desc = a.summary[:280] + ("…" if len(a.summary) > 280 else "")
        embeds.append({
            "title": a.title[:256],
            "url": a.url,
            "description": desc,
            "footer": {"text": f"{a.source} · {a.score} pkt · {matched}"[:2048]},
        })
    # Discord przyjmuje maks. 10 embedów na wiadomość.
    payloads = []
    for i in range(0, len(embeds), 10):
        payloads.append({
            "username": "AI Reader",
            "content": f"📚 **Czytelnia AI — {today}** ({len(articles)} artykułów)" if i == 0 else None,
            "embeds": embeds[i:i + 10],
            "allowed_mentions": {"parse": []},
        })
    return payloads


def send_to_discord(webhook: str, payloads: list[dict]) -> None:
    for payload in payloads:
        resp = requests.post(webhook, json=payload, timeout=20)
        if resp.status_code == 429:
            time.sleep(float(resp.json().get("retry_after", 2)))
            resp = requests.post(webhook, json=payload, timeout=20)
        resp.raise_for_status()


# ---------- główny przebieg ----------

def select_articles(per_feed: list[tuple[dict, list[Article]]], cfg: dict, seen: dict) -> list[Article]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg["lookback_hours"])
    scored = []
    for feed, articles in per_feed:
        for a in articles:
            if a.published and a.published < cutoff:
                continue
            score_article(a, feed, cfg.get("keywords", {}), cfg.get("negative_keywords", {}))
            if a.score >= cfg["threshold"]:
                scored.append(a)
    scored.sort(key=lambda a: a.score, reverse=True)
    picked: list[Article] = []
    for a in scored:
        if len(picked) >= cfg["max_items"]:
            break
        if not is_duplicate(a, seen, picked, cfg["title_similarity"]):
            picked.append(a)
    return picked


def run(config_path: Path, seen_path: Path, dry_run: bool) -> int:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    seen = load_seen(seen_path, cfg["seen_retention_days"])

    per_feed, failures = [], []
    for feed in cfg["feeds"]:
        try:
            per_feed.append((feed, fetch_feed(feed)))
        except Exception as exc:  # jeden zepsuty feed nie może zatrzymać całości
            failures.append(f"{feed['name']}: {exc}")
    for f in failures:
        print(f"⚠️  feed pominięty — {f}", file=sys.stderr)

    picked = select_articles(per_feed, cfg, seen)
    print(f"Feedy OK: {len(per_feed)}/{len(cfg['feeds'])}, wybrane artykuły: {len(picked)}")
    for a in picked:
        print(f"  [{a.score:>3}] {a.source}: {a.title}")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if dry_run or not webhook:
        if not webhook and not dry_run:
            print("Brak DISCORD_WEBHOOK_URL — tryb podglądu, nic nie wysyłam i nie zapisuję stanu.")
        return 0

    send_to_discord(webhook, build_payloads(picked))
    now = datetime.now(timezone.utc).isoformat()
    for a in picked:
        seen[normalize_url(a.url)] = {"title": normalize_title(a.title), "sent_at": now}
    save_seen(seen_path, seen)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    p.add_argument("--seen", type=Path, default=ROOT / "state" / "seen.json")
    p.add_argument("--dry-run", action="store_true", help="pokaż wybór, nie wysyłaj i nie zapisuj stanu")
    args = p.parse_args()
    sys.exit(run(args.config, args.seen, args.dry_run))


if __name__ == "__main__":
    main()
