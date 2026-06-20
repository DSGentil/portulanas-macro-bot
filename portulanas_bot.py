"""
Portulanas Macro Bot
RIVOOS WEALTH · DG

Coleta noticias de portais financeiros, filtra por relevancia,
analisa com Gemini usando a logica do Trade System WDO (correlacao
direta/inversa/contextual) e envia resumo critico no Telegram.

Roda via GitHub Actions a cada 10-15 minutos.
"""

import os
import sys
import json
import time
import hashlib
import unicodedata
import re
import requests
import feedparser
from datetime import datetime, timezone, timedelta

# Modo homologacao: ignora cache e filtro de palavras-chave, forca analise
# das N noticias mais recentes para fins de teste/auditoria do prompt.
HOMOLOGACAO = os.environ.get("PORTULANAS_HOMOLOGACAO", "0") == "1"
HOMOLOG_SAMPLE_SIZE = int(os.environ.get("PORTULANAS_HOMOLOG_SAMPLE", "3"))

# ─────────────────────────────────────────────────────────────────
# CONFIGURACAO
# ─────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY     = os.environ["GEMINI_API_KEY"]

GEMINI_MODEL = "gemini-2.5-flash-lite"  # gemini-2.0-flash foi desativado em 01/06/2026
GEMINI_URL   = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

CACHE_FILE = "seen_cache.json"
CACHE_MAX_AGE_HOURS = 36  # itens mais antigos que isso saem do cache

# Idade maxima de uma noticia para ser considerada "atual". Protege
# contra itens antigos que ainda aparecem na lista do RSS (feeds
# costumam manter os ultimos 15-20 itens publicados, nao so os de hoje)
# sendo tratados como novidade so porque o cache estava vazio/resetado.
NEWS_MAX_AGE_HOURS = 6

# Limite de grupos analisados pelo Gemini por execucao do garimpo.
# Protege a cota diaria do tier gratuito (~1500 req/dia): com execucao
# a cada 15 min (96 execucoes/dia), 12 grupos por execucao no pior caso
# da ~1150 chamadas/dia, com folga para a homologacao e picos.
MAX_GROUPS_PER_RUN = 12

# Fuso de Brasilia
TZ_BR = timezone(timedelta(hours=-3))

# Fontes RSS — feeds publicos e gratuitos
FEEDS = {
    "Investing.com":      "https://www.investing.com/rss/news_301.rss",
    "InfoMoney":          "https://www.infomoney.com.br/mercados/feed/",
    "InfoMoney Economia": "https://www.infomoney.com.br/economia/feed/",
    "Valor Economico":    "https://valor.globo.com/rss/valor",
    "ForexLive":          "https://www.forexlive.com/feed/news",
    # "Estadao Economia": removida - bloqueio 403 ativo confirmado em produção (não é falha temporária)
    "Folha Mercado":      "http://feeds.folha.uol.com.br/mercado/rss091.xml",
    "Money Times":        "https://www.moneytimes.com.br/rss/",
    "InvestNews":         "https://investnews.com.br/rss/",
    "Google News Macro":  "https://news.google.com/rss/search?q=d%C3%B3lar+OR+Fed+OR+Copom+OR+PTAX&hl=pt-BR&gl=BR&ceid=BR:pt-419",
}

# Palavras-chave de alta relevancia (filtro barato antes de gastar chamada de IA)
HIGH_RELEVANCE_KEYWORDS = [
    # Bancos centrais e juros
    "fed", "fomc", "powell", "copom", "selic", "banco central", "bacen",
    "juros", "taxa de juros", "interest rate",
    # Inflacao e atividade
    "cpi", "ipca", "payroll", "nonfarm", "pib", "gdp", "inflação", "inflation",
    # Geopolitica
    "hormuz", "irã", "iran", "guerra", "war", "conflito", "ataque", "sanç",
    "opep", "opec",
    # Cambio e commodities direto
    "dólar", "dollar", "dxy", "ptax", "petróleo", "oil", "brent", "wti",
    "treasury", "treasuries", "yield",
    # Brasil especifico
    "câmbio", "boletim focus", "fiscal", "arcabouço",
]

MEDIUM_RELEVANCE_KEYWORDS = [
    "powell", "lagarde", "ecb", "bce", "china", "tarifas", "tariff",
    "trade war", "guerra comercial", "vix", "bolsa", "stocks", "nasdaq",
    "s&p", "europe", "europa",
]

# ─────────────────────────────────────────────────────────────────
# PROMPT-MAE — LOGICA PORTULANAS / TRADE SYSTEM WDO
# ─────────────────────────────────────────────────────────────────

PORTULANAS_SYSTEM_PROMPT = """Você é o motor analítico do PORTULANAS, sistema de leitura macro da RIVOOS WEALTH, especializado em apoiar quem opera WDO (mini dólar futuro, B3).

Sua função: resumir UMA notícia por vez de forma objetiva e dizer por que ela é relevante - sem especular, sem inventar mecanismo, sem teorizar conexão que a notícia não afirma.

REGRA MAIS IMPORTANTE DESTE PROMPT - LEIA COM ATENÇÃO:
Você só pode atribuir um canal ou uma origem a uma notícia SE O PRÓPRIO TEXTO DA NOTÍCIA disser isso explicitamente. Você não pode inferir, deduzir ou imaginar uma conexão que a notícia não afirma diretamente. Isso significa:
- Se a notícia diz "o Copom decidiu manter a Selic em 14,25%", isso É sobre juros - pode marcar o canal.
- Se a notícia diz "investidores estrangeiros aumentaram posições em títulos públicos brasileiros", isso É sobre fluxo de capital - pode marcar o canal, mas especificamente para renda fixa, não para bolsa, a menos que a notícia diga bolsa.
- Se a notícia diz "Bitcoin subiu hoje", isso NÃO permite você concluir nada sobre fluxo de capital para o Brasil, apetite a risco global, ou qualquer outra coisa - a notícia não fez essa conexão, você não pode fazer por ela.
- Se a notícia fala de um evento social, um jantar, uma premiação, um lançamento de produto sem relação a nenhum dos canais abaixo, não force conexão nenhuma - marque canal como lista vazia e origem como null.
- ERROS A NÃO REPETIR: não diga "isso pode indicar fluxo de capital para a bolsa" a partir de uma notícia genérica sobre "entrada de capital" - entrada de capital pode ir para renda fixa, renda variável, ou nem ser sobre o Brasil. Não generalize. Se a notícia não especificar o destino do capital, não atribua canal "fluxo_capital" como ida para bolsa - descreva apenas o que a notícia disse, sem completar a lacuna com suposição.
- Na dúvida entre marcar um canal por inferência ou deixar em branco: deixe em branco. É preferível dizer menos com precisão do que especular com aparência de análise.

CANAIS POSSÍVEIS (marque só se a notícia falar EXPLICITAMENTE sobre isso - pode ser mais de um, ou nenhum):
- "juros" — a notícia é sobre Selic, Copom, DI, Fed, BCE ou outra decisão/expectativa de juros, dita explicitamente
- "inflacao" — a notícia traz um dado, expectativa ou fala explícita sobre inflação, CPI, IPCA, preços
- "atividade_emprego" — a notícia traz um dado explícito de PIB, produção industrial, payroll, desemprego
- "fiscal_politico" — a notícia é explicitamente sobre arcabouço fiscal, déficit, dívida pública, decisão de governo, eleição, risco político, mudança regulatória
- "fluxo_capital" — a notícia descreve explicitamente um movimento de capital estrangeiro entrando ou saindo, e diz para onde (renda fixa, renda variável, ou de forma geral se a notícia não especificar - mas então registre como "não especificado" na leitura, não complete a lacuna)

ORIGEM DO EFEITO (marque só se for claro pelo conteúdo - pode deixar null se não for óbvio):
- "domestica" — o fato é sobre o Brasil
- "externa" — o fato é sobre fora do Brasil
- "ambas" — a notícia conecta explicitamente algo externo a uma consequência doméstica, ou vice-versa

REGRAS DE PTAX (se a notícia mencionar câmbio/PTAX):
- PTAX tem horários fixos: consultas 10h, 11h, 12h, 13h (cada uma 10min) e divulgação final a partir de 13h30.
- Mencione isso só se a notícia for sobre PTAX diretamente - não force esse aviso em toda notícia de câmbio.

CLASSIFICAÇÃO DE RELEVÂNCIA (escolha uma):
- "ALTA" — fato concreto com potencial de impacto relevante e direto (decisão de juros, dado de inflação fora do esperado, escalada geopolítica grave, fala oficial de autoridade monetária ou fiscal)
- "MEDIA" — contribui para o quadro mas não é gatilho isolado (comentário de analista, dado secundário, fala de político sem novidade real)
- "BAIXA" — ruído, evento social, notícia sem nenhuma relação com os canais acima

Você pode receber UMA OU VÁRIAS notícias na mesma consulta, cada uma identificada por um número (ex: "NOTÍCIA 1", "NOTÍCIA 2"). Analise cada uma de forma independente - uma notícia não deve influenciar a análise de outra, mesmo que estejam na mesma consulta.

FORMATO DE SAÍDA — responda APENAS em JSON válido, sem markdown, sem texto antes ou depois. Se receber várias notícias, responda com uma LISTA de objetos, um por notícia, na mesma ordem em que foram apresentadas, cada um incluindo o campo "id" correspondente ao número da notícia:
[
  {
    "id": 1,
    "relevancia": "ALTA" | "MEDIA" | "BAIXA",
    "resumo": "resumo objetivo da notícia em 2-4 frases, em português, traduzindo para o português caso a notícia original esteja em outro idioma. Cubra o que aconteceu, quem disse o quê, qual dado ou número saiu. Apenas o fato - sem teorizar sobre consequências que a notícia não afirma.",
    "canais_afetados": [] - lista vazia se a notícia não falar explicitamente de nenhum canal, ou os canais que ela cita diretamente,
    "origem": "domestica" | "externa" | "ambas" | null,
    "leitura_critica": "1-2 frases dizendo por que essa notícia é relevante para quem acompanha o câmbio, usando SÓ o que a notícia disse - sem inventar mecanismo, sem 'isso pode sugerir', sem 'isso pode indicar'. Se a notícia já é auto-explicativa sobre sua relevância, pode repetir isso de forma direta em vez de forçar uma análise adicional. Se não houver nada de relevante a acrescentar além do resumo, pode deixar este campo igual ou muito próximo do resumo."
  },
  { "id": 2, ... }
]
Se receber apenas UMA notícia, responda com a LISTA contendo um único objeto (mesmo formato acima), não um objeto solto - mantenha sempre o formato de lista para que a resposta seja consistente independente de quantas notícias forem enviadas.

Notícias sobre criptomoedas (Bitcoin, Ethereum, etc.) são BAIXA relevância por padrão. Só sobem para MEDIA ou ALTA se a própria notícia conectar explicitamente a um banco central, decisão regulatória de governo, ou evento que a notícia mesma diga ter relação com o sistema financeiro tradicional - nunca por inferência sua sobre "apetite a risco".

Seja extremamente direto e econômico em palavras. Você está aqui para informar com precisão, não para parecer analítico. Prefira ser visto como "deixou de comentar algo" do que como "inventou uma conexão que não existia".
"""


# ─────────────────────────────────────────────────────────────────
# CACHE — evita reenviar a mesma noticia
# ─────────────────────────────────────────────────────────────────

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def prune_cache(cache):
    cutoff = time.time() - CACHE_MAX_AGE_HOURS * 3600
    return {k: v for k, v in cache.items() if v > cutoff}


def item_hash(title, link):
    raw = f"{title}|{link}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────
# COLETA RSS
# ─────────────────────────────────────────────────────────────────

def parse_published_datetime_utc(entry):
    """Extrai a data de publicacao do item RSS como datetime UTC.
    Retorna None se nao disponivel ou nao for possivel parsear."""
    for field in ("published_parsed", "updated_parsed"):
        time_struct = entry.get(field)
        if time_struct:
            try:
                return datetime(*time_struct[:6], tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def parse_published_date(entry):
    """Extrai a data de publicacao do item RSS e converte para horario
    de Brasilia. Retorna string formatada ou None se nao disponivel."""
    dt_utc = parse_published_datetime_utc(entry)
    if dt_utc is None:
        return None
    dt_br = dt_utc.astimezone(TZ_BR)
    return dt_br.strftime("%d/%m %H:%M")


def is_news_too_old(dt_utc, max_age_hours=NEWS_MAX_AGE_HOURS):
    """Verifica se uma noticia e mais antiga que o limite permitido.
    Protege contra itens antigos da lista do RSS sendo tratados como
    novidade. Se a data nao estiver disponivel (dt_utc is None), NAO
    bloqueia o item - deixa passar para nao perder noticia so por falta
    de metadado de data (algumas fontes nao informam published_parsed)."""
    if dt_utc is None:
        return False
    age = datetime.now(timezone.utc) - dt_utc
    return age > timedelta(hours=max_age_hours)


def fetch_feed(name, url, timeout=10):
    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PortulanasBot/1.0)"
        })
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        items = []
        for entry in parsed.entries[:15]:
            published_dt_utc = parse_published_datetime_utc(entry)
            items.append({
                "source": name,
                "title": entry.get("title", "").strip(),
                "link": entry.get("link", "").strip(),
                "summary": entry.get("summary", "")[:1200],
                "published": parse_published_date(entry),
                "published_dt_utc": published_dt_utc,
            })
        return items
    except Exception as e:
        print(f"[aviso] falha ao buscar {name}: {e}")
        return []


def collect_all_news():
    all_items = []
    for name, url in FEEDS.items():
        items = fetch_feed(name, url)
        all_items.extend(items)
    return all_items


# ─────────────────────────────────────────────────────────────────
# FILTRO DE RELEVANCIA (regras, sem IA) — primeira camada barata
# ─────────────────────────────────────────────────────────────────

def strip_accents(text):
    """Remove acentos para comparacao robusta (feeds RSS frequentemente
    vem sem acentuacao correta ou com encoding inconsistente)."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def quick_relevance_check(item):
    text = strip_accents((item["title"] + " " + item["summary"]).lower())
    for kw in HIGH_RELEVANCE_KEYWORDS:
        if strip_accents(kw) in text:
            return True
    for kw in MEDIUM_RELEVANCE_KEYWORDS:
        if strip_accents(kw) in text:
            return True
    return False


# Palavras muito comuns que nao ajudam a identificar se duas noticias
# sao a mesma coisa - removidas antes de comparar titulos.
TITLE_STOPWORDS = {
    "a", "o", "as", "os", "de", "da", "do", "das", "dos", "em", "no", "na",
    "nos", "nas", "com", "para", "por", "um", "uma", "e", "ou", "que", "se",
    "sobre", "mas", "ao", "aos", "the", "an", "of", "in", "on", "for", "to",
    "and", "or", "with", "at", "is", "are", "live", "levels",
}

# Limiar de similaridade Jaccard para considerar dois titulos "parecidos
# o suficiente para agrupar visualmente". Calibrado empiricamente: 0.18
# captura bem reformulações com vocabulário parcialmente sobreposto
# (ex: "dólar recua com ajuste de risco" vs "dólar recua... com feriado
# nos EUA"). NAO captura sinônimos puros sem nenhuma palavra-raiz em
# comum (ex: "Copom corta Selic" vs "Banco Central reduz Selic") -
# essa limitação é estrutural de comparação por palavras-chave; exigiria
# similaridade semântica (embeddings) para cobrir. Por isso o agrupamento
# é só um auxílio visual - nunca elimina nada, o operador decide.
TITLE_SIMILARITY_THRESHOLD = 0.18


def title_keywords(title):
    text = strip_accents(title.lower())
    words = re.findall(r"[a-z0-9]+", text)
    return set(w for w in words if w not in TITLE_STOPWORDS and len(w) > 2)


def title_similarity(title_a, title_b):
    words_a = title_keywords(title_a)
    words_b = title_keywords(title_b)
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def group_similar_items(items):
    """Agrupa itens com titulos parecidos (mesmo assunto, fontes
    diferentes). Nao elimina nenhum item - apenas organiza em grupos
    para que o operador veja juntos e decida se sao a mesma noticia.
    Retorna lista de grupos, cada grupo e uma lista de itens."""
    groups = []
    used = [False] * len(items)

    for i, item in enumerate(items):
        if used[i]:
            continue
        group = [item]
        used[i] = True
        for j in range(i + 1, len(items)):
            if used[j]:
                continue
            sim = title_similarity(item["title"], items[j]["title"])
            if sim >= TITLE_SIMILARITY_THRESHOLD:
                group.append(items[j])
                used[j] = True
        groups.append(group)

    return groups


# ─────────────────────────────────────────────────────────────────
# ANALISE VIA GEMINI — segunda camada, aplica logica Portulanas
# ─────────────────────────────────────────────────────────────────

def call_gemini_with_retry(payload, max_retries=3, base_wait=15):
    """Chama a API do Gemini com retry automatico em caso de rate limit (429).
    Espera progressiva: 15s, 30s, 60s entre tentativas."""
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(GEMINI_URL, json=payload, timeout=20)
            if resp.status_code == 429:
                wait = base_wait * attempt
                print(f"[aviso] rate limit (429) na tentativa {attempt}/{max_retries}, aguardando {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            if attempt == max_retries:
                raise
            print(f"[aviso] erro HTTP na tentativa {attempt}/{max_retries}: {e}")
            time.sleep(base_wait)
    return None


def diversify_by_source(groups, max_items):
    """Seleciona grupos priorizando diversidade de fontes (round-robin),
    para evitar que uma fonte com volume alto (ex: Valor Economico)
    ocupe todas as vagas do limite por execucao, deixando as demais
    fontes praticamente invisiveis nos alertas."""
    if len(groups) <= max_items:
        return groups

    # Agrupa por fonte do item representativo de cada grupo (primeiro item)
    by_source = {}
    for g in groups:
        source = g[0]["source"]
        by_source.setdefault(source, []).append(g)

    selected = []
    sources_cycle = list(by_source.keys())
    idx_per_source = {s: 0 for s in sources_cycle}

    while len(selected) < max_items:
        progressed = False
        for source in sources_cycle:
            if len(selected) >= max_items:
                break
            i = idx_per_source[source]
            if i < len(by_source[source]):
                selected.append(by_source[source][i])
                idx_per_source[source] += 1
                progressed = True
        if not progressed:
            break  # todas as fontes esgotadas

    return selected


def pick_representative_item(group):
    """Escolhe o item mais informativo de um grupo (maior resumo)
    para servir de base da analise enviada ao Gemini."""
    return max(group, key=lambda it: len(it.get("summary", "")))


def analyze_batch_with_gemini(items):
    """Analisa uma lista de itens em UMA UNICA chamada ao Gemini, em vez
    de uma chamada por item. Reduz drasticamente o numero de requisicoes
    consumidas - essencial dado o limite diario restrito da conta atual.
    Retorna uma lista de analises na mesma ordem dos itens de entrada,
    ou lista de Nones nas posicoes onde nao foi possivel obter analise."""
    if not items:
        return []

    noticias_txt = ""
    for i, item in enumerate(items, start=1):
        noticias_txt += (
            f"\nNOTÍCIA {i}:\n"
            f"Fonte: {item['source']}\n"
            f"Título: {item['title']}\n"
            f"Resumo: {item['summary']}\n"
        )

    user_content = f"Analise as seguintes {len(items)} notícia(s):\n{noticias_txt}"

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": PORTULANAS_SYSTEM_PROMPT + "\n\n" + user_content}]}
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 600 * len(items) + 200,  # margem para overhead de formatacao da lista
        }
    }

    try:
        resp = call_gemini_with_retry(payload)
        if resp is None:
            print(f"[erro] sem resposta do gemini apos retries para batch de {len(items)} itens")
            return [None] * len(items)
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]

        # Limpar possiveis blocos de markdown ```json ... ```
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        parsed_list = json.loads(text)
        if not isinstance(parsed_list, list):
            # Seguranca: caso o modelo responda objeto solto em vez de lista
            parsed_list = [parsed_list]

        # Reordena pelos IDs para garantir alinhamento com a ordem original,
        # mesmo que o modelo retorne fora de ordem
        by_id = {}
        for entry in parsed_list:
            entry_id = entry.get("id")
            if entry_id is not None:
                by_id[entry_id] = entry

        results = []
        for i in range(1, len(items) + 1):
            results.append(by_id.get(i))

        return results
    except Exception as e:
        print(f"[erro] analise em batch falhou para {len(items)} itens: {e}")
        return [None] * len(items)


# ─────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[erro] falha ao enviar telegram: {e}")


def format_homolog_message(item, analysis):
    """Formata mensagem de homologacao incluindo o JSON crú do Gemini,
    para auditoria de que a logica Portulanas esta sendo seguida."""
    raw_json = json.dumps(analysis, ensure_ascii=False, indent=2)
    pub = item.get("published") or "data não disponível"
    msg = (
        f"⚠️⚠️⚠️ <b>SIMULAÇÃO DE TESTE — NÃO É ALERTA REAL</b> ⚠️⚠️⚠️\n"
        f"🧪 <b>HOMOLOGAÇÃO · AUDITORIA DE PROMPT</b>\n\n"
        f"<b>{item['title']}</b>\n"
        f"<i>{item['source']} · {pub}</i>\n\n"
        f"<b>JSON retornado pelo Gemini:</b>\n"
        f"<pre>{raw_json}</pre>\n\n"
        f"🔗 {item['link']}\n\n"
        f"⚠️ <i>Esta notícia foi forçada para teste, mesmo sem confirmação de relevância real. "
        f"Não use para decisão de mercado — é só auditoria do prompt.</i>"
    )
    return msg


def format_alert(group, representative_item, analysis):
    rel = analysis["relevancia"]
    emoji = {"ALTA": "🔴", "MEDIA": "🟡", "BAIXA": "⚪"}.get(rel, "⚪")

    canal_labels = {
        "juros": "Juros",
        "inflacao": "Inflação",
        "atividade_emprego": "Atividade / Emprego",
        "fiscal_politico": "Fiscal / Político",
        "fluxo_capital": "Fluxo de Capital",
    }
    canais = analysis.get("canais_afetados", []) or []
    canais_txt = " · ".join(canal_labels.get(c, c) for c in canais)

    origem_labels = {
        "domestica": "🇧🇷 Doméstica",
        "externa": "🌐 Externa",
        "ambas": "🇧🇷🌐 Doméstica + Externa",
    }
    origem_txt = origem_labels.get(analysis.get("origem"))

    pub = representative_item.get("published") or "data não disponível"

    header = (
        f"{emoji} <b>PORTULANAS · ALERTA {rel}</b>\n\n"
        f"<b>{representative_item['title']}</b>\n"
        f"<i>{representative_item['source']} · {pub}</i>\n\n"
        f"📋 {analysis['resumo']}\n\n"
    )

    # So mostra Canal/Origem quando a propria noticia deu base explicita
    # para isso (regra do prompt: sem inferencia). Se vazio, nao poluir
    # a mensagem com "nao identificado".
    if canais_txt:
        header += f"⚙️ <b>Canal:</b> {canais_txt}\n"
    if origem_txt:
        header += f"🧭 <b>Origem:</b> {origem_txt}\n"
    if canais_txt or origem_txt:
        header += "\n"

    header += f"💡 {analysis['leitura_critica']}\n\n"

    if len(group) > 1:
        # Mais de uma fonte trouxe titulo parecido - agrupado para o
        # operador revisar e decidir se e a mesma noticia ou nao.
        header += f"<b>📚 Possível mesmo assunto em {len(group)} fontes:</b>\n"
        for it in group:
            pub_it = it.get("published") or "s/ data"
            header += f"• <a href=\"{it['link']}\">{it['source']} · {pub_it}</a> — {it['title'][:70]}\n"
    else:
        header += f"🔗 {representative_item['link']}"

    return header


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────

def main():
    now_br = datetime.now(TZ_BR)
    print(f"[info] iniciando garimpo em {now_br.isoformat()} (homologacao={HOMOLOGACAO})")

    cache = prune_cache(load_cache())

    raw_items = collect_all_news()
    print(f"[info] {len(raw_items)} itens coletados de {len(FEEDS)} fontes")

    fresh_items = [it for it in raw_items if not is_news_too_old(it.get("published_dt_utc"))]
    discarded_old = len(raw_items) - len(fresh_items)
    if discarded_old > 0:
        print(f"[info] {discarded_old} itens descartados por serem mais antigos que {NEWS_MAX_AGE_HOURS}h")
    raw_items = fresh_items

    if HOMOLOGACAO:
        # Modo homologacao: ignora cache e filtro de palavras-chave.
        # Forca analise das N noticias mais recentes, so para auditoria
        # do formato e da fidelidade do prompt Portulanas. Usa batch
        # processing tambem - mesmo em teste, economiza cota.
        candidates = raw_items[:HOMOLOG_SAMPLE_SIZE]
        print(f"[info] modo homologacao: forcando analise de {len(candidates)} itens em 1 chamada batch (sem filtro/cache)")

        analyses = analyze_batch_with_gemini(candidates)

        sent_count = 0
        for item, analysis in zip(candidates, analyses):
            if analysis is None:
                print(f"[aviso] gemini nao retornou analise valida para '{item['title'][:50]}'")
                continue
            msg = format_homolog_message(item, analysis)
            send_telegram(msg)
            sent_count += 1
            time.sleep(0.5)

        print(f"[info] homologacao: {sent_count} mensagens de teste enviadas")
        return  # nao salva cache em modo homologacao, para nao interferir no garimpo real

    new_items = []
    for item in raw_items:
        h = item_hash(item["title"], item["link"])
        if h in cache:
            continue
        new_items.append(item)

    print(f"[info] {len(new_items)} itens novos (nao vistos antes)")

    candidates = [it for it in new_items if quick_relevance_check(it)]
    print(f"[info] {len(candidates)} itens passaram no filtro de palavras-chave")

    groups = group_similar_items(candidates)
    multi_source_groups = sum(1 for g in groups if len(g) > 1)
    print(f"[info] {len(groups)} grupos formados ({multi_source_groups} com mais de uma fonte)")

    # Limite de seguranca: o Gemini 2.5 Flash-Lite tem cota diaria de
    # ~1500 requisicoes no tier gratuito. Com execucao a cada 15 min
    # (96 execucoes/dia), processar mais de ~12 grupos por execucao
    # arrisca esgotar a cota antes do fim do dia. Os grupos excedentes
    # ficam descartados nesta rodada mas continuam disponiveis (nao
    # marcados como vistos) para a proxima execucao, 15 min depois -
    # por isso o cache so e atualizado para os itens dos grupos
    # efetivamente processados, mais abaixo.
    if len(groups) > MAX_GROUPS_PER_RUN:
        print(f"[aviso] {len(groups)} grupos excedem o limite de {MAX_GROUPS_PER_RUN} por execucao - selecionando com diversidade entre fontes")
        groups = diversify_by_source(groups, MAX_GROUPS_PER_RUN)

    sent_count = 0

    # Batch processing: uma unica chamada ao Gemini para todos os grupos
    # selecionados, em vez de uma chamada por grupo. Reduz drasticamente
    # o numero de requisicoes consumidas - essencial dado o limite diario
    # restrito observado na conta atual (20 req/dia).
    representatives = [pick_representative_item(g) for g in groups]
    analyses = analyze_batch_with_gemini(representatives) if representatives else []

    for group, representative, analysis in zip(groups, representatives, analyses):
        # Marca no cache todos os itens deste grupo - ele foi processado
        # (tentado), independente do resultado da analise. Isso evita
        # tentar a mesma noticia infinitamente se a analise falhar por
        # erro tecnico, mas tambem significa que uma falha custa a chance
        # daquela noticia ser re-analisada depois.
        for it in group:
            h = item_hash(it["title"], it["link"])
            cache[h] = time.time()

        if analysis is None:
            continue
        # Decisao de enviar ou nao e feita aqui no codigo, usando so a
        # relevancia - nunca pelo campo "ignorar" da IA, que pode
        # contradizer a propria classificacao (ex: relevancia=BAIXA
        # mas ignorar=false). BAIXA sempre e descartada.
        if analysis.get("relevancia") != "ALTA" and analysis.get("relevancia") != "MEDIA":
            continue

        msg = format_alert(group, representative, analysis)
        send_telegram(msg)
        sent_count += 1
        time.sleep(0.5)  # pequena pausa entre envios ao Telegram, evita rajada

    print(f"[info] {sent_count} alertas enviados")

    save_cache(cache)


if __name__ == "__main__":
    main()
