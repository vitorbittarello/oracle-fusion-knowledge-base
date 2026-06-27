from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import numpy as np

DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-large-instruct"


@dataclass(frozen=True)
class SemanticContextConfig:
    """
    Configuração da seleção extrativa semântica usada no contexto da LLM.

    O modelo padrão é multilíngue e orientado a recuperação. A consulta recebe
    uma instrução explícita; os trechos documentais são codificados sem
    reescrita, preservando o conteúdo original recuperado da documentação.
    """

    model_name: str = DEFAULT_EMBEDDING_MODEL
    task_instruction: str = (
        "Given an Oracle Fusion Cloud question, retrieve passages that are most "
        "useful to identify business meaning, physical tables, columns, joins, "
        "validated rules, OTBI subject areas, and REST resources."
    )
    device: str | None = None
    batch_size: int = 16
    minimum_segment_characters: int = 24
    maximum_segment_characters: int = 900
    summary_max_characters: int = 500
    mmr_lambda: float = 0.82
    candidate_rerank_weight: float = 0.72
    candidate_minimum_relative_score: float = 0.45
    candidate_preserve_top_results: int = 4
    candidate_group_score_ratio: float = 0.10
    candidate_top_segments: int = 2


@dataclass(frozen=True)
class RankedTextSegment:
    index: int
    text: str
    relevance_score: float
    selection_score: float


class SemanticTextSelector:
    """
    Seleciona trechos originais semanticamente relacionados à consulta.

    A classe usa embeddings apenas para ordenar trechos. Nenhum texto novo é
    gerado. A seleção usa Maximal Marginal Relevance (MMR) para equilibrar
    relevância e redundância, e a saída final restaura a ordem original dos
    trechos para manter a leitura coerente.

    O modelo é carregado de forma preguiçosa na primeira seleção que realmente
    precisa de embeddings. Um modelo compatível pode ser injetado no construtor
    para testes ou execução controlada.
    """

    _sentence_boundary = re.compile(
        r"(?<=[.!?])\s+(?=(?:[A-ZÀ-Ý0-9]|[-•]))"
    )

    def __init__(
        self,
        config: SemanticContextConfig | None = None,
        *,
        model: Any | None = None,
    ) -> None:
        self.config = config or SemanticContextConfig()
        self._model = model

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "A seleção semântica requer a dependência "
                "'sentence-transformers'."
            ) from exc

        self._model = SentenceTransformer(
            self.config.model_name,
            device=self.config.device,
        )
        return self._model

    def _format_query(self, query: str) -> str:
        return (
            f"Instruct: {self.config.task_instruction}\n"
            f"Query: {query.strip()}"
        )

    @staticmethod
    def _normalize_embeddings(values: Any) -> np.ndarray:
        embeddings = np.asarray(values, dtype=np.float32)

        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)

        norms = np.linalg.norm(
            embeddings,
            axis=1,
            keepdims=True,
        )
        norms = np.where(norms == 0.0, 1.0, norms)
        return embeddings / norms

    @staticmethod
    def _split_long_segment(
        text: str,
        max_characters: int,
    ) -> list[str]:
        if len(text) <= max_characters:
            return [text]

        parts: list[str] = []
        remaining = text.strip()

        while len(remaining) > max_characters:
            boundary = remaining.rfind(
                " ",
                0,
                max_characters + 1,
            )

            if boundary <= 0:
                boundary = max_characters

            part = remaining[:boundary].strip()
            if part:
                parts.append(part)

            remaining = remaining[boundary:].strip()

        if remaining:
            parts.append(remaining)

        return parts

    def _split_text(
        self,
        text: str,
        *,
        max_segment_characters: int | None = None,
    ) -> list[str]:
        maximum = max(
            1,
            max_segment_characters
            or self.config.maximum_segment_characters,
        )

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = re.split(r"\n\s*\n|\n(?=[-•])", normalized)

        segments: list[str] = []
        seen: set[str] = set()

        for paragraph in paragraphs:
            compact = re.sub(r"\s+", " ", paragraph).strip()
            if not compact:
                continue

            sentences = self._sentence_boundary.split(compact)

            for sentence in sentences:
                for part in self._split_long_segment(
                    sentence.strip(),
                    maximum,
                ):
                    if (
                        len(part)
                        < self.config.minimum_segment_characters
                        and segments
                    ):
                        merged = f"{segments[-1]} {part}".strip()
                        if len(merged) <= maximum:
                            previous_key = segments[-1].casefold()
                            seen.discard(previous_key)
                            segments[-1] = merged
                            seen.add(merged.casefold())
                            continue

                    key = part.casefold()
                    if part and key not in seen:
                        seen.add(key)
                        segments.append(part)

        return segments

    def rank_segments(
        self,
        query: str,
        text: str,
        *,
        max_segment_characters: int | None = None,
    ) -> list[RankedTextSegment]:
        segments = self._split_text(
            text,
            max_segment_characters=max_segment_characters,
        )

        if not segments or not query.strip():
            return []

        model = self._load_model()
        formatted_query = self._format_query(query)

        query_embedding = model.encode(
            [formatted_query],
            batch_size=self.config.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        document_embeddings = model.encode(
            segments,
            batch_size=self.config.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )

        query_vector = self._normalize_embeddings(
            query_embedding
        )[0]
        document_vectors = self._normalize_embeddings(
            document_embeddings
        )

        relevance_scores = document_vectors @ query_vector
        lambda_value = min(
            max(self.config.mmr_lambda, 0.0),
            1.0,
        )

        available = set(range(len(segments)))
        selected_indexes: list[int] = []
        selection_scores: dict[int, float] = {}

        while available:
            best_index: int | None = None
            best_key: tuple[float, float, int] | None = None

            for index in available:
                redundancy = 0.0

                if selected_indexes:
                    redundancy = max(
                        float(
                            document_vectors[index]
                            @ document_vectors[selected_index]
                        )
                        for selected_index in selected_indexes
                    )

                mmr_score = (
                    lambda_value * float(relevance_scores[index])
                    - (1.0 - lambda_value) * redundancy
                )

                key = (
                    mmr_score,
                    float(relevance_scores[index]),
                    -index,
                )

                if best_key is None or key > best_key:
                    best_key = key
                    best_index = index

            if best_index is None or best_key is None:
                break

            available.remove(best_index)
            selected_indexes.append(best_index)
            selection_scores[best_index] = best_key[0]

        return [
            RankedTextSegment(
                index=index,
                text=segments[index],
                relevance_score=float(
                    relevance_scores[index]
                ),
                selection_score=selection_scores[index],
            )
            for index in selected_indexes
        ]

    def score_documents(
        self,
        query: str,
        documents: list[str | None],
        *,
        top_segments: int | None = None,
    ) -> list[float]:
        """
        Calcula a relevância semântica de vários documentos para a consulta.

        Cada documento é dividido em trechos, todos os trechos são codificados
        em uma única chamada em lote e a pontuação do documento corresponde à
        média dos trechos mais relevantes. Isso evita que uma descrição longa
        seja prejudicada por conteúdo técnico pouco relacionado e permite que
        um trecho realmente útil determine a relevância do candidato.

        O método não gera nem reescreve conteúdo. A lista devolvida mantém a
        mesma ordem da lista ``documents``.
        """
        if not documents:
            return []

        if not query.strip():
            return [0.0 for _ in documents]

        requested_top_segments = max(
            1,
            int(
                top_segments
                or self.config.candidate_top_segments
            ),
        )
        all_segments: list[str] = []
        document_segment_indexes: list[list[int]] = []

        for document in documents:
            normalized = re.sub(
                r"\s+",
                " ",
                document or "",
            ).strip()

            if not normalized:
                document_segment_indexes.append([])
                continue

            segments = self._split_text(normalized)

            if not segments:
                segments = [normalized]

            indexes: list[int] = []

            for segment in segments:
                indexes.append(len(all_segments))
                all_segments.append(segment)

            document_segment_indexes.append(indexes)

        if not all_segments:
            return [0.0 for _ in documents]

        model = self._load_model()
        formatted_query = self._format_query(query)
        query_embedding = model.encode(
            [formatted_query],
            batch_size=self.config.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        segment_embeddings = model.encode(
            all_segments,
            batch_size=self.config.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        query_vector = self._normalize_embeddings(
            query_embedding
        )[0]
        segment_vectors = self._normalize_embeddings(
            segment_embeddings
        )
        segment_scores = segment_vectors @ query_vector
        document_scores: list[float] = []

        for indexes in document_segment_indexes:
            if not indexes:
                document_scores.append(0.0)
                continue

            scores = sorted(
                (
                    float(segment_scores[index])
                    for index in indexes
                ),
                reverse=True,
            )
            selected_scores = scores[:requested_top_segments]
            document_scores.append(
                sum(selected_scores) / len(selected_scores)
            )

        return document_scores

    def select_relevant_text(
        self,
        query: str,
        text: str | None,
        *,
        max_characters: int,
    ) -> str:
        """
        Retorna somente trechos originais que caibam no orçamento informado.

        Textos curtos são devolvidos sem carregar o modelo. Para textos longos,
        os trechos são escolhidos pela ordem semântica e depois recolocados na
        ordem original. Caso nenhum trecho completo caiba, é devolvida uma
        subsequência do trecho semanticamente mais relevante.
        """
        if max_characters <= 0:
            return ""

        normalized = re.sub(r"\s+", " ", text or "").strip()
        if not normalized:
            return ""

        if len(normalized) <= max_characters:
            return normalized[:max_characters].rstrip()

        ranked = self.rank_segments(
            query,
            normalized,
            max_segment_characters=min(
                self.config.maximum_segment_characters,
                max_characters,
            ),
        )

        if not ranked:
            return normalized[:max_characters].rstrip()

        selected: list[RankedTextSegment] = []
        used = 0

        for segment in ranked:
            separator_size = 1 if selected else 0
            required = separator_size + len(segment.text)

            if used + required > max_characters:
                continue

            selected.append(segment)
            used += required

        if not selected:
            return ranked[0].text[:max_characters].rstrip()

        selected.sort(key=lambda segment: segment.index)
        result = " ".join(segment.text for segment in selected)
        return result[:max_characters].rstrip()
