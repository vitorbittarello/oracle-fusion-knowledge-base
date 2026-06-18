# Oracle Fusion Knowledge Base

Ferramenta para extrair documentação do Oracle Fusion Cloud Applications, organizar cada módulo em um diretório próprio, combinar vários módulos em um único grafo e recuperar contexto relevante para perguntas técnicas e funcionais.

O projeto não exige que toda a documentação do Fusion seja coletada. Cada módulo é incluído somente quando necessário.

## Como ler os exemplos de comando

Este README apresenta comandos em dois formatos:

- **Bash**: para Linux, macOS, WSL ou Git Bash no Windows.
- **PowerShell**: para Windows PowerShell ou PowerShell 7+.

A diferença principal está na quebra de linha.

### Bash

No Bash, a continuação de linha usa barra invertida:

```bash
\
```

Exemplo:

```bash
python build_knowledge_base.py search \
  --graph "data/graph/fusion_combined.json" \
  --query "orçamento aprovado"
```

### PowerShell

No PowerShell, a continuação de linha usa crase:

```powershell
`
```

Exemplo:

```powershell
python build_knowledge_base.py search `
  --graph "data/graph/fusion_combined.json" `
  --query "orçamento aprovado"
```

Use sempre o bloco correspondente ao seu terminal.

---

## Estrutura gerada por módulo

```text
data/modules/
├── ppm/
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
└── common/
    └── ...
```

Somente os arquivos efetivamente coletados são criados.

---

## Instalação

Crie o ambiente virtual:

```bash
python -m venv .venv
```

### Ativar no Bash/Linux/macOS/WSL/Git Bash

```bash
source .venv/bin/activate
```

### Ativar no PowerShell/Windows

```powershell
.venv\Scripts\Activate.ps1
```

Caso o PowerShell bloqueie a execução do script de ativação, execute:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Depois ative novamente:

```powershell
.venv\Scripts\Activate.ps1
```

### Instalar o projeto

O comando é o mesmo para Bash e PowerShell:

```bash
pip install -e .
```

---

## 1. Coletar um módulo físico

A URL informada em `--module-url` deve apontar para a página inicial, o `toc.htm` ou a pasta-base do dicionário de tabelas e views do módulo.

### Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py collect-module \
  --module-id ppm \
  --module-name "Project Management" \
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" \
  --output-dir "data/modules/ppm"
```

### PowerShell/Windows

```powershell
python build_knowledge_base.py collect-module `
  --module-id ppm `
  --module-name "Project Management" `
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" `
  --output-dir "data/modules/ppm"
```

O resultado físico será salvo em:

```text
data/modules/ppm/physical/manifest.json
```

A URL pode terminar em:

```text
index.html
index.htm
toc.html
toc.htm
```

O programa normaliza automaticamente a URL para a pasta-base.

---

## 2. Coletar o Applications Common separadamente

O módulo Common deve ser mantido como módulo próprio. Assim ele pode ser combinado com PPM, Financials, Procurement ou qualquer outro módulo sem duplicar a raspagem.

### Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py collect-module \
  --module-id common \
  --module-name "Applications Common" \
  --module-url "https://docs.oracle.com/en/cloud/saas/applications-common/26b/oedma/index.html" \
  --output-dir "data/modules/common"
```

### PowerShell/Windows

```powershell
python build_knowledge_base.py collect-module `
  --module-id common `
  --module-name "Applications Common" `
  --module-url "https://docs.oracle.com/en/cloud/saas/applications-common/26b/oedma/index.html" `
  --output-dir "data/modules/common"
```

---

## 3. Incluir guias funcionais, OTBI e REST

Essas fontes são opcionais. Informe somente as URLs necessárias para a demanda.

`--functional-url` pode ser repetido.

### Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py collect-module \
  --module-id ppm \
  --module-name "Project Management" \
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" \
  --functional-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oapfm/toc.htm" \
  --functional-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oapjf/toc.htm" \
  --otbi-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/faopm/toc.htm" \
  --rest-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/fapap/toc.htm" \
  --output-dir "data/modules/ppm"
```

### PowerShell/Windows

```powershell
python build_knowledge_base.py collect-module `
  --module-id ppm `
  --module-name "Project Management" `
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" `
  --functional-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oapfm/toc.htm" `
  --functional-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oapjf/toc.htm" `
  --otbi-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/faopm/toc.htm" `
  --rest-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/fapap/toc.htm" `
  --output-dir "data/modules/ppm"
```

### Teste limitado em Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py collect-module \
  --module-id ppm \
  --module-name "Project Management" \
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" \
  --functional-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oapfm/toc.htm" \
  --otbi-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/faopm/toc.htm" \
  --rest-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/fapap/toc.htm" \
  --max-functional-pages-per-guide 5 \
  --max-otbi-pages 20 \
  --max-rest-pages 30 \
  --output-dir "data/modules/ppm"
```

### Teste limitado em PowerShell/Windows

```powershell
python build_knowledge_base.py collect-module `
  --module-id ppm `
  --module-name "Project Management" `
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" `
  --functional-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oapfm/toc.htm" `
  --otbi-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/faopm/toc.htm" `
  --rest-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/fapap/toc.htm" `
  --max-functional-pages-per-guide 5 `
  --max-otbi-pages 20 `
  --max-rest-pages 30 `
  --output-dir "data/modules/ppm"
```

---

## 4. Coletar somente o catálogo físico com `gerar_skills.py`

O scraper físico também pode ser executado diretamente.

### Bash/Linux/macOS/WSL/Git Bash

```bash
python gerar_skills.py \
  --module-id ppm \
  --module-name "Project Management" \
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" \
  --output-dir "data/modules/ppm"
```

### PowerShell/Windows

```powershell
python gerar_skills.py `
  --module-id ppm `
  --module-name "Project Management" `
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" `
  --output-dir "data/modules/ppm"
```

### Controlar o nome completo do arquivo em Bash/Linux/macOS/WSL/Git Bash

```bash
python gerar_skills.py \
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" \
  --module-id ppm \
  --output "C:/oracle-kb/ppm/manifest.json"
```

### Controlar o nome completo do arquivo em PowerShell/Windows

```powershell
python gerar_skills.py `
  --module-url "https://docs.oracle.com/en/cloud/saas/project-management/26b/oedpp/index.html" `
  --module-id ppm `
  --output "C:/oracle-kb/ppm/manifest.json"
```

---

## 5. Combinar todos os módulos coletados

Para combinar automaticamente todas as subpastas de `data/modules`.

### Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py link \
  --modules-root "data/modules" \
  --output "data/graph/fusion_combined.json"
```

### PowerShell/Windows

```powershell
python build_knowledge_base.py link `
  --modules-root "data/modules" `
  --output "data/graph/fusion_combined.json"
```

O linker procura, em cada diretório de módulo:

```text
physical/manifest.json
functional/fragments.jsonl
otbi/catalog.json
rest/catalog.json
rules/validated_rules.json
config/entity_aliases.json
```

Arquivos ausentes são ignorados.

Também é possível escolher os módulos explicitamente.

### Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py link \
  --module-dir "data/modules/ppm" \
  --module-dir "data/modules/common" \
  --output "data/graph/ppm_common.json"
```

### PowerShell/Windows

```powershell
python build_knowledge_base.py link `
  --module-dir "data/modules/ppm" `
  --module-dir "data/modules/common" `
  --output "data/graph/ppm_common.json"
```

---

## 6. Como os relacionamentos entre módulos são resolvidos

Os manifestos físicos são carregados primeiro. Depois disso, o linker resolve todas as foreign keys pendentes.

Isso permite o seguinte cenário:

```text
PPM.PJF_PROJECTS_ALL_B
        ↓ BUSINESS_UNIT_ID
COMMON.FUN_ALL_BUSINESS_UNITS_V
```

A ordem dos arquivos não importa. A tabela de origem pode ser carregada antes da tabela de destino.

Se uma mesma tabela aparecer em mais de um catálogo, o grafo mantém um único nó físico e agrega as fontes e os módulos onde ela foi encontrada.

---

## 7. Pesquisar em todos os módulos

Sem `--module`, a busca considera todos os módulos do grafo e pode atravessar relacionamentos entre eles.

### Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py search \
  --graph "data/graph/fusion_combined.json" \
  --query "quais projetos estão sem orçamento aprovado" \
  --context \
  --limit 20 \
  --max-characters 14000
```

### PowerShell/Windows

```powershell
python build_knowledge_base.py search `
  --graph "data/graph/fusion_combined.json" `
  --query "quais projetos estão sem orçamento aprovado" `
  --context `
  --limit 20 `
  --max-characters 14000
```

---

## 8. Restringir a pesquisa a um módulo

### Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py search \
  --graph "data/graph/fusion_combined.json" \
  --query "orçamento aprovado" \
  --module ppm \
  --context
```

### PowerShell/Windows

```powershell
python build_knowledge_base.py search `
  --graph "data/graph/fusion_combined.json" `
  --query "orçamento aprovado" `
  --module ppm `
  --context
```

O parâmetro `--module` pode ser repetido.

### Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py search \
  --graph "data/graph/fusion_combined.json" \
  --query "projeto e unidade de negócio" \
  --module ppm \
  --module common \
  --context
```

### PowerShell/Windows

```powershell
python build_knowledge_base.py search `
  --graph "data/graph/fusion_combined.json" `
  --query "projeto e unidade de negócio" `
  --module ppm `
  --module common `
  --context
```

---

## 9. Adicionar regras e aliases específicos

Regras e aliases podem existir dentro do diretório de cada módulo:

```text
data/modules/ppm/rules/validated_rules.json
data/modules/ppm/config/entity_aliases.json
```

Ao executar `link --modules-root`, os arquivos específicos de cada módulo são combinados.

Para incluir também os arquivos curados da raiz do projeto.

### Bash/Linux/macOS/WSL/Git Bash

```bash
python build_knowledge_base.py link \
  --modules-root "data/modules" \
  --include-default-curation \
  --output "data/graph/fusion_combined.json"
```

### PowerShell/Windows

```powershell
python build_knowledge_base.py link `
  --modules-root "data/modules" `
  --include-default-curation `
  --output "data/graph/fusion_combined.json"
```

Também é possível informar arquivos curados explicitamente com `--validated-rules` e `--entity-aliases`.

---

## 10. Fluxo recomendado

```text
1. Coletar Applications Common uma vez
2. Coletar o módulo solicitado em um diretório próprio
3. Adicionar somente os guias funcionais necessários
4. Construir um grafo com Common + módulo solicitado
5. Executar a busca sobre o grafo combinado
6. Adicionar regras validadas conforme o conhecimento real evoluir
```

---

## Comandos principais

```text
collect-module  Coleta um módulo para um diretório próprio
link            Combina vários módulos em um único grafo
search          Pesquisa no grafo e pode gerar contexto para prompt
```

---

## Ajuda dos comandos

Os comandos abaixo funcionam tanto no Bash quanto no PowerShell:

```bash
python build_knowledge_base.py --help
python build_knowledge_base.py collect-module --help
python build_knowledge_base.py link --help
python build_knowledge_base.py search --help
```
