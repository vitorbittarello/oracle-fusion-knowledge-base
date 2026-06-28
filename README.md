# Oracle Fusion Knowledge Base

Ferramenta para coletar documentação do Oracle Fusion Cloud Applications, organizar o conhecimento por módulo e fonte, construir grafos especializados e gerar um contexto limpo para perguntas técnicas, funcionais e de dados.

A arquitetura atual evita colocar toda a documentação de um módulo em um único prompt. A consulta é resolvida primeiro em um grafo mestre pequeno e, em seguida, o orquestrador navega apenas pelos pontos relevantes dos grafos especializados.

Os exemplos de comando são apresentados em **Bash** e **PowerShell**. No Bash, a continuação de linha usa `\`; no PowerShell, usa crase (`` ` ``).

---

## 1. Objetivo

O projeto ajuda a localizar e relacionar:

- entidades e atributos de negócio;
- processos e domínios organizacionais da companhia;
- tabelas, views e colunas físicas;
- chaves primárias, referências e grão documentado;
- subject areas e perguntas de negócio do OTBI;
- recursos e operações REST;
- documentação funcional;
- aliases curados;
- regras validadas no ambiente;
- linhagem corporativa proveniente de views Gold e integrações com sistemas especialistas, quando registrada por curadoria.

A saída principal é um contexto rastreável para apoiar:

- análise funcional;
- descoberta de fontes;
- construção de datasets;
- geração e revisão de SQL;
- validação de joins, filtros, granularidade e regras de vigência;
- perguntas para uma LLM com menos ruído documental.

A ferramenta trabalha com documentação e metadados. Ela não consulta os dados transacionais do Oracle Fusion.

---

## 2. Arquitetura atual

O fluxo recomendado é:

```text
pergunta do usuário
        ↓
master_graph.json
        ↓
entidades, atributos e regras reconhecidos
        ↓
pontes explícitas
        ↓
grafos especializados
        ├── business.json
        ├── physical.json
        ├── otbi_analytics.json
        └── rest.json
        ↓
expansão local controlada
        ↓
seleção e resumo semântico
        ↓
contexto final para análise ou LLM
```

O grafo de segurança OTBI permanece separado:

```text
otbi_security.json
```

Ele não participa da busca federada padrão de datasets, SQL e análise de dados.

### Por que os grafos são separados

A separação impede que objetos com funções diferentes disputem o mesmo ranking global. Por exemplo:

- uma tabela física não compete diretamente com um job role;
- uma pergunta OTBI não compete diretamente com uma coluna;
- uma simples menção textual não recebe a mesma autoridade de um mapeamento curado;
- a origem de um comportamento incorreto pode ser diagnosticada por camada.

---

## 3. Camadas do grafo

### `business.json`

Contém:

- entidades de negócio;
- atributos de negócio;
- processos e domínios organizacionais;
- aliases funcionais em português e inglês;
- regras validadas;
- trechos de documentação funcional;
- referências curadas a implementações técnicas em diferentes sistemas.

A camada de negócio representa a semântica corporativa e não deve ficar limitada à divisão modular do Oracle Fusion. Um mesmo processo pode envolver Oracle Fusion, sistemas especialistas, integrações OCI, tabelas carregadas no lake e views Gold.

Exemplo:

```text
Domínio organizacional: Suprimentos
Processo: Leilão de fornecedores
Sistema especialista: OCY
Objeto especialista: OCY_AUCTION_HEADER
Objeto Oracle Fusion: PON_AUCTION_HEADERS_ALL
Módulo técnico Oracle: Procurement
```

O domínio organizacional, o sistema de origem e o módulo técnico são dimensões diferentes. A relação entre objetos deve registrar seu significado real, como `integrates_with`, `derived_from`, `materializes` ou `authoritative_for`. Não use `same_as` sem validação de equivalência e de grão.

Owners físicos variam entre ambientes. Por isso, nomes de schema devem ser tratados como metadados ambientais e abstraídos por um papel lógico, por exemplo `gold`, `fusion_silver` ou `specialized_source`. O owner real não deve determinar o ID estável do objeto nem contaminar a busca semântica.

### `physical.json`

Contém:

- tabelas e views;
- colunas;
- chaves e relacionamentos;
- grão documentado;
- referências a objetos físicos;
- `physical_table_stub` quando uma tabela é referenciada, mas ainda não foi coletada.

### `otbi_analytics.json`

Contém:

- subject areas;
- perguntas de negócio;
- páginas analíticas úteis.

### `otbi_security.json`

Contém páginas relacionadas a:

- job roles;
- duty roles;
- privilégios;
- segurança.

Essa camada é mantida para consultas específicas de segurança, mas não entra no orquestrador federado padrão.

### `rest.json`

Contém:

- recursos REST;
- operações;
- endpoints;
- parâmetros e atributos documentados.

### `master_graph.json`

É o grafo de roteamento. Ele contém apenas os principais pontos de interseção:

- entidades;
- atributos;
- regras validadas;
- tabelas e colunas explicitamente mapeadas;
- subject areas explicitamente mapeadas;
- recursos REST explicitamente mapeados.

As pontes aceitas no master são controladas, como:

```text
has_attribute
mapped_to_entity
mapped_to_attribute
uses_table
uses_column
```

### `graph_bundle.json`

É o manifesto do conjunto de grafos. Registra os arquivos gerados e suas estatísticas.

---

## 4. Requisitos

- Python 3.10 ou superior;
- acesso HTTP à documentação Oracle;
- espaço em disco compatível com o módulo coletado;
- memória suficiente para carregar os grafos necessários;
- PowerShell 5.1 ou PowerShell 7+.

Dependências principais:

- `beautifulsoup4`;
- `requests`;
- `urllib3`;
- `numpy`;
- `sentence-transformers`.

O modelo semântico padrão é:

```text
intfloat/multilingual-e5-large-instruct
```

Na primeira busca semântica, o modelo pode ser baixado e carregado. A primeira execução tende a ser mais lenta.

> **CUDA não é requisito.** A geração dos índices funciona integralmente em CPU. O projeto também suporta GPU NVIDIA com CUDA para acelerar a criação dos embeddings semânticos, especialmente na primeira indexação de módulos grandes.

---

## 5. Instalação

Crie o ambiente virtual:

#### Bash

```bash
python3 -m venv .venv
```

#### PowerShell

```powershell
py -m venv .venv
```

Caso a política do PowerShell bloqueie a ativação:

```powershell
Set-ExecutionPolicy `
  -Scope Process `
  -ExecutionPolicy Bypass
```

Ative o ambiente:

#### Bash

```bash
source .venv/bin/activate
```

#### PowerShell

```powershell
& ".\.venv\Scripts\Activate.ps1"
```

Atualize o `pip`:

#### Bash

```bash
python \
  -m pip install \
  --upgrade pip
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" `
  -m pip install `
  --upgrade pip
```

Instale o projeto em modo editável:

#### Bash

```bash
python \
  -m pip install \
  -e .
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" `
  -m pip install `
  -e .
```

Execute os testes:

#### Bash

```bash
python \
  -m unittest discover \
  -s tests \
  -v
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" `
  -m unittest discover `
  -s tests `
  -v
```

---

## 6. Configuração UTF-8

Antes de coletas e buscas, configure a sessão:

#### Bash

```bash
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
```

#### PowerShell

```powershell
$utf8 = [System.Text.UTF8Encoding]::new()

[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$env:PYTHONUTF8 = "1"
```

Para salvar a saída, use redirecionamento no Bash e `Out-File -Encoding utf8` no PowerShell:

#### Bash

```bash
python build_knowledge_base.py --help \
  > "./ajuda.txt"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py --help |
  Out-File `
    -FilePath ".\ajuda.txt" `
    -Encoding utf8
```

---

## 7. Estrutura dos módulos coletados

Cada módulo fica isolado:

```text
data/modules/
├── procurement/
│   ├── module.json
│   ├── physical/
│   │   └── manifest.json
│   ├── functional/
│   │   └── fragments.jsonl
│   ├── otbi/
│   │   └── catalog.json
│   ├── rest/
│   │   └── catalog.json
│   ├── config/
│   │   └── entity_aliases.json
│   └── rules/
│       └── validated_rules.json
├── ppm/
├── common/
└── scm/
```

Somente as fontes solicitadas são coletadas. Os arquivos de aliases e regras são criados mesmo quando inicialmente vazios.

---

## 8. Coletar um módulo

### 8.1 Coleta somente do dicionário físico

#### Bash

```bash
python -u build_knowledge_base.py collect-module \
  --module-id "ppm" \
  --module-name "Project Management" \
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" \
  --release "26B" \
  --output-dir "./data/modules/ppm"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py collect-module `
  --module-id "ppm" `
  --module-name "Project Management" `
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" `
  --release "26B" `
  --output-dir ".\data\modules\ppm"
```

Resultado principal:

```text
data/modules/ppm/physical/manifest.json
```

### 8.2 Coleta completa de um módulo

#### Bash

```bash
python -u build_knowledge_base.py collect-module \
  --module-id "procurement" \
  --module-name "Procurement" \
  --module-url "https://docs.oracle.com/en/cloud/saas/procurement/26b/oedmp/index.html" \
  --release "26B" \
  --functional-url "URL_DO_GUI_FUNCIONAL" \
  --otbi-url "URL_DO_CATALOGO_OTBI" \
  --rest-url "URL_DO_CATALOGO_REST" \
  --output-dir "./data/modules/procurement" \
  --delay-seconds 0.15 \
  2>&1 | tee "./coleta_procurement.log"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py collect-module `
  --module-id "procurement" `
  --module-name "Procurement" `
  --module-url "https://docs.oracle.com/en/cloud/saas/procurement/26b/oedmp/index.html" `
  --release "26B" `
  --functional-url "URL_DO_GUI_FUNCIONAL" `
  --otbi-url "URL_DO_CATALOGO_OTBI" `
  --rest-url "URL_DO_CATALOGO_REST" `
  --output-dir ".\data\modules\procurement" `
  --delay-seconds 0.15 `
  2>&1 |
  Tee-Object `
    -FilePath ".\coleta_procurement.log"
```

`--functional-url` pode ser repetido para vários guias.

### 8.3 Coleta limitada para teste

#### Bash

```bash
python -u build_knowledge_base.py collect-module \
  --module-id "ppm" \
  --module-name "Project Management" \
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" \
  --release "26B" \
  --functional-url "URL_DO_GUI_FUNCIONAL" \
  --otbi-url "URL_DO_CATALOGO_OTBI" \
  --rest-url "URL_DO_CATALOGO_REST" \
  --max-functional-pages-per-guide 5 \
  --max-otbi-pages 20 \
  --max-rest-pages 30 \
  --output-dir "./data/modules/ppm"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py collect-module `
  --module-id "ppm" `
  --module-name "Project Management" `
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" `
  --release "26B" `
  --functional-url "URL_DO_GUI_FUNCIONAL" `
  --otbi-url "URL_DO_CATALOGO_OTBI" `
  --rest-url "URL_DO_CATALOGO_REST" `
  --max-functional-pages-per-guide 5 `
  --max-otbi-pages 20 `
  --max-rest-pages 30 `
  --output-dir ".\data\modules\ppm"
```

### 8.4 Coleta de SCM

#### Bash

```bash
python -u build_knowledge_base.py collect-module \
  --module-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/oedsc/index.html" \
  --module-id "scm" \
  --module-name "Supply Chain Management" \
  --release "26B" \
  --output-dir "./data/modules/scm" \
  --otbi-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/faosm/toc.htm" \
  --rest-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/fasrp/toc.htm" \
  --delay-seconds 0.15 \
  2>&1 | tee "./coleta_scm.log"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py collect-module `
  --module-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/oedsc/index.html" `
  --module-id "scm" `
  --module-name "Supply Chain Management" `
  --release "26B" `
  --output-dir ".\data\modules\scm" `
  --otbi-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/faosm/toc.htm" `
  --rest-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/fasrp/toc.htm" `
  --delay-seconds 0.15 `
  2>&1 |
  Tee-Object `
    -FilePath ".\coleta_scm.log"
```

SCM é um módulo grande. O manifesto físico e o catálogo REST podem consumir bastante tempo, memória e disco.

---

## 9. Retomar uma coleta interrompida

Use o mesmo `--output-dir` e preserve o cache:

```text
.cache/oracle_docs
```

Não use `--force-refresh` durante uma retomada normal.

Quando o manifesto físico já estiver completo, evite refazê-lo com `--skip-physical`:

#### Bash

```bash
python -u build_knowledge_base.py collect-module \
  --module-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/oedsc/index.html" \
  --module-id "scm" \
  --module-name "Supply Chain Management" \
  --release "26B" \
  --output-dir "./data/modules/scm" \
  --skip-physical \
  --otbi-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/faosm/toc.htm" \
  --rest-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/fasrp/toc.htm" \
  --delay-seconds 0.15 \
  2>&1 | tee "./coleta_scm_continuacao.log"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py collect-module `
  --module-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/oedsc/index.html" `
  --module-id "scm" `
  --module-name "Supply Chain Management" `
  --release "26B" `
  --output-dir ".\data\modules\scm" `
  --skip-physical `
  --otbi-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/faosm/toc.htm" `
  --rest-url "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/26b/fasrp/toc.htm" `
  --delay-seconds 0.15 `
  2>&1 |
  Tee-Object `
    -FilePath ".\coleta_scm_continuacao.log"
```

Observações:

- `--skip-physical` mantém o manifesto físico já existente;
- o cache HTTP pode reduzir downloads repetidos;
- `--no-resume` desativa a retomada dos fragmentos funcionais e não deve ser usado em uma continuação comum;
- `--force-refresh` ignora o cache das fontes que utilizam cache e deve ser reservado para uma atualização deliberada.

---

## 10. Coletores individuais

Os comandos abaixo usam as fontes configuradas em:

```text
config/knowledge_sources.json
```

### Documentação funcional

#### Bash

```bash
python build_knowledge_base.py collect-functional \
  --config "./config/knowledge_sources.json" \
  --output "./data/functional/functional_fragments.jsonl"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py collect-functional `
  --config ".\config\knowledge_sources.json" `
  --output ".\data\functional\functional_fragments.jsonl"
```

### OTBI

#### Bash

```bash
python build_knowledge_base.py collect-otbi \
  --config "./config/knowledge_sources.json" \
  --output "./data/otbi/otbi_catalog.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py collect-otbi `
  --config ".\config\knowledge_sources.json" `
  --output ".\data\otbi\otbi_catalog.json"
```

### REST

#### Bash

```bash
python build_knowledge_base.py collect-rest \
  --config "./config/knowledge_sources.json" \
  --output "./data/rest/rest_catalog.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py collect-rest `
  --config ".\config\knowledge_sources.json" `
  --output ".\data\rest\rest_catalog.json"
```

Para projetos novos, `collect-module` é normalmente mais simples porque mantém cada módulo em seu próprio diretório.

---

## 11. Gerar os grafos separados

### 11.1 Regerar um módulo do zero lógico

Este processo recria os grafos a partir dos arquivos coletados. Ele não reaproveita um grafo combinado anterior.

Remova apenas o diretório de grafos:

#### Bash

```bash
rm -rf "./data/graph/procurement_common"
```

#### PowerShell

```powershell
if (Test-Path ".\data\graph\procurement_common") {
    Remove-Item `
      -Path ".\data\graph\procurement_common" `
      -Recurse `
      -Force
}
```

Gere o bundle:

#### Bash

```bash
python build_knowledge_base.py link \
  --module-dir "./data/modules/procurement" \
  --module-dir "./data/modules/common" \
  --include-default-curation \
  --output-dir "./data/graph/procurement_common"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py link `
  --module-dir ".\data\modules\procurement" `
  --module-dir ".\data\modules\common" `
  --include-default-curation `
  --output-dir ".\data\graph\procurement_common"
```

### 11.2 Gerar somente SCM para teste isolado

Sem curadoria padrão de outros módulos:

#### Bash

```bash
rm -rf "./data/graph/scm"

python build_knowledge_base.py link \
  --module-dir "./data/modules/scm" \
  --output-dir "./data/graph/scm"
```

#### PowerShell

```powershell
if (Test-Path ".\data\graph\scm") {
    Remove-Item `
      -Path ".\data\graph\scm" `
      -Recurse `
      -Force
}

& ".\.venv\Scripts\python.exe" build_knowledge_base.py link `
  --module-dir ".\data\modules\scm" `
  --output-dir ".\data\graph\scm"
```

O `master_graph.json` pode ficar vazio quando o módulo ainda não possui entidades, atributos ou regras curadas. O orquestrador federado usa fallback semântico por camada nesse caso.

### 11.3 Gerar um bundle multimódulo

#### Bash

```bash
rm -rf "./data/graph/fusion_modules"

python build_knowledge_base.py link \
  --modules-root "./data/modules" \
  --include-default-curation \
  --output-dir "./data/graph/fusion_modules"
```

#### PowerShell

```powershell
if (Test-Path ".\data\graph\fusion_modules") {
    Remove-Item `
      -Path ".\data\graph\fusion_modules" `
      -Recurse `
      -Force
}

& ".\.venv\Scripts\python.exe" build_knowledge_base.py link `
  --modules-root ".\data\modules" `
  --include-default-curation `
  --output-dir ".\data\graph\fusion_modules"
```

### 11.4 Arquivos gerados

```text
business.json
physical.json
otbi_analytics.json
otbi_security.json
rest.json
master_graph.json
graph_bundle.json
```

---

## 12. Conferir os grafos gerados

### Listar tamanhos

#### Bash

```bash
python - <<'PY'
from pathlib import Path

for path in sorted(Path("./data/graph/scm").iterdir()):
    if path.is_file():
        print(f"{path.name:30} {path.stat().st_size / (1024 * 1024):10.2f} MB")
PY
```

#### PowerShell

```powershell
Get-ChildItem ".\data\graph\scm" -File |
  Select-Object `
    Name,
    @{Name = "Tamanho_MB"; Expression = {
        [math]::Round($_.Length / 1MB, 2)
    }} |
  Sort-Object Name |
  Format-Table -AutoSize
```

### Ler estatísticas

#### Bash

```bash
python - <<'PY'
import json
from pathlib import Path

for path in sorted(Path("./data/graph/scm").glob("*.json")):
    if path.name == "graph_bundle.json":
        continue
    graph = json.loads(path.read_text(encoding="utf-8"))
    print(
        f"{path.name:24} "
        f"camada={graph.get('graph_layer')} "
        f"nos={graph.get('stats', {}).get('nodes')} "
        f"arestas={graph.get('stats', {}).get('edges')}"
    )
PY
```

#### PowerShell

```powershell
Get-ChildItem ".\data\graph\scm\*.json" |
  Where-Object {
      $_.Name -ne "graph_bundle.json"
  } |
  ForEach-Object {
      $graph = Get-Content `
        $_.FullName `
        -Raw `
        -Encoding UTF8 |
        ConvertFrom-Json

      [PSCustomObject]@{
          Arquivo = $_.Name
          Camada  = $graph.graph_layer
          Nos     = $graph.stats.nodes
          Arestas = $graph.stats.edges
      }
  } |
  Format-Table -AutoSize
```

### Confirmar ausência das arestas globais removidas

#### Bash

```bash
python - <<'PY'
import json
from pathlib import Path

indesejadas = {
    "mentions_entity",
    "related_by_alias",
    "incoming_foreign_key_from",
}

for path in sorted(Path("./data/graph/scm").glob("*.json")):
    if path.name == "graph_bundle.json":
        continue
    graph = json.loads(path.read_text(encoding="utf-8"))
    total = sum(
        1
        for edge in graph.get("edges", [])
        if edge.get("type") in indesejadas
    )
    print(f"{path.name:24} arestas_indesejadas={total}")
PY
```

#### PowerShell

```powershell
Get-ChildItem ".\data\graph\scm\*.json" |
  Where-Object {
      $_.Name -ne "graph_bundle.json"
  } |
  ForEach-Object {
      $graph = Get-Content `
        $_.FullName `
        -Raw `
        -Encoding UTF8 |
        ConvertFrom-Json

      $arestasIndesejadas = @(
          $graph.edges |
          Where-Object {
              $_.type -in @(
                  "mentions_entity",
                  "related_by_alias",
                  "incoming_foreign_key_from"
              )
          }
      )

      [PSCustomObject]@{
          Arquivo = $_.Name
          Arestas_Indesejadas = $arestasIndesejadas.Count
      }
  } |
  Format-Table -AutoSize
```

O esperado é zero em todos os grafos novos.

---

## 13. Busca federada — modo recomendado

O comando recomendado é:

```text
search-federated
```

Ele:

1. pesquisa o `master_graph`;
2. identifica entidades, atributos e regras;
3. percorre pontes explícitas;
4. entra nos grafos especializados pelos IDs dos nós;
5. expande localmente tabelas, colunas, FKs, subject areas, perguntas e operações REST;
6. usa semântica para seleção local e resumo;
7. monta o contexto final respeitando o orçamento de caracteres.

### Procurement

#### Bash

```bash
python build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/procurement_common" \
  --query "acordo de compra valor liberado fornecedor condições de pagamento" \
  --module "procurement" \
  --limit 20 \
  --max-characters 14000 \
  > "./resultado_federado_procurement.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\procurement_common" `
  --query "acordo de compra valor liberado fornecedor condições de pagamento" `
  --module "procurement" `
  --limit 20 `
  --max-characters 14000 |
  Out-File `
    -FilePath ".\resultado_federado_procurement.json" `
    -Encoding utf8
```

### SCM — Manufacturing

#### Bash

```bash
python build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/scm" \
  --query "qual tabela armazena ordens de produção e qual coluna identifica o número da ordem" \
  --module "scm" \
  --limit 20 \
  --max-characters 14000 \
  > "./resultado_scm_manufacturing.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\scm" `
  --query "qual tabela armazena ordens de produção e qual coluna identifica o número da ordem" `
  --module "scm" `
  --limit 20 `
  --max-characters 14000 |
  Out-File `
    -FilePath ".\resultado_scm_manufacturing.json" `
    -Encoding utf8
```

### SCM — Product Management

#### Bash

```bash
python build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/scm" \
  --query "quais tabelas e colunas representam itens por organização e o número do item" \
  --module "scm" \
  --limit 20 \
  --max-characters 14000 \
  > "./resultado_scm_product_management.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\scm" `
  --query "quais tabelas e colunas representam itens por organização e o número do item" `
  --module "scm" `
  --limit 20 `
  --max-characters 14000 |
  Out-File `
    -FilePath ".\resultado_scm_product_management.json" `
    -Encoding utf8
```

### SCM — OTBI

#### Bash

```bash
python build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/scm" \
  --query "qual subject area permite analisar custos estimados e reais por ordem de produção" \
  --module "scm" \
  --limit 20 \
  --max-characters 14000 \
  > "./resultado_scm_costing_otbi.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\scm" `
  --query "qual subject area permite analisar custos estimados e reais por ordem de produção" `
  --module "scm" `
  --limit 20 `
  --max-characters 14000 |
  Out-File `
    -FilePath ".\resultado_scm_costing_otbi.json" `
    -Encoding utf8
```

### SCM — REST

#### Bash

```bash
python build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/scm" \
  --query "qual recurso REST permite consultar quantidades disponíveis de itens em estoque por subinventário" \
  --module "scm" \
  --limit 20 \
  --max-characters 14000 \
  > "./resultado_scm_inventory_rest.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\scm" `
  --query "qual recurso REST permite consultar quantidades disponíveis de itens em estoque por subinventário" `
  --module "scm" `
  --limit 20 `
  --max-characters 14000 |
  Out-File `
    -FilePath ".\resultado_scm_inventory_rest.json" `
    -Encoding utf8
```

O parâmetro `--module` pode ser repetido em bundles multimódulo.

---

## 14. Como interpretar o resultado federado

O JSON contém:

### `query`

Pergunta original.

### `context`

Texto final pronto para ser usado em uma análise ou enviado a uma LLM.

### `results`

Evidências selecionadas, incluindo:

- tipo do nó;
- título;
- score;
- resumo;
- fonte;
- módulos;
- evidência estruturada.

### `characters`

Quantidade real de caracteres do contexto renderizado.

### `routing`

Diagnóstico do orquestrador:

- `master_business_seeds`: conceitos de negócio reconhecidos no master;
- `master_routes`: pontes explícitas percorridas;
- `semantic_fallback_roots`: raízes escolhidas semanticamente quando uma camada não possuía ponte;
- `candidate_count`: quantidade de candidatos reunidos antes do orçamento final.

O bloco `routing` é importante para diagnosticar se o resultado veio de uma curadoria explícita ou de fallback semântico.

---

## 15. Busca direta — uso diagnóstico

O comando `search` continua disponível para investigar um grafo específico:

#### Bash

```bash
python build_knowledge_base.py search \
  --graph "./data/graph/scm/physical.json" \
  --query "work order number" \
  --module "scm" \
  --context \
  --limit 20 \
  --graph-hops 1 \
  --max-characters 14000 \
  > "./resultado_diagnostico_physical.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py search `
  --graph ".\data\graph\scm\physical.json" `
  --query "work order number" `
  --module "scm" `
  --context `
  --limit 20 `
  --graph-hops 1 `
  --max-characters 14000 |
  Out-File `
    -FilePath ".\resultado_diagnostico_physical.json" `
    -Encoding utf8
```

Esse modo é útil para:

- validar o conteúdo de uma camada;
- investigar termos técnicos;
- testar um nome de tabela ou coluna;
- diagnosticar um problema de indexação.

Para perguntas de negócio em português, prefira `search-federated`. Uma busca direta em `physical.json` ou `otbi_analytics.json` pode retornar vazio quando não existe uma correspondência lexical inicial.

---

## 16. Curadoria

### Aliases de entidades

Por módulo:

```text
data/modules/<module_id>/config/entity_aliases.json
```

Curadoria padrão da raiz:

```text
config/entity_aliases.json
```

Use aliases para mapear termos de negócio a:

- entidades;
- atributos;
- processos corporativos;
- tabelas;
- colunas;
- subject areas;
- recursos REST;
- objetos de sistemas especialistas.

### Fontes corporativas de curadoria

Além da documentação Oracle, podem ser usadas como evidência corporativa:

- views da camada Gold;
- SQLs homologadas;
- mapeamentos de integrações OCI;
- tabelas carregadas de sistemas especialistas;
- documentação funcional interna;
- validações feitas pelas áreas responsáveis.

Views Gold ajudam a identificar aliases funcionais, joins utilizados, filtros de idioma, grão e linhagem até as tabelas Silver. Mapeamentos de integração ajudam a registrar origem, destino, direção do fluxo, chave de integração e sistema de autoridade.

Essas fontes não devem criar equivalência automática. Por exemplo, `OCY_AUCTION_HEADER` pode integrar-se com `PON_AUCTION_HEADERS_ALL` sem possuir necessariamente o mesmo grão ou o mesmo ciclo de vida. Registre a relação conforme a evidência disponível.

Nesta versão, o registro dessas fontes é feito por curadoria nos arquivos de aliases, regras e mapeamentos usados pelo linker. A importação automática de DDLs Gold e configurações OCI é uma evolução prevista, não um comando disponível atualmente.

### Regras validadas

Por módulo:

```text
data/modules/<module_id>/rules/validated_rules.json
```

Curadoria padrão da raiz:

```text
rules/validated_rules.json
```

Registre apenas regras confirmadas, como:

- filtros de aprovação;
- condições de vigência;
- grão;
- ranking;
- joins validados;
- significado de códigos;
- tabelas e colunas usadas.

Depois de editar aliases ou regras, gere novamente o bundle:

#### Bash

```bash
python build_knowledge_base.py link \
  --modules-root "./data/modules" \
  --include-default-curation \
  --output-dir "./data/graph/fusion_modules"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py link `
  --modules-root ".\data\modules" `
  --include-default-curation `
  --output-dir ".\data\graph\fusion_modules"
```

---

## 17. Migração de grafo antigo

O comando abaixo existe para migrar um grafo combinado sem repetir a coleta:

#### Bash

```bash
python build_knowledge_base.py split-graph \
  --graph "./data/graph/grafo_antigo.json" \
  --output-dir "./data/graph/grafo_migrado"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py split-graph `
  --graph ".\data\graph\grafo_antigo.json" `
  --output-dir ".\data\graph\grafo_migrado"
```

Para uma reconstrução limpa, prefira `link --output-dir` a partir de `data/modules`. A migração não recupera referências que já não existiam no grafo antigo.

---

## 18. Comando legado `all`

O comando `all` executa coletores definidos em `config/knowledge_sources.json` e gera um grafo combinado:

#### Bash

```bash
python build_knowledge_base.py all \
  --config "./config/knowledge_sources.json" \
  --physical-manifest "./data/physical/manifest.json" \
  --functional-output "./data/functional/functional_fragments.jsonl" \
  --otbi-output "./data/otbi/otbi_catalog.json" \
  --rest-output "./data/rest/rest_catalog.json" \
  --graph-output "./data/graph/knowledge_graph.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py all `
  --config ".\config\knowledge_sources.json" `
  --physical-manifest ".\data\physical\manifest.json" `
  --functional-output ".\data\functional\functional_fragments.jsonl" `
  --otbi-output ".\data\otbi\otbi_catalog.json" `
  --rest-output ".\data\rest\rest_catalog.json" `
  --graph-output ".\data\graph\knowledge_graph.json"
```

Esse fluxo é mantido por compatibilidade. Para a arquitetura atual, prefira:

```text
collect-module
→ link --output-dir
→ search-federated
```

---

## 19. Solução de problemas

### O grafo físico ficou muito grande

Isso é esperado em módulos extensos como SCM. Use grafos separados e a busca federada para evitar carregar toda a documentação no contexto.

### `rest/catalog.json` não foi criado

A coleta provavelmente foi interrompida antes da etapa REST. Reexecute `collect-module` com o mesmo diretório e use `--skip-physical` quando o manifesto físico já estiver concluído.

### O `master_graph.json` está vazio

O módulo ainda não possui curadoria de entidades, atributos ou regras. A busca federada usa `semantic_fallback_roots`. Analise o bloco `routing` e, depois de validar as respostas, adicione curadoria específica.

### A busca direta retornou zero resultados

Use termos técnicos em inglês ou use `search-federated`, que resolve a linguagem de negócio no master e possui fallback semântico por camada.

### Aparecem caracteres como `condi├º├Áes`

Configure UTF-8 na sessão e salve a saída com:

#### Bash

```bash
> arquivo.json
```

#### PowerShell

```powershell
Out-File -Encoding utf8
```

Também verifique se os arquivos de curadoria foram gravados em UTF-8.

### A primeira busca demora

O modelo de embeddings pode estar sendo baixado ou carregado. Execuções posteriores tendem a ser mais rápidas.

### Quero atualizar tudo ignorando cache

Use `--force-refresh` somente quando realmente quiser baixar novamente as fontes que utilizam cache:

#### Bash

```bash
python -u build_knowledge_base.py collect-module \
  --module-id "ppm" \
  --module-name "Project Management" \
  --module-url "URL_DO_DICIONARIO" \
  --output-dir "./data/modules/ppm" \
  --force-refresh
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py collect-module `
  --module-id "ppm" `
  --module-name "Project Management" `
  --module-url "URL_DO_DICIONARIO" `
  --output-dir ".\data\modules\ppm" `
  --force-refresh
```

---

## 20. Fluxo recomendado

```text
1. Coletar cada módulo em data/modules/<module_id>
2. Conferir os arquivos produzidos
3. Cadastrar aliases e regras validadas quando existirem
4. Gerar grafos separados com link --output-dir
5. Conferir estatísticas e camadas
6. Executar search-federated
7. Avaliar context, results e routing
8. Validar tecnicamente e funcionalmente
9. Registrar novos conhecimentos confirmados
10. Regerar o bundle
```

---

## 21. Referência dos comandos

| Comando | Finalidade |
|---|---|
| `collect-module` | Coleta um módulo em diretório próprio |
| `collect-functional` | Coleta guias definidos no arquivo de configuração |
| `collect-otbi` | Coleta OTBI definido no arquivo de configuração |
| `collect-rest` | Coleta REST definido no arquivo de configuração |
| `link` | Constrói grafo combinado ou bundle separado |
| `split-graph` | Migra um grafo combinado antigo |
| `search` | Pesquisa direta em um grafo |
| `search-federated` | Pesquisa recomendada com roteamento pelo master |
| `all` | Fluxo legado baseado em configuração única |

Ajuda geral:

#### Bash

```bash
python build_knowledge_base.py --help
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py --help
```

Ajuda de comandos:

#### Bash

```bash
python build_knowledge_base.py collect-module --help

python build_knowledge_base.py link --help

python build_knowledge_base.py search --help

python build_knowledge_base.py search-federated --help
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py collect-module --help

& ".\.venv\Scripts\python.exe" build_knowledge_base.py link --help

& ".\.venv\Scripts\python.exe" build_knowledge_base.py search --help

& ".\.venv\Scripts\python.exe" build_knowledge_base.py search-federated --help
```

---

## 22. Índices independentes por camada

A busca federada usa, por padrão, um bundle de índices SQLite independentes:

```text
data/graph/<bundle>/search_index/
├── index_bundle.json
├── business.sqlite
├── physical.sqlite
├── otbi_analytics.sqlite
├── otbi_security.sqlite
├── rest.sqlite
└── master.sqlite
```

O `index_bundle.json` associa cada camada ao arquivo SQLite correspondente. As ligações entre o master e os índices especializados continuam usando os IDs estáveis dos nós.

### 22.1 Migração inicial do índice monolítico

A primeira execução cria os arquivos separados. Quando o índice legado `knowledge_index.sqlite` existe, os embeddings compatíveis são reutilizados. Assim, a migração não precisa recalcular os vetores de todas as tabelas, subject areas e recursos REST.

#### Bash

```bash
python -u build_knowledge_base.py build-index \
  --graph-dir "./data/graph/scm" \
  2>&1 | tee "./build_index_scm_bundle.log"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\scm" `
  2>&1 |
  Tee-Object -FilePath ".\build_index_scm_bundle.log"
```

O log informa, por camada, quantos embeddings foram reutilizados e quantos precisaram ser gerados.

### 22.2 Rebuild seletivo

Uma alteração de aliases, entidades de negócio ou rotas curadas normalmente exige reconstruir apenas `master` e, quando aplicável, `business`:

#### Bash

```bash
python -u build_knowledge_base.py build-index \
  --graph-dir "./data/graph/scm" \
  --layer master \
  --layer business
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\scm" `
  --layer master `
  --layer business
```

Exemplos de escopo:

| Alteração | Camada a reconstruir |
|---|---|
| Alias em português ou inglês | `master`, e possivelmente `business` |
| Nova rota curada para uma tabela já existente | `master` |
| Nova view Gold usada apenas como conhecimento de negócio e linhagem | `business` e `master` |
| Novo mapeamento OCI entre objetos já existentes | `business` e `master` |
| Nova tabela, coluna, PK ou FK | `physical` |
| Atualização de subject areas | `otbi_analytics` |
| Atualização de roles OTBI | `otbi_security` |
| Atualização de recursos ou operações REST | `rest` |
| Mudança apenas de ranking ou prompt | nenhuma |

Sem `--layer`, o comando verifica todas as camadas por SHA-256 e ignora automaticamente as que não mudaram.

Dentro de uma camada modificada, embeddings cujo `content_hash` permaneceu igual são copiados do índice anterior. Somente nós novos ou alterados são enviados ao modelo semântico.

### 22.3 Forçar reconstrução

Use somente para diagnóstico ou alteração deliberada do índice:

#### Bash

```bash
python build_knowledge_base.py build-index \
  --graph-dir "./data/graph/scm" \
  --layer physical \
  --force
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\scm" `
  --layer physical `
  --force
```

Para impedir a reutilização de vetores antigos, acrescente `--no-reuse-embeddings`.

### 22.4 Validar o bundle

#### Bash

```bash
python build_knowledge_base.py validate-index \
  --graph-dir "./data/graph/scm"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py validate-index `
  --graph-dir ".\data\graph\scm"
```

A validação confere o manifesto, a associação camada/arquivo, integridade SQLite, FTS5, cobertura semântica, contagens e atualização em relação a cada grafo.

### 22.5 Busca

`search-federated` prioriza automaticamente `index_bundle.json`. O bloco `routing` informa:

```json
{
  "backend": "sqlite_bundle",
  "index_path": ".../search_index/index_bundle.json",
  "index_paths": {
    "physical": ".../search_index/physical.sqlite",
    "otbi_analytics": ".../search_index/otbi_analytics.sqlite",
    "rest": ".../search_index/rest.sqlite"
  }
}
```

O parâmetro `--index` também aceita o caminho do `index_bundle.json`, o diretório `search_index` ou um SQLite monolítico legado.

### 22.6 CPU, GPU NVIDIA e CUDA

**CUDA não é obrigatório para gerar os índices.** O comando `build-index` funciona integralmente em CPU e esta é a forma compatível com qualquer ambiente suportado pelo projeto.

#### Geração em CPU

Não informe `--semantic-device`:

##### Bash

```bash
python -u build_knowledge_base.py build-index \
  --graph-dir "./data/graph/scm"
```

##### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\scm"
```

#### Geração com CUDA

Quando houver GPU NVIDIA compatível, driver instalado e PyTorch com suporte CUDA no ambiente virtual, informe:

##### Bash

```bash
python -u build_knowledge_base.py build-index \
  --graph-dir "./data/graph/scm" \
  --semantic-device cuda \
  --semantic-batch-size 64
```

##### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\scm" `
  --semantic-device "cuda" `
  --semantic-batch-size 64
```

CUDA acelera principalmente a geração dos embeddings semânticos. A criação das tabelas SQLite, FTS5, hashes, metadados e validações continua sendo executada pela CPU. O ganho tende a ser mais relevante na primeira indexação de módulos grandes ou em alterações em massa.

Em ambientes sem CUDA, omita `--semantic-device cuda`. O rebuild seletivo por camada e a reutilização por `content_hash` reduzem a necessidade de recalcular embeddings, portanto CUDA é uma otimização de desempenho, não uma dependência funcional.
