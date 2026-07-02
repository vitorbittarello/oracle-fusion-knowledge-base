from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from gerar_skills import (
    build_antigravity_manifest,
    inferir_id_modulo,
    inferir_release,
    normalizar_url_modulo,
)
from oracle_knowledge.collectors.adf_metadata_collector import AdfMetadataCollector
from oracle_knowledge.collectors.functional_docs_collector import FunctionalDocsCollector
from oracle_knowledge.collectors.otbi_collector import OtbiCollector
from oracle_knowledge.collectors.rest_collector import RestCollector
from oracle_knowledge.common import read_json, slugify, utc_now_iso, write_json
from oracle_knowledge.linker.knowledge_linker import (
    build_graph,
    build_graph_bundle,
)
from oracle_knowledge.linker.graph_layers import (
    build_graph_bundle_from_graph,
    write_graph_bundle,
)
from oracle_knowledge.indexing import (
    build_index_bundle,
    build_search_index,
    resolve_index_source,
)
from oracle_knowledge.semantic_normalization import (
    DEFAULT_CHECKPOINT_PERCENT,
    DEFAULT_NORMALIZATION_BATCH_SIZE,
    normalize_semantic_corpus,
)
from oracle_knowledge.semantic_vectorization import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    DEFAULT_EMBEDDING_CHECKPOINT_PERCENT,
    DEFAULT_EXPECTED_DIMENSIONS,
    vectorize_semantic_corpus,
)
from oracle_knowledge.search.hybrid_search import HybridSearch, SearchConfig
from oracle_knowledge.search.federated_search import FederatedGraphSearch
from oracle_knowledge.search.semantic_context import (
    DEFAULT_LOCAL_EMBEDDING_MODEL,
    SUPPORTED_EMBEDDING_MODELS,
    SemanticTextSelector,
    resolve_embedding_model_profile,
    semantic_context_config_for_model,
)
from oracle_knowledge.validation import (
    ValidationReport,
    render_validation_report,
    validate_environment,
    validate_adf_environment,
    validate_graph_directory,
    validate_index_bundle,
    validate_index_database,
    validate_module_directory,
    validate_search_result,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config" / "knowledge_sources.json"
DEFAULT_ENTITIES = ROOT / "config" / "entity_aliases.json"
DEFAULT_RULES = ROOT / "rules" / "validated_rules.json"
DEFAULT_FUNCTIONAL = ROOT / "data" / "functional" / "functional_fragments.jsonl"
DEFAULT_OTBI = ROOT / "data" / "otbi" / "otbi_catalog.json"
DEFAULT_REST = ROOT / "data" / "rest" / "rest_catalog.json"
DEFAULT_GRAPH = ROOT / "data" / "graph" / "knowledge_graph.json"
DEFAULT_PHYSICAL = ROOT / "data" / "physical" / "antigravity_oracle_fusion_scraped_skills.json"
DEFAULT_MODULES_ROOT = ROOT / "data" / "modules"
DEFAULT_ADF_ENVIRONMENT = ROOT / "data" / "environment" / "adf"
DEFAULT_CACHE = ROOT / ".cache" / "oracle_docs"


def add_common_collection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--delay-seconds", type=float, default=0.15)
    parser.add_argument("--force-refresh", action="store_true")


def add_http_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--delay-seconds", type=float, default=0.15)
    parser.add_argument("--force-refresh", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Coleta, combina e consulta conhecimento de múltiplos módulos "
            "do Oracle Fusion Cloud Applications."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_module_parser = subparsers.add_parser(
        "collect-module",
        help="Coleta um módulo e grava tudo em um diretório próprio.",
    )
    add_http_arguments(collect_module_parser)
    collect_module_parser.add_argument("--module-url", required=True)
    collect_module_parser.add_argument("--module-id")
    collect_module_parser.add_argument("--module-name")
    collect_module_parser.add_argument("--release")
    collect_module_parser.add_argument("--toc-path", default="toc.htm")
    collect_module_parser.add_argument("--output-dir", required=True)
    collect_module_parser.add_argument(
        "--functional-url",
        action="append",
        default=[],
        help="URL do toc.htm de um guia funcional. Pode ser repetida.",
    )
    collect_module_parser.add_argument("--otbi-url")
    collect_module_parser.add_argument("--rest-url")
    collect_module_parser.add_argument("--max-functional-pages-per-guide", type=int)
    collect_module_parser.add_argument("--max-otbi-pages", type=int)
    collect_module_parser.add_argument("--max-rest-pages", type=int)
    collect_module_parser.add_argument("--no-resume", action="store_true")
    collect_module_parser.add_argument("--skip-physical", action="store_true")

    functional = subparsers.add_parser("collect-functional")
    add_common_collection_arguments(functional)
    functional.add_argument("--output", default=str(DEFAULT_FUNCTIONAL))
    functional.add_argument("--max-pages-per-guide", type=int)
    functional.add_argument("--no-resume", action="store_true")

    otbi = subparsers.add_parser("collect-otbi")
    add_common_collection_arguments(otbi)
    otbi.add_argument("--output", default=str(DEFAULT_OTBI))
    otbi.add_argument("--max-pages", type=int)

    rest = subparsers.add_parser("collect-rest")
    add_common_collection_arguments(rest)
    rest.add_argument("--output", default=str(DEFAULT_REST))
    rest.add_argument("--max-pages", type=int)

    adf = subparsers.add_parser(
        "collect-adf",
        help=(
            "Coleta automaticamente o catálogo ADF REST do ambiente e os "
            "describes dos recursos selecionados."
        ),
    )
    adf.add_argument("--base-url", required=True, help="URL base HTTPS do Fusion.")
    adf.add_argument(
        "--module-dir",
        help=(
            "Compatibilidade temporária: infere data/environment/adf a partir "
            "do diretório do módulo e migra a coleta antiga, quando necessário."
        ),
    )
    adf.add_argument(
        "--output-dir",
        default=str(DEFAULT_ADF_ENVIRONMENT),
        help=(
            "Diretório global do catálogo ADF do ambiente. "
            "Padrão: data/environment/adf."
        ),
    )
    adf.add_argument("--api-root", default="fscmRestApi")
    adf.add_argument("--api-version", default="latest")
    adf.add_argument("--accept-language", default="en-US")
    adf.add_argument("--username")
    adf.add_argument("--username-env", default="FUSION_USERNAME")
    adf.add_argument("--password-env", default="FUSION_PASSWORD")
    adf.add_argument("--bearer-token-env", default="FUSION_BEARER_TOKEN")
    adf.add_argument(
        "--anonymous",
        action="store_true",
        help="Executa sem Basic Auth nem Bearer Token.",
    )
    adf.add_argument("--resource", action="append", default=[])
    adf.add_argument("--include-regex", action="append", default=[])
    adf.add_argument("--exclude-regex", action="append", default=[])
    adf.add_argument(
        "--custom-only",
        action="store_true",
        help="Coleta somente recursos cujo nome termina em _c.",
    )
    adf.add_argument("--max-resources", type=int)
    adf.add_argument("--catalog-only", action="store_true")
    adf.add_argument("--delay-seconds", type=float, default=0.15)
    adf.add_argument("--force-refresh", action="store_true")
    adf.add_argument("--no-resume", action="store_true")
    adf.add_argument("--no-verify-ssl", action="store_true")

    link = subparsers.add_parser(
        "link",
        help="Combina módulos em um grafo único ou em grafos separados por camada.",
    )
    link.add_argument(
        "--module-dir",
        action="append",
        default=[],
        help="Diretório gerado por collect-module. Pode ser repetido.",
    )
    link.add_argument(
        "--modules-root",
        help="Diretório cujas subpastas contêm módulos coletados.",
    )
    link.add_argument("--physical-manifest", action="append", default=[])
    link.add_argument("--functional-fragments", action="append", default=[])
    link.add_argument("--otbi-catalog", action="append", default=[])
    link.add_argument("--rest-catalog", action="append", default=[])
    link.add_argument(
        "--adf-catalog",
        action="append",
        default=[],
        help=(
            "Catálogo ADF global do ambiente. Pode ser repetido. Quando omitido, "
            "o linker procura data/environment/adf/catalog.json ao lado de data/modules."
        ),
    )
    link.add_argument("--validated-rules", action="append", default=[])
    link.add_argument("--entity-aliases", action="append", default=[])
    link.add_argument(
        "--include-default-curation",
        action="store_true",
        help=(
            "Inclui config/entity_aliases.json e rules/validated_rules.json "
            "da raiz do projeto."
        ),
    )
    link.add_argument("--output", default=str(DEFAULT_GRAPH))
    link.add_argument(
        "--output-dir",
        help=(
            "Grava business, physical, otbi_analytics, otbi_security, "
            "rest e master_graph em um diretório. Quando informado, "
            "não grava o grafo combinado de --output."
        ),
    )

    split_graph_parser = subparsers.add_parser(
        "split-graph",
        help=(
            "Migra um grafo combinado existente para grafos separados "
            "por camada, sem refazer as coletas."
        ),
    )
    split_graph_parser.add_argument("--graph", required=True)
    split_graph_parser.add_argument("--output-dir", required=True)

    all_command = subparsers.add_parser("all")
    add_common_collection_arguments(all_command)
    all_command.add_argument("--physical-manifest", default=str(DEFAULT_PHYSICAL))
    all_command.add_argument("--functional-output", default=str(DEFAULT_FUNCTIONAL))
    all_command.add_argument("--otbi-output", default=str(DEFAULT_OTBI))
    all_command.add_argument("--rest-output", default=str(DEFAULT_REST))
    all_command.add_argument("--graph-output", default=str(DEFAULT_GRAPH))
    all_command.add_argument("--validated-rules", default=str(DEFAULT_RULES))
    all_command.add_argument("--entity-aliases", default=str(DEFAULT_ENTITIES))
    all_command.add_argument("--max-functional-pages-per-guide", type=int)
    all_command.add_argument("--max-otbi-pages", type=int)
    all_command.add_argument("--max-rest-pages", type=int)
    all_command.add_argument("--no-resume", action="store_true")

    search = subparsers.add_parser("search")
    search.add_argument("--graph", default=str(DEFAULT_GRAPH))
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--graph-hops", type=int, default=2)
    search.add_argument("--context", action="store_true")
    search.add_argument("--max-characters", type=int, default=14000)
    search.add_argument(
        "--module",
        action="append",
        default=[],
        help="Restringe a busca a um module_id. Pode ser repetido.",
    )

    federated = subparsers.add_parser(
        "search-federated",
        help=(
            "Resolve a consulta no master_graph e navega nos grafos "
            "physical, otbi_analytics e rest."
        ),
    )
    federated.add_argument("--graph-dir", required=True)
    federated.add_argument("--query", required=True)
    federated.add_argument("--limit", type=int, default=20)
    federated.add_argument("--max-characters", type=int, default=14000)
    federated.add_argument(
        "--index",
        help=(
            "Manifesto index_bundle.json, diretório search_index ou índice "
            "SQLite legado. O padrão prioriza o bundle separado por camada."
        ),
    )
    federated.add_argument(
        "--no-index",
        action="store_true",
        help="Força o backend JSON para diagnóstico ou comparação.",
    )
    federated.add_argument(
        "--require-index",
        action="store_true",
        help="Interrompe a busca se o índice SQLite não estiver disponível.",
    )
    federated.add_argument(
        "--semantic-model",
        choices=SUPPORTED_EMBEDDING_MODELS,
        default=DEFAULT_LOCAL_EMBEDDING_MODEL,
        help=(
            "Modelo usado na consulta semântica. Deve ser o mesmo usado na "
            "vetorização e na construção do índice."
        ),
    )
    federated.add_argument(
        "--semantic-device",
        help="Dispositivo do sentence-transformers, por exemplo cpu ou cuda.",
    )
    federated.add_argument(
        "--semantic-batch-size",
        type=int,
        default=32,
        help="Quantidade de textos codificados por lote na busca local.",
    )

    federated.add_argument(
        "--module",
        action="append",
        default=[],
        help="Restringe a busca a um module_id. Pode ser repetido.",
    )

    normalize_index = subparsers.add_parser(
        "normalize-index",
        help=(
            "Prepara e deduplica de forma idempotente, incremental e retomável "
            "as strings usadas pelos índices semânticos."
        ),
    )
    normalize_index.add_argument("--graph-dir", required=True)
    normalize_index.add_argument(
        "--output",
        help=(
            "Arquivo SQLite do corpus normalizado. O padrão é "
            "<graph-dir>/search_index/semantic_normalization.sqlite."
        ),
    )
    normalize_index.add_argument(
        "--layer",
        action="append",
        choices=[
            "business",
            "physical",
            "otbi_analytics",
            "otbi_security",
            "rest",
            "master",
        ],
        default=[],
        help=(
            "Normaliza somente a camada informada. Pode ser repetido. "
            "Sem a opção, processa todas as camadas, começando por ADF/REST."
        ),
    )
    normalize_index.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_NORMALIZATION_BATCH_SIZE,
        help="Quantidade de nós confirmados em cada transação SQLite.",
    )
    normalize_index.add_argument(
        "--checkpoint-percent",
        type=float,
        default=DEFAULT_CHECKPOINT_PERCENT,
        help=(
            "Percentual de avanço entre checkpoints formais e mensagens de "
            "progresso. Os lotes são persistidos mesmo entre checkpoints."
        ),
    )
    normalize_index.add_argument(
        "--no-resume",
        action="store_true",
        help=(
            "Inicia uma nova execução em vez de retomar um run interrompido. "
            "O cache de textos normalizados continua sendo reutilizado."
        ),
    )
    normalize_index.add_argument(
        "--force-renormalize",
        action="store_true",
        help=(
            "Reprocessa os nós mesmo quando o documento e as versões de "
            "normalização e segmentação não mudaram."
        ),
    )

    vectorize_index = subparsers.add_parser(
        "vectorize-index",
        help=(
            "Vetoriza os textos únicos do corpus normalizado de forma "
            "idempotente, incremental e retomável."
        ),
    )
    vectorize_index.add_argument("--graph-dir", required=True)
    vectorize_index.add_argument(
        "--normalization-db",
        help=(
            "Banco SQLite gerado por normalize-index. O padrão é "
            "<graph-dir>/search_index/semantic_normalization.sqlite."
        ),
    )
    vectorize_index.add_argument(
        "--output",
        help=(
            "Banco SQLite de embeddings. O padrão é "
            "<graph-dir>/search_index/semantic_embeddings.sqlite."
        ),
    )
    vectorize_index.add_argument(
        "--semantic-model",
        choices=SUPPORTED_EMBEDDING_MODELS,
        default=DEFAULT_LOCAL_EMBEDDING_MODEL,
        help=(
            "Modelo sentence-transformers usado na vetorização. O padrão "
            "local é multilingual-e5-base; o large-instruct é indicado para GPU."
        ),
    )
    vectorize_index.add_argument(
        "--semantic-device",
        help="Dispositivo do sentence-transformers, por exemplo cpu ou cuda.",
    )
    vectorize_index.add_argument(
        "--semantic-batch-size",
        type=int,
        default=DEFAULT_EMBEDDING_BATCH_SIZE,
        help="Quantidade de textos únicos codificados por lote persistente.",
    )
    vectorize_index.add_argument(
        "--checkpoint-percent",
        type=float,
        default=DEFAULT_EMBEDDING_CHECKPOINT_PERCENT,
        help=(
            "Percentual entre checkpoints e mensagens de progresso. Cada "
            "lote é confirmado no SQLite mesmo entre checkpoints."
        ),
    )
    vectorize_index.add_argument(
        "--expected-dimensions",
        type=int,
        default=DEFAULT_EXPECTED_DIMENSIONS,
        help=(
            "Sobrescreve a dimensão esperada. Por padrão, a dimensão é "
            "inferida do modelo selecionado: 768 no base e 1024 no large-instruct."
        ),
    )
    vectorize_index.add_argument(
        "--no-resume",
        action="store_true",
        help=(
            "Inicia um novo run em vez de retomar uma execução interrompida. "
            "Embeddings compatíveis já persistidos continuam reutilizáveis."
        ),
    )
    vectorize_index.add_argument(
        "--force-revectorize",
        action="store_true",
        help=(
            "Descarta e recalcula os embeddings do perfil atual para a versão "
            "normalizada selecionada."
        ),
    )

    build_index = subparsers.add_parser(
        "build-index",
        help=(
            "Constrói índices SQLite + FTS5 independentes por camada, "
            "com rebuild seletivo e reutilização de embeddings."
        ),
    )
    build_index.add_argument("--graph-dir", required=True)
    build_index.add_argument(
        "--output",
        help=(
            "Arquivo SQLite monolítico legado. Quando informado, desativa "
            "a geração do bundle separado por camada."
        ),
    )
    build_index.add_argument(
        "--output-dir",
        help=(
            "Diretório do bundle de índices. O padrão é "
            "<graph-dir>/search_index."
        ),
    )
    build_index.add_argument(
        "--layer",
        action="append",
        choices=[
            "business",
            "physical",
            "otbi_analytics",
            "otbi_security",
            "rest",
            "master",
        ],
        default=[],
        help=(
            "Reconstrói somente a camada informada. Pode ser repetido. "
            "Sem a opção, avalia todas as camadas e ignora as inalteradas."
        ),
    )
    build_index.add_argument(
        "--force",
        action="store_true",
        help="Reconstrói as camadas solicitadas mesmo quando o conteúdo não mudou.",
    )
    build_index.add_argument(
        "--no-reuse-embeddings",
        action="store_true",
        help="Não reutiliza embeddings de índices anteriores durante o rebuild.",
    )
    build_index.add_argument("--batch-size", type=int, default=1000)
    build_index.add_argument(
        "--skip-semantic-index",
        action="store_true",
        help=(
            "Não gera embeddings persistentes das raízes. "
            "Útil apenas para diagnóstico FTS5."
        ),
    )
    build_index.add_argument(
        "--semantic-model",
        choices=SUPPORTED_EMBEDDING_MODELS,
        default=DEFAULT_LOCAL_EMBEDDING_MODEL,
        help=(
            "Modelo sentence-transformers usado no índice semântico. Deve ser "
            "o mesmo usado por vectorize-index e search-federated."
        ),
    )
    build_index.add_argument(
        "--semantic-device",
        help="Dispositivo do sentence-transformers, por exemplo cpu ou cuda.",
    )
    build_index.add_argument(
        "--semantic-batch-size",
        type=int,
        default=32,
        help="Quantidade de raízes codificadas por lote.",
    )

    validate_index = subparsers.add_parser(
        "validate-index",
        help="Valida esquema, FTS5, contagens e atualização do índice SQLite.",
    )
    validate_index.add_argument("--graph-dir", required=True)
    validate_index.add_argument(
        "--index",
        help=(
            "Manifesto index_bundle.json ou índice SQLite legado. "
            "O padrão prioriza o bundle separado por camada."
        ),
    )
    validate_index.add_argument(
        "--full-hash",
        action="store_true",
        help="Recalcula SHA-256 dos grafos para detectar alterações de conteúdo.",
    )
    validate_index.add_argument(
        "--json-output",
        nargs="?",
        const="-",
        help=(
            "Produz o relatório em JSON. Sem caminho, escreve no stdout; "
            "com caminho, grava o arquivo informado."
        ),
    )

    doctor = subparsers.add_parser(
        "doctor",
        help="Valida ambiente, dependências, UTF-8, SQLite FTS5 e permissões.",
    )
    doctor.add_argument("--work-dir", default=".")
    doctor.add_argument(
        "--json-output",
        nargs="?",
        const="-",
        help=(
            "Produz o relatório em JSON. Sem caminho, escreve no stdout; "
            "com caminho, grava o arquivo informado."
        ),
    )

    validate_module = subparsers.add_parser(
        "validate-module",
        help="Valida arquivos, JSONs e fontes esperadas de um módulo coletado.",
    )
    validate_module.add_argument("--module-dir", required=True)
    validate_module.add_argument(
        "--json-output",
        nargs="?",
        const="-",
        help=(
            "Produz o relatório em JSON. Sem caminho, escreve no stdout; "
            "com caminho, grava o arquivo informado."
        ),
    )

    validate_adf = subparsers.add_parser(
        "validate-adf",
        help="Valida o catálogo ADF global e suas projeções por módulo.",
    )
    validate_adf.add_argument(
        "--adf-dir",
        default=str(DEFAULT_ADF_ENVIRONMENT),
    )
    validate_adf.add_argument(
        "--json-output",
        nargs="?",
        const="-",
        help=(
            "Produz o relatório em JSON. Sem caminho, escreve no stdout; "
            "com caminho, grava o arquivo informado."
        ),
    )

    validate_graph = subparsers.add_parser(
        "validate-graph",
        help="Valida o bundle, os grafos, as estatísticas e as arestas.",
    )
    validate_graph.add_argument("--graph-dir", required=True)
    validate_graph.add_argument(
        "--json-output",
        nargs="?",
        const="-",
        help=(
            "Produz o relatório em JSON. Sem caminho, escreve no stdout; "
            "com caminho, grava o arquivo informado."
        ),
    )

    validate_result = subparsers.add_parser(
        "validate-result",
        help="Valida estrutura, orçamento, fontes e roteamento de um resultado.",
    )
    validate_result.add_argument("--result", required=True)
    validate_result.add_argument("--max-characters", type=int, default=14000)
    validate_result.add_argument(
        "--json-output",
        nargs="?",
        const="-",
        help=(
            "Produz o relatório em JSON. Sem caminho, escreve no stdout; "
            "com caminho, grava o arquivo informado."
        ),
    )

    return parser


def _toc_url(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    filename = Path(parsed.path).name.lower()
    if filename in {"toc.htm", "toc.html"}:
        return value.split("#", 1)[0]
    base = normalizar_url_modulo(value)
    return urljoin(base, "toc.htm")


def _source_code(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    filename = Path(path).name.lower()
    if filename in {"toc.htm", "toc.html", "index.htm", "index.html"}:
        filename = Path(path).parent.name.lower()
    return re.sub(r"[^a-z0-9]+", "_", filename).strip("_") or "source"


def _module_paths(output_dir: str | Path) -> dict[str, Path]:
    root = Path(output_dir).resolve()
    return {
        "root": root,
        "metadata": root / "module.json",
        "physical": root / "physical" / "manifest.json",
        "functional": root / "functional" / "fragments.jsonl",
        "otbi": root / "otbi" / "catalog.json",
        "rest": root / "rest" / "catalog.json",
        "rules": root / "rules" / "validated_rules.json",
        "entities": root / "config" / "entity_aliases.json",
    }


def collect_module(args: argparse.Namespace) -> None:
    base_url = normalizar_url_modulo(args.module_url)
    module_id = args.module_id or inferir_id_modulo(base_url)
    module_name = args.module_name or module_id.replace("_", " ").title()
    release = inferir_release(base_url, args.release)
    paths = _module_paths(args.output_dir)
    paths["root"].mkdir(parents=True, exist_ok=True)

    outputs: dict[str, str] = {}

    if not args.skip_physical:
        _, physical_path = build_antigravity_manifest(
            base_url,
            module_id=module_id,
            module_name=module_name,
            release=release,
            toc_path=args.toc_path,
            output=str(paths["physical"]),
        )
        outputs["physical_manifest"] = physical_path

    if args.functional_url:
        guides = []
        for index, url in enumerate(args.functional_url, start=1):
            toc_url = _toc_url(url)
            code = _source_code(toc_url)
            guides.append(
                {
                    "guide_id": f"{module_id}_{code}_{index}",
                    "module_id": module_id,
                    "module_name": module_name,
                    "title": f"{module_name} - {code.upper()}",
                    "toc_url": toc_url,
                    "release": release,
                    "business_domains": [module_name, module_id],
                    "exclude_patterns": ["copyright", "about-the-guide"],
                }
            )
        collector = FunctionalDocsCollector(
            cache_dir=args.cache_dir,
            delay_seconds=args.delay_seconds,
            force_refresh=args.force_refresh,
        )
        rows = collector.collect_all(
            guides,
            output_path=str(paths["functional"]),
            max_pages_per_guide=args.max_functional_pages_per_guide,
            resume=not args.no_resume,
        )
        outputs["functional_fragments"] = str(paths["functional"])
        print(f"[FUNCTIONAL] {len(rows)} fragmentos")

    if args.otbi_url:
        source = {
            "source_id": f"{module_id}_otbi",
            "module_id": module_id,
            "module_name": module_name,
            "title": f"{module_name} OTBI",
            "toc_url": _toc_url(args.otbi_url),
            "release": release,
        }
        collector = OtbiCollector(
            cache_dir=args.cache_dir,
            delay_seconds=args.delay_seconds,
            force_refresh=args.force_refresh,
        )
        payload = collector.collect(source, max_pages=args.max_otbi_pages)
        write_json(paths["otbi"], payload)
        outputs["otbi_catalog"] = str(paths["otbi"])
        print(f"[OTBI] {payload['stats']}")

    if args.rest_url:
        source = {
            "source_id": f"{module_id}_rest",
            "module_id": module_id,
            "module_name": module_name,
            "title": f"{module_name} REST API",
            "toc_url": _toc_url(args.rest_url),
            "release": release,
        }
        collector = RestCollector(
            cache_dir=args.cache_dir,
            delay_seconds=args.delay_seconds,
            force_refresh=args.force_refresh,
        )
        payload = collector.collect(source, max_pages=args.max_rest_pages)
        write_json(paths["rest"], payload)
        outputs["rest_catalog"] = str(paths["rest"])
        print(f"[REST] {payload['stats']}")

    if not paths["rules"].exists():
        write_json(paths["rules"], {"version": "1.0.0", "rules": []})
    if not paths["entities"].exists():
        write_json(paths["entities"], {"version": "1.0.0", "entities": []})

    metadata = {
        "version": "1.0.0",
        "generated_at": utc_now_iso(),
        "module_id": module_id,
        "module_name": module_name,
        "release": release,
        "module_url": base_url,
        "source_urls": {
            "physical": base_url,
            "functional": [_toc_url(url) for url in args.functional_url],
            "otbi": _toc_url(args.otbi_url) if args.otbi_url else None,
            "rest": _toc_url(args.rest_url) if args.rest_url else None,
        },
        "output_dir": str(paths["root"]),
        "outputs": {
            **outputs,
            "validated_rules": str(paths["rules"]),
            "entity_aliases": str(paths["entities"]),
        },
    }
    write_json(paths["metadata"], metadata)
    print(f"[MODULE] Metadados gravados em {paths['metadata']}")


def collect_functional(args: argparse.Namespace) -> None:
    config = read_json(args.config, {})
    collector = FunctionalDocsCollector(
        cache_dir=args.cache_dir,
        delay_seconds=args.delay_seconds,
        force_refresh=args.force_refresh,
    )
    rows = collector.collect_all(
        config.get("functional_guides", []),
        output_path=args.output,
        max_pages_per_guide=args.max_pages_per_guide,
        resume=not args.no_resume,
    )
    print(f"[FUNCTIONAL] {len(rows)} fragmentos gravados em {args.output}")


def collect_otbi(args: argparse.Namespace) -> None:
    config = read_json(args.config, {})
    collector = OtbiCollector(
        cache_dir=args.cache_dir,
        delay_seconds=args.delay_seconds,
        force_refresh=args.force_refresh,
    )
    payload = collector.collect(config["otbi"], max_pages=args.max_pages)
    write_json(args.output, payload)
    print(f"[OTBI] {payload['stats']} gravado em {args.output}")


def collect_rest(args: argparse.Namespace) -> None:
    config = read_json(args.config, {})
    collector = RestCollector(
        cache_dir=args.cache_dir,
        delay_seconds=args.delay_seconds,
        force_refresh=args.force_refresh,
    )
    payload = collector.collect(config["rest"], max_pages=args.max_pages)
    write_json(args.output, payload)
    print(f"[REST] {payload['stats']} gravado em {args.output}")


def collect_adf(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir or DEFAULT_ADF_ENVIRONMENT).resolve()

    if args.module_dir:
        module_dir = Path(args.module_dir).resolve()
        inferred_output = module_dir.parent.parent / "environment" / "adf"
        legacy_output = module_dir / "environment" / "adf"
        output_dir = inferred_output.resolve()

        if legacy_output.exists() and not output_dir.exists():
            output_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_output), str(output_dir))
            print(f"[ADF] Layout antigo migrado para {output_dir}")

    username = args.username or os.getenv(args.username_env)
    password = os.getenv(args.password_env)
    bearer_token = os.getenv(args.bearer_token_env)

    if args.anonymous:
        username = None
        password = None
        bearer_token = None
    elif bearer_token:
        username = None
        password = None
    elif not username:
        raise SystemExit(
            "Usuário ausente. Informe --username, defina FUSION_USERNAME, "
            "defina FUSION_BEARER_TOKEN ou use --anonymous."
        )
    elif not password:
        raise SystemExit(
            f"Senha ausente. Defina a variável de ambiente {args.password_env}."
        )

    collector = AdfMetadataCollector(
        base_url=args.base_url,
        username=username,
        password=password,
        bearer_token=bearer_token,
        api_root=args.api_root,
        api_version=args.api_version,
        accept_language=args.accept_language,
        delay_seconds=args.delay_seconds,
        verify_ssl=not args.no_verify_ssl,
    )
    payload = collector.collect(
        output_dir,
        resources=args.resource,
        include_patterns=args.include_regex,
        exclude_patterns=args.exclude_regex,
        custom_only=args.custom_only,
        max_resources=args.max_resources,
        catalog_only=args.catalog_only,
        resume=not args.no_resume,
        force_refresh=args.force_refresh,
    )

    print(f"[ADF] {payload['stats']} gravado em {output_dir}")


def _existing_paths(values: list[str | Path]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        path = Path(value)
        if not path.exists():
            continue
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def _discover_module_dirs(module_dirs: list[str], modules_root: str | None) -> list[Path]:
    candidates = [Path(value) for value in module_dirs]
    if modules_root:
        root = Path(modules_root)
        if root.exists():
            candidates.extend(path for path in root.iterdir() if path.is_dir())
    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        module_paths = _module_paths(path)
        if module_paths["metadata"].exists() or module_paths["physical"].exists():
            seen.add(resolved)
            result.append(path.resolve())
    return sorted(result)


def _discover_sources(args: argparse.Namespace) -> dict[str, list[str]]:
    sources: dict[str, list[str | Path]] = {
        "physical": list(args.physical_manifest),
        "functional": list(args.functional_fragments),
        "otbi": list(args.otbi_catalog),
        "rest": list(args.rest_catalog),
        "adf": list(args.adf_catalog),
        "rules": list(args.validated_rules),
        "entities": list(args.entity_aliases),
    }

    module_dirs = _discover_module_dirs(args.module_dir, args.modules_root)
    for module_dir in module_dirs:
        paths = _module_paths(module_dir)
        sources["physical"].append(paths["physical"])
        sources["functional"].append(paths["functional"])
        sources["otbi"].append(paths["otbi"])
        sources["rest"].append(paths["rest"])
        sources["rules"].append(paths["rules"])
        sources["entities"].append(paths["entities"])

    if args.modules_root:
        modules_root = Path(args.modules_root).resolve()
        sources["adf"].append(
            modules_root.parent / "environment" / "adf" / "catalog.json"
        )
    else:
        for module_dir in module_dirs:
            sources["adf"].append(
                module_dir.parent.parent / "environment" / "adf" / "catalog.json"
            )

    if args.include_default_curation:
        if DEFAULT_RULES.exists():
            sources["rules"].append(DEFAULT_RULES)
        if DEFAULT_ENTITIES.exists():
            sources["entities"].append(DEFAULT_ENTITIES)

    return {
        key: _existing_paths(values)
        for key, values in sources.items()
    }


def link_graph(args: argparse.Namespace) -> None:
    sources = _discover_sources(args)
    if not any(sources.values()):
        raise SystemExit(
            "Nenhuma fonte encontrada. Informe --module-dir, --modules-root "
            "ou caminhos explícitos."
        )

    build_kwargs = {
        "physical_manifest": sources["physical"],
        "functional_fragments": sources["functional"],
        "otbi_catalog": sources["otbi"],
        "rest_catalog": sources["rest"],
        "adf_catalog": sources["adf"],
        "validated_rules": sources["rules"],
        "entity_aliases": sources["entities"],
    }

    if args.output_dir:
        bundle = build_graph_bundle(**build_kwargs)
        outputs = write_graph_bundle(args.output_dir, bundle)
        stats = {
            layer: graph["stats"]
            for layer, graph in bundle.items()
        }
        print(
            f"[GRAPH BUNDLE] {stats} gravado em {args.output_dir}. "
            f"Arquivos: {', '.join(sorted(outputs))}"
        )
        return

    graph = build_graph(**build_kwargs)
    write_json(args.output, graph)
    print(f"[GRAPH] {graph['stats']} gravado em {args.output}")


def split_existing_graph(args: argparse.Namespace) -> None:
    graph = read_json(args.graph, {})
    if not graph.get("nodes"):
        raise SystemExit(f"Grafo inválido ou vazio: {args.graph}")

    bundle = build_graph_bundle_from_graph(graph)
    outputs = write_graph_bundle(args.output_dir, bundle)
    stats = {
        layer: layer_graph["stats"]
        for layer, layer_graph in bundle.items()
    }
    print(
        f"[GRAPH BUNDLE] {stats} gravado em {args.output_dir}. "
        f"Arquivos: {', '.join(sorted(outputs))}"
    )


def run_all(args: argparse.Namespace) -> None:
    config = read_json(args.config, {})

    functional_collector = FunctionalDocsCollector(
        cache_dir=args.cache_dir,
        delay_seconds=args.delay_seconds,
        force_refresh=args.force_refresh,
    )
    functional_rows = functional_collector.collect_all(
        config.get("functional_guides", []),
        output_path=args.functional_output,
        max_pages_per_guide=args.max_functional_pages_per_guide,
        resume=not args.no_resume,
    )
    print(f"[FUNCTIONAL] {len(functional_rows)} fragmentos")

    otbi_collector = OtbiCollector(
        cache_dir=args.cache_dir,
        delay_seconds=args.delay_seconds,
        force_refresh=args.force_refresh,
    )
    otbi_payload = otbi_collector.collect(
        config["otbi"],
        max_pages=args.max_otbi_pages,
    )
    write_json(args.otbi_output, otbi_payload)
    print(f"[OTBI] {otbi_payload['stats']}")

    rest_collector = RestCollector(
        cache_dir=args.cache_dir,
        delay_seconds=args.delay_seconds,
        force_refresh=args.force_refresh,
    )
    rest_payload = rest_collector.collect(
        config["rest"],
        max_pages=args.max_rest_pages,
    )
    write_json(args.rest_output, rest_payload)
    print(f"[REST] {rest_payload['stats']}")

    graph = build_graph(
        physical_manifest=args.physical_manifest,
        functional_fragments=args.functional_output,
        otbi_catalog=args.otbi_output,
        rest_catalog=args.rest_output,
        validated_rules=args.validated_rules,
        entity_aliases=args.entity_aliases,
    )
    write_json(args.graph_output, graph)
    print(f"[GRAPH] {graph['stats']} gravado em {args.graph_output}")


def search_graph(args: argparse.Namespace) -> None:
    search = HybridSearch.from_file(
        args.graph,
        config=SearchConfig(graph_hops=args.graph_hops),
    )
    if args.context:
        payload = search.build_prompt_context(
            args.query,
            limit=args.limit,
            max_characters=args.max_characters,
            module_ids=set(args.module) if args.module else None,
        )
    else:
        payload = search.search(
            args.query,
            limit=args.limit,
            module_ids=set(args.module) if args.module else None,
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def search_federated_graphs(args: argparse.Namespace) -> None:
    if args.no_index and args.require_index:
        raise SystemExit("--no-index e --require-index não podem ser usados juntos.")

    resolved_index = resolve_index_source(args.graph_dir, args.index)
    if not args.no_index and not resolved_index.is_file() and not args.require_index:
        print(
            "[AVISO] Bundle de índices ou índice SQLite não encontrado; "
            "usando backend JSON. Execute build-index.",
            file=sys.stderr,
        )

    semantic_selector = SemanticTextSelector(
        semantic_context_config_for_model(
            args.semantic_model,
            device=args.semantic_device,
            batch_size=args.semantic_batch_size,
        )
    )

    with FederatedGraphSearch(
        args.graph_dir,
        index_path=args.index,
        use_index=not args.no_index,
        require_index=args.require_index,
        semantic_text_selector=semantic_selector,
        progress=lambda message: print(
            message,
            file=sys.stderr,
            flush=True,
        ),
    ) as search:
        payload = search.build_prompt_context(
            args.query,
            limit=args.limit,
            max_characters=args.max_characters,
            module_ids=set(args.module) if args.module else None,
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def run_normalize_index(args: argparse.Namespace) -> None:
    progress = lambda message: print(message, file=sys.stderr, flush=True)
    result = normalize_semantic_corpus(
        args.graph_dir,
        output_path=args.output,
        layers=args.layer or None,
        batch_size=args.batch_size,
        checkpoint_percent=args.checkpoint_percent,
        resume=not args.no_resume,
        force_renormalize=args.force_renormalize,
        progress=progress,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))


def run_vectorize_index(args: argparse.Namespace) -> None:
    progress = lambda message: print(message, file=sys.stderr, flush=True)
    profile = resolve_embedding_model_profile(args.semantic_model)
    expected_dimensions = (
        args.expected_dimensions
        if args.expected_dimensions is not None
        else profile.dimensions
    )
    result = vectorize_semantic_corpus(
        args.graph_dir,
        normalization_database_path=args.normalization_db,
        output_path=args.output,
        model_name=args.semantic_model,
        device=args.semantic_device,
        batch_size=args.semantic_batch_size,
        checkpoint_percent=args.checkpoint_percent,
        expected_dimensions=expected_dimensions,
        resume=not args.no_resume,
        force_revectorize=args.force_revectorize,
        progress=progress,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))


def run_build_index(args: argparse.Namespace) -> None:
    if args.output and args.output_dir:
        raise SystemExit("--output e --output-dir não podem ser usados juntos.")
    if args.output and args.layer:
        raise SystemExit("--layer não pode ser usado com o índice monolítico --output.")

    semantic_selector = None
    if not args.skip_semantic_index:
        semantic_selector = SemanticTextSelector(
            semantic_context_config_for_model(
                args.semantic_model,
                device=args.semantic_device,
                batch_size=args.semantic_batch_size,
            )
        )

    progress = lambda message: print(message, file=sys.stderr, flush=True)
    if args.output:
        result = build_search_index(
            args.graph_dir,
            Path(args.output).resolve(),
            batch_size=args.batch_size,
            include_semantic_embeddings=not args.skip_semantic_index,
            semantic_text_selector=semantic_selector,
            semantic_batch_size=args.semantic_batch_size,
            progress=progress,
        )
    else:
        result = build_index_bundle(
            args.graph_dir,
            layers=args.layer or None,
            output_dir=args.output_dir,
            batch_size=args.batch_size,
            include_semantic_embeddings=not args.skip_semantic_index,
            semantic_text_selector=semantic_selector,
            semantic_batch_size=args.semantic_batch_size,
            progress=progress,
            force=args.force,
            reuse_embeddings=not args.no_reuse_embeddings,
        )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))


def _emit_validation_report(
    report: ValidationReport,
    json_output: str | None,
) -> None:
    payload = report.to_dict()

    if json_output == "-":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(render_validation_report(report))
        if json_output:
            write_json(json_output, payload)
            print(f"[JSON] Relatório gravado em {Path(json_output).resolve()}")

    if report.error_count:
        raise SystemExit(1)


def run_doctor(args: argparse.Namespace) -> None:
    _emit_validation_report(
        validate_environment(args.work_dir),
        args.json_output,
    )


def run_validate_module(args: argparse.Namespace) -> None:
    _emit_validation_report(
        validate_module_directory(args.module_dir),
        args.json_output,
    )


def run_validate_adf(args: argparse.Namespace) -> None:
    _emit_validation_report(
        validate_adf_environment(args.adf_dir),
        args.json_output,
    )


def run_validate_index(args: argparse.Namespace) -> None:
    index_path = resolve_index_source(args.graph_dir, args.index)
    if index_path.suffix.casefold() == ".json":
        report = validate_index_bundle(
            index_path,
            graph_dir=args.graph_dir,
            full_hash=args.full_hash,
        )
    else:
        report = validate_index_database(
            index_path,
            graph_dir=args.graph_dir,
            full_hash=args.full_hash,
        )
    _emit_validation_report(report, args.json_output)


def run_validate_graph(args: argparse.Namespace) -> None:
    _emit_validation_report(
        validate_graph_directory(args.graph_dir),
        args.json_output,
    )


def run_validate_result(args: argparse.Namespace) -> None:
    _emit_validation_report(
        validate_search_result(
            args.result,
            max_characters=args.max_characters,
        ),
        args.json_output,
    )


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "collect-module":
        collect_module(args)
    elif args.command == "collect-functional":
        collect_functional(args)
    elif args.command == "collect-otbi":
        collect_otbi(args)
    elif args.command == "collect-rest":
        collect_rest(args)
    elif args.command == "collect-adf":
        collect_adf(args)
    elif args.command == "link":
        link_graph(args)
    elif args.command == "split-graph":
        split_existing_graph(args)
    elif args.command == "all":
        run_all(args)
    elif args.command == "search":
        search_graph(args)
    elif args.command == "search-federated":
        search_federated_graphs(args)
    elif args.command == "normalize-index":
        run_normalize_index(args)
    elif args.command == "vectorize-index":
        run_vectorize_index(args)
    elif args.command == "build-index":
        run_build_index(args)
    elif args.command == "validate-index":
        run_validate_index(args)
    elif args.command == "doctor":
        run_doctor(args)
    elif args.command == "validate-module":
        run_validate_module(args)
    elif args.command == "validate-adf":
        run_validate_adf(args)
    elif args.command == "validate-graph":
        run_validate_graph(args)
    elif args.command == "validate-result":
        run_validate_result(args)
    else:
        raise SystemExit(f"Comando desconhecido: {args.command}")


if __name__ == "__main__":
    main()
