from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from oracle_knowledge.common import utc_now_iso
from oracle_knowledge.indexing import (
    DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH,
    SEMANTIC_ROOT_TYPES,
    SEMANTIC_SEGMENT_NODE_TYPES,
    _load_graph,
    file_sha256,
)
from oracle_knowledge.linker.graph_layers import GRAPH_FILENAMES
from oracle_knowledge.search.semantic_context import SemanticContextConfig, SemanticTextSelector
from oracle_knowledge.search.semantic_documents import semantic_document_text

NORMALIZATION_SCHEMA_VERSION = "1.0.0"
NORMALIZATION_USER_VERSION = 1
NORMALIZATION_VERSION = "1.0.0"
SEGMENTATION_VERSION = "1.0.0"
DEFAULT_NORMALIZATION_FILENAME = "semantic_normalization.sqlite"
DEFAULT_NORMALIZATION_MANIFEST_FILENAME = "semantic_normalization_manifest.json"
DEFAULT_NORMALIZATION_BATCH_SIZE = 1000
DEFAULT_CHECKPOINT_PERCENT = 1.0

# O catálogo ADF pertence ao ambiente e deve ser preparado primeiro. As demais
# camadas seguem em ordem de roteamento e expansão.
NORMALIZATION_LAYER_ORDER = (
    "rest",
    "master",
    "business",
    "physical",
    "otbi_analytics",
    "otbi_security",
)

# Canonicalizações deliberadamente conservadoras. Somente nomes técnicos cujo
# significado é estável no dicionário físico Oracle Fusion são compartilhados.
# Campos genéricos como STATUS, TYPE, CODE, AMOUNT e ATTRIBUTE* não entram aqui.
CURATED_AUDIT_COLUMN_TEXTS = {
    "LAST_UPDATE_DATE": (
        "Oracle Fusion audit field LAST_UPDATE_DATE. "
        "Date and time when the record was last updated."
    ),
    "LAST_UPDATED_BY": (
        "Oracle Fusion audit field LAST_UPDATED_BY. "
        "User who last updated the record."
    ),
    "LAST_UPDATE_LOGIN": (
        "Oracle Fusion audit field LAST_UPDATE_LOGIN. "
        "Login session that last updated the record."
    ),
    "CREATION_DATE": (
        "Oracle Fusion audit field CREATION_DATE. "
        "Date and time when the record was created."
    ),
    "CREATED_BY": (
        "Oracle Fusion audit field CREATED_BY. "
        "User who created the record."
    ),
    "OBJECT_VERSION_NUMBER": (
        "Oracle Fusion control field OBJECT_VERSION_NUMBER. "
        "Optimistic locking counter incremented when the record is updated."
    ),
}

_ZERO_WIDTH_TRANSLATION = str.maketrans(
    {
        "\ufeff": "",
        "\u200b": "",
        "\u200c": "",
        "\u200d": "",
        "\u2060": "",
        "\u00a0": " ",
    }
)


@dataclass(frozen=True)
class SemanticNormalizationResult:
    graph_dir: Path
    database_path: Path
    manifest_path: Path
    run_id: str
    status: str
    layers: tuple[str, ...]
    build_signature: str
    total_nodes: int
    processed_nodes: int
    reused_nodes: int
    normalized_nodes: int
    source_segment_count: int
    unique_text_count: int
    checkpoint_percent: float
    elapsed_seconds: float
    resumed: bool
    removed_segment_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        percent = (
            100.0
            if self.total_nodes == 0 and self.status == "completed"
            else (
                (self.processed_nodes / self.total_nodes) * 100.0
                if self.total_nodes
                else 0.0
            )
        )
        deduplicated = max(self.source_segment_count - self.unique_text_count, 0)
        deduplication_percent = (
            (deduplicated / self.source_segment_count) * 100.0
            if self.source_segment_count
            else 0.0
        )
        return {
            "schema_version": NORMALIZATION_SCHEMA_VERSION,
            "normalization_version": NORMALIZATION_VERSION,
            "segmentation_version": SEGMENTATION_VERSION,
            "graph_dir": str(self.graph_dir),
            "database_path": str(self.database_path),
            "manifest_path": str(self.manifest_path),
            "run_id": self.run_id,
            "status": self.status,
            "layers": list(self.layers),
            "build_signature": self.build_signature,
            "total_nodes": self.total_nodes,
            "processed_nodes": self.processed_nodes,
            "progress_percent": round(percent, 4),
            "reused_nodes": self.reused_nodes,
            "normalized_nodes": self.normalized_nodes,
            "source_segment_count": self.source_segment_count,
            "unique_text_count": self.unique_text_count,
            "deduplicated_segment_count": deduplicated,
            "deduplication_percent": round(deduplication_percent, 4),
            "checkpoint_percent": self.checkpoint_percent,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "resumed": self.resumed,
            "removed_segment_count": self.removed_segment_count,
        }


def default_normalization_database_path(graph_dir: str | Path) -> Path:
    return (
        Path(graph_dir).resolve()
        / DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH
        / DEFAULT_NORMALIZATION_FILENAME
    )


def default_normalization_manifest_path(graph_dir: str | Path) -> Path:
    return (
        Path(graph_dir).resolve()
        / DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH
        / DEFAULT_NORMALIZATION_MANIFEST_FILENAME
    )


def normalize_semantic_text(value: str | None) -> str:
    """Normalização conservadora, determinística e idempotente.

    A função preserva caixa, acentos e pontuação porque esses elementos podem
    carregar significado em nomes técnicos. Ela normaliza somente Unicode,
    espaços invisíveis, quebras de linha e sequências de whitespace.
    """

    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = normalized.translate(_ZERO_WIDTH_TRANSLATION)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _normalize_layers(layers: Iterable[str] | None) -> tuple[str, ...]:
    requested = set(layers or GRAPH_FILENAMES)
    unknown = requested.difference(GRAPH_FILENAMES)
    if unknown:
        raise ValueError(
            "Camadas de normalização desconhecidas: "
            + ", ".join(sorted(unknown))
        )
    return tuple(layer for layer in NORMALIZATION_LAYER_ORDER if layer in requested)


def _eligible_types_for_layer(layer: str) -> set[str]:
    return set(SEMANTIC_ROOT_TYPES.get(layer, set())).union(
        SEMANTIC_SEGMENT_NODE_TYPES.get(layer, set())
    )


def _node_priority(layer: str, node: dict[str, Any]) -> tuple[int, str]:
    node_type = str(node.get("node_type") or "")
    if layer == "rest":
        priority = {
            "adf_resource": 0,
            "rest_resource": 1,
            "rest_operation": 2,
        }.get(node_type, 10)
    else:
        priority = 0 if node_type in SEMANTIC_ROOT_TYPES.get(layer, set()) else 1
    return priority, str(node.get("id") or "")


def _iter_eligible_nodes(
    graph: dict[str, Any],
    layer: str,
) -> Iterator[dict[str, Any]]:
    eligible = _eligible_types_for_layer(layer)
    nodes = [
        node
        for node in graph.get("nodes", [])
        if isinstance(node, dict)
        and str(node.get("node_type") or "") in eligible
        and str(node.get("id") or "").strip()
    ]
    nodes.sort(key=lambda item: _node_priority(layer, item))
    yield from nodes


def _root_document_text(node: dict[str, Any]) -> str:
    return "\n".join(
        value
        for value in (
            str(node.get("title") or node.get("name") or "").strip(),
            str(node.get("qualified_name") or "").strip(),
            str(node.get("search_text") or "").strip(),
            str(node.get("summary") or node.get("description") or "").strip(),
        )
        if value
    )


def _source_document(node: dict[str, Any], layer: str) -> str:
    node_type = str(node.get("node_type") or "")
    if node_type in SEMANTIC_ROOT_TYPES.get(layer, set()):
        return _root_document_text(node)
    return semantic_document_text(node)




def _is_curated_audit_column(node: dict[str, Any]) -> bool:
    if str(node.get("node_type") or "") != "physical_column":
        return False
    source = node.get("source")
    source_type = (
        str(source.get("source_type") or "")
        if isinstance(source, dict)
        else ""
    )
    column_name = str(node.get("name") or "").strip().upper()
    return (
        source_type == "oracle_data_dictionary"
        and column_name in CURATED_AUDIT_COLUMN_TEXTS
    )

def _canonical_normalized_text(
    node: dict[str, Any],
    source_text: str,
) -> str:
    if _is_curated_audit_column(node):
        column_name = str(node.get("name") or "").strip().upper()
        return CURATED_AUDIT_COLUMN_TEXTS[column_name]
    return normalize_semantic_text(source_text)


def _segments_for_document(
    node: dict[str, Any],
    layer: str,
    selector: SemanticTextSelector,
    document: str,
) -> list[tuple[str, int, str]]:
    node_type = str(node.get("node_type") or "")

    if node_type in SEMANTIC_ROOT_TYPES.get(layer, set()):
        return [('root', 0, document)] if document.strip() else []

    # Colunas técnicas curadas usam um único conceito canônico compartilhado.
    if _is_curated_audit_column(node):
        return [('detail', 0, document)] if document.strip() else []

    segments, mappings = selector.prepare_document_segments([document])
    indexes = mappings[0] if mappings else []
    return [
        ("detail", segment_index, segments[flat_index])
        for segment_index, flat_index in enumerate(indexes)
    ]


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA busy_timeout = 30000")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        f"""
        PRAGMA user_version = {NORMALIZATION_USER_VERSION};

        CREATE TABLE IF NOT EXISTS normalization_metadata (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS normalization_runs (
            run_id TEXT PRIMARY KEY,
            build_signature TEXT NOT NULL,
            graph_dir TEXT NOT NULL,
            layers_json TEXT NOT NULL,
            normalization_version TEXT NOT NULL,
            segmentation_version TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            total_nodes INTEGER NOT NULL,
            last_processed_ordinal INTEGER NOT NULL DEFAULT 0,
            reused_nodes INTEGER NOT NULL DEFAULT 0,
            normalized_nodes INTEGER NOT NULL DEFAULT 0,
            source_segment_count INTEGER NOT NULL DEFAULT 0,
            checkpoint_percent REAL NOT NULL,
            last_checkpoint_percent REAL NOT NULL DEFAULT 0,
            error_message TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_normalization_runs_signature
            ON normalization_runs(
                build_signature,
                normalization_version,
                segmentation_version,
                status,
                updated_at
            );

        CREATE TABLE IF NOT EXISTS normalized_nodes (
            graph_layer TEXT NOT NULL,
            node_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            document_hash TEXT NOT NULL,
            normalization_version TEXT NOT NULL,
            segmentation_version TEXT NOT NULL,
            segment_count INTEGER NOT NULL,
            last_seen_run_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (
                graph_layer,
                node_id,
                normalization_version,
                segmentation_version
            )
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS normalized_texts (
            normalization_version TEXT NOT NULL,
            normalized_text_hash TEXT NOT NULL,
            normalized_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (
                normalization_version,
                normalized_text_hash
            )
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS normalized_segments (
            graph_layer TEXT NOT NULL,
            node_id TEXT NOT NULL,
            node_type TEXT NOT NULL,
            segment_kind TEXT NOT NULL,
            segment_index INTEGER NOT NULL,
            source_text_hash TEXT NOT NULL,
            source_text TEXT NOT NULL,
            normalization_version TEXT NOT NULL,
            segmentation_version TEXT NOT NULL,
            normalized_text_hash TEXT NOT NULL,
            last_seen_run_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (
                graph_layer,
                node_id,
                segment_kind,
                segment_index,
                normalization_version,
                segmentation_version
            )
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_normalized_segments_text
            ON normalized_segments(
                normalization_version,
                normalized_text_hash
            );
        CREATE INDEX IF NOT EXISTS idx_normalized_segments_node
            ON normalized_segments(
                graph_layer,
                node_id,
                normalization_version,
                segmentation_version
            );
        CREATE INDEX IF NOT EXISTS idx_normalized_segments_seen
            ON normalized_segments(last_seen_run_id);
        """
    )
    metadata = {
        "schema_version": NORMALIZATION_SCHEMA_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "segmentation_version": SEGMENTATION_VERSION,
        "updated_at": utc_now_iso(),
    }
    connection.executemany(
        "INSERT OR REPLACE INTO normalization_metadata (key, value_json) VALUES (?, ?)",
        [(key, _compact_json(value)) for key, value in metadata.items()],
    )
    connection.commit()


def _graph_signature(
    graph_root: Path,
    layers: Sequence[str],
) -> tuple[str, list[dict[str, Any]]]:
    files: list[dict[str, Any]] = []
    for layer in layers:
        path = graph_root / GRAPH_FILENAMES[layer]
        if not path.is_file():
            raise FileNotFoundError(f"Arquivo de grafo não encontrado: {path}")
        stat = path.stat()
        files.append(
            {
                "layer": layer,
                "filename": path.name,
                "size_bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "sha256": file_sha256(path),
            }
        )
    payload = {
        "graph_dir": str(graph_root),
        "layers": list(layers),
        "files": files,
        "normalization_version": NORMALIZATION_VERSION,
        "segmentation_version": SEGMENTATION_VERSION,
    }
    return _sha256_text(_compact_json(payload)), files


def _count_eligible_nodes(graph_root: Path, layers: Sequence[str]) -> int:
    total = 0
    for layer in layers:
        graph = _load_graph(graph_root / GRAPH_FILENAMES[layer])
        eligible = _eligible_types_for_layer(layer)
        total += sum(
            1
            for node in graph.get("nodes", [])
            if isinstance(node, dict)
            and str(node.get("node_type") or "") in eligible
            and str(node.get("id") or "").strip()
        )
    return total


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds == float("inf"):
        return "indisponível"
    seconds_int = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _eta_seconds(
    *,
    started_monotonic: float,
    processed_this_attempt: int,
    remaining_nodes: int,
) -> float | None:
    if processed_this_attempt <= 0:
        return None
    elapsed = max(time.monotonic() - started_monotonic, 0.000001)
    rate = processed_this_attempt / elapsed
    if rate <= 0:
        return None
    return remaining_nodes / rate


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _run_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _find_existing_run(
    connection: sqlite3.Connection,
    *,
    build_signature: str,
    layers: Sequence[str],
    resume: bool,
) -> tuple[dict[str, Any] | None, bool]:
    if not resume:
        return None, False
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        """
        SELECT *
          FROM normalization_runs
         WHERE build_signature = ?
           AND layers_json = ?
           AND normalization_version = ?
           AND segmentation_version = ?
      ORDER BY CASE status
                   WHEN 'running' THEN 0
                   WHEN 'interrupted' THEN 1
                   WHEN 'failed' THEN 2
                   WHEN 'completed' THEN 3
                   ELSE 4
               END,
               updated_at DESC
         LIMIT 1
        """,
        (
            build_signature,
            _compact_json(list(layers)),
            NORMALIZATION_VERSION,
            SEGMENTATION_VERSION,
        ),
    ).fetchone()
    if row is None:
        return None, False
    payload = _run_row_to_dict(row)
    return payload, payload["status"] != "completed"


def _unique_text_count(
    connection: sqlite3.Connection,
    layers: Sequence[str],
) -> int:
    if not layers:
        return 0
    placeholders = ",".join("?" for _ in layers)
    return int(
        connection.execute(
            f"""
            SELECT COUNT(DISTINCT s.normalized_text_hash)
              FROM normalized_segments s
             WHERE s.normalization_version = ?
               AND s.segmentation_version = ?
               AND s.graph_layer IN ({placeholders})
            """,
            (
                NORMALIZATION_VERSION,
                SEGMENTATION_VERSION,
                *layers,
            ),
        ).fetchone()[0]
    )


def _run_result(
    *,
    graph_root: Path,
    database_path: Path,
    manifest_path: Path,
    row: dict[str, Any],
    layers: tuple[str, ...],
    build_signature: str,
    checkpoint_percent: float,
    elapsed_seconds: float,
    resumed: bool,
    removed_segment_count: int = 0,
    connection: sqlite3.Connection,
) -> SemanticNormalizationResult:
    return SemanticNormalizationResult(
        graph_dir=graph_root,
        database_path=database_path,
        manifest_path=manifest_path,
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        layers=layers,
        build_signature=build_signature,
        total_nodes=int(row["total_nodes"]),
        processed_nodes=int(row["last_processed_ordinal"]),
        reused_nodes=int(row["reused_nodes"]),
        normalized_nodes=int(row["normalized_nodes"]),
        source_segment_count=int(row["source_segment_count"]),
        unique_text_count=_unique_text_count(connection, layers),
        checkpoint_percent=checkpoint_percent,
        elapsed_seconds=elapsed_seconds,
        resumed=resumed,
        removed_segment_count=removed_segment_count,
    )


def _write_manifest(
    result: SemanticNormalizationResult,
    *,
    graph_files: list[dict[str, Any]],
    eta_seconds: float | None,
) -> None:
    payload = result.to_dict()
    payload["graph_files"] = graph_files
    payload["estimated_remaining_seconds"] = (
        None if eta_seconds is None else round(eta_seconds, 3)
    )
    payload["estimated_remaining"] = _format_duration(eta_seconds)
    payload["updated_at"] = utc_now_iso()
    _atomic_write_json(result.manifest_path, payload)


def normalize_semantic_corpus(
    graph_dir: str | Path,
    output_path: str | Path | None = None,
    *,
    layers: Iterable[str] | None = None,
    batch_size: int = DEFAULT_NORMALIZATION_BATCH_SIZE,
    checkpoint_percent: float = DEFAULT_CHECKPOINT_PERCENT,
    resume: bool = True,
    force_renormalize: bool = False,
    progress: Callable[[str], None] | None = None,
    semantic_text_selector: SemanticTextSelector | None = None,
) -> SemanticNormalizationResult:
    """Normaliza o corpus semântico de forma idempotente, incremental e retomável.

    A função não carrega nem executa o modelo de embeddings. Ela prepara e
    deduplica as strings persistentes que serão consumidas pelo próximo estágio
    de construção dos índices semânticos.
    """

    if batch_size <= 0:
        raise ValueError("batch_size deve ser maior que zero")
    if checkpoint_percent <= 0 or checkpoint_percent > 100:
        raise ValueError("checkpoint_percent deve estar entre 0 e 100")

    graph_root = Path(graph_dir).resolve()
    if not graph_root.is_dir():
        raise FileNotFoundError(f"Diretório de grafos não encontrado: {graph_root}")

    normalized_layers = _normalize_layers(layers)
    database_path = (
        Path(output_path).resolve()
        if output_path is not None
        else default_normalization_database_path(graph_root)
    )
    manifest_path = database_path.with_name(DEFAULT_NORMALIZATION_MANIFEST_FILENAME)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    if progress:
        progress(
            "[NORMALIZE] Calculando assinatura das camadas solicitadas..."
        )
    build_signature, graph_files = _graph_signature(graph_root, normalized_layers)
    started_monotonic = time.monotonic()

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    _configure_connection(connection)
    _create_schema(connection)

    existing_run, resumable = _find_existing_run(
        connection,
        build_signature=build_signature,
        layers=normalized_layers,
        resume=resume,
    )

    if (
        existing_run
        and existing_run["status"] == "completed"
        and not force_renormalize
    ):
        result = _run_result(
            graph_root=graph_root,
            database_path=database_path,
            manifest_path=manifest_path,
            row=existing_run,
            layers=normalized_layers,
            build_signature=build_signature,
            checkpoint_percent=checkpoint_percent,
            elapsed_seconds=0.0,
            resumed=False,
            connection=connection,
        )
        _write_manifest(result, graph_files=graph_files, eta_seconds=0.0)
        if progress:
            progress(
                "[NORMALIZE] Corpus já normalizado para o mesmo conjunto de "
                f"grafos: {result.processed_nodes}/{result.total_nodes} nós "
                "(100,00%)."
            )
        connection.close()
        return result

    if existing_run and resumable:
        run = dict(existing_run)
        run_id = str(run["run_id"])
        connection.execute(
            """
            UPDATE normalization_runs
               SET status = 'running',
                   updated_at = ?,
                   error_message = NULL,
                   checkpoint_percent = ?
             WHERE run_id = ?
            """,
            (utc_now_iso(), checkpoint_percent, run_id),
        )
        connection.commit()
        resumed = True
    else:
        if progress:
            progress(
                "[NORMALIZE] Contando nós semânticos para definir o denominador global..."
            )
        total_nodes = _count_eligible_nodes(graph_root, normalized_layers)
        run_id = uuid.uuid4().hex
        now = utc_now_iso()
        connection.execute(
            """
            INSERT INTO normalization_runs (
                run_id,
                build_signature,
                graph_dir,
                layers_json,
                normalization_version,
                segmentation_version,
                status,
                started_at,
                updated_at,
                total_nodes,
                checkpoint_percent
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                run_id,
                build_signature,
                str(graph_root),
                _compact_json(list(normalized_layers)),
                NORMALIZATION_VERSION,
                SEGMENTATION_VERSION,
                now,
                now,
                total_nodes,
                checkpoint_percent,
            ),
        )
        connection.commit()
        run = dict(
            connection.execute(
                "SELECT * FROM normalization_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        )
        resumed = False

    total_nodes = int(run["total_nodes"])
    start_ordinal = int(run["last_processed_ordinal"])
    reused_nodes = int(run["reused_nodes"])
    normalized_nodes = int(run["normalized_nodes"])
    source_segment_count = int(run["source_segment_count"])
    last_checkpoint_percent = float(run["last_checkpoint_percent"])

    if progress:
        if resumed:
            percent = (start_ordinal / total_nodes * 100.0) if total_nodes else 100.0
            progress(
                f"[NORMALIZE] Retomando run {run_id} em "
                f"{start_ordinal}/{total_nodes} nós ({percent:.2f}%)."
            )
        else:
            progress(
                f"[NORMALIZE] Plano criado: {total_nodes} nós semânticos em "
                f"{len(normalized_layers)} camadas. ADF/REST será processado primeiro."
            )

    selector = semantic_text_selector or SemanticTextSelector(
        SemanticContextConfig()
    )
    current_ordinal = 0
    processed_this_attempt = 0
    pending_nodes: list[dict[str, Any]] = []
    removed_segment_count = 0

    state_rows = connection.execute(
        """
        SELECT graph_layer, node_id, document_hash, segment_count
          FROM normalized_nodes
         WHERE normalization_version = ?
           AND segmentation_version = ?
        """,
        (NORMALIZATION_VERSION, SEGMENTATION_VERSION),
    ).fetchall()
    normalized_state: dict[tuple[str, str], tuple[str, int]] = {
        (str(row["graph_layer"]), str(row["node_id"])): (
            str(row["document_hash"]),
            int(row["segment_count"]),
        )
        for row in state_rows
    }

    def flush_batch() -> None:
        nonlocal reused_nodes
        nonlocal normalized_nodes
        nonlocal source_segment_count
        nonlocal processed_this_attempt
        nonlocal last_checkpoint_percent
        if not pending_nodes:
            return

        now = utc_now_iso()
        reused_node_updates: list[tuple[Any, ...]] = []
        reused_segment_updates: list[tuple[Any, ...]] = []
        delete_segment_rows: list[tuple[Any, ...]] = []
        normalized_text_rows: list[tuple[Any, ...]] = []
        segment_rows: list[tuple[Any, ...]] = []
        normalized_node_rows: list[tuple[Any, ...]] = []

        for item in pending_nodes:
            layer = str(item["layer"])
            node = item["node"]
            node_id = str(node["id"])
            node_type = str(node.get("node_type") or "")
            document_hash = str(item["document_hash"])
            existing = normalized_state.get((layer, node_id))

            if (
                existing is not None
                and existing[0] == document_hash
                and not force_renormalize
            ):
                segment_count = existing[1]
                reused_node_updates.append(
                    (
                        run_id,
                        now,
                        layer,
                        node_id,
                        NORMALIZATION_VERSION,
                        SEGMENTATION_VERSION,
                    )
                )
                reused_segment_updates.append(
                    (
                        run_id,
                        now,
                        layer,
                        node_id,
                        NORMALIZATION_VERSION,
                        SEGMENTATION_VERSION,
                    )
                )
                reused_nodes += 1
                source_segment_count += segment_count
                continue

            document = str(item["document"])
            segments = _segments_for_document(node, layer, selector, document)
            delete_segment_rows.append(
                (
                    layer,
                    node_id,
                    NORMALIZATION_VERSION,
                    SEGMENTATION_VERSION,
                )
            )
            inserted_for_node = 0
            for segment_kind, segment_index, source_text in segments:
                normalized_text = _canonical_normalized_text(node, source_text)
                if not normalized_text:
                    continue
                source_text_hash = _sha256_text(source_text)
                normalized_text_hash = _sha256_text(normalized_text)
                normalized_text_rows.append(
                    (
                        NORMALIZATION_VERSION,
                        normalized_text_hash,
                        normalized_text,
                        now,
                    )
                )
                segment_rows.append(
                    (
                        layer,
                        node_id,
                        node_type,
                        segment_kind,
                        segment_index,
                        source_text_hash,
                        source_text,
                        NORMALIZATION_VERSION,
                        SEGMENTATION_VERSION,
                        normalized_text_hash,
                        run_id,
                        now,
                    )
                )
                inserted_for_node += 1

            normalized_node_rows.append(
                (
                    layer,
                    node_id,
                    node_type,
                    document_hash,
                    NORMALIZATION_VERSION,
                    SEGMENTATION_VERSION,
                    inserted_for_node,
                    run_id,
                    now,
                )
            )
            normalized_state[(layer, node_id)] = (
                document_hash,
                inserted_for_node,
            )
            normalized_nodes += 1
            source_segment_count += inserted_for_node

        with connection:
            if reused_node_updates:
                connection.executemany(
                    """
                    UPDATE normalized_nodes
                       SET last_seen_run_id = ?, updated_at = ?
                     WHERE graph_layer = ?
                       AND node_id = ?
                       AND normalization_version = ?
                       AND segmentation_version = ?
                    """,
                    reused_node_updates,
                )
                connection.executemany(
                    """
                    UPDATE normalized_segments
                       SET last_seen_run_id = ?, updated_at = ?
                     WHERE graph_layer = ?
                       AND node_id = ?
                       AND normalization_version = ?
                       AND segmentation_version = ?
                    """,
                    reused_segment_updates,
                )
            if delete_segment_rows:
                connection.executemany(
                    """
                    DELETE FROM normalized_segments
                     WHERE graph_layer = ?
                       AND node_id = ?
                       AND normalization_version = ?
                       AND segmentation_version = ?
                    """,
                    delete_segment_rows,
                )
            if normalized_text_rows:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO normalized_texts (
                        normalization_version,
                        normalized_text_hash,
                        normalized_text,
                        created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    normalized_text_rows,
                )
            if segment_rows:
                connection.executemany(
                    """
                    INSERT INTO normalized_segments (
                        graph_layer,
                        node_id,
                        node_type,
                        segment_kind,
                        segment_index,
                        source_text_hash,
                        source_text,
                        normalization_version,
                        segmentation_version,
                        normalized_text_hash,
                        last_seen_run_id,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    segment_rows,
                )
            if normalized_node_rows:
                connection.executemany(
                    """
                    INSERT INTO normalized_nodes (
                        graph_layer,
                        node_id,
                        node_type,
                        document_hash,
                        normalization_version,
                        segmentation_version,
                        segment_count,
                        last_seen_run_id,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (
                        graph_layer,
                        node_id,
                        normalization_version,
                        segmentation_version
                    ) DO UPDATE SET
                        node_type = excluded.node_type,
                        document_hash = excluded.document_hash,
                        segment_count = excluded.segment_count,
                        last_seen_run_id = excluded.last_seen_run_id,
                        updated_at = excluded.updated_at
                    """,
                    normalized_node_rows,
                )

            last_ordinal = int(pending_nodes[-1]["ordinal"])
            processed_this_attempt += len(pending_nodes)
            percent = (last_ordinal / total_nodes * 100.0) if total_nodes else 100.0
            connection.execute(
                """
                UPDATE normalization_runs
                   SET updated_at = ?,
                       last_processed_ordinal = ?,
                       reused_nodes = ?,
                       normalized_nodes = ?,
                       source_segment_count = ?
                 WHERE run_id = ?
                """,
                (
                    now,
                    last_ordinal,
                    reused_nodes,
                    normalized_nodes,
                    source_segment_count,
                    run_id,
                ),
            )

        checkpoint_due = (
            percent >= 100.0
            or percent + 1e-9 >= last_checkpoint_percent + checkpoint_percent
        )
        if checkpoint_due:
            checkpoint_value = min(
                100.0,
                (int(percent / checkpoint_percent) * checkpoint_percent),
            )
            if percent >= 100.0:
                checkpoint_value = 100.0
            connection.execute(
                """
                UPDATE normalization_runs
                   SET last_checkpoint_percent = ?, updated_at = ?
                 WHERE run_id = ?
                """,
                (checkpoint_value, utc_now_iso(), run_id),
            )
            connection.commit()
            last_checkpoint_percent = checkpoint_value
            unique_count = _unique_text_count(connection, normalized_layers)
            remaining = max(total_nodes - last_ordinal, 0)
            eta = _eta_seconds(
                started_monotonic=started_monotonic,
                processed_this_attempt=processed_this_attempt,
                remaining_nodes=remaining,
            )
            row = dict(
                connection.execute(
                    "SELECT * FROM normalization_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            )
            result = _run_result(
                graph_root=graph_root,
                database_path=database_path,
                manifest_path=manifest_path,
                row=row,
                layers=normalized_layers,
                build_signature=build_signature,
                checkpoint_percent=checkpoint_percent,
                elapsed_seconds=time.monotonic() - started_monotonic,
                resumed=resumed,
                connection=connection,
            )
            _write_manifest(result, graph_files=graph_files, eta_seconds=eta)
            if progress:
                deduplicated = max(source_segment_count - unique_count, 0)
                progress(
                    f"[NORMALIZE] {last_ordinal}/{total_nodes} nós "
                    f"({percent:.2f}%) | segmentos={source_segment_count} | "
                    f"textos únicos={unique_count} | deduplicados={deduplicated} | "
                    f"reutilizados={reused_nodes} | normalizados={normalized_nodes} | "
                    f"ETA {_format_duration(eta)} | checkpoint persistido."
                )

        pending_nodes.clear()

    try:
        for layer in normalized_layers:
            graph = _load_graph(graph_root / GRAPH_FILENAMES[layer])
            for node in _iter_eligible_nodes(graph, layer):
                current_ordinal += 1
                if current_ordinal <= start_ordinal:
                    continue
                document = _source_document(node, layer)
                document_hash = _sha256_text(document)
                pending_nodes.append(
                    {
                        "ordinal": current_ordinal,
                        "layer": layer,
                        "node": node,
                        "document_hash": document_hash,
                        "document": document,
                    }
                )
                if len(pending_nodes) >= batch_size:
                    flush_batch()
        flush_batch()

        now = utc_now_iso()
        placeholders = ",".join("?" for _ in normalized_layers)
        with connection:
            obsolete_segments = connection.execute(
                f"""
                SELECT COUNT(*)
                  FROM normalized_segments
                 WHERE graph_layer IN ({placeholders})
                   AND normalization_version = ?
                   AND segmentation_version = ?
                   AND last_seen_run_id <> ?
                """,
                (
                    *normalized_layers,
                    NORMALIZATION_VERSION,
                    SEGMENTATION_VERSION,
                    run_id,
                ),
            ).fetchone()[0]
            removed_segment_count = int(obsolete_segments)
            connection.execute(
                f"""
                DELETE FROM normalized_segments
                 WHERE graph_layer IN ({placeholders})
                   AND normalization_version = ?
                   AND segmentation_version = ?
                   AND last_seen_run_id <> ?
                """,
                (
                    *normalized_layers,
                    NORMALIZATION_VERSION,
                    SEGMENTATION_VERSION,
                    run_id,
                ),
            )
            connection.execute(
                f"""
                DELETE FROM normalized_nodes
                 WHERE graph_layer IN ({placeholders})
                   AND normalization_version = ?
                   AND segmentation_version = ?
                   AND last_seen_run_id <> ?
                """,
                (
                    *normalized_layers,
                    NORMALIZATION_VERSION,
                    SEGMENTATION_VERSION,
                    run_id,
                ),
            )
            connection.execute(
                """
                DELETE FROM normalized_texts
                 WHERE normalization_version = ?
                   AND NOT EXISTS (
                       SELECT 1
                         FROM normalized_segments s
                        WHERE s.normalization_version = normalized_texts.normalization_version
                          AND s.normalized_text_hash = normalized_texts.normalized_text_hash
                   )
                """,
                (NORMALIZATION_VERSION,),
            )
            connection.execute(
                """
                UPDATE normalization_runs
                   SET status = 'completed',
                       updated_at = ?,
                       completed_at = ?,
                       last_processed_ordinal = total_nodes,
                       last_checkpoint_percent = 100,
                       error_message = NULL
                 WHERE run_id = ?
                """,
                (now, now, run_id),
            )

        row = dict(
            connection.execute(
                "SELECT * FROM normalization_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        )
        result = _run_result(
            graph_root=graph_root,
            database_path=database_path,
            manifest_path=manifest_path,
            row=row,
            layers=normalized_layers,
            build_signature=build_signature,
            checkpoint_percent=checkpoint_percent,
            elapsed_seconds=time.monotonic() - started_monotonic,
            resumed=resumed,
            removed_segment_count=removed_segment_count,
            connection=connection,
        )
        _write_manifest(result, graph_files=graph_files, eta_seconds=0.0)
        if progress:
            progress(
                f"[NORMALIZE] Concluído: {result.processed_nodes}/"
                f"{result.total_nodes} nós (100,00%) | "
                f"segmentos={result.source_segment_count} | "
                f"textos únicos={result.unique_text_count} | "
                f"deduplicados={max(result.source_segment_count - result.unique_text_count, 0)} | "
                f"reutilizados={result.reused_nodes} | "
                f"normalizados={result.normalized_nodes} | "
                f"tempo={_format_duration(result.elapsed_seconds)}."
            )
        return result
    except BaseException as exc:
        connection.rollback()
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        message = "Interrompido pelo usuário" if status == "interrupted" else str(exc)
        connection.execute(
            """
            UPDATE normalization_runs
               SET status = ?, updated_at = ?, error_message = ?
             WHERE run_id = ?
            """,
            (status, utc_now_iso(), message[:2000], run_id),
        )
        connection.commit()
        row = dict(
            connection.execute(
                "SELECT * FROM normalization_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        )
        result = _run_result(
            graph_root=graph_root,
            database_path=database_path,
            manifest_path=manifest_path,
            row=row,
            layers=normalized_layers,
            build_signature=build_signature,
            checkpoint_percent=checkpoint_percent,
            elapsed_seconds=time.monotonic() - started_monotonic,
            resumed=resumed,
            connection=connection,
        )
        eta = _eta_seconds(
            started_monotonic=started_monotonic,
            processed_this_attempt=max(
                result.processed_nodes - start_ordinal,
                0,
            ),
            remaining_nodes=max(result.total_nodes - result.processed_nodes, 0),
        )
        _write_manifest(result, graph_files=graph_files, eta_seconds=eta)
        if progress:
            progress(
                f"[NORMALIZE] {status}: último checkpoint em "
                f"{result.processed_nodes}/{result.total_nodes} nós "
                f"({result.to_dict()['progress_percent']:.2f}%). Estado preservado."
            )
        raise
    finally:
        connection.close()
