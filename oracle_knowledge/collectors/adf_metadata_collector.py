from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from oracle_knowledge.common import (
    extract_keywords,
    merge_text_fields,
    normalize_space,
    stable_id,
    utc_now_iso,
    write_json,
)

ADF_ACCEPT_CANDIDATES = (
    "*/*",
    "application/json",
    "application/vnd.oracle.openapi3+json",
)
CUSTOM_SUFFIX = "_c"
FLEXFIELD_MARKERS = (
    "dff",
    "eff",
    "flex",
)
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_filename(value: str) -> str:
    candidate = SAFE_FILENAME_RE.sub("_", value).strip("._")
    return candidate or "resource"


def _is_custom_name(value: str | None) -> bool:
    return str(value or "").lower().endswith(CUSTOM_SUFFIX)


def _is_flexfield_name(value: str | None) -> bool:
    normalized = str(value or "").lower()
    return (
        normalized.startswith("__flex_")
        or any(marker in normalized for marker in FLEXFIELD_MARKERS)
    )


def _selected_properties(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "name",
        "type",
        "title",
        "titlePlural",
        "description",
        "mandatory",
        "updatable",
        "queryable",
        "inputRequired",
        "allowChanges",
        "precision",
        "scale",
        "maxLength",
        "hasDefaultValueExpression",
        "discrColumnType",
        "nullable",
        "readOnly",
        "writeOnly",
        "x-queryable",
        "x-cardinality",
    )
    return {
        key: payload[key]
        for key in keys
        if key in payload and payload[key] not in (None, "", [], {})
    }


def _normalize_links(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        row = {
            key: item.get(key)
            for key in ("rel", "href", "name", "kind")
            if item.get(key) not in (None, "")
        }
        if row:
            result.append(row)
    return result


def _normalize_attributes(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    result: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        row = _selected_properties(item)
        name = str(item.get("name") or "")
        row["is_custom"] = _is_custom_name(name)
        row["is_flexfield_control"] = _is_flexfield_name(name)
        properties = item.get("properties")
        if isinstance(properties, dict) and properties:
            row["properties"] = properties
        if row:
            result.append(row)
    return result


def _normalize_children(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, dict):
        return []
    result: list[dict[str, Any]] = []
    for name, item in sorted(values.items(), key=lambda pair: pair[0].lower()):
        detail = item if isinstance(item, dict) else {}
        row = {
            "name": name,
            **_selected_properties(detail),
            "is_custom": _is_custom_name(name),
            "is_flexfield": _is_flexfield_name(name),
            "links": _normalize_links(detail.get("links")),
        }
        result.append(row)
    return result


def normalize_adf_resource(
    resource_name: str,
    payload: dict[str, Any],
    *,
    base_url: str,
    source_url: str,
    fetched_at: str | None,
) -> dict[str, Any]:
    resources = payload.get("Resources")
    detail: dict[str, Any] = {}
    if isinstance(resources, dict):
        candidate = resources.get(resource_name)
        if isinstance(candidate, dict):
            detail = candidate
        elif len(resources) == 1:
            only_value = next(iter(resources.values()))
            if isinstance(only_value, dict):
                detail = only_value

    attributes = _normalize_attributes(detail.get("attributes"))
    children = _normalize_children(detail.get("children"))
    actions = detail.get("actions") if isinstance(detail.get("actions"), list) else []
    links = _normalize_links(detail.get("links"))
    title = detail.get("title") or resource_name
    title_plural = detail.get("titlePlural")
    is_custom = _is_custom_name(resource_name) or any(
        bool(attribute.get("is_custom")) for attribute in attributes
    )

    search_text = merge_text_fields(
        resource_name,
        title,
        title_plural,
        [attribute.get("name") for attribute in attributes],
        [attribute.get("title") for attribute in attributes],
        [attribute.get("description") for attribute in attributes],
        [child.get("name") for child in children],
        [child.get("title") for child in children],
    )

    return {
        "id": stable_id("adf_resource", resource_name, base_url),
        "node_type": "adf_resource",
        "name": resource_name,
        "title": title,
        "title_plural": title_plural,
        "is_custom": is_custom,
        "is_standard_resource": not _is_custom_name(resource_name),
        "properties": _selected_properties(detail),
        "attributes": attributes,
        "children": children,
        "actions": actions,
        "links": links,
        "keywords": extract_keywords(search_text),
        "search_text": search_text,
        "source": {
            "source_type": "fusion_adf_rest_metadata",
            "base_url": base_url,
            "url": source_url,
            "fetched_at": fetched_at,
        },
    }


class AdfMetadataCollector:
    def __init__(
        self,
        *,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        bearer_token: str | None = None,
        api_root: str = "fscmRestApi",
        api_version: str = "latest",
        accept_language: str = "en-US",
        delay_seconds: float = 0.15,
        timeout_connect: int = 10,
        timeout_read: int = 120,
        verify_ssl: bool = True,
        session: requests.Session | None = None,
    ):
        if username and bearer_token:
            raise ValueError("Informe Basic Auth ou Bearer Token, não ambos.")
        if username and password is None:
            raise ValueError("A senha é obrigatória quando o usuário é informado.")
        if password and not username:
            raise ValueError("O usuário é obrigatório quando a senha é informada.")

        parsed = urlparse(base_url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError("base_url deve ser uma URL HTTPS válida do Fusion.")

        self.base_url = base_url.rstrip("/")
        self.api_root = api_root.strip("/")
        self.api_version = api_version.strip("/")
        self.accept_language = accept_language
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.timeout = (timeout_connect, timeout_read)
        self.verify_ssl = verify_ssl
        self.session = session or requests.Session()
        self._last_request_at = 0.0

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update(
            {
                "Accept-Language": accept_language,
                "User-Agent": "oracle-fusion-knowledge-base/adf-metadata-collector",
            }
        )
        if username:
            self.session.auth = (username, password or "")
        elif bearer_token:
            self.session.headers["Authorization"] = f"Bearer {bearer_token}"

    @property
    def catalog_url(self) -> str:
        return (
            f"{self.base_url}/{self.api_root}/resources/{self.api_version}/describe"
            "?metadataMode=minimal&includeChildren=true"
        )

    def resource_url(self, resource_name: str) -> str:
        return (
            f"{self.base_url}/{self.api_root}/resources/{self.api_version}/"
            f"{quote(resource_name, safe='')}/describe"
        )

    def _get_json(self, url: str) -> tuple[dict[str, Any], dict[str, Any]]:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)

        response = None
        attempted_accepts: list[str] = []

        for accept in ADF_ACCEPT_CANDIDATES:
            attempted_accepts.append(accept)
            response = self.session.get(
                url,
                headers={"Accept": accept},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            self._last_request_at = time.monotonic()

            if response.status_code != 406:
                break

            print(
                f"[ADF] HTTP 406 para Accept={accept}; "
                "tentando outro formato aceito pelo endpoint."
            )

        if response is None:
            raise RuntimeError(f"Nenhuma requisição foi executada para {url}.")

        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Resposta JSON inválida para {url}: objeto esperado.")
        metadata = {
            "url": response.url,
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "request_accept": response.request.headers.get("Accept")
            if getattr(response, "request", None) is not None
            else attempted_accepts[-1],
            "attempted_accepts": attempted_accepts,
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "fetched_at": utc_now_iso(),
        }
        return payload, metadata

    @staticmethod
    def _select_resource_names(
        names: Iterable[str],
        *,
        resources: list[str] | None,
        include_patterns: list[str] | None,
        exclude_patterns: list[str] | None,
        custom_only: bool,
        max_resources: int | None,
    ) -> list[str]:
        selected = sorted({str(name) for name in names if str(name).strip()}, key=str.lower)
        exact = {value.lower() for value in (resources or []) if value.strip()}
        include_regexes = [re.compile(value, re.IGNORECASE) for value in (include_patterns or [])]
        exclude_regexes = [re.compile(value, re.IGNORECASE) for value in (exclude_patterns or [])]

        if exact:
            selected = [name for name in selected if name.lower() in exact]
        if include_regexes:
            selected = [
                name
                for name in selected
                if any(pattern.search(name) for pattern in include_regexes)
            ]
        if custom_only:
            selected = [name for name in selected if _is_custom_name(name)]
        if exclude_regexes:
            selected = [
                name
                for name in selected
                if not any(pattern.search(name) for pattern in exclude_regexes)
            ]
        if max_resources is not None:
            selected = selected[: max(0, max_resources)]
        return selected

    def collect(
        self,
        output_dir: str | Path,
        *,
        resources: list[str] | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        custom_only: bool = False,
        max_resources: int | None = None,
        catalog_only: bool = False,
        resume: bool = True,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        root = Path(output_dir).resolve()
        raw_dir = root / "raw"
        resources_dir = raw_dir / "resources"
        resources_dir.mkdir(parents=True, exist_ok=True)

        catalog_raw_path = raw_dir / "catalog.json"
        catalog_meta_path = raw_dir / "catalog.meta.json"

        if resume and not force_refresh and catalog_raw_path.exists():
            catalog_payload = json.loads(catalog_raw_path.read_text(encoding="utf-8"))
            catalog_metadata = (
                json.loads(catalog_meta_path.read_text(encoding="utf-8"))
                if catalog_meta_path.exists()
                else {"url": self.catalog_url, "fetched_at": None}
            )
        else:
            catalog_payload, catalog_metadata = self._get_json(self.catalog_url)
            write_json(catalog_raw_path, catalog_payload)
            write_json(catalog_meta_path, catalog_metadata)

        catalog_resources = catalog_payload.get("Resources")
        if not isinstance(catalog_resources, dict):
            raise ValueError("O catálogo ADF não contém o objeto Resources esperado.")

        selected_names = self._select_resource_names(
            catalog_resources.keys(),
            resources=resources,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            custom_only=custom_only,
            max_resources=max_resources,
        )

        normalized_resources: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        fetched_count = 0
        reused_count = 0

        if catalog_only:
            for resource_name in selected_names:
                normalized_resources.append(
                    normalize_adf_resource(
                        resource_name,
                        {"Resources": {resource_name: catalog_resources[resource_name]}},
                        base_url=self.base_url,
                        source_url=catalog_metadata.get("url") or self.catalog_url,
                        fetched_at=catalog_metadata.get("fetched_at"),
                    )
                )
        else:
            for index, resource_name in enumerate(selected_names, start=1):
                source_url = self.resource_url(resource_name)
                safe_name = _safe_filename(resource_name)
                raw_path = resources_dir / f"{safe_name}.json"
                meta_path = resources_dir / f"{safe_name}.meta.json"
                print(f"[ADF] {index}/{len(selected_names)} {resource_name}")
                try:
                    if resume and not force_refresh and raw_path.exists():
                        payload = json.loads(raw_path.read_text(encoding="utf-8"))
                        metadata = (
                            json.loads(meta_path.read_text(encoding="utf-8"))
                            if meta_path.exists()
                            else {"url": source_url, "fetched_at": None}
                        )
                        reused_count += 1
                    else:
                        payload, metadata = self._get_json(source_url)
                        write_json(raw_path, payload)
                        write_json(meta_path, metadata)
                        fetched_count += 1

                    normalized_resources.append(
                        normalize_adf_resource(
                            resource_name,
                            payload,
                            base_url=self.base_url,
                            source_url=metadata.get("url") or source_url,
                            fetched_at=metadata.get("fetched_at"),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - coleta deve continuar por recurso
                    status_code = None
                    if isinstance(exc, requests.HTTPError) and exc.response is not None:
                        status_code = exc.response.status_code
                    errors.append(
                        {
                            "resource_name": resource_name,
                            "url": source_url,
                            "status_code": status_code,
                            "error_type": type(exc).__name__,
                            "message": normalize_space(str(exc)),
                        }
                    )
                    print(f"[ADF][ERRO] {resource_name}: {exc}")

        attribute_count = sum(len(item.get("attributes") or []) for item in normalized_resources)
        child_count = sum(len(item.get("children") or []) for item in normalized_resources)
        custom_resource_count = sum(bool(item.get("is_custom")) for item in normalized_resources)
        custom_attribute_count = sum(
            1
            for item in normalized_resources
            for attribute in item.get("attributes") or []
            if attribute.get("is_custom")
        )
        flexfield_child_count = sum(
            1
            for item in normalized_resources
            for child in item.get("children") or []
            if child.get("is_flexfield")
        )

        source = {
            "source_type": "fusion_adf_rest_metadata",
            "base_url": self.base_url,
            "environment_host": urlparse(self.base_url).netloc,
            "api_root": self.api_root,
            "api_version": self.api_version,
            "catalog_url": self.catalog_url,
            "accept_language": self.accept_language,
            "fetched_at": catalog_metadata.get("fetched_at"),
        }
        stats = {
            "catalog_resources": len(catalog_resources),
            "selected_resources": len(selected_names),
            "collected_resources": len(normalized_resources),
            "fetched_resources": fetched_count,
            "reused_resources": reused_count,
            "failed_resources": len(errors),
            "attributes": attribute_count,
            "children": child_count,
            "custom_resources": custom_resource_count,
            "custom_attributes": custom_attribute_count,
            "flexfield_children": flexfield_child_count,
        }
        normalized_catalog = {
            "version": "1.0.0",
            "generated_at": utc_now_iso(),
            "source": source,
            "selection": {
                "resources": resources or [],
                "include_patterns": include_patterns or [],
                "exclude_patterns": exclude_patterns or [],
                "custom_only": custom_only,
                "max_resources": max_resources,
                "catalog_only": catalog_only,
            },
            "resources": normalized_resources,
            "errors": errors,
            "stats": stats,
        }
        manifest = {
            "version": "1.0.0",
            "generated_at": normalized_catalog["generated_at"],
            "source": source,
            "selection": normalized_catalog["selection"],
            "stats": stats,
            "files": {
                "catalog": str((root / "catalog.json").resolve()),
                "raw_catalog": str(catalog_raw_path.resolve()),
                "raw_resources_dir": str(resources_dir.resolve()),
            },
            "errors": errors,
        }
        write_json(root / "catalog.json", normalized_catalog)
        write_json(root / "manifest.json", manifest)
        return normalized_catalog
