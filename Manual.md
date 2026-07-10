# Manual do Usuário

## Oracle Fusion Knowledge Base

Este manual explica como usar a ferramenta para localizar documentação relevante do Oracle Fusion Cloud Applications e produzir um contexto mais limpo para análise funcional, descoberta de fontes e geração de SQL.

Os exemplos de execução são apresentados em **Bash** e **PowerShell**. No Bash, a continuação de linha usa `\`; no PowerShell, usa crase (`` ` ``).

> [!IMPORTANT]
> Esta é uma **ferramenta independente e não oficial**. Ela não é um produto Oracle, não possui suporte oficial da Oracle e não substitui o **Analista Funcional Oracle Fusion**, a validação técnica nem a homologação com dados reais. Seu objetivo é acelerar a descoberta de fontes e apoiar a construção de pipelines de extração de dados do Oracle Fusion Cloud Applications.
>
> Antes de executar qualquer comando, aplique a [configuração UTF-8 obrigatória](#configuração-utf-8-obrigatória). Consulte também o [artigo do autor no Medium sobre o projeto](https://medium.com/p/4d524bfea5fa).

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

A responsabilidade final permanece distribuída entre:

- **Analista Funcional:** confirma significado, configuração, status, filtros e comportamento do processo;
- **Engenharia de Dados:** valida fonte, join, grão, performance, incrementalidade e qualidade da extração;
- **Governança e Segurança:** valida acesso, uso, retenção e exposição dos dados;
- **Ferramenta:** organiza evidências, restringe a busca, registra lacunas e acelera o trabalho dessas equipes.

---

## Configuração UTF-8 obrigatória

O projeto, os catálogos e os resultados JSON usam UTF-8. Configure o terminal antes de coletar, indexar ou pesquisar.

### Bash

```bash
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
```

### PowerShell

```powershell
chcp 65001 > $null

$utf8 = [System.Text.UTF8Encoding]::new($false)

[Console]::InputEncoding  = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

No Windows, execute o Python com `-X utf8` nos comandos do projeto. PowerShell 7+ é recomendado. O PowerShell 5.1 é suportado desde que a sessão seja configurada como acima.

> [!WARNING]
> Apenas usar `Out-File -Encoding utf8` não é suficiente. O comando grava em UTF-8, mas o pipeline pode já ter interpretado incorretamente a saída do Python. O sintoma típico é `Aquisição` aparecer como `AquisiþÒo`.

### Validar o ambiente

#### Bash

```bash
python -X utf8 -c 'import json; print(json.dumps({"texto": "Aquisição, Descrição, Não, inferência"}, ensure_ascii=False))' \
  > "./teste_utf8.json"
cat "./teste_utf8.json"
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

O texto deve permanecer exatamente igual. Se o primeiro teste do Python for correto e o arquivo gerado pelo pipeline estiver corrompido, o problema está na sessão do PowerShell, não na base de conhecimento.

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
- processos e domínios organizacionais;
- aliases em português e inglês;
- regras validadas;
- trechos funcionais;
- referências curadas a objetos técnicos.

A organização funcional da companhia não precisa coincidir com os módulos do ERP. Um processo de Suprimentos, por exemplo, pode usar simultaneamente um sistema especialista, uma integração OCI, tabelas Oracle Fusion Procurement e uma view Gold do lake.

Exemplo:

```text
Suprimentos / Leilão de fornecedores
├── OCY_AUCTION_HEADER
└── PON_AUCTION_HEADERS_ALL
```

A relação entre os objetos deve indicar se há integração, derivação, materialização ou autoridade. Não considere dois objetos equivalentes apenas porque possuem nomes ou campos parecidos.

Os owners reais dos schemas podem mudar entre ambientes. O conhecimento deve usar um papel lógico, como `gold`, `fusion_silver` ou `specialized_source`, mantendo o owner físico apenas para rastreabilidade.

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

- similaridade semântica sem ponte explícita;
- nome parecido;
- inferência por descrição;
- tabela ou coluna sem função validada;
- hipótese de negócio ainda não confirmada.

### Estados de resolução

| Estado | Interpretação | Ação |
|---|---|---|
| `resolved` | evidência forte e caminho compatível | revisar e validar com dados |
| `partial` | há ID ou parte do caminho | completar join e descrição |
| `ambiguous` | mais de um candidato ou apenas sugestão | curar ou validar funcionalmente |
| `unresolved` | não há candidato seguro | coletar ou registrar conhecimento |

### Regra de ouro

```text
mapeamento curado ou regra validada
  > nome técnico exato
  > evidência documental
  > lexical forte
  > similaridade semântica
```

A similaridade semântica serve para sugerir onde investigar. Ela não transforma uma coluna em verdade funcional.

---

## Como obter alta assertividade

Uma busca de alta qualidade combina quatro dimensões:

1. **escopo estrutural correto:** módulo, entidade e comunidade;
2. **atributos curados:** aliases e colunas qualificadas;
3. **caminhos completos:** joins, descrições e cardinalidades;
4. **regras validadas:** filtros, status, vigência, ranking e grão.

### O que deve ser curado

- termos funcionais em português e inglês;
- entidade canônica;
- atributo canônico;
- tabela e coluna qualificadas;
- relacionamento até nomes descritivos;
- regra de seleção;
- grão;
- fonte e confiança;
- diferenças da implantação.

### Conhecimento Oracle padrão

É reutilizável entre ambientes quando sustentado por documentação oficial. Exemplos candidatos para Acordos de Compra:

```text
Acordo            → PO_HEADERS_ALL.SEGMENT1
Moeda             → PO_HEADERS_ALL.CURRENCY_CODE
Data Inicial      → PO_HEADERS_ALL.START_DATE
Data Final        → PO_HEADERS_ALL.END_DATE
Data da Criação   → PO_HEADERS_ALL.CREATION_DATE
Valor Liberado    → PO_HEADERS_ALL.AMOUNT_RELEASED
```

Mesmo nesses casos, valide release, tipo de documento e definição funcional solicitada.

### Conhecimento específico da implantação

Precisa da participação do Analista Funcional e das equipes responsáveis:

- DFF, EFF e atributos customizados;
- configurações ADF;
- significado local de códigos;
- tipos de acordo efetivamente usados;
- filtros de BU, status ou vigência;
- origem oficial de valores;
- views Gold e integrações;
- sistema de autoridade;
- regras de segurança.

### Quando registrar curadoria e quando corrigir código

Registre **curadoria** quando o motor não conhece um significado funcional, uma coluna, um alias, um join, uma regra ou uma característica da implantação.

Abra uma correção de **código** quando:

- evidência curada perde para candidato genérico;
- um candidato desconectado atravessa o gating;
- o grão não é considerado;
- o mesmo input produz resultado não determinístico;
- o JSON não explica a decisão;
- o processamento não é incremental ou retomável;
- há problema de performance, cache, encoding ou serialização.

Compactação do JSON, redução de candidatos diagnósticos e tuning de thresholds são acabamento do motor. Eles não substituem a curadoria ausente.

### Evidência parcial

Exemplo:

```text
Fornecedor → PO_HEADERS_ALL.VENDOR_ID
```

Isso comprova o identificador, mas não entrega o nome. O resultado deve ser `partial` até existir caminho validado até a coluna descritiva.

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

A curadoria deve ser registrada em arquivos versionados. Evite criar regras específicas no código para responder um único caso.

### Exemplo de atributo curado

No arquivo:

```text
data/modules/<module_id>/config/entity_aliases.json
```

registre o atributo dentro da entidade:

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

O linker criará o atributo e o mapeamento para a coluna quando a coluna existir no grafo físico.

### Informações que devem acompanhar a curadoria

- origem da evidência;
- release Oracle;
- módulo;
- ambiente validado;
- pessoa ou equipe que validou;
- data da validação;
- confiança;
- grão;
- observações sobre configuração local.

### Evitar aliases excessivamente genéricos

Termos como `valor`, `data`, `status` e `descrição` podem existir em centenas de colunas. Prefira aliases contextualizados:

```text
valor do acordo
status do acordo
data inicial da vigência
data de criação do acordo
```

O alias genérico pode permanecer como termo de entrada, mas a promoção da coluna deve depender de entidade, mapeamento ou regra validada.

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

Também podem ser registrados como fontes de conhecimento corporativo:

- views Gold e seus aliases de colunas;
- SQLs validadas;
- mapeamentos de integração OCI;
- objetos de sistemas especialistas;
- sistema de autoridade de cada processo;
- relações entre origem, destino e materialização no lake.

Exemplo:

```text
Domínio: Suprimentos
Entidade: Leilão de fornecedores
Sistema especialista: OCY_AUCTION_HEADER
Oracle Fusion: PON_AUCTION_HEADERS_ALL
Relação validada: integrates_with
```

O owner real deve ser abstraído por um papel lógico. Assim, a mesma curadoria pode ser usada em desenvolvimento, homologação e produção sem transformar o nome do schema em parte do significado do objeto.

Nesta versão, essas informações são registradas por curadoria. A leitura automática de DDLs Gold e configurações de integração ainda não faz parte dos comandos disponíveis.

### Publicar e validar a curadoria

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

A normalização e a vetorização são incrementais. Execute-as quando a curadoria criar ou alterar textos, aliases e atributos. Alterações apenas no algoritmo, ranking ou formato do JSON não exigem reprocessamento do corpus.

### Checklist de revisão funcional

Antes de aprovar uma curadoria:

- o atributo representa exatamente o campo solicitado;
- a coluna existe na release coletada;
- a descrição oficial sustenta o significado;
- o grão da tabela é compatível;
- joins não multiplicam linhas indevidamente;
- IDs possuem caminho até descrições quando necessário;
- filtros e códigos foram validados;
- a origem foi registrada;
- existe teste de regressão;
- nenhum candidato proibido foi promovido.

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

Volte à seção [Configuração UTF-8 obrigatória](#configuração-utf-8-obrigatória). Não tente reparar o texto no Query Planner antes de validar a fronteira entre Python e o terminal.

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

Confirme no JSON:

```json
{
  "encoding_status": "valid",
  "corrections": [],
  "ambiguous_markers": []
}
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

A ferramenta melhora à medida que o conhecimento validado é registrado, sem transformar simples semelhanças textuais em relações permanentes. Views Gold e integrações corporativas podem enriquecer a camada de negócio, mas suas relações devem manter escopo, origem e confiança.

---

## Índices separados e atualização do conhecimento

Cada camada possui seu próprio índice:

```text
search_index/
├── index_bundle.json
├── master.sqlite
├── business.sqlite
├── physical.sqlite
├── otbi_analytics.sqlite
├── otbi_security.sqlite
└── rest.sqlite
```

A primeira criação é feita com:

### Bash

```bash
python -u build_knowledge_base.py build-index \
  --graph-dir "./data/graph/scm"
```

### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\scm"
```

O índice monolítico anterior pode ser usado como origem para reaproveitar embeddings durante a migração.

Quando a alteração for apenas em aliases e rotas de negócio, reconstrua somente o master:

### Bash

```bash
python build_knowledge_base.py build-index \
  --graph-dir "./data/graph/scm" \
  --layer master \
  --layer business
```

### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\scm" `
  --layer master `
  --layer business
```

Sem `--layer`, o sistema verifica todas as camadas e pula as que não mudaram. Dentro da camada alterada, somente nós novos ou cujo conteúdo mudou têm embeddings recalculados.

Escopo usual de rebuild:

| Alteração | Índice afetado |
|---|---|
| Alias, conceito corporativo ou rota para objeto existente | `business` e/ou `master` |
| View Gold usada como conhecimento e linhagem | `business` e `master` |
| Mapeamento OCI entre objetos já indexados | `business` e `master` |
| Nova tabela ou coluna física | `physical` |
| Mudança somente no ranking ou no prompt | nenhum |

Valide depois da atualização:

### Bash

```bash
python build_knowledge_base.py validate-index \
  --graph-dir "./data/graph/scm"
```

### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" build_knowledge_base.py validate-index `
  --graph-dir ".\data\graph\scm"
```

A busca usa o manifesto automaticamente e informa `backend: sqlite_bundle` no bloco `routing`.

### CPU e CUDA

**CUDA não é obrigatório.** Os índices podem ser gerados integralmente em CPU. Esse é o modo padrão quando `--semantic-device cuda` não é informado.

#### CPU — Bash

```bash
python -u build_knowledge_base.py build-index \
  --graph-dir "./data/graph/scm"
```

#### CPU — PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\scm"
```

Quando o computador possui GPU NVIDIA compatível e o ambiente Python possui PyTorch com CUDA, a geração dos embeddings pode ser acelerada:

#### CUDA — Bash

```bash
python -u build_knowledge_base.py build-index \
  --graph-dir "./data/graph/scm" \
  --semantic-device cuda \
  --semantic-batch-size 64
```

#### CUDA — PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -u build_knowledge_base.py build-index `
  --graph-dir ".\data\graph\scm" `
  --semantic-device "cuda" `
  --semantic-batch-size 64
```

CUDA acelera a inferência do modelo semântico. A gravação SQLite, o FTS5, os hashes e a validação continuam na CPU. Em máquinas sem CUDA, basta omitir a opção. O suporte a GPU melhora a performance, mas não altera o conteúdo funcional do índice nem é requisito para usar o projeto.

## Preparar o corpus semântico antes dos índices

A etapa de normalização é executada separadamente da vetorização.

### Bash

```bash
python -X utf8 -u build_knowledge_base.py normalize-index \
  --graph-dir "./data/graph/fusion_modules" \
  --batch-size 1000 \
  --checkpoint-percent 1
```

### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 -u build_knowledge_base.py normalize-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --batch-size 1000 `
  --checkpoint-percent 1
```

Exemplo de acompanhamento:

```text
[NORMALIZE] 45000/187793 nós (23.96%) | segmentos=81240 |
textos únicos=21980 | deduplicados=59260 | reutilizados=12000 |
normalizados=33000 | ETA 00:08:31 | checkpoint persistido.
```

O estado é gravado em
`search_index/semantic_normalization.sqlite`. O manifesto legível fica em
`search_index/semantic_normalization_manifest.json`.

O mesmo comando pode ser interrompido com `Ctrl+C` e executado novamente. A
retomada usa o ordinal do último lote confirmado e não reinicia a normalização.
Em uma nova versão dos grafos, nós com o mesmo documento são reutilizados e
somente conteúdos novos ou alterados são normalizados novamente.

A normalização é conservadora: preserva contexto para campos de negócio e só
compartilha conceitos técnicos explicitamente curados. A construção dos índices
semânticos passará a consumir esse corpus em etapa posterior.

## Vetorizar o corpus normalizado

Após a normalização, execute.

### Bash

```bash
python -X utf8 -u build_knowledge_base.py vectorize-index \
  --graph-dir "./data/graph/fusion_modules" \
  --semantic-model "intfloat/multilingual-e5-base" \
  --semantic-device cpu \
  --semantic-batch-size 32 \
  --checkpoint-percent 1
```

### PowerShell

```powershell
& ".\.venv\Scripts\python.exe" -X utf8 -u build_knowledge_base.py vectorize-index `
  --graph-dir ".\data\graph\fusion_modules" `
  --semantic-model "intfloat/multilingual-e5-base" `
  --semantic-device "cpu" `
  --semantic-batch-size 32 `
  --checkpoint-percent 1
```

O comando consome `search_index/semantic_normalization.sqlite` e grava os
vetores em `search_index/semantic_embeddings.sqlite`, com manifesto em
`search_index/semantic_embeddings_manifest.json`.

O perfil recomendado para uso local é `intfloat/multilingual-e5-base`, com 768 dimensões, normalização L2 e persistência `float32`. O perfil `intfloat/multilingual-e5-large-instruct`, com 1.024 dimensões, pode ser usado quando houver capacidade computacional compatível. Cada lote é confirmado no SQLite. Em caso de interrupção, execute o mesmo comando novamente para continuar dos textos ainda sem embedding.

Exemplo de log:

```text
[VECTORIZE] 51821/259102 textos (20.00%) | gerados=51821 |
reutilizados=0 | dimensões=1024 | ETA 07:42:10 | checkpoint persistido.
```

## Critérios finais antes de usar o contexto em um pipeline

Não avance diretamente do JSON para produção. Confirme:

- entidade, módulo e comunidade corretos;
- atributos `resolved` sustentados por evidência forte;
- atributos `partial` completados com joins descritivos;
- atributos `ambiguous` e `unresolved` revisados;
- grão do resultado;
- cardinalidade dos joins;
- significado dos status e códigos;
- filtros de configuração da implantação;
- segurança e autorização;
- execução da SQL candidata com amostra real;
- homologação funcional;
- registro da curadoria aprendida.

A ferramenta é um acelerador de engenharia e análise. Ela não substitui o processo de validação necessário para uma extração confiável do Oracle Fusion Cloud Applications.

## Referência externa

- [Artigo do autor no Medium sobre o Oracle Fusion Knowledge Base](https://medium.com/p/4d524bfea5fa)
