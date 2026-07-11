from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

# The corruption observed on Windows is produced when bytes encoded as
# Windows-1252 are decoded as CP850.  The conversion is reversible for the
# high-confidence cases below and does not require guessing individual words.
_CP850_MOJIBAKE_MARKERS = frozenset(
    "þÒÛ§¾╩├┤┬┐└┴┼╣║╗╝╚╔╦╠═╬▒▓│┐┘┌└"
)
_TECHNICAL_IDENTIFIER = re.compile(
    r"\b[A-Z][A-Z0-9_$#]*(?:\.[A-Z][A-Z0-9_$#]*)+\b"
)
_FIELD_SECTION = re.compile(
    r"\b(?:campos?|atributos?)\s*:\s*(.+?)(?:\.(?:\s|$)|\b(?:não|nao)\s+invente\b|$)",
    re.IGNORECASE | re.DOTALL,
)


def _mojibake_count(value: str) -> int:
    return sum(value.count(marker) for marker in _CP850_MOJIBAKE_MARKERS)


def _repair_cp850_fragment(value: str) -> str | None:
    if not any(marker in value for marker in _CP850_MOJIBAKE_MARKERS):
        return None
    try:
        candidate = value.encode("cp850").decode("cp1252")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if "\ufffd" in candidate or _mojibake_count(candidate) >= _mojibake_count(value):
        return None
    return candidate


def _repair_cp850_as_cp1252(value: str) -> str | None:
    """Repair CP1252 bytes decoded as CP850 without changing valid fragments.

    The complete sentence is attempted first.  If a single unrelated Unicode
    character prevents the reversible conversion, only words containing known
    mojibake markers are repaired.  This keeps the operation deterministic and
    prevents a valid technical identifier from being rewritten.
    """
    repaired = _repair_cp850_fragment(value)
    if repaired is not None:
        return repaired

    changed = False
    parts = re.split(r"(\s+)", value)
    for index, part in enumerate(parts):
        if not any(marker in part for marker in _CP850_MOJIBAKE_MARKERS):
            continue
        prefix_match = re.match(r"^([^\w]*)", part, re.UNICODE)
        suffix_match = re.search(r"([^\w]*)$", part, re.UNICODE)
        prefix = prefix_match.group(1) if prefix_match else ""
        suffix = suffix_match.group(1) if suffix_match else ""
        core_end = len(part) - len(suffix) if suffix else len(part)
        core = part[len(prefix):core_end]
        candidate = _repair_cp850_fragment(core)
        if candidate is not None:
            parts[index] = f"{prefix}{candidate}{suffix}"
            changed = True
    return "".join(parts) if changed else None


def diagnose_query_encoding(query: str) -> dict[str, Any]:
    original = str(query)
    protected: dict[str, str] = {}

    def protect(match: re.Match[str]) -> str:
        token = f"__TECH_{len(protected)}__"
        protected[token] = match.group(0)
        return token

    working = _TECHNICAL_IDENTIFIER.sub(protect, original)
    corrections: list[dict[str, str]] = []
    repaired = _repair_cp850_as_cp1252(working)
    if repaired is not None:
        corrections.append(
            {
                "from": "cp850_decoded_text",
                "to": "windows_1252_text",
                "confidence": "high",
                "method": "cp850_bytes_to_cp1252",
            }
        )
        working = repaired

    for token, identifier in protected.items():
        working = working.replace(token, identifier)

    normalized = unicodedata.normalize("NFC", working)
    remaining = sorted(
        marker for marker in _CP850_MOJIBAKE_MARKERS if marker in normalized
    )
    if corrections and not remaining:
        status = "corrected"
    elif remaining:
        status = "ambiguous"
    else:
        status = "valid"
    return {
        "original_query": original,
        "normalized_query": normalized,
        "encoding_status": status,
        "corrections": corrections,
        "ambiguous_markers": remaining,
    }


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9_$#]+", text))


def _terms(value: str) -> set[str]:
    return {term for term in _fold(value).split() if len(term) > 2}


def _node_phrases(node: dict[str, Any], *keys: str) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()
    for key in keys:
        raw = node.get(key)
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for value in values:
            folded = _fold(value)
            if folded and folded not in seen:
                seen.add(folded)
                phrases.append(folded)
    return phrases


def _phrase_in_text(phrase: str, text: str) -> bool:
    if not phrase or not text:
        return False
    return re.search(rf"(?<![a-z0-9_$#]){re.escape(phrase)}(?![a-z0-9_$#])", text) is not None


def _extract_requested_fields(query: str) -> list[str]:
    match = _FIELD_SECTION.search(query)
    if not match:
        return []
    section = " ".join(match.group(1).split())
    # Commas are authoritative.  The last conjunction is treated as a separator
    # only when it joins two short field labels.
    values: list[str] = []
    for comma_part in section.split(","):
        part = comma_part.strip(" .;:")
        if not part:
            continue
        conjunction = re.split(r"\s+e\s+(?=[^,;]{1,80}$)", part, maxsplit=1, flags=re.IGNORECASE)
        values.extend(item.strip(" .;:") for item in conjunction if item.strip(" .;:"))
    return values


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_$#]+", "_", _fold(value)).strip("_")


def _grain_name(entity_key: str | None, raw_grain: str | None) -> str | None:
    if entity_key:
        key = _slug(entity_key)
        if key.endswith("_agreement"):
            key = "agreement"
        return f"one_row_per_{key}" if key else "one_row_per_entity"
    raw_key = _slug(raw_grain or "")
    return f"one_row_per_{raw_key}" if raw_key else "one_row_per_entity"


def decompose_query(
    query: str,
    nodes: Iterable[dict[str, Any]],
    aliases: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics = diagnose_query_encoding(query)
    normalized_query = str(diagnostics["normalized_query"])
    folded_query = _fold(normalized_query)
    query_terms = _terms(normalized_query)
    node_rows = list(nodes)
    aliases = aliases or {}

    recognized_aliases: list[dict[str, str]] = []
    for canonical, values in aliases.items():
        candidates = values if isinstance(values, list) else [values]
        for value in candidates:
            folded = _fold(value)
            if _phrase_in_text(folded, folded_query):
                recognized_aliases.append(
                    {"alias": str(value), "canonical": str(canonical), "kind": "curated"}
                )

    entity_candidates: list[tuple[int, int, str, dict[str, Any], str]] = []
    for node in node_rows:
        if str(node.get("node_type") or "") != "business_entity":
            continue
        phrases = _node_phrases(node, "entity_id", "name", "title", "aliases")
        exact = [phrase for phrase in phrases if _phrase_in_text(phrase, folded_query)]
        overlap = max((len(query_terms & set(phrase.split())) for phrase in phrases), default=0)
        if not exact and overlap < 2:
            continue
        best_phrase = max(exact, key=len) if exact else ""
        entity_candidates.append(
            (1 if exact else 0, len(best_phrase), str(node.get("id") or ""), node, best_phrase)
        )
    entity_candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
    primary = entity_candidates[0][3] if entity_candidates else None
    matched_entity_phrase = entity_candidates[0][4] if entity_candidates else ""
    entity_key = str((primary or {}).get("entity_id") or "") or None
    if primary and matched_entity_phrase:
        recognized_aliases.append(
            {
                "alias": matched_entity_phrase,
                "canonical": entity_key or str(primary.get("title") or primary.get("name") or ""),
                "kind": "business_entity",
            }
        )

    explicit_fields = _extract_requested_fields(normalized_query)
    attribute_nodes = [
        node
        for node in node_rows
        if str(node.get("node_type") or "") == "business_attribute"
        and (
            not entity_key
            or str(node.get("entity_id") or "") == entity_key
        )
    ]

    details: list[dict[str, Any]] = []
    seen_attributes: set[str] = set()

    def append_attribute(label: str, node: dict[str, Any] | None, matched_phrase: str | None) -> None:
        canonical = (
            str((node or {}).get("attribute_id") or (node or {}).get("name") or "")
            or _slug(label)
        )
        canonical = _slug(canonical)
        if not canonical or canonical in seen_attributes:
            return
        seen_attributes.add(canonical)
        details.append(
            {
                "requested_label": label,
                "canonical_attribute": canonical,
                "matched_node_id": str((node or {}).get("id") or "") or None,
                "matched_phrase": matched_phrase,
                "status": "recognized" if node else "unresolved",
            }
        )
        if node and matched_phrase:
            recognized_aliases.append(
                {
                    "alias": matched_phrase,
                    "canonical": canonical,
                    "kind": "business_attribute",
                }
            )

    if explicit_fields:
        for field in explicit_fields:
            folded_field = _fold(field)
            matches: list[tuple[int, int, str, dict[str, Any], str]] = []
            for node in attribute_nodes:
                for phrase in _node_phrases(node, "attribute_id", "name", "title", "aliases"):
                    exact = folded_field == phrase
                    phrase_terms = phrase.split()
                    field_terms = folded_field.split()
                    contained = (
                        len(phrase_terms) >= 2
                        and len(field_terms) >= 2
                        and min(len(phrase), len(folded_field))
                        / max(len(phrase), len(folded_field)) >= 0.60
                        and (
                            _phrase_in_text(phrase, folded_field)
                            or _phrase_in_text(folded_field, phrase)
                        )
                    )
                    if exact or contained:
                        matches.append(
                            (1 if exact else 0, len(phrase), str(node.get("id") or ""), node, phrase)
                        )
            matches.sort(key=lambda item: (-item[0], -item[1], item[2]))
            best = matches[0] if matches else None
            append_attribute(field, best[3] if best else None, best[4] if best else None)
    else:
        matched_nodes: list[tuple[int, str, dict[str, Any], str]] = []
        for node in attribute_nodes:
            matches = [
                phrase
                for phrase in _node_phrases(node, "attribute_id", "name", "title", "aliases")
                if _phrase_in_text(phrase, folded_query)
            ]
            if matches:
                phrase = max(matches, key=len)
                matched_nodes.append((len(phrase), str(node.get("id") or ""), node, phrase))
        matched_nodes.sort(key=lambda item: (-item[0], item[1]))
        for _, _, node, phrase in matched_nodes:
            append_attribute(str(node.get("name") or node.get("title") or phrase), node, phrase)

    lowered = _fold(normalized_query)
    grain_match = re.search(
        r"\buma linha por\s+(.+?)(?:\s+e\s+os\s+campos\b|[,.;]|$)",
        lowered,
    )
    requested_grain = _grain_name(entity_key, grain_match.group(1) if grain_match else None) if grain_match else None

    constraints = []
    for marker, canonical in (
        ("nao invente", "não invente"),
        ("somente", "somente"),
        ("apenas", "apenas"),
        ("diferencie", "diferencie"),
        ("sem duplicidade", "sem duplicidade"),
    ):
        if marker in lowered:
            constraints.append(canonical)

    candidate_module = None
    if primary:
        candidate_module = primary.get("module_id")
        if not candidate_module:
            modules = list(primary.get("modules") or [])
            candidate_module = modules[0] if modules else None

    return {
        "primary_entity": entity_key or (
            str((primary or {}).get("name") or (primary or {}).get("title") or "") or None
        ),
        "primary_entity_name": (
            str((primary or {}).get("name") or (primary or {}).get("title") or "") or None
        ),
        "primary_entity_id": str((primary or {}).get("id") or "") or None,
        "requested_attributes": [item["canonical_attribute"] for item in details],
        "requested_attribute_details": details,
        "explicit_constraints": constraints,
        "requested_grain": requested_grain,
        "candidate_module": candidate_module,
        "technical_terms": sorted(set(_TECHNICAL_IDENTIFIER.findall(normalized_query))),
        "recognized_aliases": recognized_aliases,
        "encoding": diagnostics,
    }
