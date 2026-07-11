# Oracle Fusion Knowledge Base

Ferramenta para coletar documentação do Oracle Fusion Cloud Applications, organizar o conhecimento por módulo e fonte, construir grafos especializados e gerar um contexto limpo para perguntas técnicas, funcionais e de dados.

A arquitetura atual evita colocar toda a documentação de um módulo em um único prompt. A consulta é resolvida primeiro em um grafo mestre pequeno e, em seguida, o orquestrador navega apenas pelos pontos relevantes dos grafos especializados.

Os exemplos de comando são apresentados em **Bash** e **PowerShell**. No Bash, a continuação de linha usa `\`; no PowerShell, usa crase (`` ` ``).

> [!IMPORTANT]
> **Ferramenta independente e não oficial.** Este projeto não é um produto Oracle, não possui suporte oficial da Oracle e não substitui o trabalho do **Analista Funcional Oracle Fusion**, a validação técnica, a homologação com dados reais nem os controles de segurança e governança da organização. Seu objetivo é **acelerar a descoberta de fontes e a construção de pipelines de extração de dados do Oracle Fusion Cloud Applications**, produzindo contexto rastreável para análise, SQL candidata e desenho de datasets.
>
> Leia também o [artigo do autor no Medium sobre o projeto](https://medium.com/p/4d524bfea5fa).

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

O resultado deve ser entendido como **apoio à engenharia de dados**. Ele reduz o tempo de descoberta e organiza evidências para que uma equipe técnica e funcional possa construir, revisar e homologar pipelines de extração com menor risco de utilizar tabelas, colunas, joins, filtros ou granularidades incorretas.

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

### Princípios para alta assertividade na geração do contexto

A assertividade não depende apenas do modelo semântico. Ela resulta da combinação de controles estruturais, conhecimento curado e critérios conservadores de promoção:

```text
pergunta
  → decomposição em entidade, atributos, restrições e grão
  → módulo e comunidade estrutural
  → rotas explícitas e proveniência
  → candidatos locais
  → precedência da evidência curada
  → compatibilidade de grão
  → contexto explicável
```

Os princípios obrigatórios são:

1. **Estrutura antes de semântica.** A busca é restrita ao módulo, à entidade e à comunidade comprovada antes do fallback semântico.
2. **Evidência forte vence score.** Mapeamentos curados, regras validadas e nomes técnicos exatos prevalecem sobre FTS5 e embeddings.
3. **Embedding não prova equivalência funcional.** Similaridade semântica isolada gera sugestão, nunca uma coluna `resolved`.
4. **O grão faz parte da resposta.** Uma coluna de linha não deve vencer uma coluna de cabeçalho quando o pedido exige uma linha por acordo.
5. **Identificador não é descrição.** `VENDOR_ID` pode ser evidência parcial para Fornecedor, mas não substitui o nome do fornecedor.
6. **Ausência de prova é explicitada.** O motor deve devolver `ambiguous` ou `unresolved`, em vez de completar lacunas por plausibilidade.
7. **Toda decisão precisa ser rastreável.** Fonte, caminho, tipo de aresta, confiança, comunidade e critério de seleção devem aparecer no JSON explicável.

### Hierarquia de evidência

A ordem de precedência usada para selecionar atributos deve ser preservada pela curadoria e pelos testes:

| Prioridade | Evidência | Uso esperado |
|---:|---|---|
| 1 | regra validada no ambiente | seleção, filtro, join, ranking e grão homologados |
| 2 | `mapped_to_attribute` ou coluna qualificada curada | resolução determinística do atributo |
| 3 | documentação oficial inequívoca | candidato forte com proveniência oficial |
| 4 | nome técnico ou alias técnico exato | resolução quando não há conflito de significado |
| 5 | correspondência lexical forte | candidato sujeito a margem e ausência de concorrentes |
| 6 | similaridade semântica | sugestão diagnóstica; não promove sozinha |

### Estados de resolução de atributos

| Estado | Significado | Pode entrar automaticamente em SQL candidata? |
|---|---|---|
| `resolved` | há evidência forte e caminho físico compatível | sim, ainda sujeito a revisão |
| `partial` | existe parte do caminho, normalmente um ID sem descrição | não sem completar o relacionamento |
| `ambiguous` | há candidatos plausíveis sem evidência suficiente para escolher | não |
| `unresolved` | não foi encontrado candidato estruturalmente válido | não |

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
Sistema especialista: ACME
Objeto especialista: ACME_AUCTION_HEADER
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

O modelo padrão dos comandos locais de vetorização, construção e busca é:

```text
intfloat/multilingual-e5-base
```

Os modelos suportados são:

| Modelo | Dimensões | Formato de consulta/documento | Uso recomendado |
|---|---:|---|---|
| `intfloat/multilingual-e5-base` | 768 | `query:` / `passage:` | CPU local e uso individual |
| `intfloat/multilingual-e5-large-instruct` | 1.024 | instrução E5 para consulta; documento sem prefixo | GPU NVIDIA com CUDA |

Na primeira execução, o modelo selecionado pode ser baixado e carregado. O
`multilingual-e5-base` é o padrão porque oferece um compromisso melhor para a
execução em CPU de um catálogo grande. O `multilingual-e5-large-instruct` pode
ser selecionado quando se desejar o perfil maior, mas sua vetorização completa
em CPU pode levar muitas horas; para esse modelo, é recomendado utilizar uma
GPU NVIDIA com CUDA.

> **CUDA não é requisito para o modelo base.** O projeto aceita `cpu` e `cuda`
> no caminho atual baseado em `sentence-transformers`. TPU não é suportada pela
> implementação atual; não existe backend, configuração nem fluxo de execução
> preparado para TPU neste projeto.

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

Todo o projeto usa UTF-8. A configuração deve ser feita **antes** de coletar, indexar ou executar buscas. Isso é especialmente importante no Windows PowerShell 5.1, cujo pipeline de processos nativos pode interpretar bytes UTF-8 usando a code page OEM.

> [!WARNING]
> `Out-File -Encoding utf8` ou `Set-Content -Encoding utf8` controlam a gravação do arquivo, mas não corrigem bytes que o PowerShell já tenha interpretado com a code page errada. Configure também `InputEncoding`, `OutputEncoding`, `$OutputEncoding`, `PYTHONUTF8` e `PYTHONIOENCODING`.

#### Bash

```bash
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
```

#### PowerShell

```powershell
chcp 65001 > $null

$utf8 = [System.Text.UTF8Encoding]::new($false)

[Console]::InputEncoding  = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

No PowerShell, prefira também executar o Python com `-X utf8`:

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py --help
```

### Teste rápido do terminal

#### Bash

```bash
python -X utf8 -c 'import json; print(json.dumps({"texto": "Aquisição, Descrição, Não, inferência"}, ensure_ascii=False))' \
  > "./teste_utf8.json"
python -X utf8 -c 'from pathlib import Path; print(Path("teste_utf8.json").read_text(encoding="utf-8"))'
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 -c `
  "import json; print(json.dumps({'texto': 'Aquisição, Descrição, Não, inferência'}, ensure_ascii=False))" |
  Set-Content `
    -Path ".\teste_utf8.json" `
    -Encoding utf8

Get-Content ".\teste_utf8.json" -Raw -Encoding utf8
```

O resultado deve preservar exatamente `Aquisição`, `Descrição`, `Não` e `inferência`. PowerShell 7+ é recomendado, mas o PowerShell 5.1 pode ser utilizado com a configuração acima.

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
& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\procurement_common" `
  --query "acordo de compra valor liberado fornecedor condições de pagamento" `
  --module "procurement" `
  --limit 20 `
  --max-characters 14000 |
  Set-Content `
    -Path ".\resultado_federado_procurement.json" `
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

O JSON é uma trilha de decisão, não apenas uma lista de resultados. Os blocos mais importantes são:

### `query` e `query_diagnostics`

- pergunta recebida;
- consulta normalizada;
- diagnóstico de encoding;
- correções aplicadas, quando houver;
- marcadores ambíguos.

### `query_plan`

Decomposição determinística da pergunta:

- entidade principal;
- atributos solicitados;
- aliases reconhecidos;
- restrições explícitas;
- grão solicitado;
- módulo candidato.

### `entity` e `community`

Identificam a entidade escolhida e a comunidade estrutural usada para limitar a navegação. Verifique:

- `community_id`;
- estratégia;
- nó âncora;
- módulos;
- quantidade de membros;
- distribuição por tipo e camada.

Uma comunidade muito grande pode indicar arestas globais ou relações excessivamente permissivas. Uma comunidade pequena demais pode esconder relacionamentos necessários.

### `god_nodes`

Nós estruturalmente centrais da comunidade. Eles ajudam no diagnóstico, mas não são automaticamente a resposta correta. Uma tabela com muitas colunas tende a ter alto grau local.

### `attribute_evidence`

É o principal bloco para avaliar a assertividade do contexto. Para cada atributo, confira:

- `status`;
- `selected_path`;
- `alternative_paths`;
- `evidence`;
- `candidate_diagnostics`;
- `grain_compatibility`;
- `selection_basis`;
- `resolution_confidence`;
- `resolution_note`.

Um candidato sem `promotion_eligible`, sustentado apenas por `semantic_suggestion`, não deve ser tratado como coluna confirmada.

### `results` e `context`

- `results` contém evidências selecionadas para o contexto;
- `context` é o texto renderizado para análise humana ou LLM;
- `characters` informa o orçamento efetivamente utilizado.

A presença de um objeto em `results` não substitui a avaliação de `attribute_evidence`.

### `gaps`

Atributos `partial`, `ambiguous` ou `unresolved` que exigem curadoria, expansão de caminhos ou validação funcional. Um `gaps` vazio só é positivo quando as evidências também são fortes.

### `rejected_candidates` e `semantic_candidates`

Permitem verificar o que foi rejeitado pelo gating e quais candidatos vieram do fallback semântico. O fallback deve permanecer restrito à comunidade ativa.

### `routing`

Diagnóstico do orquestrador:

- módulos efetivos;
- sementes de negócio;
- rotas explícitas;
- rotas descartadas;
- escopo semântico;
- diagnóstico ADF-first;
- reutilização de embeddings;
- quantidade de vetores de consulta;
- tempos por etapa.

Os indicadores esperados em uma busca normal são:

```text
semantic_scope = active_community
embedding_recalculated = false
document_embeddings_recalculated = false
```

O bloco `routing` diferencia conhecimento curado, expansão estrutural, FTS5 e fallback semântico.

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

A curadoria é a etapa que transforma um motor de busca estruturalmente seguro em uma base de conhecimento funcionalmente assertiva. Ela não deve ser implementada como condicionais específicas no código. O conhecimento deve ser versionado em arquivos de configuração, regras e mapeamentos, com origem e confiança explícitas.

### Curadoria Oracle padrão e curadoria da implantação

**Curadoria Oracle padrão** é reutilizável entre clientes e deve ser sustentada por documentação oficial, catálogos OTBI/REST ou evidência técnica inequívoca. Exemplos:

```text
Purchase Agreement → PO_HEADERS_ALL
agreement number → PO_HEADERS_ALL.SEGMENT1
currency → PO_HEADERS_ALL.CURRENCY_CODE
start date → PO_HEADERS_ALL.START_DATE
end date → PO_HEADERS_ALL.END_DATE
creation date → PO_HEADERS_ALL.CREATION_DATE
released amount → PO_HEADERS_ALL.AMOUNT_RELEASED
```

**Curadoria específica da implantação** depende da configuração e do uso real do ERP:

- atributos flexíveis e extensíveis;
- status e códigos usados pela organização;
- tipos de documento e filtros locais;
- regras de vigência e ranking;
- relatórios homologados;
- views Gold e integrações;
- customizações ADF;
- segurança e escopo de dados;
- sistema considerado autoridade para cada informação.

A ferramenta não consegue deduzir com segurança essas características apenas pela documentação pública. Elas precisam ser validadas pelo Analista Funcional e pela equipe responsável pelo dado.

### Curadoria versus desenvolvimento de software

| Curadoria de conhecimento | Desenvolvimento e tuning do motor |
|---|---|
| aliases funcionais e técnicos | algoritmo genérico de ranking |
| entidade e atributo canônicos | structural gating |
| coluna qualificada | expansão genérica de caminhos |
| join e cardinalidade validados | cache e incrementalidade |
| significado de status e códigos | performance de FTS5 e embeddings |
| regras de vigência, filtro e ranking | compactação do JSON |
| customizações e configuração do ambiente | observabilidade e tratamento de erros |
| fonte, confiança e responsável pela validação | testes da infraestrutura do motor |

Mapeamentos como `agreement_number → PO_HEADERS_ALL.SEGMENT1` pertencem à curadoria. Regras genéricas como “evidência curada vence similaridade semântica” pertencem ao software. Evite colocar conhecimento de uma única implantação em condicionais Python.

### Conteúdo mínimo de um atributo curado

O schema atual permite declarar atributos dentro da entidade em `entity_aliases.json`:

```json
{
  "entity_id": "purchase_agreement",
  "name": "Purchase Agreement",
  "aliases": [
    "acordo de compra",
    "gerenciar acordo"
  ],
  "tables": [
    "PO_HEADERS_ALL"
  ],
  "attributes": [
    {
      "attribute_id": "agreement_number",
      "name": "Agreement Number",
      "aliases": [
        "acordo",
        "número do acordo",
        "agreement number"
      ],
      "description": "Número do documento do acordo de compra.",
      "columns": [
        "PO_HEADERS_ALL.SEGMENT1"
      ],
      "confidence": "high"
    }
  ],
  "module_id": "procurement"
}
```

Esse registro cria um nó `business_attribute`, a aresta `has_attribute` e, quando a coluna existir no grafo físico, a aresta `mapped_to_attribute`.

### Curadoria de caminhos descritivos

Mapear um ID é insuficiente quando o campo solicitado é descritivo. Exemplos que precisam de caminho validado:

```text
Fornecedor
PO_HEADERS_ALL.VENDOR_ID
  → POZ_SUPPLIERS
  → HZ_PARTIES.PARTY_NAME
```

```text
Condições de Pagamento
PO_HEADERS_ALL.TERMS_ID
  → tabela de condições
  → nome da condição
```

A curadoria deve registrar:

- coluna de origem;
- tabela e coluna de destino;
- tipo de relação;
- propósito funcional;
- grão antes e depois do join;
- cardinalidade esperada;
- fonte da evidência;
- status da validação;
- ambiente e data da validação.

### Critérios de promoção

A inclusão de um alias não deve promover qualquer candidato textual. Para um atributo ficar `resolved`, deve existir uma evidência inequívoca, como:

- coluna qualificada em `attributes[].columns`;
- regra validada;
- nome técnico exato sem conflito;
- documentação oficial diretamente vinculada ao atributo;
- caminho estrutural completo e compatível com o grão.

Um ID sem caminho até a descrição deve permanecer `partial`. Vários candidatos fortes sem desempate devem permanecer `ambiguous`.

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

Essas fontes não devem criar equivalência automática. Por exemplo, `ACME_AUCTION_HEADER` pode integrar-se com `PON_AUCTION_HEADERS_ALL` sem possuir necessariamente o mesmo grão ou o mesmo ciclo de vida. Registre a relação conforme a evidência disponível.

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

### Fluxo de publicação da curadoria

Depois de alterar aliases, atributos, regras ou mapeamentos, publique o conhecimento de forma incremental.

#### Bash

```bash
python -X utf8 build_knowledge_base.py link \
  --modules-root "./data/modules" \
  --include-default-curation \
  --output-dir "./data/graph/fusion_modules"

python -X utf8 build_knowledge_base.py build-topology \
  --graph-dir "./data/graph/fusion_modules"

python -X utf8 build_knowledge_base.py normalize-index \
  --graph-dir "./data/graph/fusion_modules" \
  --batch-size 1000 \
  --checkpoint-percent 1

python -X utf8 build_knowledge_base.py vectorize-index \
  --graph-dir "./data/graph/fusion_modules" \
  --semantic-model "intfloat/multilingual-e5-base" \
  --semantic-device cpu \
  --semantic-batch-size 32 \
  --checkpoint-percent 1

python -X utf8 build_knowledge_base.py build-index \
  --graph-dir "./data/graph/fusion_modules" \
  --layer business \
  --layer master \
  --semantic-model "intfloat/multilingual-e5-base"

python -X utf8 build_knowledge_base.py validate-index \
  --graph-dir "./data/graph/fusion_modules"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py link `
  --modules-root ".\data\modules" `
  --include-default-curation `
  --output-dir ".\data\graph\fusion_modules"

& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py build-topology `
  --graph-dir ".\data\graph\fusion_modules"

& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py normalize-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --batch-size 1000 `
  --checkpoint-percent 1

& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py vectorize-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --semantic-model "intfloat/multilingual-e5-base" `
  --semantic-device "cpu" `
  --semantic-batch-size 32 `
  --checkpoint-percent 1

& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --layer business `
  --layer master `
  --semantic-model "intfloat/multilingual-e5-base"

& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py validate-index `
  --graph-dir ".\data\graph\fusion_modules"
```

`normalize-index` e `vectorize-index` são incrementais: conteúdos inalterados são reutilizados. Para alterações exclusivas de código, ranking ou renderização, não execute essas etapas. Para novos atributos, aliases ou descrições, execute-as para manter o corpus semântico alinhado.

### Testes de referência da curadoria

Cada caso homologado deve virar um teste com:

```text
pergunta
entidade esperada
módulo esperado
grão esperado
atributos esperados
colunas esperadas
estado esperado por atributo
colunas proibidas
caminhos obrigatórios
```

A meta não é resolver tudo. A meta é obter:

- zero falso positivo promovido;
- atributos oficiais resolvidos deterministicamente;
- IDs descritivos marcados como `partial` até o caminho ser completado;
- ambiguidades registradas em `gaps`;
- nenhum objeto de outro módulo ou comunidade no contexto.

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

### Aparecem caracteres como `AquisiþÒo` ou `condi├º├Áes`

Volte à seção [Configuração UTF-8](#6-configuração-utf-8). O problema normalmente está na fronteira entre o processo Python e o terminal, especialmente no Windows PowerShell 5.1. Apenas definir a codificação do arquivo de saída não corrige bytes já interpretados com a code page errada.

#### Bash

```bash
python -X utf8 build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/fusion_modules" \
  --query "Aquisição, Descrição, Condições de Pagamento" \
  > "./resultado_utf8.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\fusion_modules" `
  --query "Aquisição, Descrição, Condições de Pagamento" |
  Set-Content `
    -Path ".\resultado_utf8.json" `
    -Encoding utf8
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

## Coleta automatizada de metadados ADF do ambiente

O catálogo ADF pertence ao ambiente Fusion inteiro. Ele não é armazenado dentro
de um módulo. O comando `collect-adf` consulta o `/describe` global e coleta os
recursos selecionados em:

```text
data/environment/adf/
├── manifest.json
├── catalog.json
├── raw/
│   ├── catalog.json
│   ├── catalog.meta.json
│   └── resources/
└── modules/
    ├── unclassified.json
    ├── procurement.json
    └── <outro_modulo>.json
```

As credenciais são fornecidas por variáveis de ambiente. O comando não solicita
senha interativamente.

#### Bash

```bash
export FUSION_USERNAME="seu.usuario"
export FUSION_PASSWORD="senha-do-processo-atual"

python -X utf8 build_knowledge_base.py collect-adf \
  --base-url "https://seu-ambiente.fa.regiao.oraclecloud.com" \
  --output-dir "./data/environment/adf" \
  --custom-only
```

#### PowerShell

```powershell
$env:FUSION_USERNAME = "seu.usuario"
$env:FUSION_PASSWORD = "senha-do-processo-atual"

& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py collect-adf `
  --base-url "https://seu-ambiente.fa.regiao.oraclecloud.com" `
  --output-dir ".\data\environment\adf" `
  --custom-only
```

A coleta é retomável. Recursos já gravados em `raw/resources` não são
consultados novamente, salvo quando `--force-refresh` é informado. O arquivo
`modules/unclassified.json` é atualizado sem sobrescrever classificações
manuais já existentes nos demais arquivos de `modules/`.

Uma projeção explícita por módulo usa apenas os nomes dos recursos globais:

```json
{
  "module_id": "procurement",
  "resources": [
    "purchaseAgreements",
    "purchaseOrders",
    "APCUSTOMBM_c"
  ]
}
```

Validação do catálogo global:

#### Bash

```bash
python -X utf8 build_knowledge_base.py validate-adf \
  --adf-dir "./data/environment/adf"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py validate-adf `
  --adf-dir ".\data\environment\adf"
```

O comando `link` procura automaticamente
`data/environment/adf/catalog.json` quando `--modules-root` aponta para
`data/modules`.

#### Bash

```bash
python -X utf8 build_knowledge_base.py link \
  --modules-root "./data/modules" \
  --include-default-curation \
  --output-dir "./data/graph/fusion_modules"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py link `
  --modules-root ".\data\modules" `
  --include-default-curation `
  --output-dir ".\data\graph\fusion_modules"
```

Também é possível informar o catálogo explicitamente com `--adf-catalog`.
Os recursos ADF entram no grafo REST como nós `adf_resource`, mantendo a origem
`fusion_adf_rest_metadata`, os atributos, filhos, ações e links publicados pelo
ambiente. Quando um recurso ADF possui exatamente o mesmo nome de um recurso
REST oficial, o linker cria a relação explícita `environment_variant_of`.
Essa relação não é tratada como prova de tabela física ou de join SQL.

## Normalização semântica retomável

Antes da vetorização, o corpus semântico pode ser preparado com o comando
`normalize-index`. Essa etapa não carrega o modelo de embeddings. Ela:

- normaliza Unicode e whitespace de forma conservadora;
- preserva caixa, acentos e pontuação;
- deduplica textos normalizados idênticos;
- aplica canonicalização curada somente a campos técnicos estáveis de auditoria;
- persiste o estado em SQLite a cada lote;
- registra checkpoint, percentual e ETA no intervalo configurado;
- retoma automaticamente um processamento interrompido;
- reutiliza nós cujo documento não mudou;
- processa ADF/REST antes das demais camadas.

Exemplo:

#### Bash

```bash
python -X utf8 -u build_knowledge_base.py normalize-index \
  --graph-dir "./data/graph/fusion_modules" \
  --batch-size 1000 \
  --checkpoint-percent 1
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 -u build_knowledge_base.py normalize-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --batch-size 1000 `
  --checkpoint-percent 1
```

Artefatos gerados:

```text
data/graph/fusion_modules/search_index/
├── semantic_normalization.sqlite
└── semantic_normalization_manifest.json
```

Os lotes são confirmados em transações SQLite mesmo entre checkpoints. O
percentual controla a frequência do manifesto e das mensagens de progresso, não
a durabilidade. Após `Ctrl+C`, basta repetir o mesmo comando para retomar do
último lote confirmado.

Para iniciar um novo run sem apagar o cache normalizado:

#### Bash

```bash
python -X utf8 -u build_knowledge_base.py normalize-index \
  --graph-dir "./data/graph/fusion_modules" \
  --no-resume
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 -u build_knowledge_base.py normalize-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --no-resume
```

A normalização possui versão própria. Campos genéricos como `STATUS`, `TYPE`,
`CODE`, `AMOUNT`, `DESCRIPTION` e `ATTRIBUTE*` continuam contextuais e não são
canonicalizados globalmente.

## Vetorização semântica retomável

Depois de concluir `normalize-index`, vetorize somente os textos normalizados
únicos com `vectorize-index`. O modelo base é o padrão e pode ser informado
explicitamente.

#### Bash

```bash
python -X utf8 -u build_knowledge_base.py vectorize-index \
  --graph-dir "./data/graph/fusion_modules" \
  --semantic-model "intfloat/multilingual-e5-base" \
  --semantic-device cpu \
  --semantic-batch-size 32 \
  --checkpoint-percent 1
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 -u build_knowledge_base.py vectorize-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --semantic-model "intfloat/multilingual-e5-base" `
  --semantic-device "cpu" `
  --semantic-batch-size 32 `
  --checkpoint-percent 1
```

O perfil base usa 768 dimensões e aplica automaticamente os prefixos corretos:

```text
consulta:  query: <pergunta>
documento: passage: <texto normalizado>
```

Para usar o modelo maior:

#### Bash

```bash
python -X utf8 -u build_knowledge_base.py vectorize-index \
  --graph-dir "./data/graph/fusion_modules" \
  --semantic-model "intfloat/multilingual-e5-large-instruct" \
  --semantic-device cuda \
  --semantic-batch-size 32 \
  --checkpoint-percent 1
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 -u build_knowledge_base.py vectorize-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --semantic-model "intfloat/multilingual-e5-large-instruct" `
  --semantic-device "cuda" `
  --semantic-batch-size 32 `
  --checkpoint-percent 1
```

O perfil `large-instruct` usa 1.024 dimensões e o formato de instrução próprio
do modelo. Para esse perfil, GPU NVIDIA com CUDA é recomendada. Em CPU, um
corpus grande pode exigir dezenas de horas. TPU não é suportada pela
implementação atual.

A dimensão esperada é inferida automaticamente pelo modelo selecionado. A opção
`--expected-dimensions` permanece disponível apenas para validação ou
diagnóstico explícito.

A etapa lê `semantic_normalization.sqlite` e grava:

```text
data/graph/fusion_modules/search_index/
├── semantic_embeddings.sqlite
└── semantic_embeddings_manifest.json
```

Os vetores são normalizados em L2 e persistidos como `float32`. A identidade do
cache combina o hash do texto normalizado, o modelo e a versão do perfil. Os
embeddings de modelos diferentes permanecem separados no mesmo banco e nunca
são misturados ou reutilizados entre si. Ao trocar de modelo, toda a
normalização é reaproveitada, mas os vetores precisam ser produzidos para o
novo espaço vetorial.

A vetorização é:

- idempotente: uma execução concluída com a mesma normalização e o mesmo perfil
  não chama novamente o modelo;
- incremental: textos cujo hash já possui embedding compatível são reutilizados;
- retomável: cada lote é confirmado no SQLite e uma nova execução continua dos
  textos ainda sem vetor;
- observável: o log apresenta percentual, contagens, dimensão e ETA aproximada.

O mesmo modelo deve ser usado em `vectorize-index`, `build-index` e
`search-federated`. Exemplo de busca com o perfil base.

#### Bash

```bash
python -X utf8 -u build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/fusion_modules" \
  --query "acordos de compra e fornecedor" \
  --semantic-model "intfloat/multilingual-e5-base" \
  --semantic-device cpu
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 -u build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\fusion_modules" `
  --query "acordos de compra e fornecedor" `
  --semantic-model "intfloat/multilingual-e5-base" `
  --semantic-device "cpu"
```

O roteamento ADF-first continua independente do modelo escolhido: o ADF reduz
o universo de recursos e entidades candidatos antes da expansão local, mas a
consulta e os documentos comparados semanticamente precisam pertencer ao mesmo
espaço vetorial.

Exemplo de progresso:

```text
[VECTORIZE] 51821/259102 textos (20.00%) | gerados=51821 |
reutilizados=0 | dimensões=768 | ETA 03:42:10 | checkpoint persistido.
```

Após `Ctrl+C`, repita o mesmo comando. Para iniciar outro run preservando o
cache compatível, use `--no-resume`. Para descartar e recalcular os vetores do
perfil atual, use `--force-revectorize`.

## Roteamento topológico explicável

A busca federada preserva a arquitetura híbrida existente e acrescenta uma camada topológica compartilhada entre os perfis semânticos. O fluxo é: diagnóstico de encoding, decomposição da pergunta, roteamento ADF/business, seleção de comunidade estrutural, uso de `god_nodes`, busca de caminhos tipados por atributo, expansão por proveniência e bloqueio de candidatos desconectados. FTS e embeddings continuam sendo mecanismos de recuperação; eles não criam comunidades nem relações.

As comunidades são vizinhanças estruturais determinísticas centradas em entidades de negócio. Apenas bridges explícitas e arestas `VALIDATED` ou `EXTRACTED` participam da derivação; ligações genéricas como `belongs_to_module` não unem domínios. O catálogo `topology_catalog.json` registra assinatura do grafo, versão do algoritmo, memberships sobrepostos quando necessários e centralidade local (`weighted_local_degree_v2`). Colunas de auditoria e nós técnicos genéricos recebem penalização para não dominarem os `god_nodes`.

#### Bash

```bash
python -X utf8 build_knowledge_base.py build-topology \
  --graph-dir "./data/graph/fusion_modules"

python -X utf8 build_knowledge_base.py build-index \
  --graph-dir "./data/graph/fusion_modules" \
  --semantic-model "intfloat/multilingual-e5-base"

python -X utf8 build_knowledge_base.py validate-index \
  --graph-dir "./data/graph/fusion_modules"

python -X utf8 build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/fusion_modules" \
  --semantic-model "intfloat/multilingual-e5-base" \
  --query "acordo de compra com fornecedor, valor liberado e condições de pagamento" \
  > "./resultado_topologia.json"
```

#### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py build-topology `
  --graph-dir ".\data\graph\fusion_modules"

& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --semantic-model "intfloat/multilingual-e5-base"

& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py validate-index `
  --graph-dir ".\data\graph\fusion_modules"

& ".\.venv\Scripts\python.exe" -X utf8 build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\fusion_modules" `
  --semantic-model "intfloat/multilingual-e5-base" `
  --query "acordo de compra com fornecedor, valor liberado e condições de pagamento" |
  Set-Content `
    -Path ".\resultado_topologia.json" `
    -Encoding utf8
```

O JSON da pesquisa inclui `query_diagnostics`, `query_plan`, uma síntese compacta da `community`, `god_nodes`, `attribute_evidence`, `semantic_candidates`, `rejected_candidates`, `gaps` e diagnóstico ADF-first. O structural gating ocorre antes da renderização, portanto candidatos rejeitados não entram em `results` nem no contexto final. O fallback semântico fica restrito à comunidade ativa e utiliza os vetores persistidos; alterar apenas query planning, topologia ou gating não exige executar `normalize-index` ou `vectorize-index` novamente.

Limitação atual: a decomposição é determinística e baseada no conteúdo efetivamente presente nos grafos. Atributos ou relações ausentes são retornados como lacunas, sem criação de tabelas, colunas, joins, filtros ou significados.

## Critérios de aceite para contexto de alta assertividade

Antes de usar o contexto para construir uma extração, confirme:

- a entidade principal representa o objeto funcional solicitado;
- o módulo e a comunidade estão corretos;
- cada atributo possui estado coerente;
- candidatos `semantic_suggestion` não foram promovidos;
- a coluna selecionada possui mapeamento curado, evidência oficial ou nome técnico inequívoco;
- o grão da tabela é compatível com o grão solicitado;
- joins descritivos possuem caminho completo e cardinalidade conhecida;
- filtros e significados de códigos vêm de regra validada ou documentação;
- `gaps` foi revisado pelo Analista Funcional;
- a SQL candidata foi validada com dados reais;
- a curadoria confirmada foi versionada e coberta por testes.

O projeto deve ser usado para acelerar o ciclo de descoberta e engenharia, não para eliminar a responsabilidade de análise e homologação.

## Referência externa

- [Artigo do autor no Medium sobre o Oracle Fusion Knowledge Base](https://medium.com/p/4d524bfea5fa)
