from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from oracle_knowledge.common import utc_now_iso
from oracle_knowledge.indexing import DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH
from oracle_knowledge.search.semantic_context import (
    DEFAULT_LOCAL_EMBEDDING_MODEL,
    SemanticTextSelector,
    resolve_embedding_model_profile,
    semantic_context_config_for_model,
)
from oracle_knowledge.semantic_normalization import (
    DEFAULT_NORMALIZATION_FILENAME,
    NORMALIZATION_SCHEMA_VERSION,
    default_normalization_database_path,
)

EMBEDDING_SCHEMA_VERSION = "1.0.0"
EMBEDDING_USER_VERSION = 1
EMBEDDING_PROFILE_VERSION = "1.0.0"
DEFAULT_EMBEDDING_FILENAME = "semantic_embeddings.sqlite"
DEFAULT_EMBEDDING_MANIFEST_FILENAME = "semantic_embeddings_manifest.json"
DEFAULT_EMBEDDING_BATCH_SIZE = 32
DEFAULT_EMBEDDING_CHECKPOINT_PERCENT = 1.0
DEFAULT_EXPECTED_DIMENSIONS: int | None = None
STORAGE_DTYPE = "float32"
STORAGE_NUMPY_DTYPE = "<f4"
VECTOR_NORMALIZATION = "l2"
SIMILARITY_METRIC = "cosine"


@dataclass(frozen=True)
class SemanticVectorizationResult:
    graph_dir: Path
    normalization_database_path: Path
    database_path: Path
    manifest_path: Path
    run_id: str
    status: str
    normalization_signature: str
    normalization_version: str
    segmentation_version: str
    model_name: str
    embedding_profile_version: str
    dimensions: int | None
    total_texts: int
    processed_texts: int
    reused_embeddings: int
    generated_embeddings: int
    checkpoint_percent: float
    elapsed_seconds: float
    resumed: bool

    def to_dict(self) -> dict[str, Any]:
        percent = (
            100.0
            if self.total_texts == 0 and self.status == "completed"
            else (
                (self.processed_texts / self.total_texts) * 100.0
                if self.total_texts
                else 0.0
            )
        )
        return {
            "schema_version": EMBEDDING_SCHEMA_VERSION,
            "embedding_profile_version": self.embedding_profile_version,
            "graph_dir": str(self.graph_dir),
            "normalization_database_path": str(self.normalization_database_path),
            "database_path": str(self.database_path),
            "manifest_path": str(self.manifest_path),
            "run_id": self.run_id,
            "status": self.status,
            "normalization_signature": self.normalization_signature,
            "normalization_version": self.normalization_version,
            "segmentation_version": self.segmentation_version,
            "model_name": self.model_name,
            "dimensions": self.dimensions,
            "inference_dtype": STORAGE_DTYPE,
            "storage_dtype": STORAGE_DTYPE,
            "vector_normalization": VECTOR_NORMALIZATION,
            "similarity_metric": SIMILARITY_METRIC,
            "total_texts": self.total_texts,
            "processed_texts": self.processed_texts,
            "progress_percent": round(percent, 4),
            "reused_embeddings": self.reused_embeddings,
            "generated_embeddings": self.generated_embeddings,
            "checkpoint_percent": self.checkpoint_percent,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "resumed": self.resumed,
        }


def default_embedding_database_path(graph_dir: str | Path) -> Path:
    return (
        Path(graph_dir).resolve()
        / DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH
        / DEFAULT_EMBEDDING_FILENAME
    )


def default_embedding_manifest_path(graph_dir: str | Path) -> Path:
    return (
        Path(graph_dir).resolve()
        / DEFAULT_INDEX_DIRECTORY_RELATIVE_PATH
        / DEFAULT_EMBEDDING_MANIFEST_FILENAME
    )


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    generated_this_attempt: int,
    remaining_texts: int,
) -> float | None:
    if generated_this_attempt <= 0:
        return None
    elapsed = max(time.monotonic() - started_monotonic, 0.000001)
    rate = generated_this_attempt / elapsed
    if rate <= 0:
        return None
    return remaining_texts / rate


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute("PRAGMA busy_timeout = 30000")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        f"""
        PRAGMA user_version = {EMBEDDING_USER_VERSION};

        CREATE TABLE IF NOT EXISTS embedding_metadata (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS embedding_runs (
            run_id TEXT PRIMARY KEY,
            normalization_signature TEXT NOT NULL,
            normalization_database_path TEXT NOT NULL,
            normalization_version TEXT NOT NULL,
            segmentation_version TEXT NOT NULL,
            model_name TEXT NOT NULL,
            embedding_profile_version TEXT NOT NULL,
            storage_dtype TEXT NOT NULL,
            vector_normalization TEXT NOT NULL,
            similarity_metric TEXT NOT NULL,
            expected_dimensions INTEGER,
            dimensions INTEGER,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            total_texts INTEGER NOT NULL,
            reused_embeddings INTEGER NOT NULL DEFAULT 0,
            generated_embeddings INTEGER NOT NULL DEFAULT 0,
            last_source_hash TEXT NOT NULL DEFAULT '',
            checkpoint_percent REAL NOT NULL,
            last_checkpoint_percent REAL NOT NULL DEFAULT 0,
            error_message TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_embedding_runs_signature
            ON embedding_runs(
                normalization_signature,
                model_name,
                embedding_profile_version,
                status,
                updated_at
            );

        CREATE TABLE IF NOT EXISTS semantic_embeddings (
            normalization_version TEXT NOT NULL,
            normalized_text_hash TEXT NOT NULL,
            model_name TEXT NOT NULL,
            embedding_profile_version TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            storage_dtype TEXT NOT NULL,
            vector_normalization TEXT NOT NULL,
            similarity_metric TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen_run_id TEXT NOT NULL,
            PRIMARY KEY (
                normalization_version,
                normalized_text_hash,
                model_name,
                embedding_profile_version
            )
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_semantic_embeddings_profile
            ON semantic_embeddings(
                model_name,
                embedding_profile_version,
                dimensions,
                storage_dtype
            );
        CREATE INDEX IF NOT EXISTS idx_semantic_embeddings_seen
            ON semantic_embeddings(last_seen_run_id);
        """
    )
    metadata = {
        "schema_version": EMBEDDING_SCHEMA_VERSION,
        "embedding_profile_version": EMBEDDING_PROFILE_VERSION,
        "storage_dtype": STORAGE_DTYPE,
        "vector_normalization": VECTOR_NORMALIZATION,
        "similarity_metric": SIMILARITY_METRIC,
        "updated_at": utc_now_iso(),
    }
    connection.executemany(
        "INSERT OR REPLACE INTO embedding_metadata (key, value_json) VALUES (?, ?)",
        [(key, _compact_json(value)) for key, value in metadata.items()],
    )
    connection.commit()


def _open_normalization_database(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(
            "Banco normalizado não encontrado: "
            f"{path}. Execute normalize-index antes da vetorização."
        )
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _normalization_source_info(
    connection: sqlite3.Connection,
) -> dict[str, Any]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    required = {
        "normalization_metadata",
        "normalization_runs",
        "normalized_texts",
        "normalized_segments",
    }
    missing = required.difference(tables)
    if missing:
        raise ValueError(
            "Banco de normalização incompatível; tabelas ausentes: "
            + ", ".join(sorted(missing))
        )

    metadata_rows = connection.execute(
        "SELECT key, value_json FROM normalization_metadata"
    ).fetchall()
    metadata = {
        str(row["key"]): json.loads(str(row["value_json"]))
        for row in metadata_rows
    }
    if str(metadata.get("schema_version") or "") != NORMALIZATION_SCHEMA_VERSION:
        raise ValueError(
            "Versão incompatível do banco de normalização: "
            f"{metadata.get('schema_version')!r}."
        )

    run = connection.execute(
        """
        SELECT *
          FROM normalization_runs
         WHERE status = 'completed'
      ORDER BY rowid DESC
         LIMIT 1
        """
    ).fetchone()
    if run is None:
        raise ValueError(
            "O banco de normalização não possui uma execução concluída. "
            "Execute ou retome normalize-index antes da vetorização."
        )

    normalization_version = str(run["normalization_version"])
    segmentation_version = str(run["segmentation_version"])
    total_texts = int(
        connection.execute(
            """
            SELECT COUNT(*)
              FROM normalized_texts
             WHERE normalization_version = ?
            """,
            (normalization_version,),
        ).fetchone()[0]
    )
    segment_count = int(
        connection.execute(
            """
            SELECT COUNT(*)
              FROM normalized_segments
             WHERE normalization_version = ?
               AND segmentation_version = ?
            """,
            (normalization_version, segmentation_version),
        ).fetchone()[0]
    )
    signature_payload = {
        "normalization_schema_version": metadata["schema_version"],
        "normalization_version": normalization_version,
        "segmentation_version": segmentation_version,
        "normalization_build_signature": str(run["build_signature"]),
        "normalization_run_id": str(run["run_id"]),
        "total_texts": total_texts,
        "segment_count": segment_count,
    }
    return {
        "signature": _sha256_text(_compact_json(signature_payload)),
        "normalization_version": normalization_version,
        "segmentation_version": segmentation_version,
        "normalization_run_id": str(run["run_id"]),
        "normalization_build_signature": str(run["build_signature"]),
        "total_texts": total_texts,
        "segment_count": segment_count,
    }


def _embedding_profile_signature(
    *,
    model_name: str,
    expected_dimensions: int | None,
) -> str:
    payload = {
        "model_name": model_name,
        "embedding_profile_version": EMBEDDING_PROFILE_VERSION,
        "expected_dimensions": expected_dimensions,
        "inference_dtype": STORAGE_DTYPE,
        "storage_dtype": STORAGE_DTYPE,
        "vector_normalization": VECTOR_NORMALIZATION,
        "similarity_metric": SIMILARITY_METRIC,
    }
    return _sha256_text(_compact_json(payload))


def _find_existing_run(
    connection: sqlite3.Connection,
    *,
    normalization_signature: str,
    model_name: str,
    expected_dimensions: int | None,
    resume: bool,
) -> tuple[dict[str, Any] | None, bool]:
    if not resume:
        return None, False
    row = connection.execute(
        """
        SELECT *
          FROM embedding_runs
         WHERE normalization_signature = ?
           AND model_name = ?
           AND embedding_profile_version = ?
           AND storage_dtype = ?
           AND vector_normalization = ?
           AND similarity_metric = ?
           AND COALESCE(expected_dimensions, -1) = COALESCE(?, -1)
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
            normalization_signature,
            model_name,
            EMBEDDING_PROFILE_VERSION,
            STORAGE_DTYPE,
            VECTOR_NORMALIZATION,
            SIMILARITY_METRIC,
            expected_dimensions,
        ),
    ).fetchone()
    if row is None:
        return None, False
    payload = {key: row[key] for key in row.keys()}
    return payload, str(payload["status"]) in {"running", "interrupted", "failed"}


def _count_reusable_embeddings(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    *,
    normalization_version: str,
    model_name: str,
    expected_dimensions: int | None,
) -> tuple[int, int | None]:
    reusable = 0
    dimensions: int | None = None
    cursor = source.execute(
        """
        SELECT normalized_text_hash
          FROM normalized_texts
         WHERE normalization_version = ?
      ORDER BY normalized_text_hash
        """,
        (normalization_version,),
    )
    while rows := cursor.fetchmany(500):
        hashes = [str(row[0]) for row in rows]
        placeholders = ",".join("?" for _ in hashes)
        stats = target.execute(
            f"""
            SELECT COUNT(*), MIN(dimensions), MAX(dimensions)
              FROM semantic_embeddings
             WHERE normalization_version = ?
               AND model_name = ?
               AND embedding_profile_version = ?
               AND storage_dtype = ?
               AND vector_normalization = ?
               AND similarity_metric = ?
               AND (? IS NULL OR dimensions = ?)
               AND normalized_text_hash IN ({placeholders})
            """,
            (
                normalization_version,
                model_name,
                EMBEDDING_PROFILE_VERSION,
                STORAGE_DTYPE,
                VECTOR_NORMALIZATION,
                SIMILARITY_METRIC,
                expected_dimensions,
                expected_dimensions,
                *hashes,
            ),
        ).fetchone()
        reusable += int(stats[0])
        if stats[1] is not None:
            minimum = int(stats[1])
            maximum = int(stats[2])
            if minimum != maximum:
                raise ValueError(
                    "O cache contém dimensões incompatíveis para o mesmo perfil."
                )
            if dimensions is None:
                dimensions = minimum
            elif dimensions != minimum:
                raise ValueError(
                    "O cache alterou a dimensão dentro do mesmo perfil."
                )
    return reusable, dimensions


def _mark_reused_embeddings_seen(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    *,
    run_id: str,
    normalization_version: str,
    model_name: str,
    expected_dimensions: int | None,
) -> None:
    source_hashes = [
        str(row[0])
        for row in source.execute(
            """
            SELECT normalized_text_hash
              FROM normalized_texts
             WHERE normalization_version = ?
            """,
            (normalization_version,),
        ).fetchall()
    ]
    if not source_hashes:
        return
    now = utc_now_iso()
    # O limite conservador de 500 parâmetros funciona em versões antigas do SQLite.
    for offset in range(0, len(source_hashes), 500):
        chunk = source_hashes[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        target.execute(
            f"""
            UPDATE semantic_embeddings
               SET last_seen_run_id = ?, updated_at = ?
             WHERE normalization_version = ?
               AND model_name = ?
               AND embedding_profile_version = ?
               AND storage_dtype = ?
               AND vector_normalization = ?
               AND similarity_metric = ?
               AND (? IS NULL OR dimensions = ?)
               AND normalized_text_hash IN ({placeholders})
            """,
            (
                run_id,
                now,
                normalization_version,
                model_name,
                EMBEDDING_PROFILE_VERSION,
                STORAGE_DTYPE,
                VECTOR_NORMALIZATION,
                SIMILARITY_METRIC,
                expected_dimensions,
                expected_dimensions,
                *chunk,
            ),
        )
    target.commit()


def _fetch_pending_batch(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    normalization_version: str,
    model_name: str,
    expected_dimensions: int | None,
    after_hash: str,
    batch_size: int,
) -> list[sqlite3.Row]:
    cursor = source.execute(
        """
        SELECT normalized_text_hash, normalized_text
          FROM normalized_texts
         WHERE normalization_version = ?
           AND normalized_text_hash > ?
      ORDER BY normalized_text_hash
        """,
        (normalization_version, after_hash),
    )
    pending: list[sqlite3.Row] = []
    while len(pending) < batch_size:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        hashes = [str(row["normalized_text_hash"]) for row in rows]
        placeholders = ",".join("?" for _ in hashes)
        existing = {
            str(row[0])
            for row in target.execute(
                f"""
                SELECT normalized_text_hash
                  FROM semantic_embeddings
                 WHERE normalization_version = ?
                   AND model_name = ?
                   AND embedding_profile_version = ?
                   AND storage_dtype = ?
                   AND vector_normalization = ?
                   AND similarity_metric = ?
                   AND (? IS NULL OR dimensions = ?)
                   AND normalized_text_hash IN ({placeholders})
                """,
                (
                    normalization_version,
                    model_name,
                    EMBEDDING_PROFILE_VERSION,
                    STORAGE_DTYPE,
                    VECTOR_NORMALIZATION,
                    SIMILARITY_METRIC,
                    expected_dimensions,
                    expected_dimensions,
                    *hashes,
                ),
            ).fetchall()
        }
        pending.extend(
            row
            for row in rows
            if str(row["normalized_text_hash"]) not in existing
        )
    return pending[:batch_size]


def _result_from_run(
    *,
    graph_dir: Path,
    normalization_database_path: Path,
    database_path: Path,
    manifest_path: Path,
    run: dict[str, Any],
    normalization_signature: str,
    normalization_version: str,
    segmentation_version: str,
    model_name: str,
    checkpoint_percent: float,
    elapsed_seconds: float,
    resumed: bool,
) -> SemanticVectorizationResult:
    total = int(run["total_texts"])
    reused = int(run["reused_embeddings"])
    generated = int(run["generated_embeddings"])
    return SemanticVectorizationResult(
        graph_dir=graph_dir,
        normalization_database_path=normalization_database_path,
        database_path=database_path,
        manifest_path=manifest_path,
        run_id=str(run["run_id"]),
        status=str(run["status"]),
        normalization_signature=normalization_signature,
        normalization_version=normalization_version,
        segmentation_version=segmentation_version,
        model_name=model_name,
        embedding_profile_version=EMBEDDING_PROFILE_VERSION,
        dimensions=(
            int(run["dimensions"])
            if run.get("dimensions") is not None
            else None
        ),
        total_texts=total,
        processed_texts=min(reused + generated, total),
        reused_embeddings=reused,
        generated_embeddings=generated,
        checkpoint_percent=checkpoint_percent,
        elapsed_seconds=elapsed_seconds,
        resumed=resumed,
    )


def _write_manifest(
    result: SemanticVectorizationResult,
    *,
    source_info: dict[str, Any],
    profile_signature: str,
    eta_seconds: float | None,
) -> None:
    payload = result.to_dict()
    payload.update(
        {
            "profile_signature": profile_signature,
            "normalization_run_id": source_info["normalization_run_id"],
            "normalization_build_signature": source_info[
                "normalization_build_signature"
            ],
            "source_segment_count": source_info["segment_count"],
            "eta_seconds": (
                round(eta_seconds, 3) if eta_seconds is not None else None
            ),
            "updated_at": utc_now_iso(),
        }
    )
    _atomic_write_json(result.manifest_path, payload)


def vectorize_semantic_corpus(
    graph_dir: str | Path,
    *,
    normalization_database_path: str | Path | None = None,
    output_path: str | Path | None = None,
    model_name: str = DEFAULT_LOCAL_EMBEDDING_MODEL,
    device: str | None = None,
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
    checkpoint_percent: float = DEFAULT_EMBEDDING_CHECKPOINT_PERCENT,
    expected_dimensions: int | None = DEFAULT_EXPECTED_DIMENSIONS,
    resume: bool = True,
    force_revectorize: bool = False,
    semantic_text_selector: SemanticTextSelector | None = None,
    progress: Callable[[str], None] | None = None,
) -> SemanticVectorizationResult:
    """Vetoriza os textos únicos do corpus normalizado de forma durável.

    A vetorização é idempotente por hash normalizado + perfil, incremental por
    ausência de vetor e retomável porque cada lote é confirmado no SQLite.
    """

    if batch_size <= 0:
        raise ValueError("batch_size deve ser maior que zero")
    if checkpoint_percent <= 0 or checkpoint_percent > 100:
        raise ValueError("checkpoint_percent deve estar entre 0 e 100")
    if expected_dimensions is not None and expected_dimensions <= 0:
        raise ValueError("expected_dimensions deve ser maior que zero")

    if expected_dimensions is None:
        expected_dimensions = resolve_embedding_model_profile(model_name).dimensions

    graph_root = Path(graph_dir).resolve()
    if not graph_root.is_dir():
        raise FileNotFoundError(f"Diretório de grafos não encontrado: {graph_root}")

    normalization_path = (
        Path(normalization_database_path).resolve()
        if normalization_database_path is not None
        else default_normalization_database_path(graph_root)
    )
    database_path = (
        Path(output_path).resolve()
        if output_path is not None
        else default_embedding_database_path(graph_root)
    )
    manifest_path = database_path.with_name(DEFAULT_EMBEDDING_MANIFEST_FILENAME)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    source = _open_normalization_database(normalization_path)
    target = sqlite3.connect(database_path)
    target.row_factory = sqlite3.Row
    _configure_connection(target)
    _create_schema(target)

    started_monotonic = time.monotonic()
    source_info: dict[str, Any] = {}
    run_id: str | None = None
    resumed = False
    try:
        source_info = _normalization_source_info(source)
        normalization_signature = str(source_info["signature"])
        normalization_version = str(source_info["normalization_version"])
        segmentation_version = str(source_info["segmentation_version"])
        total_texts = int(source_info["total_texts"])
        profile_signature = _embedding_profile_signature(
            model_name=model_name,
            expected_dimensions=expected_dimensions,
        )

        existing_run, resumable = _find_existing_run(
            target,
            normalization_signature=normalization_signature,
            model_name=model_name,
            expected_dimensions=expected_dimensions,
            resume=resume,
        )

        if (
            existing_run
            and existing_run["status"] == "completed"
            and not force_revectorize
        ):
            result = _result_from_run(
                graph_dir=graph_root,
                normalization_database_path=normalization_path,
                database_path=database_path,
                manifest_path=manifest_path,
                run=existing_run,
                normalization_signature=normalization_signature,
                normalization_version=normalization_version,
                segmentation_version=segmentation_version,
                model_name=model_name,
                checkpoint_percent=checkpoint_percent,
                elapsed_seconds=0.0,
                resumed=False,
            )
            _write_manifest(
                result,
                source_info=source_info,
                profile_signature=profile_signature,
                eta_seconds=0.0,
            )
            if progress:
                progress(
                    "[VECTORIZE] Corpus já vetorizado para o mesmo perfil: "
                    f"{result.processed_texts}/{result.total_texts} textos (100,00%)."
                )
            return result

        if existing_run and resumable and not force_revectorize:
            run_id = str(existing_run["run_id"])
            target.execute(
                """
                UPDATE embedding_runs
                   SET status = 'running',
                       updated_at = ?,
                       error_message = NULL,
                       checkpoint_percent = ?
                 WHERE run_id = ?
                """,
                (utc_now_iso(), checkpoint_percent, run_id),
            )
            target.commit()
            run = dict(existing_run)
            resumed = True
        else:
            reusable_dimensions: int | None = None
            if force_revectorize:
                with target:
                    target.execute(
                        """
                        DELETE FROM semantic_embeddings
                         WHERE normalization_version = ?
                           AND model_name = ?
                           AND embedding_profile_version = ?
                        """,
                        (
                            normalization_version,
                            model_name,
                            EMBEDDING_PROFILE_VERSION,
                        ),
                    )
                reusable = 0
            else:
                reusable, reusable_dimensions = _count_reusable_embeddings(
                    target,
                    source,
                    normalization_version=normalization_version,
                    model_name=model_name,
                    expected_dimensions=expected_dimensions,
                )
            run_id = uuid.uuid4().hex
            now = utc_now_iso()
            target.execute(
                """
                INSERT INTO embedding_runs (
                    run_id,
                    normalization_signature,
                    normalization_database_path,
                    normalization_version,
                    segmentation_version,
                    model_name,
                    embedding_profile_version,
                    storage_dtype,
                    vector_normalization,
                    similarity_metric,
                    expected_dimensions,
                    dimensions,
                    status,
                    started_at,
                    updated_at,
                    total_texts,
                    reused_embeddings,
                    checkpoint_percent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    normalization_signature,
                    str(normalization_path),
                    normalization_version,
                    segmentation_version,
                    model_name,
                    EMBEDDING_PROFILE_VERSION,
                    STORAGE_DTYPE,
                    VECTOR_NORMALIZATION,
                    SIMILARITY_METRIC,
                    expected_dimensions,
                    reusable_dimensions,
                    now,
                    now,
                    total_texts,
                    reusable,
                    checkpoint_percent,
                ),
            )
            if reusable:
                _mark_reused_embeddings_seen(
                    target,
                    source,
                    run_id=run_id,
                    normalization_version=normalization_version,
                    model_name=model_name,
                    expected_dimensions=expected_dimensions,
                )
            target.commit()
            run = dict(
                target.execute(
                    "SELECT * FROM embedding_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            )

        reused_embeddings = int(run["reused_embeddings"])
        generated_embeddings = int(run["generated_embeddings"])
        dimensions = (
            int(run["dimensions"]) if run["dimensions"] is not None else None
        )
        last_source_hash = str(run["last_source_hash"] or "")
        last_checkpoint_percent = float(run["last_checkpoint_percent"])
        generated_this_attempt = 0

        if progress:
            if resumed:
                completed = min(reused_embeddings + generated_embeddings, total_texts)
                percent = (completed / total_texts * 100.0) if total_texts else 100.0
                progress(
                    f"[VECTORIZE] Retomando run {run_id}: {completed}/{total_texts} "
                    f"textos ({percent:.2f}%), {generated_embeddings} gerados e "
                    f"{reused_embeddings} reutilizados."
                )
            else:
                pending = max(total_texts - reused_embeddings, 0)
                progress(
                    f"[VECTORIZE] Plano criado: {total_texts} textos únicos | "
                    f"reutilizáveis={reused_embeddings} | novos={pending} | "
                    f"modelo={model_name} | armazenamento=float32."
                )

        selector = semantic_text_selector or SemanticTextSelector(
            semantic_context_config_for_model(
                model_name,
                device=device,
                batch_size=batch_size,
            )
        )

        while reused_embeddings + generated_embeddings < total_texts:
            pending_rows = _fetch_pending_batch(
                source,
                target,
                normalization_version=normalization_version,
                model_name=model_name,
                expected_dimensions=expected_dimensions,
                after_hash=last_source_hash,
                batch_size=batch_size,
            )
            if not pending_rows:
                break

            hashes = [str(row["normalized_text_hash"]) for row in pending_rows]
            texts = [str(row["normalized_text"]) for row in pending_rows]
            embeddings = selector.encode_documents(texts)
            embeddings = np.asarray(embeddings, dtype=np.float32)
            if embeddings.ndim != 2 or embeddings.shape[0] != len(texts):
                raise RuntimeError(
                    "O modelo semântico devolveu uma matriz incompatível com o lote."
                )
            current_dimensions = int(embeddings.shape[1])
            if current_dimensions <= 0:
                raise RuntimeError("O modelo semântico devolveu vetores vazios.")
            if expected_dimensions is not None and current_dimensions != expected_dimensions:
                raise RuntimeError(
                    "Dimensão inesperada do modelo semântico: "
                    f"esperado={expected_dimensions}, recebido={current_dimensions}."
                )
            if dimensions is None:
                dimensions = current_dimensions
            elif dimensions != current_dimensions:
                raise RuntimeError(
                    "O modelo semântico alterou a dimensão dos vetores durante "
                    "a mesma execução."
                )

            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            norms = np.where(norms == 0.0, 1.0, norms)
            embeddings = np.asarray(embeddings / norms, dtype=STORAGE_NUMPY_DTYPE)
            now = utc_now_iso()
            rows_to_insert = [
                (
                    normalization_version,
                    normalized_hash,
                    model_name,
                    EMBEDDING_PROFILE_VERSION,
                    dimensions,
                    STORAGE_DTYPE,
                    VECTOR_NORMALIZATION,
                    SIMILARITY_METRIC,
                    sqlite3.Binary(np.ascontiguousarray(vector).tobytes(order="C")),
                    now,
                    now,
                    run_id,
                )
                for normalized_hash, vector in zip(hashes, embeddings, strict=True)
            ]
            last_source_hash = hashes[-1]
            generated_embeddings += len(rows_to_insert)
            generated_this_attempt += len(rows_to_insert)
            completed = min(reused_embeddings + generated_embeddings, total_texts)
            percent = (completed / total_texts * 100.0) if total_texts else 100.0

            with target:
                target.executemany(
                    """
                    INSERT INTO semantic_embeddings (
                        normalization_version,
                        normalized_text_hash,
                        model_name,
                        embedding_profile_version,
                        dimensions,
                        storage_dtype,
                        vector_normalization,
                        similarity_metric,
                        embedding,
                        created_at,
                        updated_at,
                        last_seen_run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (
                        normalization_version,
                        normalized_text_hash,
                        model_name,
                        embedding_profile_version
                    ) DO UPDATE SET
                        dimensions = excluded.dimensions,
                        storage_dtype = excluded.storage_dtype,
                        vector_normalization = excluded.vector_normalization,
                        similarity_metric = excluded.similarity_metric,
                        embedding = excluded.embedding,
                        updated_at = excluded.updated_at,
                        last_seen_run_id = excluded.last_seen_run_id
                    """,
                    rows_to_insert,
                )
                target.execute(
                    """
                    UPDATE embedding_runs
                       SET updated_at = ?,
                           dimensions = ?,
                           generated_embeddings = ?,
                           last_source_hash = ?
                     WHERE run_id = ?
                    """,
                    (
                        now,
                        dimensions,
                        generated_embeddings,
                        last_source_hash,
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
                    int(percent / checkpoint_percent) * checkpoint_percent,
                )
                if percent >= 100.0:
                    checkpoint_value = 100.0
                target.execute(
                    """
                    UPDATE embedding_runs
                       SET last_checkpoint_percent = ?, updated_at = ?
                     WHERE run_id = ?
                    """,
                    (checkpoint_value, utc_now_iso(), run_id),
                )
                target.commit()
                last_checkpoint_percent = checkpoint_value
                remaining = max(total_texts - completed, 0)
                eta = _eta_seconds(
                    started_monotonic=started_monotonic,
                    generated_this_attempt=generated_this_attempt,
                    remaining_texts=remaining,
                )
                current_run = dict(
                    target.execute(
                        "SELECT * FROM embedding_runs WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                )
                checkpoint_result = _result_from_run(
                    graph_dir=graph_root,
                    normalization_database_path=normalization_path,
                    database_path=database_path,
                    manifest_path=manifest_path,
                    run=current_run,
                    normalization_signature=normalization_signature,
                    normalization_version=normalization_version,
                    segmentation_version=segmentation_version,
                    model_name=model_name,
                    checkpoint_percent=checkpoint_percent,
                    elapsed_seconds=time.monotonic() - started_monotonic,
                    resumed=resumed,
                )
                _write_manifest(
                    checkpoint_result,
                    source_info=source_info,
                    profile_signature=profile_signature,
                    eta_seconds=eta,
                )
                if progress:
                    progress(
                        f"[VECTORIZE] {completed}/{total_texts} textos "
                        f"({percent:.2f}%) | gerados={generated_embeddings} | "
                        f"reutilizados={reused_embeddings} | dimensões={dimensions} | "
                        f"ETA {_format_duration(eta)} | checkpoint persistido."
                    )

        completed = min(reused_embeddings + generated_embeddings, total_texts)
        if completed != total_texts:
            # Um cursor antigo só pode ocorrer após manipulação externa do cache.
            # Reinicia a varredura uma vez e detecta qualquer lacuna remanescente.
            remaining_hash = target.execute(
                """
                SELECT COUNT(*)
                  FROM semantic_embeddings
                 WHERE normalization_version = ?
                   AND model_name = ?
                   AND embedding_profile_version = ?
                """,
                (
                    normalization_version,
                    model_name,
                    EMBEDDING_PROFILE_VERSION,
                ),
            ).fetchone()[0]
            raise RuntimeError(
                "A vetorização terminou com contagem incompatível: "
                f"esperado={total_texts}, cache_do_perfil={remaining_hash}."
            )

        now = utc_now_iso()
        with target:
            target.execute(
                """
                UPDATE embedding_runs
                   SET status = 'completed',
                       updated_at = ?,
                       completed_at = ?,
                       dimensions = ?,
                       last_checkpoint_percent = 100,
                       error_message = NULL
                 WHERE run_id = ?
                """,
                (now, now, dimensions, run_id),
            )

        final_run = dict(
            target.execute(
                "SELECT * FROM embedding_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        )
        result = _result_from_run(
            graph_dir=graph_root,
            normalization_database_path=normalization_path,
            database_path=database_path,
            manifest_path=manifest_path,
            run=final_run,
            normalization_signature=normalization_signature,
            normalization_version=normalization_version,
            segmentation_version=segmentation_version,
            model_name=model_name,
            checkpoint_percent=checkpoint_percent,
            elapsed_seconds=time.monotonic() - started_monotonic,
            resumed=resumed,
        )
        _write_manifest(
            result,
            source_info=source_info,
            profile_signature=profile_signature,
            eta_seconds=0.0,
        )
        if progress:
            progress(
                f"[VECTORIZE] Concluído: {result.processed_texts}/"
                f"{result.total_texts} textos (100,00%) | "
                f"gerados={result.generated_embeddings} | "
                f"reutilizados={result.reused_embeddings} | "
                f"dimensões={result.dimensions} | float32 | "
                f"tempo={_format_duration(result.elapsed_seconds)}."
            )
        return result
    except BaseException as exc:
        target.rollback()
        if run_id is not None:
            status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            message = (
                "Interrompido pelo usuário" if status == "interrupted" else str(exc)
            )
            target.execute(
                """
                UPDATE embedding_runs
                   SET status = ?, updated_at = ?, error_message = ?
                 WHERE run_id = ?
                """,
                (status, utc_now_iso(), message, run_id),
            )
            target.commit()
            if progress:
                row = target.execute(
                    "SELECT * FROM embedding_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is not None:
                    completed = min(
                        int(row["reused_embeddings"])
                        + int(row["generated_embeddings"]),
                        int(row["total_texts"]),
                    )
                    progress(
                        f"[VECTORIZE] {status}: {completed}/"
                        f"{int(row['total_texts'])} textos confirmados. "
                        "Estado preservado para retomada."
                    )
        raise
    finally:
        source.close()
        target.close()
