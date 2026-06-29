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
| `portulanas_bot.py` | Motor principal: coleta RSS, filtra, analisa com IA, formata e envia | `garimpo.yml` (8x/dia, seg-sex) e `retry_garimpo.yml` (condicional) |
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
| `filter_audit_log.json` | Itens que passaram no filtro de keyword mas foram BAIXA relevância (14 dias de retenção) | `portulanas_bot.py` | Revisão manual/externa periódica |
| `retry_needed.json` | Flag temporário indicando falha total da última análise de IA | `portulanas_bot.py` | `retry_garimpo.yml` (workflow de retry) |

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
| Google News Câmbio/Juros | busca `dólar OR Fed OR Copom OR PTAX OR Selic` | — |
| Google News Ações | busca `Ibovespa OR Petrobras OR Vale OR ações OR B3 OR ticker` | Adicionada em 24/06/2026 para cobrir lacuna identificada por auditoria externa |
| Google News Commodities | busca `petróleo OR minério OR ouro OR soja OR cobre` | Adicionada em 24/06/2026 |
| Google News Geopolítica | busca `Irã OR Ormuz OR Trump OR Israel OR cessar-fogo` | Adicionada em 24/06/2026 |

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

### 10.1 ~~Google News Macro busca só 4 termos cambiais~~ — RESOLVIDO em 24/06/2026

A query única foi substituída por 4 queries específicas por bloco temático (Câmbio/Juros, Ações, Commodities, Geopolítica), tratadas como fontes RSS separadas. Decisão tomada após auditoria externa (relatório de outra IA) sugerir que múltiplas queries focadas são preferíveis a ampliar uma única query ampla, para evitar que termos de blocos diferentes concorram entre si na mesma busca.

### 10.1b Comando `/stop` — RESOLVIDO em 24/06/2026

`portulanas_subscribers.py` agora trata o comando `/stop`, removendo o `chat_id` de `subscribers.json` e enviando mensagem de confirmação de descadastro. Quem não é assinante e manda `/stop` é ignorado silenciosamente (não há nada a fazer).

### 10.1c Log de auditoria de falsos positivos do filtro — NOVO em 24/06/2026

Todo item que passa no filtro de palavra-chave (`quick_relevance_check`) mas é classificado pela IA como BAIXA relevância é registrado em `filter_audit_log.json` (retenção de 14 dias). Não afeta o comportamento do bot — é um registro para revisão periódica (manual ou por outra IA), permitindo identificar de forma orientada a dado quais termos de `RISKY_TERMS_CONTEXT` precisam de contexto mais específico, em vez de depender só de revisão manual ad-hoc das mensagens recebidas.

### 10.7 Outages do Gemini (HTTP 503) e mecanismo de retry de janela — NOVO em 24/06/2026

Observado em produção: duas execuções consecutivas (horas separadas) falharam **completamente** na análise de IA, com erro 503 em todas as 4-6 tentativas de retry interno. Pesquisa externa confirmou que isso é um problema conhecido e recorrente do serviço Gemini (picos de demanda após lançamento de novos modelos podem gerar taxas de falha de até 45% em horários de pico), não exclusivo desta conta.

**Bug crítico encontrado e corrigido durante a investigação**: antes desta correção, o código marcava TODOS os itens processados como "vistos" no cache (`seen_cache.json`) independentemente do resultado da análise — inclusive quando a IA falhava completamente. Isso significava que, numa falha total, as notícias eram perdidas permanentemente (nunca mais seriam tentadas), mesmo que ainda estivessem dentro da janela de 6h de idade. Corrigido: agora só são marcados como vistos os itens cuja análise teve resposta da IA (mesmo que BAIXA relevância); itens com análise `None` (falha técnica) permanecem disponíveis para nova tentativa.

**Mecanismo de retry de janela**: quando uma execução do garimpo detecta falha total do batch (`batch_failed_completely`), escreve um flag em `retry_needed.json`. Um segundo workflow (`retry_garimpo.yml`) roda ~25-40 minutos depois de cada uma das 8 janelas e verifica esse flag: se existir e tiver menos de 40 minutos, dispara uma nova execução completa do garimpo; senão, sai sem custo. Isso evita gastar cota de IA em dias sem outage (a maioria), enquanto garante uma segunda chance em dias com instabilidade do servidor.

**Resiliência interna também ampliada**: o retry dentro de uma única chamada de IA passou de 4 tentativas (15s/30s/45s/60s, total 150s) para 6 tentativas (20s/40s/60s/80s/100s/120s, total ~7 minutos), dando mais chance de recuperação automática sem precisar do retry de janela.

### 10.8 Bug encontrado: retry generoso piorava esgotamento de cota (429)

**Efeito colateral não previsto da correção da Seção 10.7**: ao tornar o retry mais generoso (6 tentativas) para lidar bem com outages 503, o mesmo código passou a fazer até 6 tentativas reais contra a API também quando o erro era **429 (cota excedida)** — e cada tentativa, mesmo falhando, consome 1 requisição da cota diária. Como a cota desta conta é de apenas 20/dia, uma única execução com erro 429 podia consumir até 6 dessas 20 só insistindo num erro que não tem chance de se resolver por retry (429 de cota diária só se resolve no reset, à meia-noite Pacific Time).

Isso foi observado em produção: uma execução de retry de janela (que já era, ela mesma, uma segunda tentativa depois de uma falha anterior) bateu a sequência `429, 429, 429, 429, 503, 429` — 6 tentativas, a maioria 429, todas consumindo cota real sem chance de sucesso.

**Correção**: `call_gemini_with_retry` agora trata 429 separadamente dos erros de servidor (500/502/503/504). Para 429, o limite de retry é bem mais curto (`max_quota_retries=2`, não 6) — porque insistir em erro de cota não tem teoria de recuperação dentro da mesma execução. Para 503 e similares, o retry generoso de 6 tentativas continua, porque esses erros genuinamente se resolvem em minutos.

### 10.9 Bloco de Ações caindo em fallback ruim, e retry econômico na chamada secundária

Observado em produção: o resumo consolidado do Bloco 2 (Ações/RV) caía com frequência no fallback (concatenação crua de títulos, incluindo lixo de formatação do RSS original como `| ` e ` - NomeDoSite`), porque `summarize_stocks_block` chamava `call_gemini_with_retry` sem nenhum ajuste de parâmetros — herdando os mesmos 6 retries generosos da chamada principal, que sozinha já podia esgotar a cota disponível antes mesmo dessa segunda chamada (do resumo de RV) ser tentada.

**Correções**: (1) a chamada do resumo de RV agora usa retry bem mais econômico (`max_retries=2, max_quota_retries=1`) — se a cota já estiver pressionada pelo Bloco 1, não vale insistir numa chamada secundária; (2) o fallback foi reescrito para limpar o título (removendo tudo depois de `" | "` ou `" - "`) e truncar respeitando limite de palavra, nunca mais concatenando tudo cru numa única frase.

### 10.10 Piso de qualidade no modo Janela Fixa, e novos termos ambíguos (metais)

Observado em produção: a notícia "Palmas recebe Copa Brasil Ouro de Tênis de Mesa" foi enviada como alerta real, apesar de a própria IA ter classificado como BAIXA relevância e explicitamente declarado, na leitura crítica, que não tinha relação com macroeconomia. Duas causas, ambas corrigidas:

**Causa 1 — colisão de keyword**: "ouro" (de `BLOCO_COMMODITIES`) bate em "Copa Brasil **Ouro**" (categoria/divisão esportiva, não o metal). Auditoria revelou o mesmo problema em "prata", "bronze", "cobre" (também verbo comum "ele cobre a vaga"), "gold" e "copper" (colidem com "gold medal"). Todos os 6 termos foram movidos de listas diretas para `RISKY_TERMS_CONTEXT`, exigindo contexto de commodity/mercado/valor monetário (`abaixo de`, `acima de`, `cotação`, `onça`, `LME`, etc.) para contar como sinal financeiro.

**Causa 2 — falta de piso de qualidade no modo Janela Fixa**: a regra de "sempre mostrar Top N, mesmo que nada seja ALTA/MEDIA" (decidida para nunca deixar a janela em silêncio total) não tinha nenhum piso mínimo de qualidade — um item BAIXA relevância podia ser usado para "completar" o Top N mesmo sem nenhuma relação real com o propósito do bot. Corrigido: itens com `relevancia == "BAIXA"` **e** sem nenhum `canais_afetados` preenchido são descartados do pool de seleção, mesmo que isso resulte em menos de 5-6 itens enviados naquela janela. Importante: a regra exige **canal**, não origem - porque `origem` pode vir preenchida mesmo em notícia irrelevante (ex: "Palmas" é cidade brasileira, então `origem="domestica"` não é sinal confiável de relevância macroeconômica).

### 10.11 Fallback de emergência via Groq para outages prolongados do Gemini

Observado em produção (25/06/2026): um outage do Gemini durou mais de 1h20 contínuas, derrubando tanto a execução principal de uma janela quanto o retry de janela (`retry_garimpo.yml`) que rodou ~1h depois - ambos bateram 6/6 tentativas de erro 503. Isso expôs o limite real da arquitetura de defesa em camadas construída nas Seções 10.7-10.9: ela cobre bem outages de minutos, mas não outages de horas, porque tanto o retry interno (~7 min) quanto o retry de janela (1 tentativa extra) se esgotam antes do outage passar.

**Solução implementada**: `analyze_batch_with_gemini` agora aciona automaticamente um fallback via Groq (`analyze_batch_with_groq_fallback`, usando `llama-3.1-8b-instant`) sempre que a chamada ao Gemini falha totalmente (após todos os retries esgotados, ou exceção de parsing). O Groq foi escolhido para esse papel porque: (1) já foi usado como motor principal antes e tem cota gratuita generosa (14.400 req/dia, bem acima da necessidade); (2) embora tenha qualidade de seguimento de prompt inferior ao Gemini (motivo pelo qual foi revertido como motor principal - ver topo desta seção de configuração no código), para uma situação de emergência "melhor isso do que silêncio total por horas" é um trade-off aceitável.

**Transparência**: toda análise gerada pelo fallback é marcada internamente (`_from_fallback: True`) e a mensagem final no Telegram recebe um aviso visual (`FALLBACK_FOOTER`): *"⚠️ Gerado pelo motor de fallback (Groq) devido a instabilidade prolongada do Gemini."* — o operador sempre sabe quando está lendo uma análise do plano B, e pode julgar a qualidade com esse contexto em mente.

A lógica de parsing de resposta (`parse_analysis_response`) foi extraída para uma função compartilhada entre Gemini e Groq, já que ambos devem responder no mesmo formato JSON (`{"analises": [...]}`) - evita duplicação e garante que correções de parsing feitas para um se aplicam automaticamente ao outro.

**Limitação conhecida**: se o Groq também estiver indisponível no momento do outage do Gemini (cenário raro, mas possível), a notícia ainda fica sem análise - não há um terceiro provedor de fallback. O resumo do Bloco 2 (Ações/RV) não tem fallback Groq dedicado ainda - usa só o fallback de formatação (lista limpa de títulos) já existente.

### 10.13 Migração do agendamento para cron-job.org (resolve drift do GitHub Actions)

Observado em produção: execuções do `garimpo.yml` chegavam a atrasar quase 2 horas em relação ao horário agendado (`schedule:`). Pesquisa confirmou que isso é uma limitação **conhecida e documentada** do GitHub Actions - o agendamento nativo (`schedule:`) entra numa fila compartilhada globalmente, e o atraso cresce em períodos de alta carga da plataforma, sem garantia de execução no minuto exato. Fontes da comunidade relatam atrasos médios crescentes (de ~1h40 em 2025 a >4h30 em casos recentes).

**Solução implementada**: o agendamento foi migrado para o **cron-job.org** (serviço externo gratuito, sem limite de cronjobs no tier free), que dispara - no horário exato - uma chamada HTTP `POST` para a API do GitHub (`/repos/.../actions/workflows/garimpo.yml/dispatches`), acionando o workflow via `workflow_dispatch` em vez de `schedule:`. Isso não move nenhuma execução para fora do GitHub Actions - código, logs, Secrets e todo o restante da infraestrutura permanecem exatamente como estavam. Apenas o **gatilho de horário** passou a vir de fora, evitando a fila de agendamento nativo do GitHub. Teste real confirmou: disparo programado para 08:30 BRT executou de fato as 08:32, dentro de margem aceitável.

**Configuração**: 8 cronjobs (um por janela), cada um com um Personal Access Token do GitHub (permissão restrita a "Actions: Read and write" no repositório específico) no header `Authorization`, e corpo `{"ref":"main"}`.

### 10.14 Lacunas geopolíticas e janela de cobertura para a primeira execução de segunda-feira — 26/06/2026

**Lacuna geopolítica**: Rússia, Ucrânia, Putin, Zelensky, Venezuela e Maduro não estavam em nenhuma lista de keywords - notícias sobre esses temas nunca chegavam a ser analisadas pela IA, sendo descartadas já no filtro de palavra-chave (não era questão de perder por relevância, era nem chegar a ser avaliada). Adicionados ao `BLOCO_GEOPOLITICA`. "Maduro" foi tratado como termo de risco (`RISKY_TERMS_CONTEXT`) por ser também adjetivo comum em português ("mercado mais maduro", "fruta madura") - exige contexto venezuelano (Venezuela, Caracas, Guiana/Essequibo, chavismo, mobilização, etc.) para contar como sinal geopolítico.

**Janela de cobertura de fim de semana (Weekend Recap)**: como o garimpo não roda sábado/domingo, a primeira execução de segunda-feira herdava um "buraco" de 60+ horas (desde sexta à noite) que o filtro padrão de idade (6h) descartava integralmente. A primeira abordagem testada (ampliar o filtro de idade da própria janela das 08:30) foi descartada em favor de uma solução mais limpa: um **workflow dedicado** (`weekend_recap.yml`), disparado uma única vez pelo cron-job.org (sugestão: 08:00 BRT, antes da janela normal), usando uma janela **absoluta** de data (`get_weekend_recap_window`) - sexta a partir das 18h BRT até domingo às 23h59 BRT - em vez de relativa ("últimas N horas"). Isso evita qualquer sobreposição com a janela normal das 08:30 (que continua usando o filtro padrão de 6h, cobrindo o overnight de domingo→segunda normalmente) e mantém a responsabilidade de "resumo do fim de semana" separada da rotina diária, com identificação visual própria na mensagem (`🗞️ Resumo do Fim de Semana`). Ativado via variável de ambiente `PORTULANAS_WEEKEND_RECAP=1`.

### 10.12 Deduplicação entre janelas, canal inválido, e melhoria do fallback de RV — 25/06/2026

Auditoria de um dia completo de produção revelou 3 problemas adicionais:

**1. Mesma notícia reaparecendo em janelas diferentes do mesmo dia.** Observado: a declaração de Galípolo sobre comunicação do Copom apareceu em 4 mensagens distintas ao longo do dia, com pequenas variações de manchete ("explicar demais" vs "excesso de explicação"). Causa: o agrupamento por similaridade (`group_similar_items`) só compara itens **dentro da mesma execução**, nunca contra o que já foi enviado em janelas anteriores - e o cache de deduplicação (`seen_cache.json`) usa hash de título+link, que nunca repete porque cada fonte/Google News gera uma URL distinta para a mesma notícia.

A correção precisou lidar com uma limitação adicional: a similaridade de título tradicional (Jaccard) não captura reformulações com vocabulário muito diferente ("explicar demais" vs "excesso de explicação" tem similaridade de apenas ~0.10, abaixo do limiar de 0.18). Solução implementada (`is_duplicate_of_recent_history`): combina dois sinais - (1) similaridade de título tradicional, e (2) **nome próprio central compartilhado** (extraído via capitalização) dentro de uma janela de tempo (`CROSS_WINDOW_DEDUP_HOURS = 5`). O segundo sinal captura o caso Galípolo (ambos os títulos compartilham "Galípolo"), mas foi calibrado para não bloquear declarações relacionadas de pessoas diferentes sobre o mesmo evento (testado: uma declaração de Picchetti sobre o mesmo episódio do Copom, sem citar Galípolo, corretamente NÃO é tratada como duplicata).

**2. IA inventando canal fora do enum válido.** Observado: `"canais_afetados": ["commodities"]` - "commodities" nunca foi um dos 5 canais oficiais (juros, inflação, atividade_emprego, decisao_fiscal_regulatoria, fluxo_capital), e isso fazia o canal aparecer sem tradução no Telegram (`⚙️ Fiscal / Regulatório · commodities`, em minúsculo cru). Duas correções: (a) o prompt agora declara explicitamente que esses são os **únicos 5 valores válidos**, com o erro real ("commodities") citado como exemplo do que não fazer; (b) `format_alert` ganhou um fallback defensivo que capitaliza qualquer canal não reconhecido, em vez de exibir cru.

**3. Fallback do Bloco RV acionando com frequência maior que o desejado.** A chamada de `summarize_stocks_block` usava retry deliberadamente econômico (`max_retries=2, max_quota_retries=1`, decidido na Seção 10.9 para não competir por cota com a chamada principal) - na prática, isso desistia rápido demais e o fallback de lista crua (sem agrupamento temático, sem tradução) acionava com frequência visível em produção. Corrigido: (a) retry um pouco mais tolerante (`max_retries=3, max_quota_retries=2`); (b) adicionado fallback via Groq **antes** de cair no fallback de formatação - mesma lógica de emergência da Seção 10.11, agora replicada para a chamada secundária de RV. O fallback de lista crua (último recurso) permanece sem tradução de título por design, para não exigir uma terceira chamada de IA quando as duas primeiras já falharam - mas a frequência desse caminho deve cair bastante com as duas tentativas adicionais antes dele.

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

### 10.15 Fallback Groq falhando com 413 em lotes grandes — 29/06/2026

Observado em produção: numa execução real onde o Gemini falhou totalmente (6×503, mesmo padrão da Seção 10.7) e o fallback Groq foi acionado automaticamente (Seção 10.11), o **próprio fallback também falhou**, com erro `413 Payload Too Large`. Investigação confirmou que isso não é sobre tamanho de payload em bytes, mas sobre **TPM (tokens por minuto) do modelo `llama-3.1-8b-instant`** - o Groq aplica limites de TPM por modelo, mais restritivos que o do Gemini, e o lote enviado (20 itens, o máximo de `MAX_GROUPS_PER_RUN`, com `max_tokens` calculado dinamicamente como `600 * len(items) + 200`) excedeu esse limite numa única chamada. O fallback nunca tinha sido testado com o tamanho máximo real de lote - os testes anteriores usaram poucos itens.

**Correção**: `analyze_batch_with_groq_fallback` agora divide o lote em sub-lotes de no máximo `GROQ_FALLBACK_MAX_BATCH_SIZE = 6` itens, processando cada sub-lote em uma chamada separada (com pequena pausa de 2s entre elas) e combinando os resultados antes de retornar. Isso significa que, em caso de outage do Gemini com um lote grande, o fallback de emergência pode fazer múltiplas chamadas ao Groq (ex: 4 chamadas para 20 itens) em vez de uma só - ainda assim dentro da cota generosa do Groq (14.400 req/dia), e preferível a falhar por completo.

---



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
