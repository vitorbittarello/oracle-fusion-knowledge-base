from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
TEXT_BLOCK_TAGS = {"p", "li", "pre", "code", "blockquote"}
TABLE_NAME_RE = re.compile(r"\b[A-Z][A-Z0-9$#]{1,15}_[A-Z0-9_$#]{2,}\b")
COLUMN_NAME_RE = re.compile(r"\b[A-Z][A-Z0-9_$#]{2,}\b")
WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "how", "in", "is", "it", "of", "on", "or", "that", "the", "this",
    "to", "use", "using", "what", "when", "which", "with", "you", "your",
    "de", "da", "das", "do", "dos", "e", "em", "é", "o", "os", "para",
    "por", "que", "qual", "quais", "um", "uma", "no", "na", "nos", "nas",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_accents(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def normalize_text(value: str | None) -> str:
    return normalize_space(strip_accents(value or "").lower())


def tokenize(value: str | None) -> list[str]:
    tokens = [token.lower() for token in WORD_RE.findall(strip_accents(value or ""))]
    return [token for token in tokens if len(token) > 1 and token not in STOPWORDS]


def slugify(value: str) -> str:
    value = normalize_text(value)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "item"


def stable_id(prefix: str, *parts: str) -> str:
    joined = "|".join(normalize_space(part) for part in parts)
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]
    readable = slugify(parts[0] if parts else prefix)[:48]
    return f"{prefix}:{readable}:{digest}"


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = normalize_space(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def ensure_parent(path: str | os.PathLike[str]) -> Path:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    resolved = Path(path)
    if not resolved.exists():
        return default
    with resolved.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: str | os.PathLike[str], payload: Any, *, indent: int = 2) -> None:
    resolved = ensure_parent(path)
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=indent, ensure_ascii=False)
    temporary.replace(resolved)


def read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    resolved = Path(path)
    if not resolved.exists():
        return []
    rows: list[dict[str, Any]] = []
    with resolved.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL inválido em {resolved}:{line_number}") from exc
    return rows


def write_jsonl(path: str | os.PathLike[str], rows: Iterable[dict[str, Any]]) -> None:
    resolved = ensure_parent(path)
    temporary = resolved.with_suffix(resolved.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(resolved)


def append_jsonl(path: str | os.PathLike[str], rows: Iterable[dict[str, Any]]) -> None:
    resolved = ensure_parent(path)
    with resolved.open("a", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class HttpSettings:
    timeout_connect: int = 10
    timeout_read: int = 60
    retries: int = 3
    backoff_factor: float = 1.0
    delay_seconds: float = 0.15
    cache_dir: str | None = None
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    )


class CachedHttpClient:
    def __init__(self, settings: HttpSettings | None = None):
        self.settings = settings or HttpSettings()
        self.session = requests.Session()
        retry = Retry(
            total=self.settings.retries,
            connect=self.settings.retries,
            read=self.settings.retries,
            backoff_factor=self.settings.backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update(
            {
                "User-Agent": self.settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._last_request_at = 0.0
        self.cache_dir = Path(self.settings.cache_dir) if self.settings.cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_paths(self, url: str) -> tuple[Path, Path] | None:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.body", self.cache_dir / f"{key}.meta.json"

    def get_text(self, url: str, *, force_refresh: bool = False) -> tuple[str, dict[str, Any]]:
        cache_paths = self._cache_paths(url)
        if cache_paths and not force_refresh:
            body_path, metadata_path = cache_paths
            if body_path.exists() and metadata_path.exists():
                return body_path.read_text(encoding="utf-8"), read_json(metadata_path, {})

        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.settings.delay_seconds:
            time.sleep(self.settings.delay_seconds - elapsed)

        response = self.session.get(
            url,
            timeout=(self.settings.timeout_connect, self.settings.timeout_read),
        )
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        body = response.text
        metadata = {
            "url": response.url,
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "fetched_at": utc_now_iso(),
        }
        if cache_paths:
            body_path, metadata_path = cache_paths
            body_path.write_text(body, encoding="utf-8")
            write_json(metadata_path, metadata)
        return body, metadata

    def get_soup(self, url: str, *, force_refresh: bool = False) -> tuple[BeautifulSoup, dict[str, Any]]:
        body, metadata = self.get_text(url, force_refresh=force_refresh)
        content_type = normalize_text(metadata.get("content_type"))
        if content_type and "html" not in content_type and "xml" not in content_type:
            raise ValueError(f"Conteúdo não HTML retornado para {url}: {content_type}")
        return BeautifulSoup(body, "html.parser"), metadata


def remove_oracle_chrome(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()
    for selector in [
        "header", "footer", "nav", ".footer", ".header", ".toolbar", ".feedback",
        "#footer", "#header", "#breadcrumbs", ".breadcrumbs", ".related-topics",
    ]:
        for tag in soup.select(selector):
            tag.decompose()


def get_main_content(soup: BeautifulSoup) -> Tag:
    return (
        soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.find("article")
        or soup.body
        or soup
    )


def extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return normalize_space(h1.get_text(" ", strip=True))
    if soup.title:
        return normalize_space(soup.title.get_text(" ", strip=True))
    return "Untitled"


def heading_level(tag: Tag) -> int:
    if tag.name and re.fullmatch(r"h[1-6]", tag.name):
        return int(tag.name[1])
    return 7


def table_to_records(table: Tag) -> list[dict[str, str]]:
    rows = table.find_all("tr")
    if not rows:
        return []
    headers: list[str] = []
    header_row: Tag | None = None
    for row in rows:
        cells = row.find_all(["th", "td"])
        values = [normalize_space(cell.get_text(" ", strip=True)) for cell in cells]
        if not values:
            continue
        if row.find_all("th") or any(value.lower() in {"name", "type", "description", "columns", "parameter"} for value in values):
            headers = [slugify(value).replace("-", "_") or f"column_{index + 1}" for index, value in enumerate(values)]
            header_row = row
            break
    records: list[dict[str, str]] = []
    for row in rows:
        if row is header_row:
            continue
        cells = row.find_all("td")
        if not cells:
            continue
        values = [normalize_space(cell.get_text(" ", strip=True)) for cell in cells]
        if not any(values):
            continue
        if not headers:
            headers = [f"column_{index + 1}" for index in range(len(values))]
        record = {
            headers[index] if index < len(headers) else f"column_{index + 1}": value
            for index, value in enumerate(values)
        }
        records.append(record)
    return records


def extract_heading_sections(soup: BeautifulSoup) -> list[dict[str, Any]]:
    remove_oracle_chrome(soup)
    main = get_main_content(soup)
    sections: list[dict[str, Any]] = []
    stack: list[tuple[int, str]] = []
    current: dict[str, Any] | None = None
    seen_tables: set[int] = set()

    for element in main.find_all(list(HEADING_TAGS | TEXT_BLOCK_TAGS | {"table"}), recursive=True):
        if not isinstance(element, Tag):
            continue
        if element.name in HEADING_TAGS:
            title = normalize_space(element.get_text(" ", strip=True))
            if not title:
                continue
            level = heading_level(element)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current = {
                "title": title,
                "level": level,
                "section_path": [item[1] for item in stack],
                "text_blocks": [],
                "list_items": [],
                "tables": [],
            }
            sections.append(current)
            continue

        if current is None:
            continue

        if element.name == "table":
            marker = id(element)
            if marker in seen_tables:
                continue
            seen_tables.add(marker)
            records = table_to_records(element)
            if records:
                current["tables"].append(records)
            continue

        if element.find_parent("table"):
            continue
        if element.name == "li" and element.find_parent("li"):
            continue
        text = normalize_space(element.get_text(" ", strip=True))
        if element.name == "li":
            if text and text not in current["list_items"]:
                current["list_items"].append(text)
        elif text and text not in current["text_blocks"]:
            current["text_blocks"].append(text)

    for section in sections:
        section["text"] = normalize_space(" ".join(section.pop("text_blocks")))
    return [section for section in sections if section["text"] or section["list_items"] or section["tables"] or section["level"] == 1]


def extract_section_by_title(soup: BeautifulSoup, title: str) -> dict[str, Any] | None:
    target = normalize_text(title)
    for section in extract_heading_sections(soup):
        if normalize_text(section["title"]) == target:
            return section
    return None


def extract_table_mentions(text: str) -> list[str]:
    return unique_preserving_order(match.group(0).upper() for match in TABLE_NAME_RE.finditer(text or ""))


def extract_uppercase_identifiers(text: str) -> list[str]:
    return unique_preserving_order(match.group(0).upper() for match in COLUMN_NAME_RE.finditer(text or ""))


def extract_keywords(text: str, *, limit: int = 30) -> list[str]:
    frequencies: dict[str, int] = {}
    for token in tokenize(text):
        frequencies[token] = frequencies.get(token, 0) + 1
    ranked = sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _ in ranked[:limit]]


def is_same_document_base(url: str, base_url: str) -> bool:
    parsed = urlparse(url)
    base = urlparse(base_url)
    return parsed.netloc == base.netloc and parsed.path.startswith(base.path.rsplit("/", 1)[0] + "/")


def iter_toc_links(
    soup: BeautifulSoup,
    toc_url: str,
    *,
    include_extensions: Sequence[str] = (".html", ".htm"),
    exclude_names: Sequence[str] = ("index.html", "toc.htm", "toc.html"),
) -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    excluded = {name.lower() for name in exclude_names}
    for anchor in soup.find_all("a", href=True):
        href = normalize_space(anchor.get("href"))
        label = normalize_space(anchor.get_text(" ", strip=True))
        if not href or not label or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(toc_url, href).split("#", 1)[0]
        parsed = urlparse(url)
        filename = Path(parsed.path).name.lower()
        if filename in excluded:
            continue
        if include_extensions and not any(parsed.path.lower().endswith(ext) for ext in include_extensions):
            continue
        if not is_same_document_base(url, toc_url):
            continue
        if url in seen:
            continue
        seen.add(url)

        ancestors: list[str] = []
        parent = anchor.find_parent("li")
        while parent:
            direct_anchor = parent.find("a", recursive=False)
            if direct_anchor and direct_anchor is not anchor:
                parent_label = normalize_space(direct_anchor.get_text(" ", strip=True))
                if parent_label:
                    ancestors.append(parent_label)
            parent = parent.find_parent("li")
        ancestors.reverse()
        yield {"url": url, "label": label, "toc_path": ancestors + [label]}


def merge_text_fields(*values: Any) -> str:
    flattened: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            flattened.append(value)
        elif isinstance(value, dict):
            flattened.extend(str(item) for pair in value.items() for item in pair)
        elif isinstance(value, Iterable):
            flattened.extend(str(item) for item in value)
        else:
            flattened.append(str(value))
    return normalize_space(" ".join(flattened))


def confidence_to_score(value: str | float | int | None) -> float:
    if isinstance(value, (int, float)):
        return min(1.0, max(0.0, float(value)))
    mapping = {
        "very_high": 1.0,
        "high": 0.9,
        "medium": 0.7,
        "low": 0.45,
        "unknown": 0.3,
    }
    return mapping.get(normalize_text(value).replace(" ", "_"), 0.6)


def safe_log(value: float) -> float:
    return math.log(max(value, 1e-12))
