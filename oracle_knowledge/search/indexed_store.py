from __future__ import annotations

import heapq
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from oracle_knowledge.common import tokenize
from oracle_knowledge.indexing import (
    INDEX_BUNDLE_VERSION,
    SUPPORTED_INDEX_SCHEMA_VERSIONS,
    default_index_path,
    file_sha256,
    read_index_bundle,
    read_index_metadata,
)
from oracle_knowledge.search.semantic_context import SemanticTextSelector


ROOT_NODE_TYPES = {
    "physical": {"physical_table"},
    "otbi_analytics": {"otbi_subject_area"},
    "rest": {"rest_resource", "adf_resource"},
}


class IndexedGraphStore:
    """Acesso somente leitura ao índice persistente dos grafos."""

    def __init__(
        self,
        graph_dir: str | Path,
        *,
        index_path: str | Path | None = None,
        semantic_text_selector: SemanticTextSelector | None = None,
    ) -> None:
        self.graph_dir = Path(graph_dir).resolve()
        self.index_path = (
            Path(index_path).resolve()
            if index_path is not None
            else default_index_path(self.graph_dir)
        )
        if not self.index_path.is_file():
            raise FileNotFoundError(f"Índice SQLite não encontrado: {self.index_path}")

        self.connection = sqlite3.connect(
            f"file:{self.index_path.as_posix()}?mode=ro",
            uri=True,
        )
        self.connection.row_factory = sqlite3.Row
        self.semantic_text_selector = semantic_text_selector or SemanticTextSelector()
        self.metadata = read_index_metadata(self.connection)
        self._tables = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self._validate_compatibility()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "IndexedGraphStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _validate_compatibility(self) -> None:
        schema_version = self.metadata.get("schema_version")
        if schema_version not in SUPPORTED_INDEX_SCHEMA_VERSIONS:
            raise ValueError(
                "Índice SQLite incompatível com a versão atual. "
                f"Versões aceitas {sorted(SUPPORTED_INDEX_SCHEMA_VERSIONS)}, "
                f"encontrado {schema_version}. Execute build-index novamente."
            )

        graph_files = self.connection.execute(
            """
            SELECT filename, size_bytes, modified_ns
              FROM graph_files
             ORDER BY graph_layer
            """
        ).fetchall()
        for row in graph_files:
            source = self.graph_dir / str(row["filename"])
            if not source.is_file():
                raise ValueError(
                    f"O grafo de origem do índice não existe mais: {source}. "
                    "Execute build-index novamente."
                )
            stat = source.stat()
            if (
                stat.st_size != int(row["size_bytes"])
                or stat.st_mtime_ns != int(row["modified_ns"])
            ):
                stored_hash_row = self.connection.execute(
                    "SELECT sha256 FROM graph_files WHERE filename = ?",
                    (str(row["filename"]),),
                ).fetchone()
                stored_hash = str(stored_hash_row[0]) if stored_hash_row else ""
                if not stored_hash or file_sha256(source) != stored_hash:
                    raise ValueError(
                        f"O índice está desatualizado em relação a {source.name}. "
                        "Execute build-index novamente."
                    )

    @property
    def has_semantic_roots(self) -> bool:
        return bool(self.metadata.get("semantic_root_count"))

    @property
    def has_semantic_segments(self) -> bool:
        return (
            "semantic_segments" in self._tables
            and bool(self.metadata.get("semantic_segment_count"))
        )

    @staticmethod
    def _decode_node(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise ValueError(f"Payload de nó inválido no índice: {row['node_id']}")
        return payload

    @staticmethod
    def _chunks(values: Iterable[str], size: int = 500) -> Iterable[list[str]]:
        chunk: list[str] = []
        for value in values:
            chunk.append(value)
            if len(chunk) >= size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk

    def fetch_nodes(
        self,
        layer: str,
        node_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        identifiers = list(dict.fromkeys(str(value) for value in node_ids if str(value)))
        result: dict[str, dict[str, Any]] = {}
        for chunk in self._chunks(identifiers):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT node_id, payload_json
                  FROM nodes
                 WHERE graph_layer = ?
                   AND node_id IN ({placeholders})
                """,
                [layer, *chunk],
            ).fetchall()
            for row in rows:
                result[str(row["node_id"])] = self._decode_node(row)
        return result

    def _module_clause(
        self,
        module_ids: set[str] | None,
        *,
        node_alias: str = "n",
    ) -> tuple[str, list[str]]:
        if not module_ids:
            return "", []
        modules = sorted(str(value) for value in module_ids)
        placeholders = ",".join("?" for _ in modules)
        return (
            f" AND EXISTS ("
            f"SELECT 1 FROM node_modules nm "
            f"WHERE nm.node_pk = {node_alias}.node_pk "
            f"AND nm.module_id IN ({placeholders})"
            f")",
            modules,
        )

    def semantic_roots(
        self,
        query: str,
        layer: str,
        *,
        module_ids: set[str] | None,
        limit: int,
        query_vector: np.ndarray | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        if limit <= 0:
            return []
        root_types = ROOT_NODE_TYPES[layer]
        if not self.has_semantic_roots:
            return self.fts_roots(
                query,
                layer,
                module_ids=module_ids,
                limit=limit,
            )

        type_placeholders = ",".join("?" for _ in root_types)
        module_sql, module_params = self._module_clause(module_ids)
        cursor = self.connection.execute(
            f"""
            SELECT n.node_pk, n.node_id, n.payload_json,
                   s.dimensions, s.embedding
              FROM semantic_roots s
              JOIN nodes n ON n.node_pk = s.node_pk
             WHERE n.graph_layer = ?
               AND n.node_type IN ({type_placeholders})
               {module_sql}
            """,
            [layer, *sorted(root_types), *module_params],
        )

        if query_vector is None:
            query_vector = self.semantic_text_selector.encode_query(query)
        if query_vector.size == 0:
            return []

        best: list[tuple[float, str, dict[str, Any]]] = []
        while rows := cursor.fetchmany(512):
            vectors: list[np.ndarray] = []
            valid_rows: list[sqlite3.Row] = []
            for row in rows:
                dimensions = int(row["dimensions"])
                vector = np.frombuffer(row["embedding"], dtype="<f4")
                if vector.size != dimensions or dimensions != query_vector.size:
                    continue
                vectors.append(vector)
                valid_rows.append(row)
            if not vectors:
                continue
            matrix = np.vstack(vectors)
            scores = matrix @ query_vector
            for row, score_value in zip(valid_rows, scores, strict=True):
                score = float(score_value)
                node = self._decode_node(row)
                key = (score, str(row["node_id"]), node)
                if len(best) < limit:
                    heapq.heappush(best, key)
                elif (score, str(row["node_id"])) > (best[0][0], best[0][1]):
                    heapq.heapreplace(best, key)

        ranked = sorted(best, key=lambda item: (-item[0], item[1]))
        return [(node, score) for score, _, node in ranked]

    def semantic_segment_scores(
        self,
        layer: str,
        node_ids: Iterable[str],
        query_vector: np.ndarray,
        *,
        top_segments: int | None = None,
    ) -> dict[str, float]:
        """Pontua nós usando os segmentos persistidos no SQLite."""
        identifiers = list(
            dict.fromkeys(str(value) for value in node_ids if str(value))
        )
        if (
            not identifiers
            or query_vector.size == 0
            or not self.has_semantic_segments
        ):
            return {}

        requested_top_segments = max(
            1,
            int(
                top_segments
                or self.semantic_text_selector.config.candidate_top_segments
            ),
        )
        model_name = str(
            self.metadata.get("semantic_model_name")
            or self.semantic_text_selector.config.model_name
            or ""
        )
        scores_by_node: dict[str, list[float]] = {}

        for chunk in self._chunks(identifiers):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT n.node_id,
                       s.segment_index,
                       s.dimensions,
                       s.embedding
                  FROM semantic_segments s
                  JOIN nodes n ON n.node_pk = s.node_pk
                 WHERE n.graph_layer = ?
                   AND n.node_id IN ({placeholders})
                   AND s.model_name = ?
                 ORDER BY n.node_id, s.segment_index
                """,
                [layer, *chunk, model_name],
            ).fetchall()

            valid_rows: list[sqlite3.Row] = []
            vectors: list[np.ndarray] = []
            for row in rows:
                dimensions = int(row["dimensions"])
                vector = np.frombuffer(row["embedding"], dtype="<f4")
                if vector.size != dimensions or dimensions != query_vector.size:
                    continue
                valid_rows.append(row)
                vectors.append(vector)

            if not vectors:
                continue

            matrix = np.vstack(vectors)
            values = matrix @ query_vector
            for row, value in zip(valid_rows, values, strict=True):
                scores_by_node.setdefault(str(row["node_id"]), []).append(
                    float(value)
                )

        return {
            node_id: (
                sum(sorted(values, reverse=True)[:requested_top_segments])
                / min(len(values), requested_top_segments)
            )
            for node_id, values in scores_by_node.items()
            if values
        }

    def fts_roots(
        self,
        query: str,
        layer: str,
        *,
        module_ids: set[str] | None,
        limit: int,
    ) -> list[tuple[dict[str, Any], float]]:
        tokens = list(dict.fromkeys(tokenize(query)))
        if not tokens or limit <= 0:
            return []
        expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        root_types = ROOT_NODE_TYPES[layer]
        type_placeholders = ",".join("?" for _ in root_types)
        module_sql, module_params = self._module_clause(module_ids)
        rows = self.connection.execute(
            f"""
            SELECT n.node_id, n.payload_json, bm25(nodes_fts) AS rank_value
              FROM nodes_fts
              JOIN nodes n ON n.node_pk = nodes_fts.rowid
             WHERE nodes_fts MATCH ?
               AND n.graph_layer = ?
               AND n.node_type IN ({type_placeholders})
               {module_sql}
             ORDER BY rank_value ASC, n.title ASC
             LIMIT ?
            """,
            [expression, layer, *sorted(root_types), *module_params, limit],
        ).fetchall()
        if not rows:
            return []
        raw_scores = [-float(row["rank_value"] or 0.0) for row in rows]
        maximum = max(raw_scores) if raw_scores else 1.0
        minimum = min(raw_scores) if raw_scores else 0.0
        span = maximum - minimum
        result: list[tuple[dict[str, Any], float]] = []
        for row, raw_score in zip(rows, raw_scores, strict=True):
            score = 1.0 if span <= 0.0 else (raw_score - minimum) / span
            result.append((self._decode_node(row), float(score)))
        return result

    def prefilter_children(
        self,
        layer: str,
        source_queries: dict[str, str],
        edge_types: set[str],
        *,
        limit_per_source: int,
    ) -> tuple[
        dict[str, list[tuple[dict[str, Any], dict[str, Any]]]],
        dict[str, int],
        dict[str, int],
    ]:
        """
        Pré-seleciona filhos relacionados usando FTS5 e um limite rígido.

        A busca lexical é executada apenas entre os filhos de cada origem. Se
        não houver candidatos suficientes, o restante é preenchido de forma
        determinística, sem carregar todos os filhos em memória e sem enviá-los
        ao modelo semântico.
        """
        identifiers = list(
            dict.fromkeys(
                str(value)
                for value in source_queries
                if str(value)
            )
        )
        if not identifiers or not edge_types or limit_per_source <= 0:
            return {}, {}, {}

        source_pk_rows: dict[str, int] = {}
        for chunk in self._chunks(identifiers):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT node_id, node_pk
                  FROM nodes
                 WHERE graph_layer = ?
                   AND node_id IN ({placeholders})
                """,
                [layer, *chunk],
            ).fetchall()
            for row in rows:
                source_pk_rows[str(row["node_id"])] = int(row["node_pk"])

        type_placeholders = ",".join("?" for _ in edge_types)
        sorted_edge_types = sorted(edge_types)
        results: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {
            source_id: [] for source_id in identifiers
        }
        linked_counts: dict[str, int] = {source_id: 0 for source_id in identifiers}
        fts_counts: dict[str, int] = {source_id: 0 for source_id in identifiers}

        for source_id in identifiers:
            source_pk = source_pk_rows.get(source_id)
            if source_pk is None:
                continue

            linked_counts[source_id] = int(
                self.connection.execute(
                    f"""
                    SELECT COUNT(*)
                      FROM edges e
                     WHERE e.graph_layer = ?
                       AND e.source_node_pk = ?
                       AND e.edge_type IN ({type_placeholders})
                    """,
                    [layer, source_pk, *sorted_edge_types],
                ).fetchone()[0]
            )

            seen_node_ids: set[str] = set()
            query_tokens = list(
                dict.fromkeys(tokenize(source_queries.get(source_id)))
            )
            if query_tokens:
                expression = " OR ".join(
                    f'"{token.replace(chr(34), chr(34) * 2)}"'
                    for token in query_tokens
                )
                rows = self.connection.execute(
                    f"""
                    SELECT e.payload_json AS edge_payload,
                           n.node_id,
                           n.payload_json,
                           bm25(nodes_fts) AS rank_value
                      FROM nodes_fts
                      JOIN nodes n
                        ON n.node_pk = nodes_fts.rowid
                      JOIN edges e
                        ON e.target_node_pk = n.node_pk
                     WHERE nodes_fts MATCH ?
                       AND e.graph_layer = ?
                       AND e.source_node_pk = ?
                       AND e.edge_type IN ({type_placeholders})
                       AND n.graph_layer = ?
                     ORDER BY rank_value ASC, n.title ASC, n.node_id ASC
                     LIMIT ?
                    """,
                    [
                        expression,
                        layer,
                        source_pk,
                        *sorted_edge_types,
                        layer,
                        limit_per_source,
                    ],
                ).fetchall()
                for row in rows:
                    node_id = str(row["node_id"])
                    if node_id in seen_node_ids:
                        continue
                    seen_node_ids.add(node_id)
                    edge = json.loads(str(row["edge_payload"]))
                    results[source_id].append((edge, self._decode_node(row)))
                fts_counts[source_id] = len(results[source_id])

            remaining = limit_per_source - len(results[source_id])
            if remaining <= 0:
                continue

            exclusion_sql = ""
            exclusion_params: list[str] = []
            if seen_node_ids:
                placeholders = ",".join("?" for _ in seen_node_ids)
                exclusion_sql = f" AND n.node_id NOT IN ({placeholders})"
                exclusion_params = sorted(seen_node_ids)

            rows = self.connection.execute(
                f"""
                SELECT e.payload_json AS edge_payload,
                       n.node_id,
                       n.payload_json
                  FROM edges e
                  JOIN nodes n
                    ON n.node_pk = e.target_node_pk
                 WHERE e.graph_layer = ?
                   AND e.source_node_pk = ?
                   AND e.edge_type IN ({type_placeholders})
                   AND n.graph_layer = ?
                   {exclusion_sql}
                 ORDER BY n.title ASC, n.node_id ASC
                 LIMIT ?
                """,
                [
                    layer,
                    source_pk,
                    *sorted_edge_types,
                    layer,
                    *exclusion_params,
                    remaining,
                ],
            ).fetchall()
            for row in rows:
                node_id = str(row["node_id"])
                if node_id in seen_node_ids:
                    continue
                seen_node_ids.add(node_id)
                edge = json.loads(str(row["edge_payload"]))
                results[source_id].append((edge, self._decode_node(row)))

        return results, linked_counts, fts_counts

    def children(
        self,
        layer: str,
        source_ids: Iterable[str],
        edge_types: set[str],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return self._related(layer, source_ids, edge_types, outgoing=True)

    def parents(
        self,
        layer: str,
        target_ids: Iterable[str],
        edge_types: set[str],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return self._related(layer, target_ids, edge_types, outgoing=False)

    def _related(
        self,
        layer: str,
        node_ids: Iterable[str],
        edge_types: set[str],
        *,
        outgoing: bool,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        identifiers = list(dict.fromkeys(str(value) for value in node_ids if str(value)))
        if not identifiers or not edge_types:
            return []
        results: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for chunk in self._chunks(identifiers):
            id_placeholders = ",".join("?" for _ in chunk)
            type_placeholders = ",".join("?" for _ in edge_types)
            if outgoing:
                id_column = "e.source_id"
                related_pk = "e.target_node_pk"
            else:
                id_column = "e.target_id"
                related_pk = "e.source_node_pk"
            rows = self.connection.execute(
                f"""
                SELECT e.payload_json AS edge_payload,
                       n.node_id, n.payload_json
                  FROM edges e
                  JOIN nodes n ON n.node_pk = {related_pk}
                 WHERE e.graph_layer = ?
                   AND {id_column} IN ({id_placeholders})
                   AND e.edge_type IN ({type_placeholders})
                """,
                [layer, *chunk, *sorted(edge_types)],
            ).fetchall()
            for row in rows:
                edge = json.loads(str(row["edge_payload"]))
                results.append((edge, self._decode_node(row)))
        return results


class IndexedGraphBundleStore:
    """Federa índices SQLite independentes por camada."""

    def __init__(
        self,
        graph_dir: str | Path,
        *,
        bundle_path: str | Path,
        semantic_text_selector: SemanticTextSelector | None = None,
    ) -> None:
        self.graph_dir = Path(graph_dir).resolve()
        self.index_path = Path(bundle_path).resolve()
        if not self.index_path.is_file():
            raise FileNotFoundError(
                f"Manifesto de índices não encontrado: {self.index_path}"
            )
        self.semantic_text_selector = semantic_text_selector or SemanticTextSelector()
        self.payload = read_index_bundle(self.index_path)
        if self.payload.get("version") != INDEX_BUNDLE_VERSION:
            raise ValueError(
                "Manifesto de índices incompatível. "
                f"Esperado {INDEX_BUNDLE_VERSION}, encontrado "
                f"{self.payload.get('version')}."
            )
        indexes = self.payload.get("indexes")
        if not isinstance(indexes, dict):
            raise ValueError("index_bundle.json não contém o objeto indexes.")
        self._entries: dict[str, dict[str, Any]] = {
            str(layer): dict(entry)
            for layer, entry in indexes.items()
            if isinstance(entry, dict)
        }
        self._stores: dict[str, IndexedGraphStore] = {}

    @property
    def index_paths(self) -> dict[str, str]:
        return {
            layer: str(self._entry_path(entry))
            for layer, entry in self._entries.items()
        }

    def _entry_path(self, entry: dict[str, Any]) -> Path:
        raw_path = Path(str(entry.get("path") or ""))
        if raw_path.is_absolute():
            return raw_path.resolve()
        return (self.index_path.parent / raw_path).resolve()

    def _store(self, layer: str) -> IndexedGraphStore:
        if layer not in self._stores:
            entry = self._entries.get(layer)
            if entry is None:
                raise FileNotFoundError(
                    f"O bundle não contém índice para a camada {layer}."
                )
            self._stores[layer] = IndexedGraphStore(
                self.graph_dir,
                index_path=self._entry_path(entry),
                semantic_text_selector=self.semantic_text_selector,
            )
        return self._stores[layer]

    def has_layer(self, layer: str) -> bool:
        return layer in self._entries

    def close(self) -> None:
        for store in self._stores.values():
            store.close()
        self._stores.clear()

    def __enter__(self) -> "IndexedGraphBundleStore":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def fetch_nodes(
        self,
        layer: str,
        node_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        return self._store(layer).fetch_nodes(layer, node_ids)

    def semantic_roots(
        self,
        query: str,
        layer: str,
        *,
        module_ids: set[str] | None,
        limit: int,
        query_vector: np.ndarray | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        return self._store(layer).semantic_roots(
            query,
            layer,
            module_ids=module_ids,
            limit=limit,
            query_vector=query_vector,
        )

    def semantic_segment_scores(
        self,
        layer: str,
        node_ids: Iterable[str],
        query_vector: np.ndarray,
        *,
        top_segments: int | None = None,
    ) -> dict[str, float]:
        return self._store(layer).semantic_segment_scores(
            layer,
            node_ids,
            query_vector,
            top_segments=top_segments,
        )

    def fts_roots(
        self,
        query: str,
        layer: str,
        *,
        module_ids: set[str] | None,
        limit: int,
    ) -> list[tuple[dict[str, Any], float]]:
        return self._store(layer).fts_roots(
            query,
            layer,
            module_ids=module_ids,
            limit=limit,
        )

    def prefilter_children(
        self,
        layer: str,
        source_queries: dict[str, str],
        edge_types: set[str],
        *,
        limit_per_source: int,
    ) -> tuple[
        dict[str, list[tuple[dict[str, Any], dict[str, Any]]]],
        dict[str, int],
        dict[str, int],
    ]:
        return self._store(layer).prefilter_children(
            layer,
            source_queries,
            edge_types,
            limit_per_source=limit_per_source,
        )

    def children(
        self,
        layer: str,
        source_ids: Iterable[str],
        edge_types: set[str],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return self._store(layer).children(layer, source_ids, edge_types)

    def parents(
        self,
        layer: str,
        target_ids: Iterable[str],
        edge_types: set[str],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return self._store(layer).parents(layer, target_ids, edge_types)
