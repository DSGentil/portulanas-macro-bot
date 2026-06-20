# Portulanas Macro Bot

Robô de garimpo de notícias macro para apoio à operação de WDO (mini dólar futuro, B3), seguindo a lógica do Trade System RIVOOS WEALTH.

## O que ele faz

1. **Garimpo contínuo** (a cada 30 minutos, todos os dias): coleta notícias de Investing.com, InfoMoney, Valor Econômico, ForexLive, Money Times, InvestNews, Google News (busca direcionada) e Folha Mercado. Filtra por palavras-chave de relevância e, para as candidatas, usa o Groq (llama-3.1-8b-instant) em uma única chamada batch por execução para classificar relevância (ALTA/MÉDIA/BAIXA), identificar canais afetados (juros, inflação, atividade/emprego, fiscal/político, fluxo de capital) e origem (doméstica/externa/ambas) — só quando a própria notícia afirmar isso explicitamente, sem inferência especulativa. Só envia alerta no Telegram se a notícia for relevante.

2. **Panorama de abertura** (08:30 BRT, dias de semana): visão geral do macro para começar o dia.

3. **Checkpoint hora a hora** (09h às 16h BRT, dias de semana): rodada obrigatória de contexto, mesmo sem notícia nova.

4. **Homologação** (16h às 20h30 BRT, todos os dias, ou disparo manual a qualquer momento): workflow de teste/auditoria. Ignora cache e filtro de palavras-chave, força a análise via Groq das notícias mais recentes e envia o JSON crú retornado no Telegram. Serve para verificar, a qualquer momento, se o Groq continua seguindo fielmente a lógica de correlação do Trade System — não é parte do fluxo de produção, é só uma ferramenta de controle de qualidade.

## Agrupamento de notícias por assunto

Quando fontes diferentes publicam sobre o mesmo assunto com títulos parecidos (ex: Valor e InfoMoney noticiando o mesmo movimento do dólar com palavras diferentes), o robô agrupa esses itens antes de gastar uma análise de IA em cada um separadamente. A mensagem mostra a análise feita uma única vez, seguida da lista de todas as fontes/títulos agrupados com seus links — o operador decide, ao abrir os links, se é de fato a mesma notícia ou se há nuance que justifica leitura separada.

Esse agrupamento é feito por similaridade de palavras-chave entre os títulos (sem custo de IA), com limiar calibrado para capturar reformulações com vocabulário parcialmente sobreposto. Tem uma limitação conhecida: não captura sinônimos puros sem nenhuma palavra-raiz em comum (ex: "Copom corta Selic" vs "Banco Central reduz Selic" pode não agrupar). O agrupamento nunca elimina nenhuma notícia — é só uma organização visual para facilitar a revisão humana.

## Arquitetura

- `portulanas_bot.py` — script do garimpo contínuo, contém também o modo de homologação (ativado pela variável de ambiente `PORTULANAS_HOMOLOGACAO`)
- `portulanas_panorama.py` — script das rodadas programadas (abertura/checkpoint)
- `.github/workflows/garimpo.yml` — agenda o garimpo a cada 15 min (produção)
- `.github/workflows/rodadas.yml` — agenda abertura e checkpoints (produção)
- `.github/workflows/homologacao.yml` — agenda janelas de teste e permite disparo manual (auditoria, não é produção)
- `seen_cache.json` — cache de notícias já processadas (evita repetição), atualizado automaticamente pelo workflow de garimpo. Não é usado nem afetado pelo modo de homologação.

Os três workflows são independentes entre si. Editar ou rodar a homologação nunca interfere no comportamento do garimpo contínuo ou das rodadas programadas — eles não compartilham estado, exceto o código-base do `portulanas_bot.py`, que muda de comportamento apenas quando a variável `PORTULANAS_HOMOLOGACAO` está presente (o que só ocorre dentro do workflow de homologação).

## Configuração necessária

Três secrets no repositório (`Settings → Secrets and variables → Actions`):

- `TELEGRAM_BOT_TOKEN` — token do bot, obtido via @BotFather
- `TELEGRAM_CHAT_ID` — ID do chat para onde as mensagens são enviadas
- `GROQ_API_KEY` — chave de API do Groq (console.groq.com, tier gratuito)

## Custo

100% gratuito no volume de uso esperado:
- GitHub Actions: dentro do tier gratuito mensal (repositório público = ilimitado)
- Groq (llama-3.1-8b-instant): dentro do tier gratuito de 14.400 requisições/dia
- Telegram Bot API: sempre gratuita

## Ajustando a lógica

A lógica de correlação macro está descrita no `PORTULANAS_SYSTEM_PROMPT`, dentro de `portulanas_bot.py`. Para recalibrar qualquer correlação (ex: petróleo, ouro, mudança de regime de mercado), edite o texto desse prompt diretamente — não é necessário alterar nenhuma outra parte do código.

As palavras-chave de relevância (filtro antes de gastar chamada de IA) estão em `HIGH_RELEVANCE_KEYWORDS` e `MEDIUM_RELEVANCE_KEYWORDS`, no mesmo arquivo.

## Auditando o prompt (homologação)

Depois de qualquer ajuste no `PORTULANAS_SYSTEM_PROMPT`, é recomendável conferir se o Groq está de fato seguindo a lógica nova antes de confiar nos alertas de produção. Para isso:

1. Vá na aba **Actions** → workflow **"Portulanas - Homologação"** → **Run workflow**
2. Em poucos segundos, chegam no Telegram mensagens com o JSON completo retornado pelo Groq para as notícias mais recentes
3. Compare a classificação de `correlacao_wdo` e o texto de `leitura_critica` com o que o Trade System diz que deveria ser

Esse workflow nunca interfere no garimpo de produção — não usa o cache (`seen_cache.json`) e não respeita o filtro de palavras-chave, propositalmente, para garantir que sempre haverá algo para testar.
