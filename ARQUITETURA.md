# PORTULANAS MACRO BOT — Documento de Arquitetura para Auditoria Técnica

**RIVOOS WEALTH · DG**
**Versão deste documento:** 24/06/2026
**Propósito:** este documento descreve a arquitetura completa, decisões de design, e código-fonte do Portulanas Macro Bot, para permitir auditoria de eficiência e qualidade por outras IAs ou desenvolvedores. Não é material de marketing — é documentação técnica honesta, incluindo limitações conhecidas.

---

## 1. Visão geral em uma frase

Um robô que garimpa notícias de mercado financeiro de fontes RSS públicas, filtra por relevância usando regras de palavra-chave (sem custo de IA), analisa as candidatas com um modelo de linguagem (Gemini 2.5 Flash-Lite), e envia alertas formatados para uma lista de assinantes via Telegram, em 8 janelas fixas por dia úteis, mais um resumo do dia anterior pela manhã.

## 2. Por que esta arquitetura, e não outra

### 2.1 Por que GitHub Actions, não um servidor próprio

Não há servidor 24/7 rodando. Todo o sistema é orquestrado por **GitHub Actions** (cron jobs gratuitos), que disparam scripts Python em horários fixos. Isso elimina custo de infraestrutura (servidor, banco de dados gerenciado), mas tem duas limitações importantes:

- **Sem estado em memória entre execuções** — cada execução começa do zero, lendo o estado anterior de arquivos JSON commitados no próprio repositório Git (`seen_cache.json`, `subscribers.json`, `daily_history.json`). Isso funciona bem para o volume atual, mas não escalaria para milhares de assinantes ou alta frequência.
- **Sem capacidade de "escutar" eventos em tempo real** (webhooks) — por isso o reconhecimento de novos assinantes via `/start` do Telegram é feito por **polling** (consulta periódica à API, a cada 15 minutos), não por notificação instantânea.

### 2.2 Por que janelas fixas, não execução contínua

A primeira versão do sistema rodava a cada 15-30 minutos continuamente. Isso foi abandonado por dois motivos documentados durante o desenvolvimento:

1. **Cota de API**: a conta do Gemini em uso está limitada a ~20 requisições/dia (não os 1.500/dia publicados como padrão — causa não identificada, possivelmente relacionada a verificação de conta). Execução contínua excede essa cota rapidamente, mesmo com batching.
2. **Ruído editorial**: rodar a cada 15 minutos significa que, em janelas de baixo volume de notícia real, o sistema processava e enviava notícias de baixa qualidade/relevância só para "preencher" a frequência.

A solução adotada foi reduzir para **8 janelas fixas por dia, em dias de semana**, alinhadas a momentos relevantes do pregão B3 (pré-abertura, pós-abertura, PTAX, fechamento). Cada janela consome **1 chamada de IA via batch processing** (todas as notícias candidatas analisadas numa única chamada), o que mantém o consumo de cota em ~8-10 chamadas/dia, dentro do limite.

### 2.3 Por que filtro de palavra-chave antes da IA

Toda notícia coletada passa primeiro por um filtro barato e determinístico (comparação de string com regras de contexto) antes de qualquer chamada de IA. Isso existe para não gastar a cota limitada de IA analisando notícias claramente irrelevantes (esportes, entretenimento, etc.). O filtro é detalhado na Seção 5.

---

## 3. Componentes do sistema

| Arquivo | Papel | Disparado por |
|---|---|---|
| `portulanas_bot.py` | Motor principal: coleta RSS, filtra, analisa com IA, formata e envia | `garimpo.yml` (8x/dia, seg-sex) |
| `portulanas_subscribers.py` | Detecta novos `/start` no Telegram, mantém lista de assinantes | `assinantes.yml` (a cada 15 min) |
| `portulanas_resumo_diario.py` | Gera resenha do dia anterior a partir do histórico | (workflow pendente de criação — ver Seção 9) |
| `portulanas_panorama.py` | Script legado de panorama/checkpoint horário | **Pausado** — ver Seção 8.1 |

### 3.1 Arquivos de estado (persistidos via commit Git)

| Arquivo | Conteúdo | Escrito por | Lido por |
|---|---|---|---|
| `seen_cache.json` | Hash (título+link) de notícias já processadas, para deduplicação | `portulanas_bot.py` | `portulanas_bot.py` |
| `subscribers.json` | Lista de `chat_id` do Telegram inscritos | `portulanas_subscribers.py` | `portulanas_bot.py`, `portulanas_resumo_diario.py` |
| `telegram_update_offset.json` | Offset da última mensagem processada (API `getUpdates`) | `portulanas_subscribers.py` | `portulanas_subscribers.py` |
| `daily_history.json` | Histórico de análises completas enviadas no dia (título, relevância, canal, origem, leitura crítica) | `portulanas_bot.py` | `portulanas_resumo_diario.py` |

**Risco conhecido**: como o estado é persistido via commit Git, execuções concorrentes (ex: duas execuções do mesmo workflow rodando simultaneamente) podem gerar conflito de push (`non-fast-forward`). Isso já ocorreu em produção (ver Seção 10.3) e foi parcialmente mitigado com `git pull --rebase` antes do push, mas o risco não é zero.

---

## 4. Pipeline completo (função `main()` em `portulanas_bot.py`)

```
1. Coleta RSS de 9 fontes (collect_all_news)
   ↓
2. Filtro de idade: descarta itens publicados há mais de 6h (NEWS_MAX_AGE_HOURS)
   ↓
3. Filtro de deduplicação: descarta itens já no seen_cache.json
   ↓
4. Filtro de relevância por palavra-chave (quick_relevance_check) — ver Seção 5
   ↓
5. Agrupamento por similaridade de título (group_similar_items) — junta notícias
   de fontes diferentes sobre o mesmo fato, usando similaridade de Jaccard
   ↓
6. Corte de segurança: no máximo MAX_GROUPS_PER_RUN (20) grupos por execução
   ↓
7. Análise via IA em UMA ÚNICA chamada batch (analyze_batch_with_gemini)
   ↓
8. Separação: itens classificados como Ações/Mercado (is_stocks_news) vs gerais
   ↓
9. Ordenação hierárquica dos itens gerais: origem (doméstica > ambas > externa)
   > relevância (ALTA > MEDIA > BAIXA)
   ↓
10. Seleção: até TOP_N_GUARANTEED (5) itens gerais + até 5 itens de Ações
   ↓
11. Envio Bloco 1 (mensagens individuais, uma por notícia geral)
   ↓
12. Envio separador visual + resumo consolidado do Bloco 2 (1 chamada de IA extra,
    pequena, para sintetizar várias notícias de Ações em um parágrafo único)
   ↓
13. Envio disclaimer
   ↓
14. Registro de cada item enviado no daily_history.json (para o Resumo do Dia)
   ↓
15. Commit do seen_cache.json atualizado
```

### 4.1 Modo "Janela Fixa" vs modo "Garimpo Padrão"

O código suporta dois modos, controlados pela variável de ambiente `PORTULANAS_JANELA_FIXA`:

- **Janela Fixa** (`=1`, usado em produção hoje): sempre envia o Top N de notícias mais relevantes disponíveis, mesmo que nenhuma seja ALTA/MEDIA — para nunca ficar em silêncio total numa janela fraca.
- **Garimpo Padrão** (`=0`, modo legado, não usado atualmente nos workflows): só envia notícias ALTA/MEDIA, podendo não enviar nada numa janela fraca.

---

## 5. Filtro de relevância por palavra-chave (`quick_relevance_check`)

### 5.1 O bug estrutural mais recorrente do projeto: substring sem word boundary

A causa-raiz de praticamente todos os falsos positivos encontrados durante o desenvolvimento foi **comparação de substring sem respeitar limite de palavra**. Exemplos reais capturados em produção antes da correção:

| Keyword buscada | Encontrada dentro de | Resultado |
|---|---|---|
| `"fed"` | "Polícia **Fed**eral" | Notícia sobre operação policial classificada como relevante por "Fed" |
| `"ira"` (de "Irã") | "escolas **bra**sil**eira**s" | Notícia sobre educação inclusiva classificada como relevante |
| `"acao"` (de "ação") | "infl**acao**" | Toda notícia de inflação contaminava a detecção de "ação" |
| `"win"` | "Brazil hopes to **win** World Cup" | Notícia esportiva classificada como relevante (WIN = mini índice B3) |
| `"vale"` | "isso **vale** a pena" | Qualquer uso comum da palavra "vale" disparava alerta de ações |

**Correção estrutural aplicada**: todas as funções de detecção usam uma função central `keyword_matches(kw, text)`, que aplica regex com `\b` (word boundary) em vez de `in` (substring simples):

```python
def keyword_matches(kw, text_normalized):
    kw_norm = strip_accents(kw)
    return re.search(r"\b" + re.escape(kw_norm) + r"\b", text_normalized) is not None
```

**Lição para auditoria**: qualquer keyword nova adicionada ao sistema deve ser testada contra um conjunto de frases comuns do idioma antes de ir para produção. O padrão de teste usado neste projeto está documentado na Seção 11.

### 5.2 Taxonomia por bloco temático

A lista de keywords é organizada em blocos temáticos (não uma lista solta), cada um representando um domínio de impacto:

| Bloco | Constante no código | Conteúdo |
|---|---|---|
| Câmbio | `BLOCO_CAMBIO` | dólar, real, câmbio, PTAX, DXY, FX, exchange rate |
| Juros | `BLOCO_JUROS` | Selic, Copom, Fed, FOMC, yield, interest rate |
| Inflação | `BLOCO_INFLACAO` | IPCA, CPI, core inflation |
| Emprego | `BLOCO_EMPREGO` | payroll, desemprego, CAGED, salários |
| Risco | `BLOCO_RISCO` | CDS, déficit, dívida pública, rating soberano |
| Derivativos | `BLOCO_DERIVATIVOS` | swap, hedge, rollover |
| Commodities | `BLOCO_COMMODITIES` | petróleo, minério, soja, ouro, cobre |
| Geopolítica | `BLOCO_GEOPOLITICA` | Hormuz/Ormuz, Irã, Trump, Israel, cessar-fogo |
| PIB/Atividade | `BLOCO_PIB_ATIVIDADE` | PIB, GDP |
| Bolsa (índices) | `BLOCO_BOLSA` | Ibovespa, IFIX, IDIV, SMLL, Nasdaq, S&P 500 |
| Empresas BR | `BLOCO_EMPRESAS_BR` | ~55 nomes de empresas do Ibovespa |
| Tickers BR | `BLOCO_TICKERS_BR` | ~52 códigos de negociação (PETR4, VALE3, etc.) |
| Empresas Global | `BLOCO_EMPRESAS_GLOBAL` | Tesla, Nvidia, big techs, bancos americanos |

`HIGH_RELEVANCE_KEYWORDS` é a soma dos blocos de maior prioridade (Câmbio, Juros, Inflação, Emprego, Risco, Derivativos, Commodities, Geopolítica, PIB). `MEDIUM_RELEVANCE_KEYWORDS` cobre Bolsa e Empresas.

### 5.3 Termos de risco (coocorrência obrigatória)

Alguns termos são genuinamente ambíguos — têm um sentido comum na língua além do sentido financeiro. Esses termos **não entram nas listas diretas**; em vez disso, vivem em `RISKY_TERMS_CONTEXT`, um dicionário onde a chave é o termo ambíguo e o valor é uma lista de termos de contexto — o termo ambíguo só conta como sinal financeiro se **pelo menos um** termo de contexto também aparecer no texto (em qualquer lugar, não precisa estar próximo):

```python
RISKY_TERMS_CONTEXT = {
    "ação":   ["ticker", "bolsa", "ibovespa", "b3", "earnings", ...],
    "futuro": ["di futuro", "contrato futuro", "b3", "vencimento", ...],
    "win":    ["b3", "pregao", "ibovespa", "mini indice", ...],
    "vale":   ["mineracao", "vale on", "vale3", "resultado trimestral", ...],
    "apple":  ["iphone", "tim cook", "nasdaq", ...],
    # ... (lista completa no código-fonte)
}
```

Termos atualmente nesta tabela: `ação`, `ações`, `futuro`, `opções`, `opcoes`, `win`, `wdo`, `orçamento`, `orcamento`, `options`, `futures`, `vencimento`, `alimentos`, `serviços`, `servicos`, `vale`, `rumo`, `azul`, `gol`, `apple`, `meta`.

A função `has_risky_term_with_context(text)` percorre essa tabela e retorna `True` se qualquer par (termo ambíguo + contexto) coocorrer no texto.

### 5.4 Tratamento especial: "fiscal"

"fiscal" e "arcabouço" têm tratamento próprio (`has_fiscal_government_context`), separado de `RISKY_TERMS_CONTEXT`, porque a distinção não é "tem outro sentido comum" — é "pode ser sobre política pública OU sobre uma empresa específica" (ex: "crédito fiscal de uma empresa" vs "arcabouço fiscal do governo"). Só conta como relevante se aparecer junto de um termo de escopo de governo (`governo`, `congresso`, `tesouro nacional`, etc.).

### 5.5 Filtro de conteúdo patrocinado

Itens cujo link contém `/patrocinado/` são descartados antes de qualquer análise de keyword — esse padrão de URL identifica publieditorial/conteúdo de afiliado, que tende a mencionar termos financeiros (Selic, IPCA) em contexto promocional, não jornalístico.

---

## 6. Análise via IA — prompt e regras de comportamento

### 6.1 Modelo em uso

`gemini-2.5-flash-lite`, chamado via endpoint REST direto (`generateContent`), não via SDK. Histórico de modelos testados:

| Modelo | Período | Motivo da mudança |
|---|---|---|
| `gemini-2.0-flash` | Inicial | Descontinuado pelo Google em 01/06/2026 |
| `gemini-2.5-flash-lite` | Atual (após reversão) | — |
| `llama-3.1-8b-instant` (Groq) | Migração temporária | Revertido — modelo menor seguia instrução de prompt com menos precisão (campos vazios, classificação incorreta mesmo com regras explícitas no prompt) |

**Decisão de design**: apesar do Gemini ter cota mais restrita (~20 req/dia vs 14.400/dia do Groq), foi escolhido por maior fidelidade às instruções do prompt. A cota deixou de ser um problema crítico após a migração para janelas fixas (8-10 chamadas/dia) e batch processing.

### 6.2 Batch processing

Cada execução faz **uma única chamada de IA** para todas as notícias candidatas daquela janela (até 20), em vez de uma chamada por notícia. O prompt principal recebe a lista numerada de notícias e devolve um objeto JSON com a chave `"analises"`, contendo uma lista de objetos na mesma ordem, cada um com campo `"id"` para realinhamento (a IA pode retornar fora de ordem; o código reordena por ID).

### 6.3 Regra central do prompt: proibição de inferência não-afirmada

A regra mais importante do prompt (`PORTULANAS_SYSTEM_PROMPT`) é: **a IA só pode atribuir um canal (juros, inflação, etc.) ou uma origem (doméstica/externa) a uma notícia se o próprio texto afirmar isso explicitamente.** Essa regra foi adicionada após observar a IA inferindo conexões especulativas (ex: "Bitcoin subiu" sendo interpretado como sinal de "apetite a risco global afetando fluxo de capital para o Brasil" — uma cadeia de inferência de 2-3 saltos lógicos sem base no texto original).

O prompt contém exemplos explícitos do que NÃO fazer, extraídos de erros reais observados em produção (ver Seção 10 para o histórico completo desses erros).

### 6.4 Canal "decisão fiscal/regulatória" — histórico de refinamento

Esse canal passou por duas iterações por causa de falsos positivos recorrentes:
1. Versão inicial (`fiscal_politico`): capturava qualquer notícia política, incluindo resultado eleitoral e pesquisa de opinião.
2. Versão atual (`decisao_fiscal_regulatoria`): exige uma decisão **já tomada** com efeito fiscal/regulatório concreto, com exemplos explícitos no prompt do que não marcar (pesquisa Datafolha, desistência de candidatura, cobrança de transparência sem decisão tomada).

### 6.5 Estilo da "leitura crítica" — condicional, não declarativo

Após feedback de que o texto gerado parecia repetitivo ("esta notícia é relevante porque..."), o prompt foi reescrito para exigir **raciocínio condicional** ("se o dado vier acima do esperado, X; se vier abaixo, Y") em vez de declaração de relevância. O campo aceita 3-5 frases (anteriormente 1-2), com instrução explícita para variar a estrutura entre frases.

### 6.6 Resumo consolidado do Bloco de Ações (segunda chamada de IA)

Diferente do Bloco 1 (uma mensagem por notícia), o Bloco 2 (Ações/FIIs) usa uma **chamada de IA separada e menor** (`summarize_stocks_block` / `STOCKS_SUMMARY_PROMPT`) que recebe até 5 notícias de ações e devolve um único parágrafo consolidado, em vez de análise individual por item. Custo adicional: 1 chamada extra por janela, só quando há itens de ações disponíveis.

---

## 7. Fontes RSS

| Fonte | URL | Observação |
|---|---|---|
| Investing.com | `investing.com/rss/news_301.rss` | — |
| InfoMoney | `infomoney.com.br/mercados/feed/` | — |
| InfoMoney Economia | `infomoney.com.br/economia/feed/` | — |
| Valor Econômico | `valor.globo.com/rss/valor` | Contém conteúdo patrocinado (filtrado por URL) |
| ForexLive | `forexlive.com/feed/news` | — |
| Folha Mercado | `feeds.folha.uol.com.br/mercado/rss091.xml` | — |
| Money Times | `moneytimes.com.br/rss/` | — |
| InvestNews | `investnews.com.br/rss/` | — |
| Google News Macro | busca `dólar OR Fed OR Copom OR PTAX` | **Limitação conhecida** — ver Seção 9.1 |

**Removida**: Estadão Economia (bloqueio HTTP 403 confirmado em produção, não é falha temporária).

**Limite por fonte**: até 30 itens mais recentes por execução (`entries[:30]`, ampliado de 15 após suspeita de perda de notícia relevante em fontes de alto volume).

**Filtro de idade**: itens publicados há mais de 6 horas (`NEWS_MAX_AGE_HOURS`) são descartados antes de qualquer outro processamento — protege contra itens antigos que ainda aparecem no feed RSS sendo tratados como novidade.

---

## 8. Formatação e envio (Telegram)

### 8.1 Estrutura de uma janela completa

```
[Disparo 1] PORTULANAS · ALERTA {ALTA|MEDIA|BAIXA}
            Título (traduzido se original em outro idioma)
            Fonte · Data
            Resumo objetivo
            Canal: ... (só se houver base explícita)
            Origem: ... (só se houver base explícita)
            Leitura crítica (condicional, 3-5 frases)
            Link(s) — agrupado se múltiplas fontes relataram o mesmo fato

[Disparo 2-5] (mesma estrutura, demais notícias do Bloco 1, ordenadas por
              origem doméstica > ambas > externa, e relevância dentro de
              cada grupo)

[Disparo 6] ➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖
            📊 AÇÕES & FUNDOS
            (parágrafo único resumindo até 5 notícias de ações/FIIs)
            🔗 Fontes: lista de até 5 links

[Disparo 7] Disclaimer (texto fixo sobre uso de IA e ausência de
            recomendação de investimento)
```

### 8.2 Painel histórico/legado: Checkpoint e Panorama (pausado)

`portulanas_panorama.py` gerava um "panorama de abertura" e "checkpoints horários" descrevendo o estado do mercado (DXY, treasuries, bolsas) **sem nenhuma fonte de dado real** — o modelo de IA "chutava" o cenário com base em conhecimento geral, sem ler nenhuma notícia ou cotação. Isso gerou alucinação visível: o mesmo cenário descrito ("DXY pressionado, treasuries subindo") levava a conclusões de viés diferentes (alta/baixa/neutro) em execuções subsequentes no mesmo dia. **Pausado em 21/06/2026.** O arquivo permanece no repositório mas nenhum workflow o aciona.

---

## 9. Sistema de assinantes (multi-usuário)

Originalmente o bot enviava para um único `TELEGRAM_CHAT_ID` fixo. Foi migrado para suportar múltiplos assinantes:

1. `portulanas_subscribers.py` faz polling (`getUpdates`) a cada 15 minutos
2. Qualquer pessoa que mande `/start` (ou qualquer mensagem, se for a primeira) é adicionada a `subscribers.json` e recebe uma mensagem de boas-vindas
3. Se já for assinante e mandar `/start` de novo, recebe confirmação de que já está inscrito (não duplica na lista)
4. `send_telegram()` em `portulanas_bot.py` itera sobre todos os `chat_id` da lista, com fallback para o `TELEGRAM_CHAT_ID` original se a lista ainda não existir

**Limitação conhecida**: não há mecanismo de descadastro (`/stop`). Não há diferenciação de conteúdo por assinante (todos recebem o mesmo conteúdo).

---

## 10. Limitações conhecidas e pendências (lista honesta, não exaustiva)

### 10.1 Google News Macro busca só 4 termos cambiais

A query atual (`dólar OR Fed OR Copom OR PTAX`) nunca traz notícia de ações/empresas especificamente, mesmo que o restante do pipeline já saiba reconhecê-las. **Esta limitação ainda não foi corrigida** — está em discussão se a solução é ampliar a query existente ou criar uma segunda fonte de Google News dedicada a ações.

### 10.2 Resumo do Dia Anterior — implementado mas não homologado em produção

`portulanas_resumo_diario.py` foi escrito e lê `daily_history.json`, mas **não tem workflow associado ainda** (pendente de criação do `.yml` com cron às 08:00 BRT) e não foi testado com dados reais de um dia inteiro de operação. Decisão operacional: aguardar acumular 2-3 dias de histórico real antes de homologar.

### 10.3 Instabilidade momentânea do Gemini (HTTP 503)

Observado em produção: o servidor do Gemini retornou `503 Service Unavailable` em uma execução real, e o tratamento de retry da época só cobria erro 429 (rate limit) com backoff progressivo — outros códigos de erro tinham retry mais fraco. Corrigido para tratar 429/500/502/503/504 com a mesma lógica de backoff (15s, 30s, 45s, 60s).

### 10.4 Risco de concorrência em escrita de estado via Git

Documentado na Seção 3.1. Mitigado parcialmente com `git pull --rebase`, mas não eliminado — duas execuções muito próximas no tempo (ex: disparo manual durante uma execução automática) podem ainda gerar conflito.

### 10.5 Cota da conta Gemini não explicada

A conta em uso está limitada a ~20 requisições/dia, abaixo do padrão de 1.500/dia documentado publicamente pelo Google para esse modelo. A causa não foi identificada (testada: não é limite de RPM, não é cota de tokens — o painel do Google AI Studio mostra explicitamente "RPD: 40/20", indicando que o teto de 20 é real para esta conta específica). Não foi testado se um projeto novo no Google AI Studio teria cota diferente.

### 10.6 Sem mecanismo de feedback do usuário sobre qualidade

Não há botão de avaliação (👍/👎) nem qualquer telemetria sobre quais alertas o operador considera úteis. Isso significa que ajustes de prompt e taxonomia dependem inteiramente de revisão manual do operador lendo as mensagens recebidas.

---

## 11. Checklist sugerido para auditoria por outra IA

Ao revisar este projeto, sugerimos verificar especificamente:

1. **Toda nova keyword adicionada respeita word boundary?** (ver Seção 5.1) — testar contra um corpus de frases comuns do idioma antes de aceitar.
2. **Toda keyword ambígua tem entrada em `RISKY_TERMS_CONTEXT` com contexto suficientemente específico?** — contexto genérico demais (ex: "governo" sozinho) pode não distinguir bem.
3. **O prompt da IA (`PORTULANAS_SYSTEM_PROMPT`) ainda proíbe inferência de canal/origem não-afirmada?** Esta é a regra mais frequentemente "esquecida" por modelos menores em testes anteriores (Llama 8B via Groq).
4. **A ordenação hierárquica (origem > relevância) no Bloco 1 está sendo respeitada na prática?** Verificar logs reais.
5. **O fallback de `leitura_critica` vazia (`analysis.get("leitura_critica") or analysis.get("resumo", "")`) ainda está presente em `format_alert`?** — proteção contra modelos que não preenchem o campo apesar da instrução.
6. **A cota de IA está sendo respeitada?** — verificar `RPD` no painel do Google AI Studio periodicamente.
7. **Existe overlap ou contradição entre `BLOCO_EMPRESAS_BR`/`BLOCO_TICKERS_BR` e `EMPRESAS_AMBIGUAS_CONTEXT`?** — uma empresa não deveria estar em ambos com tratamento diferente.

---

## 12. Código-fonte completo

O código-fonte integral dos arquivos Python e workflows YAML está anexado como arquivos separados junto a este documento, para inspeção linha a linha:

- `portulanas_bot.py` — motor principal (coleta, filtro, análise, formatação, envio)
- `portulanas_subscribers.py` — gerenciamento de assinantes
- `portulanas_resumo_diario.py` — resumo do dia anterior
- `portulanas_panorama.py` — painel legado (pausado)
- `.github/workflows/garimpo.yml` — agendamento das 8 janelas diárias
- `.github/workflows/assinantes.yml` — agendamento do polling de assinantes
- `.github/workflows/homologacao.yml` — disparo manual para testes
- `.github/workflows/rodadas.yml` — agendamento do painel legado (pausado)

**Recomendação para a IA auditora**: leia primeiro este documento de arquitetura por completo, depois o código-fonte na ordem listada acima. As decisões de design e o histórico de bugs documentados aqui devem ser tratados como contexto autoritativo sobre *por que* o código está estruturado da forma que está — várias escolhas que podem parecer subótimas a uma primeira leitura (ex: filtro de keyword antes de IA, janelas fixas em vez de contínuo, batch processing) são respostas deliberadas a restrições reais de custo e qualidade observadas em produção, não decisões arbitrárias.

