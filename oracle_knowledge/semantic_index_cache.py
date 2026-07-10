from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from oracle_knowledge.search.semantic_context import resolve_embedding_model_profile


@dataclass(frozen=True)
class SemanticEmbeddingCacheInfo:
    normalization_database_path: Path
    embedding_database_path: Path
    normalization_signature: str
    normalization_version: str
    segmentation_version: str
    model_name: str
    embedding_profile_version: str
    dimensions: int
    storage_dtype: str
    vector_normalization: str
    similarity_metric: str
    total_texts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalization_database_path": str(self.normalization_database_path),
            "embedding_database_path": str(self.embedding_database_path),
            "normalization_signature": self.normalization_signature,
            "normalization_version": self.normalization_version,
            "segmentation_version": self.segmentation_version,
            "model_name": self.model_name,
            "embedding_profile_version": self.embedding_profile_version,
            "dimensions": self.dimensions,
            "storage_dtype": self.storage_dtype,
            "vector_normalization": self.vector_normalization,
            "similarity_metric": self.similarity_metric,
            "total_texts": self.total_texts,
        }


@dataclass(frozen=True)
class SemanticCachePopulationResult:
    semantic_root_count: int
    semantic_segment_count: int
    semantic_segment_node_count: int
    dimensions: int
    model_name: str


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def resolve_semantic_embedding_cache(
    graph_dir: str | Path,
    *,
    model_name: str,
    normalization_database_path: str | Path | None = None,
    embedding_database_path: str | Path | None = None,
) -> SemanticEmbeddingCacheInfo:
    """Valida e resolve o cache completo para um perfil de embeddings."""

    # Imports locais evitam o ciclo indexing -> cache -> vectorization -> indexing.
    from oracle_knowledge.semantic_normalization import (
        default_normalization_database_path,
    )
    from oracle_knowledge.semantic_vectorization import (
        EMBEDDING_PROFILE_VERSION,
        SIMILARITY_METRIC,
        STORAGE_DTYPE,
        VECTOR_NORMALIZATION,
        _normalization_source_info,
        default_embedding_database_path,
    )

    graph_root = Path(graph_dir).resolve()
    normalization_path = (
        Path(normalization_database_path).resolve()
        if normalization_database_path is not None
        else default_normalization_database_path(graph_root)
    )
    embedding_path = (
        Path(embedding_database_path).resolve()
        if embedding_database_path is not None
        else default_embedding_database_path(graph_root)
    )

    if not normalization_path.is_file():
        raise FileNotFoundError(
            f"Banco normalizado não encontrado: {normalization_path}. "
            "Execute normalize-index antes de build-index."
        )
    if not embedding_path.is_file():
        raise FileNotFoundError(
            f"Banco de embeddings não encontrado: {embedding_path}. "
            "Execute vectorize-index antes de build-index."
        )

    profile = resolve_embedding_model_profile(model_name)
    normalization_connection = _open_readonly(normalization_path)
    embedding_connection = _open_readonly(embedding_path)
    try:
        source_info = _normalization_source_info(normalization_connection)
        normalization_signature = str(source_info["signature"])
        normalization_version = str(source_info["normalization_version"])
        segmentation_version = str(source_info["segmentation_version"])
        total_texts = int(source_info["total_texts"])

        run = embedding_connection.execute(
            """
            SELECT *
              FROM embedding_runs
             WHERE status = 'completed'
               AND normalization_signature = ?
               AND model_name = ?
               AND embedding_profile_version = ?
               AND storage_dtype = ?
               AND vector_normalization = ?
               AND similarity_metric = ?
               AND dimensions = ?
          ORDER BY completed_at DESC, rowid DESC
             LIMIT 1
            """,
            (
                normalization_signature,
                model_name,
                EMBEDDING_PROFILE_VERSION,
                STORAGE_DTYPE,
                VECTOR_NORMALIZATION,
                SIMILARITY_METRIC,
                profile.dimensions,
            ),
        ).fetchone()
        if run is None:
            raise ValueError(
                "Não existe uma vetorização concluída e compatível para o "
                f"modelo {model_name}, {profile.dimensions} dimensões e o "
                "corpus normalizado atual. Execute vectorize-index."
            )

        embedding_count = int(
            embedding_connection.execute(
                """
                SELECT COUNT(*)
                  FROM semantic_embeddings
                 WHERE normalization_version = ?
                   AND model_name = ?
                   AND embedding_profile_version = ?
                   AND dimensions = ?
                   AND storage_dtype = ?
                   AND vector_normalization = ?
                   AND similarity_metric = ?
                """,
                (
                    normalization_version,
                    model_name,
                    EMBEDDING_PROFILE_VERSION,
                    profile.dimensions,
                    STORAGE_DTYPE,
                    VECTOR_NORMALIZATION,
                    SIMILARITY_METRIC,
                ),
            ).fetchone()[0]
        )
        if embedding_count != total_texts:
            raise ValueError(
                "O cache de embeddings está incompleto para o corpus atual: "
                f"normalizados={total_texts}, embeddings={embedding_count}. "
                "Execute ou retome vectorize-index."
            )

        return SemanticEmbeddingCacheInfo(
            normalization_database_path=normalization_path,
            embedding_database_path=embedding_path,
            normalization_signature=normalization_signature,
            normalization_version=normalization_version,
            segmentation_version=segmentation_version,
            model_name=model_name,
            embedding_profile_version=EMBEDDING_PROFILE_VERSION,
            dimensions=profile.dimensions,
            storage_dtype=STORAGE_DTYPE,
            vector_normalization=VECTOR_NORMALIZATION,
            similarity_metric=SIMILARITY_METRIC,
            total_texts=total_texts,
        )
    finally:
        embedding_connection.close()
        normalization_connection.close()


def populate_semantic_index_from_cache(
    connection: sqlite3.Connection,
    cache: SemanticEmbeddingCacheInfo,
    *,
    layers: Sequence[str],
    progress: Callable[[str], None] | None = None,
) -> SemanticCachePopulationResult:
    """Associa os vetores persistidos aos nós do índice em construção."""

    if not layers:
        return SemanticCachePopulationResult(
            semantic_root_count=0,
            semantic_segment_count=0,
            semantic_segment_node_count=0,
            dimensions=cache.dimensions,
            model_name=cache.model_name,
        )

    layer_placeholders = ",".join("?" for _ in layers)
    normalization_alias = "normalization_cache"
    embedding_alias = "embedding_cache"

    connection.execute(
        f"ATTACH DATABASE ? AS {normalization_alias}",
        (str(cache.normalization_database_path),),
    )
    connection.execute(
        f"ATTACH DATABASE ? AS {embedding_alias}",
        (str(cache.embedding_database_path),),
    )
    try:
        missing = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                  FROM {normalization_alias}.normalized_segments s
                  JOIN nodes n
                    ON n.graph_layer = s.graph_layer
                   AND n.node_id = s.node_id
                  LEFT JOIN {embedding_alias}.semantic_embeddings e
                    ON e.normalization_version = s.normalization_version
                   AND e.normalized_text_hash = s.normalized_text_hash
                   AND e.model_name = ?
                   AND e.embedding_profile_version = ?
                   AND e.dimensions = ?
                   AND e.storage_dtype = ?
                   AND e.vector_normalization = ?
                   AND e.similarity_metric = ?
                 WHERE s.normalization_version = ?
                   AND s.segmentation_version = ?
                   AND s.graph_layer IN ({layer_placeholders})
                   AND e.normalized_text_hash IS NULL
                """,
                (
                    cache.model_name,
                    cache.embedding_profile_version,
                    cache.dimensions,
                    cache.storage_dtype,
                    cache.vector_normalization,
                    cache.similarity_metric,
                    cache.normalization_version,
                    cache.segmentation_version,
                    *layers,
                ),
            ).fetchone()[0]
        )
        if missing:
            raise ValueError(
                f"Existem {missing} segmentos das camadas solicitadas sem "
                f"embedding compatível para {cache.model_name}."
            )

        connection.execute(
            f"""
            INSERT INTO semantic_root_refs (
                node_pk, normalization_version, normalized_text_hash
            )
            SELECT n.node_pk,
                   s.normalization_version,
                   s.normalized_text_hash
              FROM nodes n
              JOIN {normalization_alias}.normalized_segments s
                ON s.graph_layer = n.graph_layer
               AND s.node_id = n.node_id
             WHERE s.normalization_version = ?
               AND s.segmentation_version = ?
               AND s.segment_kind = 'root'
               AND s.graph_layer IN ({layer_placeholders})
            """,
            (
                cache.normalization_version,
                cache.segmentation_version,
                *layers,
            ),
        )
        semantic_root_count = int(
            connection.execute("SELECT COUNT(*) FROM semantic_root_refs").fetchone()[0]
        )
        if progress:
            progress(
                "[INDEX] Raízes semânticas associadas ao cache: "
                f"{semantic_root_count}."
            )

        connection.execute(
            f"""
            INSERT INTO semantic_segment_refs (
                node_pk,
                segment_index,
                normalization_version,
                normalized_text_hash
            )
            SELECT n.node_pk,
                   s.segment_index,
                   s.normalization_version,
                   s.normalized_text_hash
              FROM nodes n
              JOIN {normalization_alias}.normalized_segments s
                ON s.graph_layer = n.graph_layer
               AND s.node_id = n.node_id
             WHERE s.normalization_version = ?
               AND s.segmentation_version = ?
               AND s.segment_kind <> 'root'
               AND s.graph_layer IN ({layer_placeholders})
            """,
            (
                cache.normalization_version,
                cache.segmentation_version,
                *layers,
            ),
        )
        semantic_segment_count = int(
            connection.execute("SELECT COUNT(*) FROM semantic_segment_refs").fetchone()[0]
        )
        semantic_segment_node_count = int(
            connection.execute(
                "SELECT COUNT(DISTINCT node_pk) FROM semantic_segment_refs"
            ).fetchone()[0]
        )
        if progress:
            progress(
                "[INDEX] Segmentos semânticos associados ao cache: "
                f"{semantic_segment_count} segmentos em "
                f"{semantic_segment_node_count} nós."
            )

        result = SemanticCachePopulationResult(
            semantic_root_count=semantic_root_count,
            semantic_segment_count=semantic_segment_count,
            semantic_segment_node_count=semantic_segment_node_count,
            dimensions=cache.dimensions,
            model_name=cache.model_name,
        )
        connection.commit()
        return result
    finally:
        connection.execute(f"DETACH DATABASE {embedding_alias}")
        connection.execute(f"DETACH DATABASE {normalization_alias}")
