from __future__ import annotations

import hashlib
import json
import os
import sqlite3

import numpy as np
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from oracle_knowledge.common import utc_now_iso
from oracle_knowledge.linker.graph_layers import GRAPH_FILENAMES
from oracle_knowledge.search.semantic_context import (
    DEFAULT_EMBEDDING_MODEL,
    SemanticTextSelector,
)
from oracle_knowledge.search.semantic_documents import semantic_document_text

INDEX_SCHEMA_VERSION = "4.0.0"
INDEX_USER_VERSION = 4
SUPPORTED_INDEX_SCHEMA_VERSIONS = {"2.0.0", "3.0.0", INDEX_SCHEMA_VERSION}
INDEX_BUNDLE_VERSION = "1.0.0"
DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH = Path("search_index")
DEFAULT_INDEX_RELATIVE_PATH = DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH / "knowledge_index.sqlite"
DEFAULT_INDEX_BUNDLE_RELATIVE_PATH = DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH / "index_bundle.json"
LAYER_INDEX_FILENAMES = {
    layer: f"{layer}.sqlite"
    for layer in GRAPH_FILENAMES
}

REQUIRED_SQL_INDEXES = {
    "idx_nodes_layer_type",
    "idx_nodes_layer_title",
    "idx_nodes_qualified_name",
    "idx_node_modules_module",
    "idx_edges_source",
    "idx_edges_target",
    "idx_edges_type",
    "idx_semantic_roots_model",
    "idx_semantic_segments_model",
}


@dataclass(frozen=True)
class GraphFileInfo:
    layer: str
    filename: str
    path: Path
    size_bytes: int
    modified_ns: int
    sha256: str
    graph_version: str | None
    generated_at: str | None
    node_count: int
    edge_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "filename": self.filename,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "modified_ns": self.modified_ns,
            "sha256": self.sha256,
            "graph_version": self.graph_version,
            "generated_at": self.generated_at,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
        }


@dataclass(frozen=True)
class IndexBuildResult:
    graph_dir: Path
    index_path: Path
    graph_files: tuple[GraphFileInfo, ...]
    node_count: int
    edge_count: int
    fts_row_count: int
    module_link_count: int
    semantic_root_count: int
    semantic_dimensions: int | None
    semantic_model_name: str | None
    semantic_segment_count: int = 0
    semantic_segment_node_count: int = 0
    indexed_layers: tuple[str, ...] = ()
    reused_semantic_root_count: int = 0
    generated_semantic_root_count: int = 0
    reused_semantic_segment_count: int = 0
    generated_semantic_segment_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_dir": str(self.graph_dir),
            "index_path": str(self.index_path),
            "schema_version": INDEX_SCHEMA_VERSION,
            "graph_files": [item.to_dict() for item in self.graph_files],
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "fts_row_count": self.fts_row_count,
            "module_link_count": self.module_link_count,
            "semantic_root_count": self.semantic_root_count,
            "semantic_dimensions": self.semantic_dimensions,
            "semantic_model_name": self.semantic_model_name,
            "semantic_segment_count": self.semantic_segment_count,
            "semantic_segment_node_count": self.semantic_segment_node_count,
            "indexed_layers": list(self.indexed_layers),
            "reused_semantic_root_count": self.reused_semantic_root_count,
            "generated_semantic_root_count": self.generated_semantic_root_count,
            "reused_semantic_segment_count": self.reused_semantic_segment_count,
            "generated_semantic_segment_count": self.generated_semantic_segment_count,
        }


@dataclass(frozen=True)
class IndexBundleBuildResult:
    graph_dir: Path
    bundle_path: Path
    requested_layers: tuple[str, ...]
    built_layers: tuple[str, ...]
    skipped_layers: tuple[str, ...]
    indexes: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_dir": str(self.graph_dir),
            "bundle_path": str(self.bundle_path),
            "bundle_version": INDEX_BUNDLE_VERSION,
            "schema_version": INDEX_SCHEMA_VERSION,
            "requested_layers": list(self.requested_layers),
            "built_layers": list(self.built_layers),
            "skipped_layers": list(self.skipped_layers),
            "indexes": self.indexes,
        }


def default_index_path(graph_dir: str | Path) -> Path:
    """Caminho legado do índice monolítico."""
    return Path(graph_dir).resolve() / DEFAULT_INDEX_RELATIVE_PATH


def default_index_bundle_path(graph_dir: str | Path) -> Path:
    return Path(graph_dir).resolve() / DEFAULT_INDEX_BUNDLE_RELATIVE_PATH


def default_layer_index_path(graph_dir: str | Path, layer: str) -> Path:
    if layer not in LAYER_INDEX_FILENAMES:
        raise ValueError(f"Camada de índice desconhecida: {layer}")
    return (
        Path(graph_dir).resolve()
        / DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH
        / LAYER_INDEX_FILENAMES[layer]
    )


def resolve_index_source(
    graph_dir: str | Path,
    index_path: str | Path | None = None,
) -> Path:
    if index_path is not None:
        candidate = Path(index_path).resolve()
        if candidate.is_dir():
            return candidate / "index_bundle.json"
        return candidate
    bundle_path = default_index_bundle_path(graph_dir)
    if bundle_path.is_file():
        return bundle_path
    return default_index_path(graph_dir)


def read_index_bundle(path: str | Path) -> dict[str, Any]:
    bundle_path = Path(path).resolve()
    try:
        with bundle_path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Manifesto de índices inválido ou ilegível: {bundle_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"O manifesto de índices deve ser um objeto JSON: {bundle_path}")
    return payload


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        while chunk := file.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _load_graph(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            payload = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Grafo inválido ou ilegível: {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"O grafo deve ser um objeto JSON: {path}")
    if not isinstance(payload.get("nodes"), list):
        raise ValueError(f"O grafo não contém uma lista nodes válida: {path}")
    if not isinstance(payload.get("edges"), list):
        raise ValueError(f"O grafo não contém uma lista edges válida: {path}")
    return payload


def _configure_build_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = OFF")
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA cache_size = -131072")
    connection.execute("PRAGMA foreign_keys = OFF")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA user_version = 4;

        CREATE TABLE index_metadata (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE graph_files (
            graph_layer TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            source_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            modified_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            graph_version TEXT,
            generated_at TEXT,
            node_count INTEGER NOT NULL,
            edge_count INTEGER NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE nodes (
            node_pk INTEGER PRIMARY KEY,
            graph_layer TEXT NOT NULL,
            node_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            title TEXT NOT NULL,
            qualified_name TEXT,
            source_type TEXT,
            search_text TEXT NOT NULL,
            summary TEXT,
            modules_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE (graph_layer, node_id)
        );

        CREATE TABLE node_modules (
            node_pk INTEGER NOT NULL,
            module_id TEXT NOT NULL,
            PRIMARY KEY (node_pk, module_id),
            FOREIGN KEY (node_pk) REFERENCES nodes(node_pk) ON DELETE CASCADE
        ) WITHOUT ROWID;

        CREATE TABLE edges (
            edge_pk INTEGER PRIMARY KEY,
            graph_layer TEXT NOT NULL,
            source_node_pk INTEGER NOT NULL,
            target_node_pk INTEGER NOT NULL,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL,
            source_layer TEXT,
            target_layer TEXT,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            FOREIGN KEY (source_node_pk) REFERENCES nodes(node_pk),
            FOREIGN KEY (target_node_pk) REFERENCES nodes(node_pk)
        );

        CREATE VIRTUAL TABLE nodes_fts USING fts5(
            node_id UNINDEXED,
            graph_layer UNINDEXED,
            title,
            qualified_name,
            search_text,
            tokenize = 'unicode61 remove_diacritics 2'
        );

        CREATE TABLE semantic_roots (
            node_pk INTEGER PRIMARY KEY,
            model_name TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            FOREIGN KEY (node_pk) REFERENCES nodes(node_pk) ON DELETE CASCADE
        ) WITHOUT ROWID;

        CREATE TABLE semantic_segments (
            node_pk INTEGER NOT NULL,
            segment_index INTEGER NOT NULL,
            model_name TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            PRIMARY KEY (node_pk, segment_index),
            FOREIGN KEY (node_pk) REFERENCES nodes(node_pk) ON DELETE CASCADE
        ) WITHOUT ROWID;

        CREATE INDEX idx_nodes_layer_type
            ON nodes(graph_layer, node_type);
        CREATE INDEX idx_nodes_layer_title
            ON nodes(graph_layer, title);
        CREATE INDEX idx_nodes_qualified_name
            ON nodes(qualified_name)
            WHERE qualified_name IS NOT NULL;
        CREATE INDEX idx_node_modules_module
            ON node_modules(module_id, node_pk);
        CREATE INDEX idx_edges_source
            ON edges(graph_layer, source_node_pk, edge_type);
        CREATE INDEX idx_edges_target
            ON edges(graph_layer, target_node_pk, edge_type);
        CREATE INDEX idx_edges_type
            ON edges(graph_layer, edge_type);
        CREATE INDEX idx_semantic_roots_model
            ON semantic_roots(model_name, dimensions);
        CREATE INDEX idx_semantic_segments_model
            ON semantic_segments(model_name, dimensions, node_pk);
        """
    )


def _node_modules(node: dict[str, Any]) -> list[str]:
    modules: list[str] = []
    raw_modules = node.get("modules")
    if isinstance(raw_modules, (list, tuple, set)):
        modules.extend(str(value).strip() for value in raw_modules if str(value).strip())

    module_id = str(node.get("module_id") or "").strip()
    if module_id:
        modules.append(module_id)

    source = node.get("source")
    if isinstance(source, dict):
        source_module = str(source.get("module_id") or "").strip()
        if source_module:
            modules.append(source_module)

    return list(dict.fromkeys(modules))


def _node_summary(node: dict[str, Any]) -> str:
    for key in ("summary", "description", "text", "question"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _insert_graph(
    connection: sqlite3.Connection,
    *,
    graph_dir: Path,
    layer: str,
    filename: str,
    node_pk_start: int,
    edge_pk_start: int,
    batch_size: int,
) -> tuple[GraphFileInfo, int, int, int, int, int]:
    path = graph_dir / filename
    if not path.is_file():
        raise FileNotFoundError(f"Arquivo de grafo não encontrado: {path}")

    graph = _load_graph(path)
    declared_layer = str(graph.get("graph_layer") or layer)
    if declared_layer != layer:
        raise ValueError(
            f"Camada incompatível em {path}: esperado {layer}, encontrado {declared_layer}"
        )

    stat = path.stat()
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    declared_stats = graph.get("stats") if isinstance(graph.get("stats"), dict) else {}
    if declared_stats.get("nodes") not in (None, len(nodes)):
        raise ValueError(
            f"Contagem de nós inconsistente em {path}: "
            f"declarado {declared_stats.get('nodes')}, real {len(nodes)}"
        )
    if declared_stats.get("edges") not in (None, len(edges)):
        raise ValueError(
            f"Contagem de arestas inconsistente em {path}: "
            f"declarado {declared_stats.get('edges')}, real {len(edges)}"
        )

    file_info = GraphFileInfo(
        layer=layer,
        filename=filename,
        path=path.resolve(),
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        sha256=file_sha256(path),
        graph_version=str(graph.get("version")) if graph.get("version") is not None else None,
        generated_at=str(graph.get("generated_at")) if graph.get("generated_at") is not None else None,
        node_count=len(nodes),
        edge_count=len(edges),
    )

    connection.execute(
        """
        INSERT INTO graph_files (
            graph_layer, filename, source_path, size_bytes, modified_ns,
            sha256, graph_version, generated_at, node_count, edge_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            file_info.layer,
            file_info.filename,
            str(file_info.path),
            file_info.size_bytes,
            file_info.modified_ns,
            file_info.sha256,
            file_info.graph_version,
            file_info.generated_at,
            file_info.node_count,
            file_info.edge_count,
        ),
    )

    node_pk_by_id: dict[str, int] = {}
    node_rows: list[tuple[Any, ...]] = []
    fts_rows: list[tuple[Any, ...]] = []
    module_rows: list[tuple[Any, ...]] = []
    node_pk = node_pk_start

    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError(f"Nó inválido na camada {layer}: esperado objeto JSON")
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            raise ValueError(f"Nó sem id na camada {layer}")
        if node_id in node_pk_by_id:
            raise ValueError(f"Nó duplicado na camada {layer}: {node_id}")

        node_type = str(node.get("node_type") or "unknown")
        title = str(node.get("title") or node.get("name") or node_id)
        qualified_name = str(node.get("qualified_name") or "").strip() or None
        source = node.get("source")
        source_type = (
            str(source.get("source_type") or "").strip() or None
            if isinstance(source, dict)
            else None
        )
        search_text = str(node.get("search_text") or title)
        summary = _node_summary(node)
        modules = _node_modules(node)
        payload_json = _compact_json(node)

        node_pk_by_id[node_id] = node_pk
        node_rows.append(
            (
                node_pk,
                layer,
                node_id,
                node_type,
                title,
                qualified_name,
                source_type,
                search_text,
                summary,
                _compact_json(modules),
                payload_json,
                hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            )
        )
        fts_rows.append(
            (
                node_pk,
                node_id,
                layer,
                title,
                qualified_name or "",
                search_text,
            )
        )
        module_rows.extend((node_pk, module_id) for module_id in modules)
        node_pk += 1

        if len(node_rows) >= batch_size:
            connection.executemany(
                """
                INSERT INTO nodes (
                    node_pk, graph_layer, node_id, node_type, title,
                    qualified_name, source_type, search_text, summary,
                    modules_json, payload_json, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                node_rows,
            )
            connection.executemany(
                """
                INSERT INTO nodes_fts (
                    rowid, node_id, graph_layer, title, qualified_name, search_text
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                fts_rows,
            )
            if module_rows:
                connection.executemany(
                    "INSERT OR IGNORE INTO node_modules (node_pk, module_id) VALUES (?, ?)",
                    module_rows,
                )
            node_rows.clear()
            fts_rows.clear()
            module_rows.clear()

    if node_rows:
        connection.executemany(
            """
            INSERT INTO nodes (
                node_pk, graph_layer, node_id, node_type, title,
                qualified_name, source_type, search_text, summary,
                modules_json, payload_json, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            node_rows,
        )
        connection.executemany(
            """
            INSERT INTO nodes_fts (
                rowid, node_id, graph_layer, title, qualified_name, search_text
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            fts_rows,
        )
        if module_rows:
            connection.executemany(
                "INSERT OR IGNORE INTO node_modules (node_pk, module_id) VALUES (?, ?)",
                module_rows,
            )

    edge_rows: list[tuple[Any, ...]] = []
    edge_pk = edge_pk_start
    for edge in edges:
        if not isinstance(edge, dict):
            raise ValueError(f"Aresta inválida na camada {layer}: esperado objeto JSON")
        source_id = str(edge.get("source") or "").strip()
        target_id = str(edge.get("target") or "").strip()
        edge_type = str(edge.get("type") or "").strip()
        if not source_id or not target_id or not edge_type:
            raise ValueError(f"Aresta incompleta na camada {layer}: {edge}")
        source_node_pk = node_pk_by_id.get(source_id)
        target_node_pk = node_pk_by_id.get(target_id)
        if source_node_pk is None or target_node_pk is None:
            raise ValueError(
                f"Aresta órfã na camada {layer}: {source_id} -> {target_id} ({edge_type})"
            )
        payload_json = _compact_json(edge)
        weight = edge.get("weight")
        edge_rows.append(
            (
                edge_pk,
                layer,
                source_node_pk,
                target_node_pk,
                source_id,
                target_id,
                edge_type,
                float(weight) if isinstance(weight, (int, float)) else None,
                str(edge.get("source_layer") or "").strip() or None,
                str(edge.get("target_layer") or "").strip() or None,
                payload_json,
                hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            )
        )
        edge_pk += 1

        if len(edge_rows) >= batch_size:
            connection.executemany(
                """
                INSERT INTO edges (
                    edge_pk, graph_layer, source_node_pk, target_node_pk,
                    source_id, target_id, edge_type, weight,
                    source_layer, target_layer, payload_json, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                edge_rows,
            )
            edge_rows.clear()

    if edge_rows:
        connection.executemany(
            """
            INSERT INTO edges (
                edge_pk, graph_layer, source_node_pk, target_node_pk,
                source_id, target_id, edge_type, weight,
                source_layer, target_layer, payload_json, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            edge_rows,
        )

    module_link_count = connection.execute(
        "SELECT COUNT(*) FROM node_modules WHERE node_pk >= ? AND node_pk < ?",
        (node_pk_start, node_pk),
    ).fetchone()[0]

    return (
        file_info,
        node_pk,
        edge_pk,
        len(nodes),
        len(edges),
        int(module_link_count),
    )


SEMANTIC_ROOT_TYPES = {
    "master": {"business_entity", "business_attribute", "validated_rule"},
    "physical": {"physical_table"},
    "otbi_analytics": {"otbi_subject_area"},
    "rest": {"rest_resource", "adf_resource"},
}

SEMANTIC_SEGMENT_NODE_TYPES = {
    "physical": {"physical_column"},
    "otbi_analytics": {"otbi_business_question"},
    "rest": {"rest_operation"},
}

SEMANTIC_SEGMENT_PROFILE_VERSION = "1"


def _semantic_root_text(row: sqlite3.Row) -> str:
    return "\n".join(
        value
        for value in (
            str(row["title"] or "").strip(),
            str(row["qualified_name"] or "").strip(),
            str(row["search_text"] or "").strip(),
            str(row["summary"] or "").strip(),
        )
        if value
    )


def _load_reusable_semantic_roots(
    index_path: str | Path | None,
    *,
    model_name: str,
) -> dict[tuple[str, str, str], tuple[int, bytes]]:
    if index_path is None:
        return {}
    path = Path(index_path).resolve()
    if not path.is_file():
        return {}

    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
        )
    except sqlite3.Error:
        return {}

    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"nodes", "semantic_roots", "index_metadata"}.issubset(tables):
            return {}
        metadata = read_index_metadata(connection)
        if metadata.get("schema_version") not in SUPPORTED_INDEX_SCHEMA_VERSIONS:
            return {}

        rows = connection.execute(
            """
            SELECT n.graph_layer,
                   n.node_id,
                   n.content_hash,
                   s.dimensions,
                   s.embedding
              FROM semantic_roots s
              JOIN nodes n ON n.node_pk = s.node_pk
             WHERE s.model_name = ?
            """,
            (model_name,),
        ).fetchall()
        reusable: dict[tuple[str, str, str], tuple[int, bytes]] = {}
        for layer, node_id, content_hash, dimensions, embedding in rows:
            dimensions = int(dimensions)
            payload = bytes(embedding)
            if dimensions <= 0 or len(payload) != dimensions * 4:
                continue
            reusable[(str(layer), str(node_id), str(content_hash))] = (
                dimensions,
                payload,
            )
        return reusable
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def _semantic_segment_profile(selector: SemanticTextSelector) -> dict[str, Any]:
    return {
        "version": SEMANTIC_SEGMENT_PROFILE_VERSION,
        "minimum_segment_characters": (
            selector.config.minimum_segment_characters
        ),
        "maximum_segment_characters": (
            selector.config.maximum_segment_characters
        ),
    }


def _load_reusable_semantic_segments(
    index_path: str | Path | None,
    *,
    model_name: str,
    profile: dict[str, Any],
) -> dict[tuple[str, str, str], list[tuple[int, int, bytes]]]:
    if index_path is None:
        return {}
    path = Path(index_path).resolve()
    if not path.is_file():
        return {}

    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
        )
    except sqlite3.Error:
        return {}

    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {
            "nodes",
            "semantic_segments",
            "index_metadata",
        }.issubset(tables):
            return {}

        metadata = read_index_metadata(connection)
        if metadata.get("schema_version") not in SUPPORTED_INDEX_SCHEMA_VERSIONS:
            return {}
        if metadata.get("semantic_segment_profile") != profile:
            return {}

        rows = connection.execute(
            """
            SELECT n.graph_layer,
                   n.node_id,
                   n.content_hash,
                   s.segment_index,
                   s.dimensions,
                   s.embedding
              FROM semantic_segments s
              JOIN nodes n ON n.node_pk = s.node_pk
             WHERE s.model_name = ?
             ORDER BY n.node_pk, s.segment_index
            """,
            (model_name,),
        ).fetchall()

        reusable: dict[
            tuple[str, str, str],
            list[tuple[int, int, bytes]],
        ] = {}
        for (
            layer,
            node_id,
            content_hash,
            segment_index,
            dimensions,
            embedding,
        ) in rows:
            dimensions = int(dimensions)
            payload = bytes(embedding)
            if dimensions <= 0 or len(payload) != dimensions * 4:
                continue
            key = (str(layer), str(node_id), str(content_hash))
            reusable.setdefault(key, []).append(
                (int(segment_index), dimensions, payload)
            )
        return reusable
    except sqlite3.Error:
        return {}
    finally:
        connection.close()


def _build_semantic_segment_index(
    connection: sqlite3.Connection,
    selector: SemanticTextSelector,
    *,
    batch_size: int,
    progress: Callable[[str], None] | None,
    reusable_segments: dict[
        tuple[str, str, str],
        list[tuple[int, int, bytes]],
    ] | None = None,
) -> tuple[int, int, int | None, str, int, int]:
    if batch_size <= 0:
        raise ValueError("semantic_batch_size deve ser maior que zero")

    clauses: list[str] = []
    parameters: list[str] = []
    for layer, node_types in SEMANTIC_SEGMENT_NODE_TYPES.items():
        placeholders = ",".join("?" for _ in node_types)
        clauses.append(
            f"(graph_layer = ? AND node_type IN ({placeholders}))"
        )
        parameters.extend([layer, *sorted(node_types)])

    connection.row_factory = sqlite3.Row
    cursor = connection.execute(
        f"""
        SELECT node_pk,
               graph_layer,
               node_id,
               content_hash,
               payload_json
          FROM nodes
         WHERE {' OR '.join(clauses)}
         ORDER BY node_pk
        """,
        parameters,
    )

    model_name = selector.config.model_name or DEFAULT_EMBEDDING_MODEL
    reusable = reusable_segments or {}
    inserted_segments = 0
    indexed_nodes = 0
    reused_count = 0
    generated_count = 0
    dimensions: int | None = None

    while rows := cursor.fetchmany(batch_size):
        insert_rows: list[
            tuple[int, int, str, int, sqlite3.Binary]
        ] = []
        encode_segments: list[str] = []
        encode_targets: list[tuple[int, int]] = []

        for row in rows:
            key = (
                str(row["graph_layer"]),
                str(row["node_id"]),
                str(row["content_hash"]),
            )
            reused = reusable.get(key)
            if reused:
                reusable_dimensions = {item[1] for item in reused}
                if len(reusable_dimensions) == 1:
                    reused_dimension = next(iter(reusable_dimensions))
                    if dimensions is None:
                        dimensions = reused_dimension
                    if dimensions == reused_dimension:
                        insert_rows.extend(
                            (
                                int(row["node_pk"]),
                                segment_index,
                                model_name,
                                reused_dimension,
                                sqlite3.Binary(embedding),
                            )
                            for segment_index, _, embedding in reused
                        )
                        reused_count += len(reused)
                        indexed_nodes += 1
                        continue

            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                raise ValueError(
                    "Payload de nó inválido durante a indexação semântica: "
                    f"{row['node_id']}"
                )
            document = semantic_document_text(payload)
            segments, mappings = selector.prepare_document_segments([document])
            indexes = mappings[0] if mappings else []
            if not indexes:
                continue

            for segment_index, flat_index in enumerate(indexes):
                encode_segments.append(segments[flat_index])
                encode_targets.append((int(row["node_pk"]), segment_index))
            indexed_nodes += 1

        if encode_segments:
            embeddings = selector.encode_documents(encode_segments)
            if embeddings.shape[0] != len(encode_targets):
                raise RuntimeError(
                    "O modelo semântico devolveu quantidade incompatível de "
                    "vetores para os segmentos persistidos."
                )
            current_dimensions = (
                int(embeddings.shape[1]) if embeddings.ndim == 2 else 0
            )
            if current_dimensions <= 0:
                raise RuntimeError("O modelo semântico devolveu vetores vazios.")
            if dimensions is None:
                dimensions = current_dimensions
            elif dimensions != current_dimensions:
                raise RuntimeError(
                    "O modelo semântico alterou a dimensão dos vetores durante "
                    "a indexação."
                )

            insert_rows.extend(
                (
                    node_pk,
                    segment_index,
                    model_name,
                    dimensions,
                    sqlite3.Binary(
                        np.asarray(vector, dtype="<f4").tobytes()
                    ),
                )
                for (node_pk, segment_index), vector in zip(
                    encode_targets,
                    embeddings,
                    strict=True,
                )
            )
            generated_count += len(encode_targets)

        if insert_rows:
            connection.executemany(
                """
                INSERT INTO semantic_segments (
                    node_pk,
                    segment_index,
                    model_name,
                    dimensions,
                    embedding
                ) VALUES (?, ?, ?, ?, ?)
                """,
                insert_rows,
            )
            inserted_segments += len(insert_rows)

        if progress:
            progress(
                "[INDEX] Segmentos semânticos: "
                f"{indexed_nodes} nós, {inserted_segments} segmentos "
                f"({reused_count} reutilizados, {generated_count} gerados)."
            )

    return (
        inserted_segments,
        indexed_nodes,
        dimensions,
        model_name,
        reused_count,
        generated_count,
    )


def _build_semantic_root_index(
    connection: sqlite3.Connection,
    selector: SemanticTextSelector,
    *,
    batch_size: int,
    progress: Callable[[str], None] | None,
    reusable_roots: dict[tuple[str, str, str], tuple[int, bytes]] | None = None,
) -> tuple[int, int | None, str, int, int]:
    if batch_size <= 0:
        raise ValueError("semantic_batch_size deve ser maior que zero")

    clauses: list[str] = []
    parameters: list[str] = []
    for layer, node_types in SEMANTIC_ROOT_TYPES.items():
        placeholders = ",".join("?" for _ in node_types)
        clauses.append(
            f"(graph_layer = ? AND node_type IN ({placeholders}))"
        )
        parameters.extend([layer, *sorted(node_types)])

    connection.row_factory = sqlite3.Row
    cursor = connection.execute(
        f"""
        SELECT node_pk, graph_layer, node_id, content_hash,
               title, qualified_name, search_text, summary
          FROM nodes
         WHERE {' OR '.join(clauses)}
         ORDER BY node_pk
        """,
        parameters,
    )

    model_name = selector.config.model_name or DEFAULT_EMBEDDING_MODEL
    reusable = reusable_roots or {}
    inserted = 0
    reused_count = 0
    generated_count = 0
    dimensions: int | None = None

    while rows := cursor.fetchmany(batch_size):
        insert_rows: list[tuple[int, str, int, sqlite3.Binary]] = []
        encode_rows: list[sqlite3.Row] = []
        encode_documents: list[str] = []

        for row in rows:
            key = (
                str(row["graph_layer"]),
                str(row["node_id"]),
                str(row["content_hash"]),
            )
            reused = reusable.get(key)
            if reused is None:
                encode_rows.append(row)
                encode_documents.append(_semantic_root_text(row))
                continue

            reused_dimensions, reused_embedding = reused
            if dimensions is None:
                dimensions = reused_dimensions
            elif dimensions != reused_dimensions:
                encode_rows.append(row)
                encode_documents.append(_semantic_root_text(row))
                continue
            insert_rows.append(
                (
                    int(row["node_pk"]),
                    model_name,
                    reused_dimensions,
                    sqlite3.Binary(reused_embedding),
                )
            )
            reused_count += 1

        if encode_rows:
            embeddings = selector.encode_documents(encode_documents)
            if embeddings.shape[0] != len(encode_rows):
                raise RuntimeError(
                    "O modelo semântico devolveu quantidade incompatível de vetores."
                )
            current_dimensions = int(embeddings.shape[1]) if embeddings.ndim == 2 else 0
            if current_dimensions <= 0:
                raise RuntimeError("O modelo semântico devolveu vetores vazios.")
            if dimensions is None:
                dimensions = current_dimensions
            elif dimensions != current_dimensions:
                raise RuntimeError(
                    "O modelo semântico alterou a dimensão dos vetores durante a indexação."
                )

            insert_rows.extend(
                (
                    int(row["node_pk"]),
                    model_name,
                    dimensions,
                    sqlite3.Binary(
                        np.asarray(vector, dtype="<f4").tobytes()
                    ),
                )
                for row, vector in zip(encode_rows, embeddings, strict=True)
            )
            generated_count += len(encode_rows)

        if insert_rows:
            connection.executemany(
                """
                INSERT INTO semantic_roots (
                    node_pk, model_name, dimensions, embedding
                ) VALUES (?, ?, ?, ?)
                """,
                insert_rows,
            )
        inserted += len(rows)
        if progress:
            progress(
                "[INDEX] Embeddings semânticos: "
                f"{inserted} raízes processadas "
                f"({reused_count} reutilizadas, {generated_count} geradas)."
            )

    return inserted, dimensions, model_name, reused_count, generated_count


def _graph_bundle_info(graph_root: Path) -> dict[str, Any]:
    bundle_path = graph_root / "graph_bundle.json"
    if not bundle_path.is_file():
        raise FileNotFoundError(f"Manifesto do bundle não encontrado: {bundle_path}")
    try:
        with bundle_path.open("r", encoding="utf-8-sig") as file:
            bundle_payload = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"graph_bundle.json inválido ou ilegível: {exc}") from exc
    if not isinstance(bundle_payload, dict):
        raise ValueError("graph_bundle.json deve conter um objeto JSON")
    bundle_stat = bundle_path.stat()
    return {
        "path": str(bundle_path.resolve()),
        "size_bytes": bundle_stat.st_size,
        "modified_ns": bundle_stat.st_mtime_ns,
        "sha256": file_sha256(bundle_path),
        "version": bundle_payload.get("version"),
        "generated_at": bundle_payload.get("generated_at"),
    }


def _normalize_layers(layers: Iterable[str] | None) -> tuple[str, ...]:
    if layers is None:
        return tuple(GRAPH_FILENAMES)
    normalized = tuple(dict.fromkeys(str(layer).strip() for layer in layers if str(layer).strip()))
    unknown = sorted(set(normalized) - set(GRAPH_FILENAMES))
    if unknown:
        raise ValueError(f"Camadas de índice desconhecidas: {', '.join(unknown)}")
    if not normalized:
        raise ValueError("Ao menos uma camada deve ser informada.")
    return normalized


def _build_index_for_layers(
    graph_dir: str | Path,
    output_path: str | Path,
    *,
    layers: Sequence[str],
    index_mode: str,
    batch_size: int,
    include_semantic_embeddings: bool,
    semantic_text_selector: SemanticTextSelector | None,
    semantic_batch_size: int,
    progress: Callable[[str], None] | None,
    reuse_index_path: str | Path | None = None,
) -> IndexBuildResult:
    if batch_size <= 0:
        raise ValueError("batch_size deve ser maior que zero")

    graph_root = Path(graph_dir).resolve()
    if not graph_root.is_dir():
        raise FileNotFoundError(f"Diretório de grafos não encontrado: {graph_root}")

    normalized_layers = _normalize_layers(layers)
    index_path = Path(output_path).resolve()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = index_path.with_suffix(index_path.suffix + ".tmp")
    if temporary_path.exists():
        temporary_path.unlink()

    graph_files: list[GraphFileInfo] = []
    total_nodes = 0
    total_edges = 0
    total_module_links = 0
    node_pk = 1
    edge_pk = 1
    semantic_root_count = 0
    semantic_dimensions: int | None = None
    semantic_model_name: str | None = None
    semantic_segment_count = 0
    semantic_segment_node_count = 0
    reused_semantic_root_count = 0
    generated_semantic_root_count = 0
    reused_semantic_segment_count = 0
    generated_semantic_segment_count = 0
    semantic_segment_profile: dict[str, Any] | None = None

    connection = sqlite3.connect(temporary_path)
    try:
        _configure_build_connection(connection)
        _create_schema(connection)

        with connection:
            for layer in normalized_layers:
                filename = GRAPH_FILENAMES[layer]
                if progress:
                    progress(f"[INDEX] Processando {filename}...")
                (
                    file_info,
                    node_pk,
                    edge_pk,
                    node_count,
                    edge_count,
                    module_link_count,
                ) = _insert_graph(
                    connection,
                    graph_dir=graph_root,
                    layer=layer,
                    filename=filename,
                    node_pk_start=node_pk,
                    edge_pk_start=edge_pk,
                    batch_size=batch_size,
                )
                graph_files.append(file_info)
                total_nodes += node_count
                total_edges += edge_count
                total_module_links += module_link_count
                if progress:
                    progress(
                        f"[INDEX] {filename}: {node_count} nós, "
                        f"{edge_count} arestas."
                    )

            if include_semantic_embeddings:
                selector = semantic_text_selector or SemanticTextSelector()
                model_name = selector.config.model_name or DEFAULT_EMBEDDING_MODEL
                reusable_roots = _load_reusable_semantic_roots(
                    reuse_index_path,
                    model_name=model_name,
                )
                if progress:
                    if reusable_roots:
                        progress(
                            "[INDEX] Construindo índice semântico das raízes "
                            f"com {len(reusable_roots)} vetores reutilizáveis..."
                        )
                    else:
                        progress("[INDEX] Construindo índice semântico das raízes...")
                (
                    semantic_root_count,
                    semantic_dimensions,
                    semantic_model_name,
                    reused_semantic_root_count,
                    generated_semantic_root_count,
                ) = _build_semantic_root_index(
                    connection,
                    selector,
                    batch_size=semantic_batch_size,
                    progress=progress,
                    reusable_roots=reusable_roots,
                )
                semantic_segment_profile = _semantic_segment_profile(selector)
                reusable_segments = _load_reusable_semantic_segments(
                    reuse_index_path,
                    model_name=model_name,
                    profile=semantic_segment_profile,
                )
                if progress:
                    if reusable_segments:
                        progress(
                            "[INDEX] Construindo segmentos semânticos persistidos "
                            f"com {len(reusable_segments)} nós reutilizáveis..."
                        )
                    else:
                        progress(
                            "[INDEX] Construindo segmentos semânticos persistidos..."
                        )
                (
                    semantic_segment_count,
                    semantic_segment_node_count,
                    segment_dimensions,
                    segment_model_name,
                    reused_semantic_segment_count,
                    generated_semantic_segment_count,
                ) = _build_semantic_segment_index(
                    connection,
                    selector,
                    batch_size=semantic_batch_size,
                    progress=progress,
                    reusable_segments=reusable_segments,
                )
                if semantic_dimensions is None:
                    semantic_dimensions = segment_dimensions
                elif (
                    segment_dimensions is not None
                    and semantic_dimensions != segment_dimensions
                ):
                    raise RuntimeError(
                        "As raízes e os segmentos semânticos possuem dimensões "
                        "incompatíveis."
                    )
                semantic_model_name = semantic_model_name or segment_model_name

            metadata: dict[str, Any] = {
                "schema_version": INDEX_SCHEMA_VERSION,
                "built_at": utc_now_iso(),
                "index_mode": index_mode,
                "indexed_layers": list(normalized_layers),
                "graph_dir": str(graph_root),
                "graph_file_count": len(graph_files),
                "node_count": total_nodes,
                "edge_count": total_edges,
                "fts_row_count": total_nodes,
                "module_link_count": total_module_links,
                "semantic_root_count": semantic_root_count,
                "semantic_dimensions": semantic_dimensions,
                "semantic_model_name": semantic_model_name,
                "semantic_segment_count": semantic_segment_count,
                "semantic_segment_node_count": semantic_segment_node_count,
                "semantic_segment_profile": semantic_segment_profile,
                "reused_semantic_root_count": reused_semantic_root_count,
                "generated_semantic_root_count": generated_semantic_root_count,
                "reused_semantic_segment_count": reused_semantic_segment_count,
                "generated_semantic_segment_count": generated_semantic_segment_count,
            }
            if index_mode == "monolithic":
                metadata["graph_bundle"] = _graph_bundle_info(graph_root)

            connection.executemany(
                "INSERT INTO index_metadata (key, value_json) VALUES (?, ?)",
                [(key, _compact_json(value)) for key, value in metadata.items()],
            )

        connection.execute("INSERT INTO nodes_fts(nodes_fts) VALUES ('optimize')")
        connection.execute("ANALYZE")
        connection.execute("PRAGMA optimize")
        connection.commit()

        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Falha na integridade do índice SQLite: {integrity}")
    except Exception:
        connection.close()
        if temporary_path.exists():
            temporary_path.unlink()
        raise
    else:
        connection.close()

    os.replace(temporary_path, index_path)

    return IndexBuildResult(
        graph_dir=graph_root,
        index_path=index_path,
        graph_files=tuple(graph_files),
        node_count=total_nodes,
        edge_count=total_edges,
        fts_row_count=total_nodes,
        module_link_count=total_module_links,
        semantic_root_count=semantic_root_count,
        semantic_dimensions=semantic_dimensions,
        semantic_model_name=semantic_model_name,
        semantic_segment_count=semantic_segment_count,
        semantic_segment_node_count=semantic_segment_node_count,
        indexed_layers=normalized_layers,
        reused_semantic_root_count=reused_semantic_root_count,
        generated_semantic_root_count=generated_semantic_root_count,
        reused_semantic_segment_count=reused_semantic_segment_count,
        generated_semantic_segment_count=generated_semantic_segment_count,
    )


def build_search_index(
    graph_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    batch_size: int = 1000,
    include_semantic_embeddings: bool = False,
    semantic_text_selector: SemanticTextSelector | None = None,
    semantic_batch_size: int = 32,
    progress: Callable[[str], None] | None = None,
) -> IndexBuildResult:
    """Constrói o índice monolítico legado, mantido para compatibilidade."""
    graph_root = Path(graph_dir).resolve()
    index_path = Path(output_path).resolve() if output_path else default_index_path(graph_root)
    return _build_index_for_layers(
        graph_root,
        index_path,
        layers=tuple(GRAPH_FILENAMES),
        index_mode="monolithic",
        batch_size=batch_size,
        include_semantic_embeddings=include_semantic_embeddings,
        semantic_text_selector=semantic_text_selector,
        semantic_batch_size=semantic_batch_size,
        progress=progress,
    )


def build_layer_search_index(
    graph_dir: str | Path,
    layer: str,
    output_path: str | Path | None = None,
    *,
    batch_size: int = 1000,
    include_semantic_embeddings: bool = True,
    semantic_text_selector: SemanticTextSelector | None = None,
    semantic_batch_size: int = 32,
    progress: Callable[[str], None] | None = None,
    reuse_index_path: str | Path | None = None,
) -> IndexBuildResult:
    graph_root = Path(graph_dir).resolve()
    index_path = (
        Path(output_path).resolve()
        if output_path is not None
        else default_layer_index_path(graph_root, layer)
    )
    return _build_index_for_layers(
        graph_root,
        index_path,
        layers=(layer,),
        index_mode="layer",
        batch_size=batch_size,
        include_semantic_embeddings=include_semantic_embeddings,
        semantic_text_selector=semantic_text_selector,
        semantic_batch_size=semantic_batch_size,
        progress=progress,
        reuse_index_path=reuse_index_path,
    )


def _bundle_entry_from_result(
    result: IndexBuildResult,
    *,
    bundle_dir: Path,
) -> dict[str, Any]:
    graph_file = result.graph_files[0]
    try:
        relative_path = result.index_path.relative_to(bundle_dir)
        path_value = relative_path.as_posix()
    except ValueError:
        path_value = str(result.index_path)
    return {
        "path": path_value,
        "schema_version": INDEX_SCHEMA_VERSION,
        "graph_layer": graph_file.layer,
        "graph_filename": graph_file.filename,
        "graph_path": str(graph_file.path),
        "graph_size_bytes": graph_file.size_bytes,
        "graph_modified_ns": graph_file.modified_ns,
        "graph_sha256": graph_file.sha256,
        "graph_version": graph_file.graph_version,
        "graph_generated_at": graph_file.generated_at,
        "node_count": result.node_count,
        "edge_count": result.edge_count,
        "semantic_root_count": result.semantic_root_count,
        "semantic_dimensions": result.semantic_dimensions,
        "semantic_model_name": result.semantic_model_name,
        "semantic_segment_count": result.semantic_segment_count,
        "semantic_segment_node_count": result.semantic_segment_node_count,
        "reused_semantic_root_count": result.reused_semantic_root_count,
        "generated_semantic_root_count": result.generated_semantic_root_count,
        "reused_semantic_segment_count": result.reused_semantic_segment_count,
        "generated_semantic_segment_count": result.generated_semantic_segment_count,
        "built_at": utc_now_iso(),
    }


def _resolve_bundle_entry_path(bundle_dir: Path, entry: dict[str, Any]) -> Path:
    raw_path = Path(str(entry.get("path") or ""))
    return raw_path.resolve() if raw_path.is_absolute() else (bundle_dir / raw_path).resolve()


def _layer_requires_semantic_embeddings(layer: str) -> bool:
    return layer in SEMANTIC_ROOT_TYPES



def _refresh_index_graph_metadata(
    index_path: Path,
    *,
    layer: str,
    graph_path: Path,
    graph_hash: str,
) -> None:
    stat = graph_path.stat()
    connection = sqlite3.connect(index_path)
    try:
        with connection:
            updated = connection.execute(
                """
                UPDATE graph_files
                   SET source_path = ?,
                       size_bytes = ?,
                       modified_ns = ?,
                       sha256 = ?
                 WHERE graph_layer = ?
                """,
                (
                    str(graph_path.resolve()),
                    stat.st_size,
                    stat.st_mtime_ns,
                    graph_hash,
                    layer,
                ),
            ).rowcount
            if updated != 1:
                raise ValueError(
                    f"O índice {index_path} não contém exatamente uma camada {layer}."
                )
            connection.execute(
                "INSERT OR REPLACE INTO index_metadata (key, value_json) VALUES (?, ?)",
                ("source_metadata_refreshed_at", _compact_json(utc_now_iso())),
            )
    finally:
        connection.close()

def build_index_bundle(
    graph_dir: str | Path,
    *,
    layers: Iterable[str] | None = None,
    output_dir: str | Path | None = None,
    batch_size: int = 1000,
    include_semantic_embeddings: bool = True,
    semantic_text_selector: SemanticTextSelector | None = None,
    semantic_batch_size: int = 32,
    progress: Callable[[str], None] | None = None,
    force: bool = False,
    reuse_embeddings: bool = True,
) -> IndexBundleBuildResult:
    graph_root = Path(graph_dir).resolve()
    if not graph_root.is_dir():
        raise FileNotFoundError(f"Diretório de grafos não encontrado: {graph_root}")
    requested_layers = _normalize_layers(layers)
    bundle_dir = (
        Path(output_dir).resolve()
        if output_dir is not None
        else graph_root / DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH
    )
    bundle_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = bundle_dir / "index_bundle.json"

    existing_payload: dict[str, Any] = {}
    if bundle_path.is_file():
        existing_payload = read_index_bundle(bundle_path)
    existing_indexes = (
        existing_payload.get("indexes")
        if isinstance(existing_payload.get("indexes"), dict)
        else {}
    )
    indexes: dict[str, dict[str, Any]] = {
        str(layer): dict(entry)
        for layer, entry in existing_indexes.items()
        if isinstance(entry, dict)
    }

    selector = semantic_text_selector or SemanticTextSelector()
    semantic_model_name = selector.config.model_name or DEFAULT_EMBEDDING_MODEL
    legacy_index = default_index_path(graph_root)
    built_layers: list[str] = []
    skipped_layers: list[str] = []

    for layer in requested_layers:
        graph_path = graph_root / GRAPH_FILENAMES[layer]
        if not graph_path.is_file():
            raise FileNotFoundError(f"Arquivo de grafo não encontrado: {graph_path}")
        graph_stat = graph_path.stat()
        graph_hash = file_sha256(graph_path)
        entry = indexes.get(layer) if isinstance(indexes.get(layer), dict) else None
        existing_layer_path = (
            _resolve_bundle_entry_path(bundle_dir, entry)
            if entry is not None
            else default_layer_index_path(graph_root, layer)
        )
        semantic_expected = include_semantic_embeddings and _layer_requires_semantic_embeddings(layer)
        model_matches = (
            not semantic_expected
            or str((entry or {}).get("semantic_model_name") or "") == semantic_model_name
        )
        unchanged = bool(
            not force
            and entry is not None
            and existing_layer_path.is_file()
            and str(entry.get("schema_version") or "") == INDEX_SCHEMA_VERSION
            and str(entry.get("graph_sha256") or "") == graph_hash
            and model_matches
        )

        if unchanged:
            _refresh_index_graph_metadata(
                existing_layer_path,
                layer=layer,
                graph_path=graph_path,
                graph_hash=graph_hash,
            )
            refreshed_entry = dict(entry)
            refreshed_entry.update(
                {
                    "graph_path": str(graph_path.resolve()),
                    "graph_size_bytes": graph_stat.st_size,
                    "graph_modified_ns": graph_stat.st_mtime_ns,
                    "graph_sha256": graph_hash,
                    "skipped_at": utc_now_iso(),
                }
            )
            indexes[layer] = refreshed_entry
            skipped_layers.append(layer)
            if progress:
                progress(
                    f"[INDEX] {layer}: conteúdo inalterado; índice reutilizado sem rebuild."
                )
            continue

        output_path = bundle_dir / LAYER_INDEX_FILENAMES[layer]
        reuse_index_path: Path | None = None
        if reuse_embeddings:
            if existing_layer_path.is_file():
                reuse_index_path = existing_layer_path
            elif legacy_index.is_file():
                reuse_index_path = legacy_index

        result = build_layer_search_index(
            graph_root,
            layer,
            output_path,
            batch_size=batch_size,
            include_semantic_embeddings=semantic_expected,
            semantic_text_selector=selector,
            semantic_batch_size=semantic_batch_size,
            progress=progress,
            reuse_index_path=reuse_index_path,
        )
        indexes[layer] = _bundle_entry_from_result(
            result,
            bundle_dir=bundle_dir,
        )
        built_layers.append(layer)

    for layer, entry in list(indexes.items()):
        if layer not in GRAPH_FILENAMES or not isinstance(entry, dict):
            indexes.pop(layer, None)
            continue
        index_file = _resolve_bundle_entry_path(bundle_dir, entry)
        if not index_file.is_file():
            indexes.pop(layer, None)

    graph_bundle = _graph_bundle_info(graph_root)
    payload = {
        "version": INDEX_BUNDLE_VERSION,
        "schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": utc_now_iso(),
        "graph_dir": str(graph_root),
        "graph_bundle": graph_bundle,
        "semantic_model_name": semantic_model_name if include_semantic_embeddings else None,
        "indexes": {
            layer: indexes[layer]
            for layer in GRAPH_FILENAMES
            if layer in indexes
        },
    }
    temporary_bundle = bundle_path.with_suffix(bundle_path.suffix + ".tmp")
    temporary_bundle.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_bundle, bundle_path)

    return IndexBundleBuildResult(
        graph_dir=graph_root,
        bundle_path=bundle_path,
        requested_layers=requested_layers,
        built_layers=tuple(built_layers),
        skipped_layers=tuple(skipped_layers),
        indexes=payload["indexes"],
    )


def read_index_metadata(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT key, value_json FROM index_metadata ORDER BY key"
    ).fetchall()
    metadata: dict[str, Any] = {}
    for key, value_json in rows:
        try:
            metadata[str(key)] = json.loads(value_json)
        except json.JSONDecodeError:
            metadata[str(key)] = value_json
    return metadata
