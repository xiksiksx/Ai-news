from datetime import datetime, timedelta, timezone

import feedparser

from ai_reader.main import (
    Article, build_payloads, is_duplicate, normalize_title, normalize_url,
    parse_entries, score_article, select_articles,
)

NOW = datetime.now(timezone.utc)
KW = {r"\bevals?\b|evaluation": 3, r"in production": 3, r"\bLLMs?\b|\bAI\b": 1}
NEG = {r"raises \$|funding round": -5}


def art(title, url="https://x.com/a", summary="", hours_ago=1, source="S"):
    return Article(title, url, source, summary, NOW - timedelta(hours=hours_ago))


def test_normalize_url_strips_tracking_and_www():
    a = normalize_url("http://www.Example.com/post/?utm_source=x&id=5#frag")
    b = normalize_url("https://example.com/post?id=5&utm_medium=rss")
    assert a == b == "https://example.com/post?id=5"


def test_title_match_counts_double_and_negative_lowers():
    a = score_article(art("Running LLM evals in production"), {"weight": 1}, KW, NEG)
    assert a.score == 1 + 6 + 6 + 2
    b = score_article(art("AI startup raises $50M"), {"weight": 1}, KW, NEG)
    assert b.score == 1 + 2 - 10


def test_require_keyword_drops_unrelated():
    a = score_article(art("My favourite keyboard"), {"weight": 5, "require_keyword": True}, KW, NEG)
    assert a.score < 0


def test_dedup_by_url_and_similar_title():
    seen = {normalize_url("https://x.com/a"): {"title": "x", "sent_at": NOW.isoformat()}}
    assert is_duplicate(art("whatever", "https://www.x.com/a/?utm_source=rss"), seen, [], 0.85)
    seen = {"https://other/1": {"title": normalize_title("How we run LLM evals in production"), "sent_at": NOW.isoformat()}}
    assert is_duplicate(art("How We Run LLM Evals in Production!", "https://y.com/b"), seen, [], 0.85)
    assert not is_duplicate(art("Totally different topic about agents", "https://y.com/c"), seen, [], 0.85)


def test_select_respects_threshold_lookback_limit_and_order():
    cfg = {"lookback_hours": 36, "threshold": 4, "max_items": 2, "title_similarity": 0.85,
           "keywords": KW, "negative_keywords": NEG}
    feed = {"weight": 1}
    arts = [
        art("LLM evals in production", "https://a/1"),
        art("Evaluation of AI agents", "https://a/2"),
        art("Evals deep dive", "https://a/3"),
        art("Old evals in production post", "https://a/4", hours_ago=100),
        art("Cooking pasta", "https://a/5"),
    ]
    picked = select_articles([(feed, arts)], cfg, {})
    assert [a.url for a in picked] == ["https://a/1", "https://a/2"]


def test_parse_rss_and_payload_chunking():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Evals &amp; you</title><link>https://b/1</link>
    <description>&lt;p&gt;Hello &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description>
    <pubDate>Thu, 12 Nov 2026 08:00:00 GMT</pubDate></item>
    <item><title>No link</title></item></channel></rss>"""
    entries = parse_entries(feedparser.parse(xml), "Blog")
    assert len(entries) == 1
    assert entries[0].title == "Evals & you" and entries[0].summary == "Hello world"
    assert entries[0].published == datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)

    payloads = build_payloads([art(f"t{i}", f"https://c/{i}") for i in range(12)])
    assert len(payloads) == 2 and len(payloads[0]["embeds"]) == 10 and payloads[1]["content"] is None
    assert build_payloads([]) == []
