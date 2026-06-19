# Portulanas Macro Bot

Robô de garimpo de notícias macro para apoio à operação de WDO (mini dólar futuro, B3), seguindo a lógica do Trade System RIVOOS WEALTH.

## O que ele faz

1. **Garimpo contínuo** (a cada 15 minutos): coleta notícias de Reuters, Investing.com, InfoMoney, Valor Econômico e ForexLive. Filtra por palavras-chave de relevância e, para as candidatas, usa o Gemini para classificar relevância (ALTA/MÉDIA/BAIXA) e aplicar a lógica de correlação direta/inversa/contextual com o WDO. Só envia alerta no Telegram se a notícia for relevante.

2. **Panorama de abertura** (08:30 BRT, dias de semana): visão geral do macro para começar o dia.

3. **Checkpoint hora a hora** (09h às 16h BRT, dias de semana): rodada obrigatória de contexto, mesmo sem notícia nova.

## Arquitetura

- `portulanas_bot.py` — script do garimpo contínuo
- `portulanas_panorama.py` — script das rodadas programadas (abertura/checkpoint)
- `.github/workflows/garimpo.yml` — agenda o garimpo a cada 15 min
- `.github/workflows/rodadas.yml` — agenda abertura e checkpoints
- `seen_cache.json` — cache de notícias já processadas (evita repetição), atualizado automaticamente pelo workflow

## Configuração necessária

Três secrets no repositório (`Settings → Secrets and variables → Actions`):

- `TELEGRAM_BOT_TOKEN` — token do bot, obtido via @BotFather
- `TELEGRAM_CHAT_ID` — ID do chat para onde as mensagens são enviadas
- `GEMINI_API_KEY` — chave de API do Google AI Studio (Gemini, tier gratuito)

## Custo

100% gratuito no volume de uso esperado:
- GitHub Actions: dentro do tier gratuito mensal (repositório público = ilimitado)
- Gemini Flash: dentro do tier gratuito diário do Google AI Studio
- Telegram Bot API: sempre gratuita

## Ajustando a lógica

A lógica de correlação macro está descrita no `PORTULANAS_SYSTEM_PROMPT`, dentro de `portulanas_bot.py`. Para recalibrar qualquer correlação (ex: petróleo, ouro, mudança de regime de mercado), edite o texto desse prompt diretamente — não é necessário alterar nenhuma outra parte do código.

As palavras-chave de relevância (filtro antes de gastar chamada de IA) estão em `HIGH_RELEVANCE_KEYWORDS` e `MEDIUM_RELEVANCE_KEYWORDS`, no mesmo arquivo.
