import hashlib
import re
from collections import Counter
from typing import Any


def build_digest(articles: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    ranked = _rank_articles(articles, settings)
    selected = ranked[: int(settings["max_items"])]
    categories = Counter(a.get("category") or "기타" for a in selected)
    keywords = _top_keywords(selected)
    text = _format_digest(selected, categories, keywords, settings)
    digest_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "hash": digest_hash,
        "text": text,
        "items": selected,
        "category_counts": dict(categories),
        "keywords": keywords,
    }


def _rank_articles(articles: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    include_ranked = bool(settings.get("include_ranked", True))

    def score(article: dict[str, Any]) -> tuple[int, int, int, str]:
        importance = _to_int(article.get("importance"), 0)
        views = _to_int(article.get("views"), 0)
        rank = _to_int(article.get("rank"), 999)
        ranked_bonus = 1 if include_ranked and rank < 999 else 0
        collected = str(article.get("collected_at") or "")
        return (importance, ranked_bonus, views, collected)

    deduped = {}
    for article in articles:
        key = article.get("url") or article.get("title")
        if not key:
            continue
        current = deduped.get(key)
        if current is None or score(article) > score(current):
            deduped[key] = article

    return sorted(deduped.values(), key=score, reverse=True)


def _format_digest(
    articles: list[dict[str, Any]],
    categories: Counter,
    keywords: list[str],
    settings: dict[str, Any],
) -> str:
    headline = settings.get("headline") or "시황 브리핑"
    lines = [f"[{headline}]"]
    if not articles:
        lines.append("조건에 맞는 신규 주요 뉴스가 없습니다.")
        return "\n".join(lines)

    category_text = ", ".join(f"{name} {count}건" for name, count in categories.most_common())
    lines.append(f"주요 카테고리: {category_text}")
    if keywords:
        lines.append(f"핵심 키워드: {', '.join(keywords[:6])}")
    lines.append("")
    lines.append("[중요 뉴스]")

    include_links = bool(settings.get("include_links", True))
    for idx, article in enumerate(articles, 1):
        title = _compact(article.get("title") or "", 70)
        category = article.get("category") or "기타"
        importance = article.get("importance") or "-"
        sentiment = article.get("sentiment") or "중립"
        summary = _first_sentence(article.get("summary") or "")
        prefix = f"{idx}. [{category}/{importance}점/{sentiment}]"
        lines.append(f"{prefix} {title}")
        if summary:
            lines.append(f"- {summary}")
        if include_links and article.get("url"):
            lines.append(str(article["url"]))
        lines.append("")

    return "\n".join(lines).strip()


def _top_keywords(articles: list[dict[str, Any]]) -> list[str]:
    counts: Counter[str] = Counter()
    for article in articles:
        raw = article.get("keywords") or ""
        for token in re.split(r"[,/| ]+", raw):
            token = token.strip()
            if len(token) >= 2:
                counts[token] += 1
    return [word for word, _ in counts.most_common(8)]


def _first_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?。])\s+", text)
    return _compact(parts[0], 130)


def _compact(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _to_int(value: Any, default: int) -> int:
    try:
        if value is None or value == "":
            return default
        return int(str(value).replace(",", ""))
    except ValueError:
        return default
