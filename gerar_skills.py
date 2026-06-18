import argparse
import json
import os
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


NOME_OBJETO_RE = re.compile(r"^[A-Z][A-Z0-9_$#_]{2,}$")
CABECALHOS_HTML = {"h1", "h2", "h3", "h4", "h5", "h6"}

AUDIT_COLUMNS = {
    "CREATION_DATE",
    "CREATED_BY",
    "LAST_UPDATE_DATE",
    "LAST_UPDATED_BY",
    "LAST_UPDATE_LOGIN"
}

MARCADORES_NOT_NULL = {
    "YES",
    "Y",
    "TRUE",
    "NOT NULL",
    "MANDATORY"
}


def criar_sessao():
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",)
    )

    session.mount("https://", HTTPAdapter(max_retries=retry))

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9"
    })

    return session


def obter_soup(session, url):
    response = session.get(
        url,
        timeout=(10, 60)
    )

    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "")

    if "html" not in content_type.lower():
        raise ValueError(
            f"Conteúdo retornado não é HTML: "
            f"url={url}, content_type={content_type}"
        )

    return BeautifulSoup(response.text, "html.parser")


def localizar_secao(soup, titulo):
    titulo_normalizado = titulo.casefold()

    for heading in soup.find_all(CABECALHOS_HTML):
        texto = heading.get_text(" ", strip=True).casefold()

        if texto == titulo_normalizado:
            return heading

    return None


def localizar_tabela_da_secao(soup, titulo):
    heading = localizar_secao(soup, titulo)

    if not heading:
        return None

    for elemento in heading.find_all_next():
        if elemento is heading:
            continue

        if elemento.name in CABECALHOS_HTML:
            return None

        if elemento.name == "table":
            return elemento

    return None


def normalizar_cabecalho(texto):
    return re.sub(
        r"[^a-z0-9]+",
        "_",
        texto.strip().lower()
    ).strip("_")


def extrair_descricao(soup):
    h1 = soup.find("h1")

    if not h1:
        return None

    paragrafos = []

    for elemento in h1.find_all_next():
        if elemento.name in CABECALHOS_HTML:
            break

        if elemento.name == "p":
            descricao = elemento.get_text(" ", strip=True)

            if descricao:
                paragrafos.append(descricao)

    return " ".join(paragrafos) or None


def extrair_tipo_objeto(soup):
    texto = soup.get_text(" ", strip=True)

    match = re.search(
        r"Object\s+type\s*:\s*(TABLE|VIEW)",
        texto,
        flags=re.IGNORECASE
    )

    if not match:
        return None

    return match.group(1).upper()


def extrair_chave_primaria(soup):
    tabela = localizar_tabela_da_secao(soup, "Primary Key")

    if not tabela:
        return []

    colunas_pk = []

    for linha in tabela.find_all("tr"):
        celulas = linha.find_all("td")

        if len(celulas) < 2:
            continue

        nome_constraint = celulas[0].get_text(
            " ",
            strip=True
        ).strip().upper()
        texto_colunas = celulas[-1].get_text(
            " ",
            strip=True
        )

        if (
            nome_constraint in {"NAME", "CONSTRAINT", "PRIMARY KEY"}
            or texto_colunas.strip().upper() == "COLUMNS"
        ):
            continue

        for coluna in re.split(r"[,;\s]+", texto_colunas):
            coluna = coluna.strip().upper()

            if (
                coluna
                and NOME_OBJETO_RE.fullmatch(coluna)
                and coluna not in colunas_pk
            ):
                colunas_pk.append(coluna)

    return colunas_pk


def extrair_metadados_colunas(soup):
    tabela = localizar_tabela_da_secao(soup, "Columns")

    if not tabela:
        return []

    linhas = tabela.find_all("tr")

    if not linhas:
        return []

    cabecalhos = []

    for linha in linhas:
        celulas_cabecalho = linha.find_all("th")

        if not celulas_cabecalho:
            continue

        cabecalhos = [
            normalizar_cabecalho(
                celula.get_text(" ", strip=True)
            )
            for celula in celulas_cabecalho
        ]
        break

    linha_cabecalho_sem_th = None

    if not cabecalhos:
        for linha in linhas:
            celulas = linha.find_all("td")
            valores = [
                normalizar_cabecalho(
                    celula.get_text(" ", strip=True)
                )
                for celula in celulas
            ]

            if valores and valores[0] == "name":
                cabecalhos = valores
                linha_cabecalho_sem_th = linha
                break

    if not cabecalhos:
        cabecalhos = [
            "name",
            "datatype",
            "length",
            "precision",
            "not_null",
            "comments"
        ]

    colunas = []
    colunas_processadas = set()

    for linha in linhas:
        if linha is linha_cabecalho_sem_th:
            continue

        celulas = linha.find_all("td")

        if not celulas:
            continue

        valores = [
            celula.get_text(" ", strip=True)
            for celula in celulas
        ]

        registro = {
            cabecalhos[indice]: valor
            for indice, valor in enumerate(valores)
            if indice < len(cabecalhos)
        }

        nome = registro.get("name", "").strip().upper()

        if (
            not NOME_OBJETO_RE.fullmatch(nome)
            or nome in colunas_processadas
        ):
            continue

        colunas_processadas.add(nome)

        not_null = registro.get("not_null", "").strip().upper()

        metadado = {
            "name": nome,
            "datatype": registro.get("datatype") or None,
            "length": registro.get("length") or None,
            "precision": registro.get("precision") or None,
            "nullable": not_null not in MARCADORES_NOT_NULL,
            "description": registro.get("comments") or None
        }

        flexfield_mapping = registro.get("flexfield_mapping")

        if flexfield_mapping:
            metadado["flexfield_mapping"] = flexfield_mapping

        campos_conhecidos = {
            "name",
            "datatype",
            "length",
            "precision",
            "not_null",
            "comments",
            "flexfield_mapping"
        }

        atributos_adicionais = {
            chave: valor
            for chave, valor in registro.items()
            if chave not in campos_conhecidos and valor
        }

        if atributos_adicionais:
            metadado["documented_attributes"] = atributos_adicionais

        colunas.append(metadado)

    return colunas


def extrair_colunas(soup):
    """Mantém compatibilidade com o formato anterior."""
    return [
        coluna["name"]
        for coluna in extrair_metadados_colunas(soup)
    ]


def extrair_relacionamentos(soup, nome_tabela_atual):
    tabela = localizar_tabela_da_secao(soup, "Foreign Keys")

    if not tabela:
        return {
            "outgoing": [],
            "incoming": []
        }

    nome_tabela_atual = nome_tabela_atual.upper()

    relacionamentos_saida = []
    relacionamentos_entrada = []
    relacionamentos_processados = set()

    for linha in tabela.find_all("tr"):
        celulas = linha.find_all(["td", "th"])

        if len(celulas) < 3:
            continue

        tabela_origem = celulas[0].get_text(
            " ",
            strip=True
        ).upper()

        tabela_referenciada = celulas[1].get_text(
            " ",
            strip=True
        ).upper()

        coluna_fk_texto = celulas[2].get_text(
            " ",
            strip=True
        ).upper()

        if tabela_origem in {"TABLE", ""}:
            continue

        if tabela_referenciada in {"FOREIGN TABLE", ""}:
            continue

        colunas_fk = [
            coluna.strip()
            for coluna in re.split(r"\s*,\s*", coluna_fk_texto)
            if coluna.strip()
        ]

        for coluna_fk in colunas_fk:
            chave = (
                tabela_origem,
                tabela_referenciada,
                coluna_fk
            )

            if chave in relacionamentos_processados:
                continue

            relacionamentos_processados.add(chave)

            relacionamento = {
                "source_table": tabela_origem,
                "source_column": coluna_fk,
                "target_table": tabela_referenciada,
                "target_column": None,
                "source": "oracle_documentation",
                "confidence": "high"
            }

            if (
                tabela_origem == nome_tabela_atual
                and tabela_referenciada == nome_tabela_atual
            ):
                relacionamento["relationship_type"] = (
                    "self_reference"
                )
                relacionamentos_saida.append(relacionamento)

            elif tabela_origem == nome_tabela_atual:
                relacionamento["relationship_type"] = "outgoing"
                relacionamentos_saida.append(relacionamento)

            elif tabela_referenciada == nome_tabela_atual:
                relacionamento["relationship_type"] = "incoming"
                relacionamentos_entrada.append(relacionamento)

    return {
        "outgoing": relacionamentos_saida,
        "incoming": relacionamentos_entrada
    }


def extrair_valores_documentados(descricao):
    if not descricao:
        return []

    valores = []

    for match in re.finditer(
        r"\b([A-Z0-9])\s*\(([^)]+)\)",
        descricao
    ):
        codigo = match.group(1)
        significado = match.group(2).strip()

        item = {
            "value": codigo,
            "meaning": significado
        }

        if item not in valores:
            valores.append(item)

    if valores:
        return valores

    match_yn = re.search(
        r"\bY\s*(?:/|OR)\s*N\b",
        descricao,
        flags=re.IGNORECASE
    )

    if match_yn:
        return [
            {"value": "Y", "meaning": "Yes"},
            {"value": "N", "meaning": "No"}
        ]

    return []


def obter_relacionamentos_da_coluna(
    nome_coluna,
    relacionamentos
):
    return [
        relacionamento
        for relacionamento in relacionamentos.get("outgoing", [])
        if relacionamento.get("source_column") == nome_coluna
    ]


def classificar_semantica_coluna(
    coluna,
    primary_key,
    relacionamentos
):
    nome = coluna["name"]
    descricao = coluna.get("description") or ""
    descricao_lower = descricao.lower()
    datatype = (coluna.get("datatype") or "").upper()

    referencias = obter_relacionamentos_da_coluna(
        nome,
        relacionamentos
    )

    papeis = []
    fontes = []

    def adicionar_papel(papel, source, confidence):
        if papel not in papeis:
            papeis.append(papel)
            fontes.append({
                "role": papel,
                "source": source,
                "confidence": confidence
            })

    if nome in primary_key:
        adicionar_papel(
            "primary_key",
            "oracle_documentation",
            "high"
        )

    if referencias:
        adicionar_papel(
            "foreign_key",
            "oracle_documentation",
            "high"
        )

    if nome in AUDIT_COLUMNS:
        adicionar_papel(
            "audit",
            "oracle_documentation",
            "high"
        )

    if nome == "OBJECT_VERSION_NUMBER":
        adicionar_papel(
            "optimistic_lock",
            "oracle_documentation",
            "high"
        )

    if nome in {"LANGUAGE", "SOURCE_LANG"}:
        adicionar_papel(
            "language",
            "oracle_documentation",
            "high"
        )

    if nome.endswith("_FLAG"):
        adicionar_papel(
            "categorical_flag",
            "column_name_inference",
            "medium"
        )

    if (
        nome.endswith("_STATUS")
        or nome.endswith("_STATUS_CODE")
        or nome == "STATUS"
    ):
        adicionar_papel(
            "status",
            "column_name_inference",
            "medium"
        )

    if (
        nome.endswith("_DATE")
        or nome.endswith("_DATETIME")
        or "TIMESTAMP" in datatype
        or datatype == "DATE"
    ):
        adicionar_papel(
            "temporal",
            "datatype_or_name_inference",
            "medium"
        )

    if nome.endswith("_ID") and not referencias:
        adicionar_papel(
            "identifier",
            "column_name_inference",
            "medium"
        )

    if (
        nome.endswith("_CODE")
        or nome == "CODE"
    ):
        adicionar_papel(
            "business_code",
            "column_name_inference",
            "medium"
        )

    if (
        "percentage" in descricao_lower
        or "percent" in descricao_lower
        or nome.endswith("_PERCENT")
        or nome.endswith("_PERCENTAGE")
    ):
        adicionar_papel(
            "percentage",
            "description_or_name_inference",
            "medium"
        )

    if (
        "amount" in descricao_lower
        or nome.endswith("_AMOUNT")
        or nome.endswith("_AMT")
    ):
        adicionar_papel(
            "amount",
            "description_or_name_inference",
            "medium"
        )

    if (
        "quantity" in descricao_lower
        or nome.endswith("_QUANTITY")
        or nome.endswith("_QTY")
    ):
        adicionar_papel(
            "quantity",
            "description_or_name_inference",
            "medium"
        )

    if (
        nome in {"NAME", "DESCRIPTION"}
        or nome.endswith("_NAME")
        or nome.endswith("_DESCRIPTION")
    ):
        adicionar_papel(
            "descriptive_attribute",
            "column_name_inference",
            "medium"
        )

    if not papeis:
        adicionar_papel(
            "attribute",
            (
                "oracle_documentation"
                if descricao
                else "column_name_inference"
            ),
            "medium" if descricao else "low"
        )

    resultado = {
        "column": nome,
        "semantic_role": papeis[0],
        "semantic_roles": papeis,
        "role_evidence": fontes,
        "description": coluna.get("description"),
        "datatype": coluna.get("datatype"),
        "nullable": coluna.get("nullable")
    }

    if referencias:
        resultado["references"] = [
            {
                "table": referencia.get("target_table"),
                "column": referencia.get("target_column")
            }
            for referencia in referencias
        ]

    valores_documentados = extrair_valores_documentados(descricao)

    if valores_documentados:
        resultado["documented_values"] = valores_documentados

    return resultado


def gerar_column_semantics(
    colunas,
    primary_key,
    relacionamentos
):
    return [
        classificar_semantica_coluna(
            coluna,
            primary_key,
            relacionamentos
        )
        for coluna in colunas
    ]


def gerar_business_rules(
    colunas,
    primary_key,
    relacionamentos,
    descricao_tabela=None
):
    nomes = {
        coluna["name"]
        for coluna in colunas
    }

    colunas_por_nome = {
        coluna["name"]: coluna
        for coluna in colunas
    }

    regras = []

    if descricao_tabela:
        regras.append({
            "rule_type": "documented_business_context",
            "columns": [],
            "rule": descricao_tabela,
            "source": "oracle_documentation",
            "confidence": "high"
        })

    if primary_key:
        regras.append({
            "rule_type": "uniqueness",
            "columns": primary_key,
            "rule": (
                "A combinação das colunas da chave primária "
                "identifica unicamente cada registro."
            ),
            "source": "oracle_documentation",
            "confidence": "high"
        })

    if "OBJECT_VERSION_NUMBER" in nomes:
        descricao = (
            colunas_por_nome["OBJECT_VERSION_NUMBER"]
            .get("description")
        )

        regras.append({
            "rule_type": "optimistic_locking",
            "columns": ["OBJECT_VERSION_NUMBER"],
            "rule": (
                descricao
                or (
                    "OBJECT_VERSION_NUMBER é incrementado a cada "
                    "atualização e pode ser usado para detectar "
                    "alterações concorrentes."
                )
            ),
            "source": (
                "oracle_documentation"
                if descricao
                else "standard_oracle_pattern"
            ),
            "confidence": "high" if descricao else "medium"
        })

    for inicio, fim in (
        ("START_DATE_ACTIVE", "END_DATE_ACTIVE"),
        ("EFFECTIVE_START_DATE", "EFFECTIVE_END_DATE")
    ):
        if {inicio, fim}.issubset(nomes):
            regras.append({
                "rule_type": "effective_dating",
                "columns": [inicio, fim],
                "rule": (
                    f"A validade temporal do registro é determinada "
                    f"pelo intervalo entre {inicio} e {fim}."
                ),
                "source": "column_semantics_inference",
                "confidence": "medium"
            })

    if {"LANGUAGE", "SOURCE_LANG"}.issubset(nomes):
        regras.append({
            "rule_type": "translation",
            "columns": ["LANGUAGE", "SOURCE_LANG"],
            "rule": (
                "LANGUAGE identifica o idioma da linha traduzida e "
                "SOURCE_LANG identifica o idioma original."
            ),
            "source": "oracle_documentation",
            "confidence": "high"
        })

    colunas_auditoria = sorted(
        nomes.intersection(AUDIT_COLUMNS)
    )

    if colunas_auditoria:
        regras.append({
            "rule_type": "audit",
            "columns": colunas_auditoria,
            "rule": (
                "As colunas de auditoria registram a criação e a "
                "última alteração do registro."
            ),
            "source": "oracle_documentation",
            "confidence": "high"
        })

    for relacionamento in relacionamentos.get("outgoing", []):
        regras.append({
            "rule_type": "referential_integrity",
            "columns": [relacionamento["source_column"]],
            "rule": (
                f"{relacionamento['source_column']} referencia "
                f"{relacionamento['target_table']}."
            ),
            "referenced_table": relacionamento["target_table"],
            "referenced_column": relacionamento.get("target_column"),
            "source": "oracle_documentation",
            "confidence": "high"
        })

    for coluna in colunas:
        nome = coluna["name"]
        descricao = coluna.get("description") or ""
        valores = extrair_valores_documentados(descricao)

        if nome.endswith("_FLAG") and descricao:
            regra = {
                "rule_type": "flag_domain",
                "columns": [nome],
                "rule": descricao,
                "source": "oracle_documentation",
                "confidence": "high"
            }

            if valores:
                regra["allowed_values"] = valores

            regras.append(regra)

    return regras


def gerar_result_grain(nome_tabela, primary_key):
    if not primary_key:
        return {
            "description": None,
            "grain_columns": [],
            "uniqueness_source": None,
            "table": nome_tabela,
            "source": "not_identified",
            "confidence": "unknown"
        }

    colunas_formatadas = ", ".join(primary_key)

    return {
        "description": (
            "Uma linha por combinação única de "
            f"{colunas_formatadas}."
        ),
        "grain_columns": primary_key,
        "uniqueness_source": "primary_key",
        "table": nome_tabela,
        "source": "oracle_documentation",
        "confidence": "high"
    }


def gerar_ranking_rules(colunas, primary_key):
    nomes = {
        coluna["name"]
        for coluna in colunas
    }

    regras = []

    version_columns = sorted(
        coluna
        for coluna in nomes
        if (
            coluna == "VERSION_NUMBER"
            or coluna.endswith("_VERSION_NUMBER")
            or coluna.endswith("_VERSION_NUM")
        )
        and coluna != "OBJECT_VERSION_NUMBER"
    )

    current_flags = sorted(
        coluna
        for coluna in nomes
        if coluna.endswith("_FLAG")
        and (
            coluna.startswith("CURRENT_")
            or coluna.startswith("LATEST_")
            or "_CURRENT_" in coluna
            or "_LATEST_" in coluna
        )
    )

    for version_column in version_columns:
        partition_by_candidate = (
            [
                coluna
                for coluna in primary_key
                if coluna != version_column
            ]
            if version_column in primary_key
            else []
        )

        order_by = [{
            "column": version_column,
            "direction": "DESC"
        }]

        if "LAST_UPDATE_DATE" in nomes:
            order_by.append({
                "column": "LAST_UPDATE_DATE",
                "direction": "DESC"
            })

        regras.append({
            "purpose": "latest_business_version_candidate",
            "partition_by": partition_by_candidate,
            "order_by": order_by,
            "requires_business_validation": True,
            "requires_business_partition": (
                not bool(partition_by_candidate)
            ),
            "source": "column_name_inference",
            "confidence": "medium",
            "warning": (
                "Validar se a partição proposta representa a chave "
                "da entidade versionada. OBJECT_VERSION_NUMBER não "
                "deve ser usado como versão funcional."
            )
        })

    for coluna in current_flags:
        regras.append({
            "purpose": "current_record_candidate",
            "filter": {
                "column": coluna,
                "operator": "=",
                "value": "Y"
            },
            "requires_business_validation": True,
            "source": "column_name_inference",
            "confidence": "low",
            "warning": (
                "Validar no comentário da coluna se Y representa "
                "efetivamente o registro vigente."
            )
        })

    if "LAST_UPDATE_DATE" in nomes:
        regras.append({
            "purpose": "most_recent_update_candidate",
            "partition_by": [],
            "order_by": [{
                "column": "LAST_UPDATE_DATE",
                "direction": "DESC"
            }],
            "requires_business_partition": True,
            "source": "technical_inference",
            "confidence": "low",
            "warning": (
                "LAST_UPDATE_DATE indica atualização técnica. A chave "
                "de particionamento deve ser definida conforme a "
                "entidade de negócio."
            )
        })

    return regras


def localizar_links_de_objetos(soup, url_sumario):
    objetos = {}

    for link in soup.find_all("a", href=True):
        texto = link.get_text(" ", strip=True).upper()
        href = link.get("href", "").strip()

        if not texto or not href:
            continue

        if "_" not in texto:
            continue

        if not NOME_OBJETO_RE.fullmatch(texto):
            continue

        url_objeto = urljoin(url_sumario, href)
        path = urlparse(url_objeto).path.lower()

        if not path.endswith(".html"):
            continue

        objetos[texto] = url_objeto

    return objetos


def extrair_tabelas_de_modulo(url_base, path_sumario):
    session = criar_sessao()
    url_sumario = urljoin(url_base, path_sumario)

    print(f"\n[EXPLORANDO] Sumário: {url_sumario}")

    try:
        soup_sumario = obter_soup(session, url_sumario)
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Não foi possível acessar o sumário {url_sumario}. "
            f"Verifique se o caminho correto é 'toc.htm'."
        ) from exc

    links_objetos = localizar_links_de_objetos(
        soup_sumario,
        url_sumario
    )

    print(
        f"[SUMÁRIO] {len(links_objetos)} objetos candidatos encontrados."
    )

    tabelas = []

    for indice, (nome_sumario, url_objeto) in enumerate(
        links_objetos.items(),
        start=1
    ):
        try:
            soup_objeto = obter_soup(session, url_objeto)

            h1 = soup_objeto.find("h1")

            if not h1:
                print(f"[IGNORADO] Página sem H1: {url_objeto}")
                continue

            nome_real = h1.get_text(" ", strip=True).upper()
            tipo_objeto = extrair_tipo_objeto(soup_objeto)

            if tipo_objeto != "TABLE":
                continue

            descricao = extrair_descricao(soup_objeto)
            chave_primaria = extrair_chave_primaria(soup_objeto)
            metadados_colunas = extrair_metadados_colunas(
                soup_objeto
            )
            colunas = [
                coluna["name"]
                for coluna in metadados_colunas
            ]

            relacionamentos = extrair_relacionamentos(
                soup_objeto,
                nome_real
            )

            column_semantics = gerar_column_semantics(
                metadados_colunas,
                chave_primaria,
                relacionamentos
            )

            business_rules = gerar_business_rules(
                metadados_colunas,
                chave_primaria,
                relacionamentos,
                descricao_tabela=descricao
            )

            result_grain = gerar_result_grain(
                nome_real,
                chave_primaria
            )

            ranking_rules = gerar_ranking_rules(
                metadados_colunas,
                chave_primaria
            )

            tabelas.append({
                "table_name": nome_real,
                "description": descricao,
                "primary_key": chave_primaria,
                "fields_to_extract": colunas,
                "columns": metadados_colunas,
                "column_semantics": column_semantics,
                "business_rules": business_rules,
                "result_grain": result_grain,
                "ranking_rules": ranking_rules,
                "relationships": relacionamentos,
                "source_url": url_objeto
            })

            total_relacionamentos = (
                len(relacionamentos["outgoing"])
                + len(relacionamentos["incoming"])
            )

            print(
                f"[{indice}/{len(links_objetos)}] "
                f"{nome_real}: "
                f"{len(colunas)} colunas, "
                f"{total_relacionamentos} relacionamentos, "
                f"{len(business_rules)} regras, "
                f"PK={chave_primaria or 'não identificada'}"
            )

        except Exception as exc:
            print(
                f"[ERRO] Falha ao processar "
                f"{nome_sumario} ({url_objeto}): {exc}"
            )

    print(f"[SUCESSO] {len(tabelas)} tabelas físicas extraídas.")

    return tabelas


def normalizar_url_modulo(url_modulo):
    """Normaliza index.html/toc.htm para a pasta-base da documentação."""
    url_modulo = (url_modulo or "").strip()

    if not url_modulo:
        raise ValueError("A URL do módulo não pode ser vazia.")

    parsed = urlparse(url_modulo)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "URL inválida. Informe uma URL HTTP/HTTPS da documentação Oracle."
        )

    path = parsed.path
    filename = path.rsplit("/", 1)[-1].lower()

    if filename in {
        "index.html",
        "index.htm",
        "toc.html",
        "toc.htm",
    }:
        path = path.rsplit("/", 1)[0] + "/"
    elif not path.endswith("/"):
        path += "/"

    return parsed._replace(path=path, params="", query="", fragment="").geturl()


def inferir_id_modulo(url_modulo):
    base_url = normalizar_url_modulo(url_modulo)
    partes = [parte for parte in urlparse(base_url).path.split("/") if parte]

    if not partes:
        return "oracle_module"

    codigo_documentacao = partes[-1].lower()
    produto = partes[-3].lower() if len(partes) >= 3 else codigo_documentacao
    valor = f"{produto}_{codigo_documentacao}"
    valor = re.sub(r"[^a-z0-9]+", "_", valor).strip("_")
    return valor or "oracle_module"


def inferir_release(url_modulo, release=None):
    if release:
        return release.upper()

    for parte in urlparse(url_modulo).path.split("/"):
        if re.fullmatch(r"\d{2}[a-zA-Z]", parte):
            return parte.upper()

    return "UNKNOWN"


def resolver_arquivo_saida(module_id, output=None, output_dir=None):
    if output:
        arquivo_saida = os.path.abspath(output)
    else:
        if output_dir:
            diretorio_modulo = os.path.abspath(output_dir)
        else:
            diretorio_modulo = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "data",
                "modules",
                module_id,
            )

        arquivo_saida = os.path.join(
            diretorio_modulo,
            "physical",
            "manifest.json",
        )

    os.makedirs(os.path.dirname(arquivo_saida), exist_ok=True)
    return arquivo_saida


def build_antigravity_manifest(
    module_url,
    *,
    module_id=None,
    module_name=None,
    release=None,
    toc_path="toc.htm",
    output=None,
    output_dir=None,
):
    base_url = normalizar_url_modulo(module_url)
    module_id = module_id or inferir_id_modulo(base_url)
    module_name = module_name or module_id.replace("_", " ").title()
    release = inferir_release(base_url, release)
    arquivo_saida = resolver_arquivo_saida(
        module_id,
        output=output,
        output_dir=output_dir,
    )

    tabelas = extrair_tabelas_de_modulo(
        base_url,
        toc_path,
    )

    manifesto = {
        "$schema": "https://antigravity.ai/schemas/v1/skills.json",
        "version": "2.0.0",
        "metadata": {
            "name": f"oracle_fusion_{module_id}_scraped_skills",
            "display_name": (
                f"Oracle Fusion Cloud - {module_name} ({release})"
            ),
            "description": (
                "Catálogo físico extraído da documentação oficial "
                "do Oracle Fusion Cloud Applications."
            ),
            "module_id": module_id,
            "module_name": module_name,
            "module_url": base_url,
            "release_version": release,
        },
        "extraction_profile": {
            "incremental_loading": {
                "watermark_column": "LAST_UPDATE_DATE",
                "audit_columns": [
                    "CREATION_DATE",
                    "CREATED_BY",
                    "LAST_UPDATE_DATE",
                    "LAST_UPDATED_BY",
                ],
            }
        },
        "skills_catalog": [
            {
                "module_id": module_id,
                "module_name": module_name,
                "sub_module": module_name,
                "source_url": base_url,
                "purpose": (
                    f"Mapeamento de tabelas físicas para {module_name}."
                ),
                "components": tabelas,
            }
        ],
    }

    with open(arquivo_saida, "w", encoding="utf-8") as arquivo:
        json.dump(
            manifesto,
            arquivo,
            indent=4,
            ensure_ascii=False,
        )

    print("\n[CONCLUÍDO] Manifesto gerado com sucesso.")
    print(f"Módulo: {module_name} ({module_id})")
    print(f"URL: {base_url}")
    print(f"Arquivo: {arquivo_saida}")

    return manifesto, arquivo_saida


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Extrai tabelas, colunas e relacionamentos de um módulo "
            "do Oracle Fusion Cloud Applications."
        )
    )
    parser.add_argument(
        "--module-url",
        required=True,
        help=(
            "URL da página inicial, do toc.htm ou da pasta-base do "
            "dicionário físico do módulo."
        ),
    )
    parser.add_argument(
        "--module-id",
        help="Identificador curto, por exemplo: ppm, common ou financials.",
    )
    parser.add_argument(
        "--module-name",
        help="Nome legível do módulo.",
    )
    parser.add_argument(
        "--release",
        help="Release Oracle, por exemplo 26B. Se omitida, será inferida da URL.",
    )
    parser.add_argument(
        "--toc-path",
        default="toc.htm",
        help="Nome do sumário dentro da pasta do módulo.",
    )
    parser.add_argument(
        "--output",
        help="Caminho completo do manifesto JSON.",
    )
    parser.add_argument(
        "--output-dir",
        help=(
            "Diretório do módulo. O manifesto será salvo em "
            "<output-dir>/physical/manifest.json."
        ),
    )
    return parser


def main():
    args = build_parser().parse_args()
    build_antigravity_manifest(
        args.module_url,
        module_id=args.module_id,
        module_name=args.module_name,
        release=args.release,
        toc_path=args.toc_path,
        output=args.output,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
