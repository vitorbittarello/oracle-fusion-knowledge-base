# Manual do Usuário

## Oracle Fusion Knowledge Base

Este manual explica como usar a ferramenta para localizar documentação relevante do Oracle Fusion Cloud Applications e produzir um contexto mais limpo para análise funcional, descoberta de fontes e geração de SQL.

Os exemplos de execução são apresentados em **Bash** e **PowerShell**. No Bash, a continuação de linha usa `\`; no PowerShell, usa crase (`` ` ``).

---

## 1. Para que serve

A ferramenta ajuda a responder perguntas como:

- onde determinado dado está documentado;
- quais tabelas e colunas são candidatas;
- quais relacionamentos podem ligar os objetos;
- qual subject area do OTBI trata do assunto;
- qual recurso REST pode atender à necessidade;
- quais regras já foram validadas;
- qual é o grão documentado;
- quais pontos ainda precisam de validação funcional.

Ela não envia a documentação completa do módulo para uma LLM. Primeiro seleciona uma rota e depois recupera apenas as evidências mais pertinentes.

---

## 2. O que a ferramenta não faz

A ferramenta:

- não consulta dados transacionais;
- não executa SQL no banco;
- não substitui o BI Publisher, o OTBI ou o Oracle Fusion;
- não garante que uma inferência seja uma regra de negócio;
- não conhece customizações que não tenham sido documentadas;
- não elimina a necessidade de validação funcional;
- não deve inventar joins, filtros ou significados de códigos.

O resultado é um **contexto de apoio**, não uma homologação automática.

---

## 3. Como o conhecimento está organizado

A documentação é separada em grafos com papéis diferentes.

### Negócio

```text
business.json
```

Reúne:

- entidades;
- atributos;
- regras validadas;
- trechos funcionais.

### Modelo físico

```text
physical.json
```

Reúne:

- tabelas;
- views;
- colunas;
- chaves;
- relacionamentos;
- grão documentado.

### OTBI analítico

```text
otbi_analytics.json
```

Reúne:

- subject areas;
- perguntas de negócio;
- páginas analíticas.

### Segurança OTBI

```text
otbi_security.json
```

Reúne:

- job roles;
- duty roles;
- privilégios;
- páginas de segurança.

Essa camada não entra na busca padrão de datasets e SQL.

### REST

```text
rest.json
```

Reúne:

- recursos;
- operações;
- endpoints;
- parâmetros e atributos.

### Grafo mestre

```text
master_graph.json
```

É um mapa pequeno com as principais interseções entre negócio, modelo físico, OTBI e REST.

---

## 4. Como funciona uma busca

A busca federada segue este caminho:

```text
pergunta de negócio
        ↓
conceitos reconhecidos no master
        ↓
pontes documentadas ou curadas
        ↓
tabelas, colunas, subject areas e recursos REST
        ↓
expansão local
        ↓
contexto final
```

Exemplo:

```text
Condições de pagamento
        ↓
Payment Terms
        ↓
PO_HEADERS_ALL.TERMS_ID
        ↓
tabela referenciada de condições de pagamento
```

A ferramenta evita percorrer toda a documentação apenas porque um texto contém palavras parecidas.

---

## 5. Quando usar

Use a ferramenta no início ou durante uma análise quando precisar:

- descobrir fontes candidatas;
- validar se uma tabela está ligada ao assunto;
- encontrar colunas de valor, status, datas ou identificadores;
- verificar referências físicas;
- localizar subject areas;
- localizar recursos REST;
- criar um contexto para uma IA gerar ou revisar SQL;
- registrar uma regra validada para uso futuro.

---

## 6. Como formular uma boa pergunta

Inclua, sempre que possível:

- módulo;
- processo de negócio;
- objeto principal;
- campos desejados;
- grão esperado;
- histórico ou posição atual;
- tipo de fonte procurada;
- resultado desejado.

### Pergunta fraca

```text
qual tabela usar
```

### Pergunta melhor

```text
no Procurement, quais tabelas e colunas representam acordo de compra, fornecedor, valor liberado e condições de pagamento
```

### Pergunta ainda melhor

```text
preciso de um dataset com uma linha por acordo de compra contendo fornecedor, valor do acordo, valor liberado, moeda, status, vigência e condição de pagamento
```

---

## 7. Exemplos de perguntas

### Project Management

```text
quais tabelas e regras identificam o orçamento aprovado vigente de cada projeto
```

```text
quais projetos não possuem orçamento aprovado vigente
```

### Procurement

```text
quais fontes representam acordo de compra, fornecedor, valor liberado e condição de pagamento
```

```text
como relacionar uma requisição com a ordem de compra correspondente
```

### SCM — Manufacturing

```text
qual tabela armazena ordens de produção e qual coluna identifica o número da ordem
```

### SCM — Product Management

```text
quais tabelas representam itens por organização e qual coluna contém o número do item
```

### SCM — Inventory

```text
qual recurso REST consulta disponibilidade de itens por subinventário
```

### OTBI

```text
qual subject area permite analisar custos reais e estimados por ordem de produção
```

---

## 8. Executar uma busca federada

Antes da execução, configure UTF-8:

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

### Exemplo de Procurement

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

### Exemplo de SCM

#### Bash

```bash
python build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/scm" \
  --query "quais tabelas e colunas representam itens por organização e o número do item" \
  --module "scm" \
  --limit 20 \
  --max-characters 14000 \
  > "./resultado_scm_itens.json"
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
    -FilePath ".\resultado_scm_itens.json" `
    -Encoding utf8
```

O arquivo JSON salvo contém o contexto e as evidências usadas.

---

## 9. Como interpretar o resultado

O resultado possui cinco partes principais.

### `query`

Pergunta enviada.

### `context`

Texto pronto para análise ou uso em uma conversa com IA.

### `results`

Lista das evidências escolhidas.

Cada resultado pode informar:

- título;
- tipo;
- score;
- resumo;
- fonte;
- módulo;
- evidência estruturada;
- link para documentação oficial.

### `characters`

Tamanho real do contexto. Ele deve respeitar o limite definido em:

```text
--max-characters
```

### `routing`

Mostra como o orquestrador chegou ao resultado.

Campos importantes:

- `master_business_seeds`: conceitos de negócio reconhecidos;
- `master_routes`: pontes explícitas usadas;
- `semantic_fallback_roots`: entradas escolhidas por similaridade quando não existia curadoria;
- `candidate_count`: quantidade de candidatos analisados antes do corte final.

---

## 10. Como avaliar a qualidade do roteamento

### Resultado mais confiável

O bloco `routing` mostra:

- uma entidade ou atributo reconhecido;
- uma ponte explícita;
- uma tabela, coluna, subject area ou recurso coerente.

Exemplo conceitual:

```text
Payment Terms
→ PO_HEADERS_ALL.TERMS_ID
```

### Resultado que precisa de mais revisão

O bloco contém apenas:

```text
semantic_fallback_roots
```

Isso não significa que o resultado esteja errado. Significa que o módulo ainda não possuía uma ponte curada e a ferramenta escolheu pontos de entrada por semelhança semântica.

Nesse caso:

1. revise as evidências;
2. valide a resposta;
3. registre o conhecimento confirmado;
4. gere os grafos novamente.

---

## 11. Confiança das informações

### Alta confiança

- regra validada no ambiente;
- relacionamento documentado;
- chave documentada;
- descrição oficial;
- mapeamento explícito de entidade ou atributo.

### Média confiança

- inferência apoiada por nome e descrição;
- associação semântica dentro de uma camada;
- referência incompleta ou tabela stub.

### Baixa confiança

- semelhança textual isolada;
- ausência de mapeamento;
- conclusão não confirmada;
- código de status sem lookup ou documentação.

A resposta final deve diferenciar fatos documentados de inferências.

---

## 12. Contexto não é resposta final

Quando o sistema retorna um contexto, ele está dizendo:

> Estas são as evidências selecionadas para responder à pergunta.

Ele ainda não está:

- executando a consulta;
- validando o resultado com dados reais;
- confirmando uma regra funcional;
- garantindo que não existam customizações.

O contexto deve ser usado para apoiar a próxima etapa.

---

## 13. Usar o contexto com uma IA

Depois de executar a busca, abra o JSON e copie o campo `context`.

Exemplo de solicitação:

```text
Atue como especialista em Oracle Fusion Cloud Applications.

Com base somente no contexto abaixo:
1. identifique as fontes candidatas;
2. diferencie documentação oficial, regra validada e inferência;
3. proponha joins apenas quando houver evidência;
4. alerte sobre granularidade e duplicidade;
5. não invente colunas;
6. gere a SQL somente depois de explicar as incertezas.

[cole aqui o conteúdo de context]
```

Isso reduz o risco de a IA inventar tabela, coluna, join ou regra.

---

## 14. Cuidados ao interpretar tabelas e colunas

### `OBJECT_VERSION_NUMBER`

Normalmente representa controle técnico de concorrência. Não deve ser usado automaticamente como versão de negócio.

### Chave primária

Indica unicidade física, mas não necessariamente o grão desejado no dataset.

### Campos de status

O nome do campo não explica sozinho o significado dos códigos.

### Flags

Nem toda coluna terminada em `_FLAG` usa somente `Y` e `N`.

### Datas

É necessário distinguir:

- criação;
- atualização;
- aprovação;
- vigência;
- processamento;
- data contábil;
- data do documento.

### Valores monetários

Valide:

- moeda;
- nível de cabeçalho ou linha;
- valor original;
- valor liberado;
- valor comprometido;
- conversão cambial.

---

## 15. Registrar uma regra validada

Quando uma análise for confirmada, registre:

- pergunta;
- módulo;
- entidade;
- tabelas;
- colunas;
- joins;
- filtros;
- grão;
- ranking;
- SQL validada;
- ambiente;
- responsável;
- data da validação.

O arquivo por módulo fica em:

```text
data/modules/<module_id>/rules/validated_rules.json
```

Depois da alteração, gere novamente os grafos.

Exemplo:

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

## 16. Registrar aliases e mapeamentos

Aliases ligam a linguagem do usuário aos objetos técnicos.

Exemplos:

```text
acordo de compra
purchase agreement
blanket agreement
```

```text
valor liberado
released amount
amount released
```

O arquivo por módulo fica em:

```text
data/modules/<module_id>/config/entity_aliases.json
```

Um alias não deve criar centenas de relações por simples ocorrência textual. Ele deve apontar para uma entidade ou atributo e, quando conhecido, para objetos técnicos específicos.

---

## 17. Coletar ou atualizar um módulo

Essa atividade normalmente é técnica, mas pode ser acompanhada pelo responsável funcional.

### Coleta de SCM

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

### Continuar depois de interrupção

Quando o manifesto físico já estiver completo:

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

Não apague o diretório do módulo nem o cache durante uma continuação normal.

---

## 18. Gerar os grafos de um módulo

### SCM isolado

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

### Procurement com Common

#### Bash

```bash
rm -rf "./data/graph/procurement_common"

python build_knowledge_base.py link \
  --module-dir "./data/modules/procurement" \
  --module-dir "./data/modules/common" \
  --include-default-curation \
  --output-dir "./data/graph/procurement_common"
```

#### PowerShell

```powershell
if (Test-Path ".\data\graph\procurement_common") {
    Remove-Item `
      -Path ".\data\graph\procurement_common" `
      -Recurse `
      -Force
}

& ".\.venv\Scripts\python.exe" build_knowledge_base.py link `
  --module-dir ".\data\modules\procurement" `
  --module-dir ".\data\modules\common" `
  --include-default-curation `
  --output-dir ".\data\graph\procurement_common"
```

### Todos os módulos

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

---

## 19. Testar um módulo sem viés de confirmação

Para verificar se a ferramenta não está ajustada somente ao caso usado durante o desenvolvimento:

1. colete um módulo diferente;
2. gere um bundle isolado;
3. não inclua curadoria de outro módulo;
4. faça perguntas de áreas distintas;
5. analise o `routing`;
6. compare respostas técnicas, OTBI e REST;
7. valide se resultados úteis aparecem sem aliases específicos.

Exemplo de conjunto de testes SCM:

#### Bash

```bash
python build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/scm" \
  --query "qual tabela armazena ordens de produção e qual coluna identifica o número da ordem" \
  --module "scm" \
  --limit 20 \
  --max-characters 14000 \
  > "./resultado_scm_manufacturing.json"

python build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/scm" \
  --query "quais tabelas e colunas representam itens por organização e o número do item" \
  --module "scm" \
  --limit 20 \
  --max-characters 14000 \
  > "./resultado_scm_product_management.json"

python build_knowledge_base.py search-federated \
  --graph-dir "./data/graph/scm" \
  --query "qual subject area permite analisar custos estimados e reais por ordem de produção" \
  --module "scm" \
  --limit 20 \
  --max-characters 14000 \
  > "./resultado_scm_costing_otbi.json"

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
  --query "qual tabela armazena ordens de produção e qual coluna identifica o número da ordem" `
  --module "scm" `
  --limit 20 `
  --max-characters 14000 |
  Out-File `
    -FilePath ".\resultado_scm_manufacturing.json" `
    -Encoding utf8

& ".\.venv\Scripts\python.exe" build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\scm" `
  --query "quais tabelas e colunas representam itens por organização e o número do item" `
  --module "scm" `
  --limit 20 `
  --max-characters 14000 |
  Out-File `
    -FilePath ".\resultado_scm_product_management.json" `
    -Encoding utf8

& ".\.venv\Scripts\python.exe" build_knowledge_base.py search-federated `
  --graph-dir ".\data\graph\scm" `
  --query "qual subject area permite analisar custos estimados e reais por ordem de produção" `
  --module "scm" `
  --limit 20 `
  --max-characters 14000 |
  Out-File `
    -FilePath ".\resultado_scm_costing_otbi.json" `
    -Encoding utf8

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

---

## 20. Problemas comuns

### Resultado vazio em busca direta

Uma pergunta em português pode não encontrar um termo técnico em inglês na primeira etapa lexical. Use a busca federada.

### `master_graph.json` vazio

O módulo não possui curadoria. O orquestrador usa fallback semântico. Verifique `semantic_fallback_roots`.

### Resultado tecnicamente parecido, mas funcionalmente inadequado

A similaridade textual não substitui mapeamento. Valide a função da tabela, coluna ou subject area.

### Contexto muito grande

Reduza:

```text
--limit
```

ou:

```text
--max-characters
```

### Caracteres corrompidos

Configure UTF-8 e salve com:

#### Bash

```bash
> arquivo.json
```

#### PowerShell

```powershell
Out-File -Encoding utf8
```

### Fonte ausente

Verifique se o arquivo correspondente existe:

```text
physical/manifest.json
functional/fragments.jsonl
otbi/catalog.json
rest/catalog.json
```

---

## 21. Glossário

### Entidade de negócio

Objeto reconhecido pelo usuário, como projeto, orçamento, fornecedor, acordo ou ordem de produção.

### Atributo de negócio

Informação associada à entidade, como valor liberado, status ou condição de pagamento.

### Grafo mestre

Mapa compacto que liga conceitos de negócio aos principais pontos dos grafos especializados.

### Ponte explícita

Relacionamento cadastrado ou documentado entre dois objetos.

### Fallback semântico

Ponto de entrada escolhido por similaridade quando não existe ponte explícita.

### Tabela stub

Representação de uma tabela referenciada, mas ainda não coletada no catálogo físico.

### Grão

Nível de detalhe do resultado, como uma linha por acordo, projeto, item ou ordem.

### OTBI

Camada analítica do Oracle Fusion organizada em subject areas.

### REST

Interface de serviços organizada em recursos e operações.

### Regra validada

Conhecimento confirmado pela equipe e registrado para reutilização.

---

## 22. Fluxo recomendado

```text
pergunta de negócio
        ↓
busca federada
        ↓
revisão de context, results e routing
        ↓
resposta ou SQL candidata
        ↓
validação com documentação e dados reais
        ↓
registro de aliases e regras confirmadas
        ↓
nova geração dos grafos
```

A ferramenta melhora à medida que o conhecimento validado é registrado, sem transformar simples semelhanças textuais em relações permanentes.
