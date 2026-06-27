from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from oracle_knowledge.linker.graph_layers import (
    BUSINESS_CORE_NODE_TYPES,
    EXCLUDED_EDGE_TYPES,
    GRAPH_FILENAMES,
    MASTER_EDGE_TYPES,
)
from oracle_knowledge.indexing import (
    INDEX_SCHEMA_VERSION,
    INDEX_USER_VERSION,
    REQUIRED_SQL_INDEXES,
    default_index_path,
    file_sha256,
    read_index_metadata,
)

STATUS_OK = "OK"
STATUS_WARNING = "WARNING"
STATUS_ERROR = "ERROR"

MODULE_CORE_FILES: tuple[tuple[str, str, str], ...] = (
    ("metadata", "module.json", "json"),
    ("validated_rules", "rules/validated_rules.json", "json"),
    ("entity_aliases", "config/entity_aliases.json", "json"),
)

MODULE_SOURCE_FILES: dict[str, tuple[str, str, str]] = {
    "physical": ("physical_manifest", "physical/manifest.json", "json"),
    "functional": ("functional_fragments", "functional/fragments.jsonl", "jsonl"),
    "otbi": ("otbi_catalog", "otbi/catalog.json", "json"),
    "rest": ("rest_catalog", "rest/catalog.json", "json"),
}

MOJIBAKE_MARKERS = ("├", "┬", "�")


@dataclass(frozen=True)
class ValidationCheck:
    status: str
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "code": self.code,
            "message": self.message,
        }
        if self.path:
            payload["path"] = self.path
        if self.details:
            payload["details"] = self.details
        return payload


@dataclass
class ValidationReport:
    validation_type: str
    subject: str
    checks: list[ValidationCheck] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add(
        self,
        status: str,
        code: str,
        message: str,
        *,
        path: str | Path | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.checks.append(
            ValidationCheck(
                status=status,
                code=code,
                message=message,
                path=str(path) if path is not None else None,
                details=details or {},
            )
        )

    def ok(
        self,
        code: str,
        message: str,
        *,
        path: str | Path | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.add(STATUS_OK, code, message, path=path, details=details)

    def warning(
        self,
        code: str,
        message: str,
        *,
        path: str | Path | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.add(STATUS_WARNING, code, message, path=path, details=details)

    def error(
        self,
        code: str,
        message: str,
        *,
        path: str | Path | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.add(STATUS_ERROR, code, message, path=path, details=details)

    @property
    def ok_count(self) -> int:
        return sum(check.status == STATUS_OK for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(check.status == STATUS_WARNING for check in self.checks)

    @property
    def error_count(self) -> int:
        return sum(check.status == STATUS_ERROR for check in self.checks)

    @property
    def succeeded(self) -> bool:
        return self.error_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "validation_type": self.validation_type,
            "subject": self.subject,
            "status": (
                "FAILED"
                if self.error_count
                else "PASSED_WITH_WARNINGS"
                if self.warning_count
                else "PASSED"
            ),
            "summary": {
                "ok": self.ok_count,
                "warnings": self.warning_count,
                "errors": self.error_count,
            },
            "metadata": self.metadata,
            "checks": [check.to_dict() for check in self.checks],
        }


def _size_details(path: Path) -> dict[str, Any]:
    size_bytes = path.stat().st_size
    return {
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 2),
    }


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def _validate_json_file(
    report: ValidationReport,
    path: Path,
    *,
    required: bool,
    code_prefix: str,
) -> Any | None:
    if not path.exists():
        if required:
            report.error(
                f"{code_prefix}_MISSING",
                "Arquivo obrigatório não encontrado.",
                path=path,
            )
        else:
            report.ok(
                f"{code_prefix}_NOT_REQUIRED",
                "Fonte não configurada; o arquivo não é obrigatório.",
                path=path,
            )
        return None

    if not path.is_file():
        report.error(
            f"{code_prefix}_NOT_FILE",
            "O caminho esperado não é um arquivo.",
            path=path,
        )
        return None

    size = _size_details(path)
    if size["size_bytes"] == 0:
        report.error(
            f"{code_prefix}_EMPTY",
            "O arquivo existe, mas está vazio.",
            path=path,
            details=size,
        )
        return None

    report.ok(
        f"{code_prefix}_EXISTS",
        "Arquivo encontrado.",
        path=path,
        details=size,
    )

    try:
        payload = _load_json(path)
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        report.error(
            f"{code_prefix}_INVALID_JSON",
            f"JSON inválido ou ilegível: {exc}",
            path=path,
        )
        return None

    report.ok(
        f"{code_prefix}_VALID_JSON",
        "JSON válido em UTF-8.",
        path=path,
    )
    return payload


def _validate_jsonl_file(
    report: ValidationReport,
    path: Path,
    *,
    required: bool,
    code_prefix: str,
) -> int | None:
    if not path.exists():
        if required:
            report.error(
                f"{code_prefix}_MISSING",
                "Arquivo obrigatório não encontrado.",
                path=path,
            )
        else:
            report.ok(
                f"{code_prefix}_NOT_REQUIRED",
                "Fonte não configurada; o arquivo não é obrigatório.",
                path=path,
            )
        return None

    if not path.is_file():
        report.error(
            f"{code_prefix}_NOT_FILE",
            "O caminho esperado não é um arquivo.",
            path=path,
        )
        return None

    size = _size_details(path)
    if size["size_bytes"] == 0:
        report.error(
            f"{code_prefix}_EMPTY",
            "O arquivo existe, mas está vazio.",
            path=path,
            details=size,
        )
        return None

    report.ok(
        f"{code_prefix}_EXISTS",
        "Arquivo encontrado.",
        path=path,
        details=size,
    )

    rows = 0
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            for line_number, line in enumerate(file, start=1):
                if not line.strip():
                    continue
                json.loads(line)
                rows += 1
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        report.error(
            f"{code_prefix}_INVALID_JSONL",
            f"JSONL inválido ou ilegível: {exc}",
            path=path,
            details={"line": locals().get("line_number")},
        )
        return None

    report.ok(
        f"{code_prefix}_VALID_JSONL",
        f"JSONL válido com {rows} registros.",
        path=path,
        details={"rows": rows},
    )
    return rows


def _has_configured_source(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_configured_source(item) for item in value)
    return value is not None and bool(value)


def _module_source_expected(
    metadata: dict[str, Any],
    *,
    source_name: str,
    output_key: str,
    path: Path,
) -> bool:
    source_urls = metadata.get("source_urls")
    outputs = metadata.get("outputs")

    configured = (
        isinstance(source_urls, dict)
        and _has_configured_source(source_urls.get(source_name))
    )
    declared_output = (
        isinstance(outputs, dict)
        and _has_configured_source(outputs.get(output_key))
    )
    return configured or declared_output or path.exists()


def validate_module_directory(module_dir: str | Path) -> ValidationReport:
    root = Path(module_dir).resolve()
    report = ValidationReport("module", str(root))

    if not root.exists():
        report.error(
            "MODULE_DIRECTORY_MISSING",
            "Diretório do módulo não encontrado.",
            path=root,
        )
        return report

    if not root.is_dir():
        report.error(
            "MODULE_DIRECTORY_INVALID",
            "O caminho informado não é um diretório.",
            path=root,
        )
        return report

    report.ok(
        "MODULE_DIRECTORY_EXISTS",
        "Diretório do módulo encontrado.",
        path=root,
    )

    metadata_path = root / "module.json"
    metadata_payload = _validate_json_file(
        report,
        metadata_path,
        required=True,
        code_prefix="MODULE_METADATA",
    )
    metadata = metadata_payload if isinstance(metadata_payload, dict) else {}

    if metadata_payload is not None and not isinstance(metadata_payload, dict):
        report.error(
            "MODULE_METADATA_STRUCTURE",
            "module.json deve conter um objeto JSON.",
            path=metadata_path,
        )

    required_metadata_fields = ("module_id", "module_name", "release")
    missing_metadata_fields = [
        field_name
        for field_name in required_metadata_fields
        if not str(metadata.get(field_name) or "").strip()
    ]
    if missing_metadata_fields:
        report.error(
            "MODULE_METADATA_FIELDS",
            "module.json não contém todos os campos básicos.",
            path=metadata_path,
            details={"missing_fields": missing_metadata_fields},
        )
    elif metadata:
        report.ok(
            "MODULE_METADATA_FIELDS",
            "Campos básicos do módulo estão preenchidos.",
            path=metadata_path,
            details={
                "module_id": metadata.get("module_id"),
                "module_name": metadata.get("module_name"),
                "release": metadata.get("release"),
            },
        )

    for _, relative_path, file_type in MODULE_CORE_FILES[1:]:
        path = root / relative_path
        code_prefix = relative_path.upper().replace("/", "_").replace(".", "_")
        if file_type == "json":
            payload = _validate_json_file(
                report,
                path,
                required=True,
                code_prefix=code_prefix,
            )
            if payload is not None and not isinstance(payload, dict):
                report.error(
                    f"{code_prefix}_STRUCTURE",
                    "O arquivo deve conter um objeto JSON.",
                    path=path,
                )

    collected_sources: dict[str, dict[str, Any]] = {}

    for source_name, (output_key, relative_path, file_type) in MODULE_SOURCE_FILES.items():
        path = root / relative_path
        required = _module_source_expected(
            metadata,
            source_name=source_name,
            output_key=output_key,
            path=path,
        )
        code_prefix = f"MODULE_{source_name.upper()}"

        if file_type == "jsonl":
            rows = _validate_jsonl_file(
                report,
                path,
                required=required,
                code_prefix=code_prefix,
            )
            collected_sources[source_name] = {
                "expected": required,
                "exists": path.exists(),
                "rows": rows,
            }
            continue

        payload = _validate_json_file(
            report,
            path,
            required=required,
            code_prefix=code_prefix,
        )
        if payload is not None and not isinstance(payload, dict):
            report.error(
                f"{code_prefix}_STRUCTURE",
                "O catálogo deve conter um objeto JSON.",
                path=path,
            )
        collected_sources[source_name] = {
            "expected": required,
            "exists": path.exists(),
            "stats": payload.get("stats") if isinstance(payload, dict) else None,
        }

    report.metadata = {
        "module_id": metadata.get("module_id"),
        "module_name": metadata.get("module_name"),
        "release": metadata.get("release"),
        "sources": collected_sources,
    }
    return report


def _duplicates(values: Iterable[str], *, sample_limit: int = 10) -> tuple[int, list[str]]:
    seen: set[str] = set()
    duplicated: set[str] = set()
    for value in values:
        if value in seen:
            duplicated.add(value)
        else:
            seen.add(value)
    return len(duplicated), sorted(duplicated)[:sample_limit]


def _validate_graph_payload(
    report: ValidationReport,
    *,
    path: Path,
    expected_layer: str,
    bundle_stats: dict[str, Any] | None,
) -> None:
    payload = _validate_json_file(
        report,
        path,
        required=True,
        code_prefix=f"GRAPH_{expected_layer.upper()}",
    )
    if payload is None:
        return
    if not isinstance(payload, dict):
        report.error(
            "GRAPH_STRUCTURE",
            "O grafo deve conter um objeto JSON.",
            path=path,
        )
        return

    actual_layer = str(payload.get("graph_layer") or "")
    if actual_layer != expected_layer:
        report.error(
            "GRAPH_LAYER_MISMATCH",
            "A camada declarada no arquivo não corresponde ao nome esperado.",
            path=path,
            details={
                "expected": expected_layer,
                "actual": actual_layer,
            },
        )
    else:
        report.ok(
            "GRAPH_LAYER_VALID",
            f"Camada {expected_layer} corretamente declarada.",
            path=path,
        )

    nodes = payload.get("nodes")
    edges = payload.get("edges")
    stats = payload.get("stats")

    if not isinstance(nodes, list):
        report.error(
            "GRAPH_NODES_STRUCTURE",
            "O campo nodes deve ser uma lista.",
            path=path,
        )
        return
    if not isinstance(edges, list):
        report.error(
            "GRAPH_EDGES_STRUCTURE",
            "O campo edges deve ser uma lista.",
            path=path,
        )
        return
    if not isinstance(stats, dict):
        report.error(
            "GRAPH_STATS_STRUCTURE",
            "O campo stats deve ser um objeto JSON.",
            path=path,
        )
        stats = {}

    node_count = len(nodes)
    edge_count = len(edges)
    report.ok(
        "GRAPH_COUNTS",
        f"Grafo carregado com {node_count} nós e {edge_count} arestas.",
        path=path,
        details={"nodes": node_count, "edges": edge_count},
    )

    declared_nodes = stats.get("nodes")
    declared_edges = stats.get("edges")
    if declared_nodes != node_count or declared_edges != edge_count:
        report.error(
            "GRAPH_STATS_MISMATCH",
            "As estatísticas declaradas não correspondem ao conteúdo do grafo.",
            path=path,
            details={
                "declared_nodes": declared_nodes,
                "actual_nodes": node_count,
                "declared_edges": declared_edges,
                "actual_edges": edge_count,
            },
        )
    else:
        report.ok(
            "GRAPH_STATS_VALID",
            "Estatísticas de nós e arestas estão consistentes.",
            path=path,
        )

    if isinstance(bundle_stats, dict):
        bundle_nodes = bundle_stats.get("nodes")
        bundle_edges = bundle_stats.get("edges")
        if bundle_nodes != node_count or bundle_edges != edge_count:
            report.error(
                "GRAPH_BUNDLE_STATS_MISMATCH",
                "As estatísticas do graph_bundle não correspondem ao arquivo.",
                path=path,
                details={
                    "bundle_nodes": bundle_nodes,
                    "actual_nodes": node_count,
                    "bundle_edges": bundle_edges,
                    "actual_edges": edge_count,
                },
            )
        else:
            report.ok(
                "GRAPH_BUNDLE_STATS_VALID",
                "Estatísticas compatíveis com graph_bundle.json.",
                path=path,
            )

    node_ids: list[str] = []
    invalid_node_indexes: list[int] = []
    wrong_layer_nodes: list[str] = []

    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            invalid_node_indexes.append(index)
            continue
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            invalid_node_indexes.append(index)
            continue
        node_ids.append(node_id)
        if expected_layer != "master" and node.get("graph_layer") != expected_layer:
            wrong_layer_nodes.append(node_id)

    if invalid_node_indexes:
        report.error(
            "GRAPH_INVALID_NODES",
            "Existem nós sem estrutura válida ou sem id.",
            path=path,
            details={
                "count": len(invalid_node_indexes),
                "sample_indexes": invalid_node_indexes[:10],
            },
        )

    duplicate_count, duplicate_sample = _duplicates(node_ids)
    if duplicate_count:
        report.error(
            "GRAPH_DUPLICATE_NODE_IDS",
            "Existem IDs de nós duplicados.",
            path=path,
            details={"count": duplicate_count, "sample": duplicate_sample},
        )
    else:
        report.ok(
            "GRAPH_UNIQUE_NODE_IDS",
            "Todos os IDs de nós são únicos.",
            path=path,
        )

    if wrong_layer_nodes:
        report.error(
            "GRAPH_NODE_LAYER_MISMATCH",
            "Existem nós atribuídos a uma camada diferente do arquivo.",
            path=path,
            details={
                "count": len(wrong_layer_nodes),
                "sample": wrong_layer_nodes[:10],
            },
        )

    node_id_set = set(node_ids)
    orphan_edges: list[dict[str, Any]] = []
    forbidden_edges: list[dict[str, Any]] = []
    invalid_edges: list[int] = []
    unexpected_master_edges: list[dict[str, Any]] = []
    edge_keys: list[str] = []

    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            invalid_edges.append(index)
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        edge_type = str(edge.get("type") or "")
        if not source or not target or not edge_type:
            invalid_edges.append(index)
            continue
        edge_keys.append(f"{source}|{target}|{edge_type}")
        if source not in node_id_set or target not in node_id_set:
            orphan_edges.append(
                {"source": source, "target": target, "type": edge_type}
            )
        if edge_type in EXCLUDED_EDGE_TYPES:
            forbidden_edges.append(
                {"source": source, "target": target, "type": edge_type}
            )
        if expected_layer == "master" and edge_type not in MASTER_EDGE_TYPES:
            unexpected_master_edges.append(
                {"source": source, "target": target, "type": edge_type}
            )

    if invalid_edges:
        report.error(
            "GRAPH_INVALID_EDGES",
            "Existem arestas sem estrutura mínima válida.",
            path=path,
            details={"count": len(invalid_edges), "sample_indexes": invalid_edges[:10]},
        )

    if orphan_edges:
        report.error(
            "GRAPH_ORPHAN_EDGES",
            "Existem arestas que apontam para nós ausentes no arquivo.",
            path=path,
            details={"count": len(orphan_edges), "sample": orphan_edges[:10]},
        )
    else:
        report.ok(
            "GRAPH_NO_ORPHAN_EDGES",
            "Nenhuma aresta órfã foi encontrada.",
            path=path,
        )

    if forbidden_edges:
        report.error(
            "GRAPH_FORBIDDEN_EDGES",
            "Foram encontradas arestas removidas pela arquitetura atual.",
            path=path,
            details={"count": len(forbidden_edges), "sample": forbidden_edges[:10]},
        )
    else:
        report.ok(
            "GRAPH_NO_FORBIDDEN_EDGES",
            "Nenhuma aresta global indesejada foi encontrada.",
            path=path,
        )

    if unexpected_master_edges:
        report.error(
            "MASTER_UNEXPECTED_EDGE_TYPES",
            "O master_graph contém tipos de aresta não autorizados.",
            path=path,
            details={
                "count": len(unexpected_master_edges),
                "sample": unexpected_master_edges[:10],
            },
        )

    duplicate_edge_count, duplicate_edge_sample = _duplicates(edge_keys)
    if duplicate_edge_count:
        report.warning(
            "GRAPH_DUPLICATE_EDGES",
            "Existem arestas duplicadas com a mesma origem, destino e tipo.",
            path=path,
            details={
                "count": duplicate_edge_count,
                "sample": duplicate_edge_sample,
            },
        )

    if expected_layer == "physical":
        stub_count = sum(
            isinstance(node, dict)
            and node.get("node_type") == "physical_table_stub"
            for node in nodes
        )
        if stub_count:
            report.warning(
                "GRAPH_PHYSICAL_STUBS",
                "O grafo físico contém tabelas referenciadas ainda não coletadas.",
                path=path,
                details={"count": stub_count},
            )

    if expected_layer == "master":
        business_seed_count = sum(
            isinstance(node, dict)
            and node.get("node_type") in BUSINESS_CORE_NODE_TYPES
            for node in nodes
        )
        if not nodes:
            report.warning(
                "MASTER_EMPTY",
                "master_graph vazio; a busca federada dependerá de fallback semântico.",
                path=path,
            )
        elif not business_seed_count:
            report.warning(
                "MASTER_WITHOUT_BUSINESS_SEEDS",
                "master_graph não contém entidades, atributos ou regras de negócio.",
                path=path,
            )
        else:
            report.ok(
                "MASTER_BUSINESS_SEEDS",
                "master_graph contém pontos de entrada de negócio.",
                path=path,
                details={"count": business_seed_count},
            )


def validate_graph_directory(graph_dir: str | Path) -> ValidationReport:
    root = Path(graph_dir).resolve()
    report = ValidationReport("graph", str(root))

    if not root.exists():
        report.error(
            "GRAPH_DIRECTORY_MISSING",
            "Diretório de grafos não encontrado.",
            path=root,
        )
        return report
    if not root.is_dir():
        report.error(
            "GRAPH_DIRECTORY_INVALID",
            "O caminho informado não é um diretório.",
            path=root,
        )
        return report

    report.ok(
        "GRAPH_DIRECTORY_EXISTS",
        "Diretório de grafos encontrado.",
        path=root,
    )

    bundle_path = root / "graph_bundle.json"
    bundle_payload = _validate_json_file(
        report,
        bundle_path,
        required=True,
        code_prefix="GRAPH_BUNDLE",
    )
    bundle = bundle_payload if isinstance(bundle_payload, dict) else {}
    if bundle_payload is not None and not isinstance(bundle_payload, dict):
        report.error(
            "GRAPH_BUNDLE_STRUCTURE",
            "graph_bundle.json deve conter um objeto JSON.",
            path=bundle_path,
        )

    bundle_stats = bundle.get("stats") if isinstance(bundle.get("stats"), dict) else {}

    for layer, filename in GRAPH_FILENAMES.items():
        _validate_graph_payload(
            report,
            path=root / filename,
            expected_layer=layer,
            bundle_stats=bundle_stats.get(layer) if isinstance(bundle_stats, dict) else None,
        )

    report.metadata = {
        "expected_files": [
            *GRAPH_FILENAMES.values(),
            "graph_bundle.json",
        ],
        "bundle_version": bundle.get("version"),
        "generated_at": bundle.get("generated_at"),
    }
    return report


def validate_index_database(
    index_path: str | Path | None = None,
    *,
    graph_dir: str | Path | None = None,
    full_hash: bool = False,
) -> ValidationReport:
    graph_root = Path(graph_dir).resolve() if graph_dir is not None else None
    path = (
        Path(index_path).resolve()
        if index_path is not None
        else default_index_path(graph_root or Path("."))
    )
    report = ValidationReport("index", str(path))

    if graph_root is not None:
        if graph_root.is_dir():
            report.ok(
                "INDEX_GRAPH_DIRECTORY",
                "Diretório de grafos encontrado.",
                path=graph_root,
            )
        else:
            report.error(
                "INDEX_GRAPH_DIRECTORY",
                "Diretório de grafos não encontrado.",
                path=graph_root,
            )

    if not path.exists():
        report.error(
            "INDEX_FILE_MISSING",
            "Índice SQLite não encontrado.",
            path=path,
        )
        return report
    if not path.is_file():
        report.error(
            "INDEX_FILE_INVALID",
            "O caminho do índice não é um arquivo.",
            path=path,
        )
        return report

    report.ok(
        "INDEX_FILE_EXISTS",
        "Índice SQLite encontrado.",
        path=path,
        details=_size_details(path),
    )

    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        report.error(
            "INDEX_OPEN_FAILED",
            f"Não foi possível abrir o índice SQLite: {exc}",
            path=path,
        )
        return report

    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        integrity_value = str(integrity[0]) if integrity else "unknown"
        if integrity_value == "ok":
            report.ok(
                "INDEX_INTEGRITY",
                "PRAGMA integrity_check retornou ok.",
                path=path,
            )
        else:
            report.error(
                "INDEX_INTEGRITY",
                "O SQLite reportou falha de integridade.",
                path=path,
                details={"result": integrity_value},
            )

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        required_tables = {
            "index_metadata",
            "graph_files",
            "nodes",
            "node_modules",
            "edges",
            "nodes_fts",
        }
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            report.error(
                "INDEX_SCHEMA_TABLES",
                "O índice não contém todas as estruturas obrigatórias.",
                path=path,
                details={"missing": missing_tables},
            )
            return report
        report.ok(
            "INDEX_SCHEMA_TABLES",
            "Todas as estruturas obrigatórias foram encontradas.",
            path=path,
        )

        metadata = read_index_metadata(connection)
        schema_version = metadata.get("schema_version")
        if schema_version != INDEX_SCHEMA_VERSION:
            report.error(
                "INDEX_SCHEMA_VERSION",
                "Versão do esquema do índice incompatível.",
                path=path,
                details={
                    "expected": INDEX_SCHEMA_VERSION,
                    "actual": schema_version,
                },
            )
        else:
            report.ok(
                "INDEX_SCHEMA_VERSION",
                "Versão do esquema do índice compatível.",
                path=path,
                details={"version": schema_version},
            )

        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != INDEX_USER_VERSION:
            report.error(
                "INDEX_USER_VERSION",
                "PRAGMA user_version incompatível.",
                path=path,
                details={
                    "expected": INDEX_USER_VERSION,
                    "actual": user_version,
                },
            )
        else:
            report.ok(
                "INDEX_USER_VERSION",
                "PRAGMA user_version compatível.",
                path=path,
                details={"version": user_version},
            )

        sql_indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        missing_indexes = sorted(REQUIRED_SQL_INDEXES - sql_indexes)
        if missing_indexes:
            report.error(
                "INDEX_SQL_INDEXES",
                "Existem índices SQL obrigatórios ausentes.",
                path=path,
                details={"missing": missing_indexes},
            )
        else:
            report.ok(
                "INDEX_SQL_INDEXES",
                "Índices SQL obrigatórios encontrados.",
                path=path,
                details={"count": len(REQUIRED_SQL_INDEXES)},
            )

        counts = {
            "nodes": int(connection.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]),
            "edges": int(connection.execute("SELECT COUNT(*) FROM edges").fetchone()[0]),
            "node_modules": int(
                connection.execute("SELECT COUNT(*) FROM node_modules").fetchone()[0]
            ),
            "nodes_fts": int(
                connection.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
            ),
        }

        declared_counts = {
            "nodes": metadata.get("node_count"),
            "edges": metadata.get("edge_count"),
            "node_modules": metadata.get("module_link_count"),
            "nodes_fts": metadata.get("fts_row_count"),
        }
        mismatches = {
            key: {"declared": declared_counts[key], "actual": actual}
            for key, actual in counts.items()
            if declared_counts.get(key) != actual
        }
        if mismatches:
            report.error(
                "INDEX_METADATA_COUNTS",
                "As contagens do índice divergem dos metadados.",
                path=path,
                details={"mismatches": mismatches},
            )
        else:
            report.ok(
                "INDEX_METADATA_COUNTS",
                "As contagens do índice correspondem aos metadados.",
                path=path,
                details=counts,
            )

        if counts["nodes_fts"] != counts["nodes"]:
            report.error(
                "INDEX_FTS_COVERAGE",
                "A quantidade de linhas FTS5 difere da quantidade de nós.",
                path=path,
                details={
                    "nodes": counts["nodes"],
                    "fts_rows": counts["nodes_fts"],
                },
            )
        else:
            report.ok(
                "INDEX_FTS_COVERAGE",
                "Todos os nós possuem uma linha no FTS5.",
                path=path,
                details={"count": counts["nodes_fts"]},
            )

        try:
            connection.execute(
                "SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ? LIMIT 1",
                ('"validation"',),
            ).fetchall()
        except sqlite3.Error as exc:
            report.error(
                "INDEX_FTS_QUERY",
                f"O FTS5 não conseguiu executar uma consulta: {exc}",
                path=path,
            )
        else:
            report.ok(
                "INDEX_FTS_QUERY",
                "O FTS5 executou uma consulta de validação.",
                path=path,
            )

        orphan_edges = int(
            connection.execute(
                """
                SELECT COUNT(*)
                  FROM edges e
             LEFT JOIN nodes s ON s.node_pk = e.source_node_pk
             LEFT JOIN nodes t ON t.node_pk = e.target_node_pk
                 WHERE s.node_pk IS NULL OR t.node_pk IS NULL
                """
            ).fetchone()[0]
        )
        if orphan_edges:
            report.error(
                "INDEX_ORPHAN_EDGES",
                "Existem arestas órfãs no índice.",
                path=path,
                details={"count": orphan_edges},
            )
        else:
            report.ok(
                "INDEX_ORPHAN_EDGES",
                "Nenhuma aresta órfã foi encontrada.",
                path=path,
            )

        orphan_modules = int(
            connection.execute(
                """
                SELECT COUNT(*)
                  FROM node_modules m
             LEFT JOIN nodes n ON n.node_pk = m.node_pk
                 WHERE n.node_pk IS NULL
                """
            ).fetchone()[0]
        )
        if orphan_modules:
            report.error(
                "INDEX_ORPHAN_MODULES",
                "Existem vínculos de módulo órfãos no índice.",
                path=path,
                details={"count": orphan_modules},
            )
        else:
            report.ok(
                "INDEX_ORPHAN_MODULES",
                "Nenhum vínculo de módulo órfão foi encontrado.",
                path=path,
            )

        bundle_metadata = metadata.get("graph_bundle")
        if not isinstance(bundle_metadata, dict):
            report.error(
                "INDEX_BUNDLE_METADATA",
                "O índice não contém metadados do graph_bundle.json.",
                path=path,
            )
        else:
            bundle_source = (
                graph_root / "graph_bundle.json"
                if graph_root is not None
                else Path(str(bundle_metadata.get("path") or ""))
            )
            if not bundle_source.is_file():
                report.error(
                    "INDEX_BUNDLE_SOURCE",
                    "graph_bundle.json usado na construção não foi encontrado.",
                    path=bundle_source,
                )
            else:
                bundle_stat = bundle_source.stat()
                bundle_stale = (
                    bundle_stat.st_size != int(bundle_metadata.get("size_bytes") or -1)
                    or bundle_stat.st_mtime_ns != int(bundle_metadata.get("modified_ns") or -1)
                )
                if not bundle_stale and full_hash:
                    bundle_stale = (
                        file_sha256(bundle_source)
                        != str(bundle_metadata.get("sha256") or "")
                    )
                if bundle_stale:
                    report.error(
                        "INDEX_BUNDLE_STALE",
                        "O índice está desatualizado em relação ao graph_bundle.json.",
                        path=bundle_source,
                    )
                else:
                    report.ok(
                        "INDEX_BUNDLE_FRESHNESS",
                        "O graph_bundle.json corresponde ao índice.",
                        path=bundle_source,
                        details={"full_hash": full_hash},
                    )

        graph_rows = connection.execute(
            """
            SELECT graph_layer, filename, source_path, size_bytes,
                   modified_ns, sha256, node_count, edge_count
              FROM graph_files
          ORDER BY graph_layer
            """
        ).fetchall()
        if len(graph_rows) != len(GRAPH_FILENAMES):
            report.error(
                "INDEX_GRAPH_FILE_COUNT",
                "A quantidade de grafos indexados é incompatível.",
                path=path,
                details={
                    "expected": len(GRAPH_FILENAMES),
                    "actual": len(graph_rows),
                },
            )
        else:
            report.ok(
                "INDEX_GRAPH_FILE_COUNT",
                "Todos os grafos esperados estão registrados no índice.",
                path=path,
                details={"count": len(graph_rows)},
            )

        stale_files: list[dict[str, Any]] = []
        missing_files: list[str] = []
        count_mismatches: list[dict[str, Any]] = []
        for (
            layer,
            filename,
            source_path,
            size_bytes,
            modified_ns,
            stored_hash,
            declared_nodes,
            declared_edges,
        ) in graph_rows:
            expected_filename = GRAPH_FILENAMES.get(str(layer))
            if expected_filename != filename:
                stale_files.append(
                    {
                        "layer": layer,
                        "reason": "filename",
                        "expected": expected_filename,
                        "actual": filename,
                    }
                )

            source = (
                graph_root / str(filename)
                if graph_root is not None
                else Path(str(source_path))
            )
            if not source.is_file():
                missing_files.append(str(source))
                continue

            stat = source.stat()
            if stat.st_size != int(size_bytes) or stat.st_mtime_ns != int(modified_ns):
                stale_files.append(
                    {
                        "layer": layer,
                        "reason": "size_or_modified_time",
                        "path": str(source),
                        "stored_size": int(size_bytes),
                        "current_size": stat.st_size,
                        "stored_modified_ns": int(modified_ns),
                        "current_modified_ns": stat.st_mtime_ns,
                    }
                )
            elif full_hash:
                current_hash = file_sha256(source)
                if current_hash != stored_hash:
                    stale_files.append(
                        {
                            "layer": layer,
                            "reason": "sha256",
                            "path": str(source),
                            "stored": stored_hash,
                            "current": current_hash,
                        }
                    )

            database_counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM nodes WHERE graph_layer = ?),
                    (SELECT COUNT(*) FROM edges WHERE graph_layer = ?)
                """,
                (layer, layer),
            ).fetchone()
            if (
                int(database_counts[0]) != int(declared_nodes)
                or int(database_counts[1]) != int(declared_edges)
            ):
                count_mismatches.append(
                    {
                        "layer": layer,
                        "declared_nodes": int(declared_nodes),
                        "actual_nodes": int(database_counts[0]),
                        "declared_edges": int(declared_edges),
                        "actual_edges": int(database_counts[1]),
                    }
                )

        if missing_files:
            report.error(
                "INDEX_SOURCE_FILES_MISSING",
                "Existem grafos de origem ausentes.",
                path=path,
                details={"files": missing_files},
            )
        elif stale_files:
            report.error(
                "INDEX_STALE",
                "O índice está desatualizado em relação aos grafos de origem.",
                path=path,
                details={"files": stale_files},
            )
        else:
            report.ok(
                "INDEX_FRESHNESS",
                "O índice corresponde aos arquivos de grafo atuais.",
                path=path,
                details={"full_hash": full_hash},
            )

        if count_mismatches:
            report.error(
                "INDEX_LAYER_COUNTS",
                "As contagens por camada estão inconsistentes.",
                path=path,
                details={"layers": count_mismatches},
            )
        else:
            report.ok(
                "INDEX_LAYER_COUNTS",
                "As contagens por camada estão consistentes.",
                path=path,
            )

        report.metadata = {
            "index_path": str(path),
            "graph_dir": str(graph_root) if graph_root is not None else metadata.get("graph_dir"),
            "schema_version": schema_version,
            "counts": counts,
            "full_hash": full_hash,
        }
    except sqlite3.DatabaseError as exc:
        report.error(
            "INDEX_DATABASE_ERROR",
            f"Falha ao validar o banco SQLite: {exc}",
            path=path,
        )
    finally:
        connection.close()

    return report


def validate_search_result(
    result_path: str | Path,
    *,
    max_characters: int = 14000,
) -> ValidationReport:
    path = Path(result_path).resolve()
    report = ValidationReport("result", str(path))
    payload = _validate_json_file(
        report,
        path,
        required=True,
        code_prefix="SEARCH_RESULT",
    )
    if payload is None:
        return report
    if not isinstance(payload, dict):
        report.error(
            "SEARCH_RESULT_STRUCTURE",
            "O resultado deve conter um objeto JSON.",
            path=path,
        )
        return report

    query = payload.get("query")
    context = payload.get("context")
    results = payload.get("results")
    characters = payload.get("characters")

    if not isinstance(query, str) or not query.strip():
        report.error(
            "SEARCH_RESULT_QUERY",
            "O resultado não contém uma query válida.",
            path=path,
        )
    else:
        report.ok(
            "SEARCH_RESULT_QUERY",
            "Query encontrada no resultado.",
            path=path,
        )

    if not isinstance(context, str):
        report.error(
            "SEARCH_RESULT_CONTEXT",
            "O campo context deve ser uma string.",
            path=path,
        )
        context = ""
    else:
        report.ok(
            "SEARCH_RESULT_CONTEXT",
            "Contexto encontrado no resultado.",
            path=path,
        )

    if not isinstance(results, list):
        report.error(
            "SEARCH_RESULT_ITEMS",
            "O campo results deve ser uma lista.",
            path=path,
        )
        results = []
    elif not results:
        report.warning(
            "SEARCH_RESULT_EMPTY",
            "A pesquisa não retornou evidências.",
            path=path,
        )
    else:
        report.ok(
            "SEARCH_RESULT_ITEMS",
            f"A pesquisa retornou {len(results)} evidências.",
            path=path,
            details={"count": len(results)},
        )

    actual_characters = len(context)
    if characters != actual_characters:
        report.error(
            "SEARCH_RESULT_CHARACTER_MISMATCH",
            "O campo characters não corresponde ao contexto renderizado.",
            path=path,
            details={
                "declared": characters,
                "actual": actual_characters,
            },
        )
    else:
        report.ok(
            "SEARCH_RESULT_CHARACTER_COUNT",
            "A contagem de caracteres está correta.",
            path=path,
            details={"characters": actual_characters},
        )

    if actual_characters > max_characters:
        report.error(
            "SEARCH_RESULT_BUDGET_EXCEEDED",
            "O contexto excede o limite informado.",
            path=path,
            details={
                "characters": actual_characters,
                "max_characters": max_characters,
            },
        )
    else:
        report.ok(
            "SEARCH_RESULT_BUDGET",
            "O contexto respeita o limite de caracteres.",
            path=path,
            details={
                "characters": actual_characters,
                "max_characters": max_characters,
            },
        )

    result_ids: list[str] = []
    missing_source_indexes: list[int] = []
    invalid_result_indexes: list[int] = []
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            invalid_result_indexes.append(index)
            continue
        result_id = str(result.get("id") or "").strip()
        if not result_id:
            invalid_result_indexes.append(index)
        else:
            result_ids.append(result_id)
        if not result.get("source") and not result.get("sources"):
            missing_source_indexes.append(index)

    if invalid_result_indexes:
        report.error(
            "SEARCH_RESULT_INVALID_ITEMS",
            "Existem evidências sem estrutura válida ou sem id.",
            path=path,
            details={
                "count": len(invalid_result_indexes),
                "sample_indexes": invalid_result_indexes[:10],
            },
        )

    duplicate_count, duplicate_sample = _duplicates(result_ids)
    if duplicate_count:
        report.error(
            "SEARCH_RESULT_DUPLICATES",
            "Existem evidências duplicadas no resultado.",
            path=path,
            details={"count": duplicate_count, "sample": duplicate_sample},
        )
    elif result_ids:
        report.ok(
            "SEARCH_RESULT_UNIQUE_ITEMS",
            "As evidências selecionadas são únicas.",
            path=path,
        )

    if missing_source_indexes:
        report.warning(
            "SEARCH_RESULT_MISSING_SOURCES",
            "Existem evidências sem informação de fonte.",
            path=path,
            details={
                "count": len(missing_source_indexes),
                "sample_indexes": missing_source_indexes[:10],
            },
        )

    routing = payload.get("routing")
    if routing is None:
        report.warning(
            "SEARCH_RESULT_WITHOUT_ROUTING",
            "O resultado não contém diagnóstico de roteamento federado.",
            path=path,
        )
    elif not isinstance(routing, dict):
        report.error(
            "SEARCH_RESULT_ROUTING_STRUCTURE",
            "O campo routing deve ser um objeto JSON.",
            path=path,
        )
    else:
        required_routing_fields = (
            "master_business_seeds",
            "master_routes",
            "semantic_fallback_roots",
            "candidate_count",
        )
        missing_fields = [
            field_name
            for field_name in required_routing_fields
            if field_name not in routing
        ]
        if missing_fields:
            report.warning(
                "SEARCH_RESULT_ROUTING_FIELDS",
                "O diagnóstico de roteamento está incompleto.",
                path=path,
                details={"missing_fields": missing_fields},
            )
        else:
            report.ok(
                "SEARCH_RESULT_ROUTING",
                "Diagnóstico de roteamento federado encontrado.",
                path=path,
            )

    combined_text = f"{query or ''}\n{context}"
    markers_found = sorted(
        marker for marker in MOJIBAKE_MARKERS if marker in combined_text
    )
    if markers_found:
        report.warning(
            "SEARCH_RESULT_ENCODING",
            "Foram encontrados sinais de possível corrupção de codificação.",
            path=path,
            details={"markers": markers_found},
        )
    else:
        report.ok(
            "SEARCH_RESULT_ENCODING",
            "Nenhum marcador comum de mojibake foi encontrado.",
            path=path,
        )

    report.metadata = {
        "result_count": len(results),
        "characters": actual_characters,
        "max_characters": max_characters,
    }
    return report


def validate_environment(work_dir: str | Path = ".") -> ValidationReport:
    root = Path(work_dir).resolve()
    report = ValidationReport("doctor", str(root))

    minimum_version = (3, 10)
    current_version = sys.version_info[:3]
    if current_version < minimum_version:
        report.error(
            "PYTHON_VERSION",
            "Versão do Python inferior à mínima suportada.",
            details={
                "current": ".".join(map(str, current_version)),
                "minimum": ".".join(map(str, minimum_version)),
            },
        )
    else:
        report.ok(
            "PYTHON_VERSION",
            "Versão do Python compatível.",
            details={"current": ".".join(map(str, current_version))},
        )

    dependencies = {
        "bs4": "beautifulsoup4",
        "requests": "requests",
        "urllib3": "urllib3",
        "numpy": "numpy",
        "sentence_transformers": "sentence-transformers",
    }
    missing_dependencies = [
        package_name
        for module_name, package_name in dependencies.items()
        if importlib.util.find_spec(module_name) is None
    ]
    if missing_dependencies:
        report.error(
            "PYTHON_DEPENDENCIES",
            "Existem dependências obrigatórias não instaladas.",
            details={"missing": missing_dependencies},
        )
    else:
        report.ok(
            "PYTHON_DEPENDENCIES",
            "Dependências obrigatórias encontradas.",
            details={"packages": sorted(dependencies.values())},
        )

    try:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE VIRTUAL TABLE validation_fts USING fts5(content)")
        connection.close()
    except sqlite3.Error as exc:
        report.error(
            "SQLITE_FTS5",
            f"SQLite FTS5 não está disponível: {exc}",
            details={"sqlite_version": sqlite3.sqlite_version},
        )
    else:
        report.ok(
            "SQLITE_FTS5",
            "SQLite e FTS5 estão disponíveis para o futuro índice de pesquisa.",
            details={"sqlite_version": sqlite3.sqlite_version},
        )

    stdout_encoding = (sys.stdout.encoding or "").lower()
    if "utf" not in stdout_encoding:
        report.warning(
            "STDOUT_ENCODING",
            "A saída padrão não está configurada como UTF-8.",
            details={"encoding": sys.stdout.encoding},
        )
    else:
        report.ok(
            "STDOUT_ENCODING",
            "Saída padrão configurada em UTF-8.",
            details={"encoding": sys.stdout.encoding},
        )

    if not root.exists():
        report.error(
            "WORK_DIRECTORY",
            "Diretório de trabalho não encontrado.",
            path=root,
        )
        return report

    probe = root / ".oracle_kb_write_test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        report.error(
            "WORK_DIRECTORY_WRITABLE",
            f"Sem permissão de escrita no diretório: {exc}",
            path=root,
        )
    else:
        report.ok(
            "WORK_DIRECTORY_WRITABLE",
            "Diretório de trabalho possui permissão de escrita.",
            path=root,
        )

    disk_usage = shutil.disk_usage(root)
    free_gb = round(disk_usage.free / (1024 ** 3), 2)
    if free_gb < 1:
        report.warning(
            "DISK_SPACE",
            "Há menos de 1 GB livre no volume de trabalho.",
            path=root,
            details={"free_gb": free_gb},
        )
    else:
        report.ok(
            "DISK_SPACE",
            "Espaço livre disponível no volume de trabalho.",
            path=root,
            details={"free_gb": free_gb},
        )

    project_markers = (
        root / "build_knowledge_base.py",
        root / "pyproject.toml",
        root / "oracle_knowledge",
    )
    missing_markers = [str(path) for path in project_markers if not path.exists()]
    if missing_markers:
        report.warning(
            "PROJECT_ROOT",
            "O diretório informado pode não ser a raiz do projeto.",
            path=root,
            details={"missing": missing_markers},
        )
    else:
        report.ok(
            "PROJECT_ROOT",
            "Estrutura básica do projeto encontrada.",
            path=root,
        )

    report.metadata = {
        "python_executable": sys.executable,
        "python_version": ".".join(map(str, current_version)),
        "sqlite_version": sqlite3.sqlite_version,
        "work_dir": str(root),
    }
    return report


def render_validation_report(report: ValidationReport) -> str:
    labels = {
        STATUS_OK: "OK",
        STATUS_WARNING: "AVISO",
        STATUS_ERROR: "ERRO",
    }
    lines = [
        f"Validação: {report.validation_type}",
        f"Alvo: {report.subject}",
        "",
    ]

    for check in report.checks:
        line = f"[{labels.get(check.status, check.status)}] {check.message}"
        if check.path:
            line += f" ({check.path})"
        lines.append(line)
        if check.details:
            details = ", ".join(
                f"{key}={value}"
                for key, value in check.details.items()
            )
            lines.append(f"       {details}")

    lines.extend(
        [
            "",
            "Validação concluída",
            f"OK: {report.ok_count}",
            f"Avisos: {report.warning_count}",
            f"Erros: {report.error_count}",
            (
                "Status: REPROVADO"
                if report.error_count
                else "Status: APROVADO COM AVISOS"
                if report.warning_count
                else "Status: APROVADO"
            ),
        ]
    )
    return "\n".join(lines)
