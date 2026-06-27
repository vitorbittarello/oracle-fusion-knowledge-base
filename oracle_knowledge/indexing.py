from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from oracle_knowledge.common import utc_now_iso
from oracle_knowledge.linker.graph_layers import GRAPH_FILENAMES

INDEX_SCHEMA_VERSION = "1.0.0"
INDEX_USER_VERSION = 1
DEFAULT_INDEX_RELATIVE_PATH = Path("search_index") / "knowledge_index.sqlite"

REQUIRED_SQL_INDEXES = {
    "idx_nodes_layer_type",
    "idx_nodes_layer_title",
    "idx_nodes_qualified_name",
    "idx_node_modules_module",
    "idx_edges_source",
    "idx_edges_target",
    "idx_edges_type",
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
        }


def default_index_path(graph_dir: str | Path) -> Path:
    return Path(graph_dir).resolve() / DEFAULT_INDEX_RELATIVE_PATH


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
        PRAGMA user_version = 1;

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


def build_search_index(
    graph_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    batch_size: int = 1000,
    progress: Callable[[str], None] | None = None,
) -> IndexBuildResult:
    if batch_size <= 0:
        raise ValueError("batch_size deve ser maior que zero")

    graph_root = Path(graph_dir).resolve()
    if not graph_root.is_dir():
        raise FileNotFoundError(f"Diretório de grafos não encontrado: {graph_root}")

    index_path = Path(output_path).resolve() if output_path else default_index_path(graph_root)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = index_path.with_suffix(index_path.suffix + ".tmp")
    if temporary_path.exists():
        temporary_path.unlink()

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
    bundle_info = {
        "path": str(bundle_path.resolve()),
        "size_bytes": bundle_stat.st_size,
        "modified_ns": bundle_stat.st_mtime_ns,
        "sha256": file_sha256(bundle_path),
        "version": bundle_payload.get("version"),
        "generated_at": bundle_payload.get("generated_at"),
    }

    graph_files: list[GraphFileInfo] = []
    total_nodes = 0
    total_edges = 0
    total_module_links = 0
    node_pk = 1
    edge_pk = 1

    connection = sqlite3.connect(temporary_path)
    try:
        _configure_build_connection(connection)
        _create_schema(connection)

        with connection:
            for layer, filename in GRAPH_FILENAMES.items():
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

            metadata = {
                "schema_version": INDEX_SCHEMA_VERSION,
                "built_at": utc_now_iso(),
                "graph_dir": str(graph_root),
                "graph_file_count": len(graph_files),
                "graph_bundle": bundle_info,
                "node_count": total_nodes,
                "edge_count": total_edges,
                "fts_row_count": total_nodes,
                "module_link_count": total_module_links,
            }
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
