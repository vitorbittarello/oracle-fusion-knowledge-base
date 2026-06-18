# Manual do Usuário de Negócio

## Oracle Fusion Knowledge Base

Este manual explica como um usuário de negócio pode usar a ferramenta de apoio ao entendimento do Oracle Fusion Cloud Applications.

A ferramenta não substitui o Oracle Fusion, o OTBI, o BI Publisher ou o banco de dados. Ela funciona como uma base de conhecimento para ajudar a entender tabelas, colunas, relacionamentos, regras e caminhos possíveis para responder perguntas de negócio.

---

## 1. Objetivo da ferramenta

A ferramenta ajuda a responder perguntas como:

- Onde encontro determinada informação no Oracle Fusion?
- Quais tabelas parecem estar relacionadas a um assunto?
- Quais colunas indicam status, datas, valores, versões ou auditoria?
- Existe alguma regra conhecida para identificar registros vigentes?
- Quais módulos podem estar envolvidos em uma análise?
- Como montar um contexto mais confiável para gerar uma consulta SQL?
- Quais documentos oficiais da Oracle ajudam a entender determinado tema?

O foco é apoiar análise, descoberta e entendimento do modelo de dados do Oracle Fusion Cloud Applications.

---

## 2. O que a ferramenta faz

A ferramenta coleta e organiza informações públicas da documentação oficial da Oracle e de regras validadas internamente.

Ela pode usar fontes como:

- dicionário físico de tabelas e views;
- documentação funcional;
- documentação OTBI;
- documentação REST API;
- regras validadas pela equipe;
- aliases de negócio cadastrados no projeto;
- relacionamentos entre tabelas de diferentes módulos.

Com isso, ela monta uma base pesquisável.

---

## 3. O que a ferramenta não faz

A ferramenta não executa consultas no banco de dados.

Ela também não garante, sozinha, que uma resposta esteja 100% correta para o ambiente da empresa.

Ela não substitui validação funcional.

Ela não deve ser usada como fonte única para tomada de decisão sem conferência.

Ela não acessa dados transacionais do Fusion. Ela trabalha com documentação e metadados.

---

## 4. Quando usar

Use a ferramenta quando houver perguntas como:

- "Quais tabelas tratam de orçamento de projeto?"
- "Como identifico a versão aprovada de um orçamento?"
- "Quais tabelas ligam projetos e unidades de negócio?"
- "Onde encontro informações de fornecedores?"
- "Quais campos parecem controlar vigência?"
- "Qual tabela pode conter status de uma requisição?"
- "Quais módulos podem estar envolvidos nessa análise?"
- "Que contexto devo passar para gerar uma SQL mais segura?"

A ferramenta é especialmente útil no início de uma análise, quando ainda não está claro onde procurar.

---

## 5. Como formular boas perguntas

Perguntas boas são objetivas e usam termos de negócio.

### Exemplos bons

```text
quais projetos estão sem orçamento aprovado
```

```text
onde encontro as versões de orçamento de projeto
```

```text
quais tabelas relacionam projeto com unidade de negócio
```

```text
como identificar a versão vigente de um plano financeiro
```

```text
quais campos indicam data de início e fim de validade
```

```text
quais tabelas de fornecedores se relacionam com invoices
```

### Exemplos fracos

```text
me ajuda
```

```text
onde está isso
```

```text
faz uma query
```

```text
qual tabela usar
```

Essas perguntas são vagas demais.

---

## 6. Como melhorar uma pergunta

Inclua sempre que possível:

- o processo de negócio;
- o módulo;
- o objeto principal;
- o resultado esperado;
- se deseja regra vigente, histórico ou último registro;
- se precisa de SQL, explicação ou apenas indicação de tabelas.

### Exemplo ruim

```text
orçamento
```

### Exemplo melhor

```text
quais tabelas e regras ajudam a identificar o orçamento aprovado vigente de cada projeto
```

### Exemplo ainda melhor

```text
quero identificar projetos sem orçamento aprovado vigente no módulo Project Management
```

---

## 7. Tipos de saída esperados

A ferramenta pode retornar um contexto com informações como:

- entidades relacionadas;
- tabelas candidatas;
- colunas importantes;
- relacionamentos;
- regras validadas;
- documentação relacionada;
- grão sugerido;
- possíveis filtros;
- alertas e limitações.

Exemplo de saída esperada:

```text
Entidade relacionada:
- Financial Plan Version

Tabelas:
- PJO_PLAN_VERSIONS_VL
- PJF_PROJECTS_ALL_B

Regra validada:
- PLAN_CLASS_CODE = 'BUDGET'
- PLAN_STATUS_CODE = 'B'
- CURRENT_PLAN_STATUS_FLAG = 'Y'
- PROCESSING_TIME IS NOT NULL

Estratégia sugerida:
- Partir da tabela de projetos
- Verificar ausência de orçamento aprovado com NOT EXISTS
```

Essa saída é um contexto de apoio. Ela pode ser usada por uma pessoa ou por uma IA para gerar uma resposta final mais assertiva.

---

## 8. Contexto não é resposta final

Quando a ferramenta retorna um contexto, ela está dizendo:

> "Estas são as informações mais relevantes que encontrei para responder sua pergunta."

Ela ainda não está necessariamente entregando a resposta final.

Por exemplo, para a pergunta:

```text
quais projetos estão sem orçamento aprovado
```

A ferramenta pode retornar:

- quais tabelas usar;
- quais filtros indicam orçamento aprovado;
- quais relacionamentos existem;
- qual estratégia de consulta usar.

Mas ela não lista os projetos reais, porque não executa consulta no banco.

---

## 9. Como interpretar a confiança

A ferramenta pode classificar informações por origem e confiança.

### Alta confiança

Informações vindas de:

- documentação oficial da Oracle;
- chave primária documentada;
- relacionamento documentado;
- regra validada internamente.

### Média confiança

Informações inferidas a partir de:

- nomes de colunas;
- padrões comuns do Oracle Fusion;
- combinação entre documentação e estrutura física.

### Baixa confiança

Informações sugeridas por:

- semelhança textual;
- termos aproximados;
- aliases incompletos;
- ausência de regra validada.

Sempre dê mais peso a regras validadas e documentação oficial.

---

## 10. Regras validadas

Regras validadas são conhecimentos já confirmados pela equipe.

Exemplo:

```text
Para identificar orçamento aprovado vigente:
- PLAN_CLASS_CODE = 'BUDGET'
- PLAN_STATUS_CODE = 'B'
- CURRENT_PLAN_STATUS_FLAG = 'Y'
- PROCESSING_TIME IS NOT NULL
```

Esse tipo de regra tem mais valor do que uma simples inferência pelo nome da coluna.

Se uma regra foi validada em produção ou homologação, ela deve ser cadastrada no projeto para reaproveitamento futuro.

---

## 11. Múltiplos módulos

O Oracle Fusion é dividido em módulos, mas os dados frequentemente se conectam entre eles.

Exemplos:

- Project Management pode se relacionar com Applications Common;
- Financials pode se relacionar com Procurement;
- Procurement pode se relacionar com Suppliers;
- módulos diferentes podem compartilhar tabelas comuns.

Por isso, a ferramenta permite coletar mais de um módulo e pesquisar entre eles.

O módulo **Applications Common** é importante porque costuma conter estruturas compartilhadas entre vários módulos.

---

## 12. Exemplos de perguntas por módulo

### Project Management

```text
quais tabelas armazenam orçamento de projeto
```

```text
como identificar a versão aprovada do orçamento do projeto
```

```text
quais projetos estão sem orçamento aprovado
```

```text
quais tabelas ligam projeto, tarefa e plano financeiro
```

### Applications Common

```text
quais tabelas comuns armazenam unidades de negócio
```

```text
onde encontro organizações usadas por outros módulos
```

```text
quais tabelas comuns se relacionam com projetos
```

### Financials

```text
quais tabelas armazenam invoices de fornecedores
```

```text
onde encontro status de uma invoice
```

```text
quais campos indicam data contábil e data de criação
```

### Procurement

```text
quais tabelas armazenam requisições de compra
```

```text
como relacionar requisição com ordem de compra
```

```text
onde encontro aprovador de uma requisição
```

---

## 13. Como usar o resultado em uma conversa com IA

Depois de gerar o contexto, ele pode ser usado em uma pergunta para uma IA.

Exemplo:

```text
Com base no contexto abaixo, gere uma SQL Oracle para identificar projetos sem orçamento aprovado vigente.

[cole aqui o contexto gerado pela ferramenta]
```

Isso tende a produzir uma resposta melhor do que perguntar diretamente:

```text
gere uma SQL para projetos sem orçamento aprovado
```

Sem contexto, a IA pode inventar tabela, coluna ou filtro.

---

## 14. Boas práticas

### Use perguntas específicas

Prefira:

```text
como identificar orçamento aprovado vigente por projeto
```

Em vez de:

```text
orçamento
```

### Informe o módulo quando souber

Prefira:

```text
no Project Management, quais tabelas armazenam orçamento aprovado
```

### Peça o tipo de saída desejada

Exemplos:

```text
retorne apenas tabelas candidatas
```

```text
retorne contexto para gerar SQL
```

```text
explique a regra de negócio
```

```text
indique possíveis relacionamentos
```

### Valide regras críticas

Filtros de status, vigência, versão e aprovação devem ser validados com usuário funcional ou consulta já homologada.

---

## 15. Cuidados importantes

### Nem toda coluna com `_FLAG` significa `Y/N`

Algumas flags podem usar outros valores. Verifique a descrição documentada ou dados reais.

### Nem todo campo `STATUS_CODE` é autoexplicativo

O significado dos códigos pode depender de lookup, documentação funcional ou configuração do ambiente.

### `OBJECT_VERSION_NUMBER` não é versão de negócio

Em geral, esse campo é usado para controle técnico de concorrência. Não deve ser usado automaticamente para escolher o registro mais recente do negócio.

### Chave primária não é necessariamente o grão analítico

A chave primária indica unicidade física. O grão de uma análise pode ser diferente.

### Documentação oficial não cobre customizações

A ferramenta não conhece extensões, views customizadas ou regras internas se elas não forem cadastradas.

---

## 16. Glossário rápido

### Entidade de negócio

Objeto compreendido pelo usuário funcional.

Exemplos:

- Projeto;
- Orçamento;
- Fornecedor;
- Invoice;
- Requisição;
- Ordem de compra.

### Tabela física

Tabela ou view documentada pela Oracle.

Exemplo:

```text
PJO_PLAN_VERSIONS_VL
```

### Coluna

Campo dentro de uma tabela.

Exemplo:

```text
PLAN_STATUS_CODE
```

### Regra validada

Regra confirmada por análise técnica, validação funcional ou uso em produção.

### OTBI

Camada analítica do Oracle Fusion usada para relatórios e subject areas.

### REST API

Interface de serviços usada para consultar ou manipular recursos do Oracle Fusion.

### Grão

Nível de detalhe de uma resposta.

Exemplos:

- uma linha por projeto;
- uma linha por projeto e versão;
- uma linha por fornecedor e invoice;
- uma linha por requisição e linha.

### Ranking

Critério usado para escolher um registro entre vários candidatos.

Exemplo:

```text
ORDER BY VERSION_NUMBER DESC, LAST_UPDATE_DATE DESC
```

---

## 17. Fluxo recomendado para o usuário de negócio

1. Defina a pergunta de negócio.
2. Informe o módulo, se souber.
3. Execute ou solicite a busca na ferramenta.
4. Revise as tabelas, regras e alertas retornados.
5. Peça geração de SQL ou explicação usando o contexto.
6. Valide a regra com dados reais.
7. Se a regra estiver correta, peça para cadastrá-la como regra validada.

---

## 18. Exemplo completo

### Pergunta

```text
quais projetos estão sem orçamento aprovado
```

### O que a ferramenta deve ajudar a encontrar

- tabela de projetos;
- tabela de versões de plano financeiro;
- regra para orçamento aprovado;
- relacionamento por `PROJECT_ID`;
- estratégia para identificar ausência.

### Contexto esperado

```text
Projetos:
- PJF_PROJECTS_ALL_B ou view equivalente

Versões de plano:
- PJO_PLAN_VERSIONS_VL

Regra de orçamento aprovado:
- PLAN_CLASS_CODE = 'BUDGET'
- PLAN_STATUS_CODE = 'B'
- CURRENT_PLAN_STATUS_FLAG = 'Y'
- PROCESSING_TIME IS NOT NULL

Estratégia:
- selecionar projetos
- usar NOT EXISTS contra versões de orçamento aprovadas
```

### Uso posterior

Com esse contexto, uma IA ou analista pode montar uma SQL mais segura.

---

## 19. Como reportar melhoria da base

Sempre que uma análise for concluída, registre:

- pergunta original;
- módulo;
- tabelas usadas;
- joins usados;
- filtros validados;
- regra de ranking;
- SQL final;
- validação realizada;
- data da validação;
- responsável pela validação.

Isso transforma conhecimento pontual em conhecimento reutilizável.

---

## 20. Resumo final

A ferramenta serve para acelerar entendimento do Oracle Fusion Cloud Applications.

Ela ajuda a encontrar caminhos, tabelas, regras e evidências.

Ela não substitui validação funcional nem execução no banco.

O melhor uso é:

```text
pergunta de negócio
    ↓
busca de contexto
    ↓
geração de SQL ou explicação
    ↓
validação com dados reais
    ↓
registro da regra validada
```

Quanto mais regras validadas forem cadastradas, mais útil e assertiva a ferramenta se torna.
