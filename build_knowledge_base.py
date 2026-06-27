from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from gerar_skills import (
    build_antigravity_manifest,
    inferir_id_modulo,
    inferir_release,
    normalizar_url_modulo,
)
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
from oracle_knowledge.search.hybrid_search import HybridSearch, SearchConfig
from oracle_knowledge.search.federated_search import FederatedGraphSearch
from oracle_knowledge.validation import (
    ValidationReport,
    render_validation_report,
    validate_environment,
    validate_graph_directory,
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
        "--module",
        action="append",
        default=[],
        help="Restringe a busca a um module_id. Pode ser repetido.",
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
    search = FederatedGraphSearch(args.graph_dir)
    payload = search.build_prompt_context(
        args.query,
        limit=args.limit,
        max_characters=args.max_characters,
        module_ids=set(args.module) if args.module else None,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


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
    elif args.command == "doctor":
        run_doctor(args)
    elif args.command == "validate-module":
        run_validate_module(args)
    elif args.command == "validate-graph":
        run_validate_graph(args)
    elif args.command == "validate-result":
        run_validate_result(args)
    else:
        raise SystemExit(f"Comando desconhecido: {args.command}")


if __name__ == "__main__":
    main()
