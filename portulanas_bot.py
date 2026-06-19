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

# Fuso de Brasilia
TZ_BR = timezone(timedelta(hours=-3))

# Fontes RSS — feeds publicos e gratuitos
FEEDS = {
    "Investing.com":      "https://www.investing.com/rss/news_301.rss",
    "InfoMoney":          "https://www.infomoney.com.br/mercados/feed/",
    "InfoMoney Economia": "https://www.infomoney.com.br/economia/feed/",
    "Valor Economico":    "https://valor.globo.com/rss/valor",
    "ForexLive":          "https://www.forexlive.com/feed/news",
    "Estadao Economia":   "https://estadao.com.br/arc/outboundfeed/economia",
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

PORTULANAS_SYSTEM_PROMPT = """Você é o motor analítico do PORTULANAS, sistema de leitura macro da RIVOOS WEALTH, especializado em WDO (mini dólar futuro, B3).

Sua função: avaliar UMA notícia por vez e decidir se ela é relevante para quem opera WDO agora, aplicando a lógica de correlação do Trade System.

REGRAS DE CORRELAÇÃO COM O WDO (não invente outras):
- DXY sobe → WDO tende a subir (direta)
- Treasuries (preço do título) sobem → yield cai → USD menos atrativo → WDO tende a cair (inversa)
- Bolsas internacionais (S&P, Dow, DAX, etc.) sobem → risco-on → WDO tende a cair (inversa)
- Pares EUR/USD, GBP/USD, AUD/USD sobem → USD perde força → WDO tende a cair (inversa)
- USD/JPY, USD/CAD sobem → USD forte → WDO tende a subir (direta)
- Pares emergentes (USD/CNH, USD/MXN, USD/ZAR) sobem → USD forte global → WDO tende a subir (direta)
- DI Futuro sobe → WDO tende a subir junto (direta)
- WIN (mini índice B3) sobe → WDO tende a cair (inversa, correlação intraday, pode descorrelacionar)
- Petróleo (Brent/WTI) e Ouro: correlação CONTEXTUAL — avalie pelo conteúdo da notícia se o movimento é por risco geopolítico (tende a reforçar força do dólar) ou por outro motivo. Se não tiver certeza, marque como "contextual, requer leitura humana"
- VIX: NÃO é validador direto. É só visão geral de clima de risco. Nunca trate como confirmação de direção.
- Eventos de agenda (CPI, payroll, decisão de juros, Copom, PIB): sempre alta relevância, independente de correlação direta, pelo IMPACTO esperado em volatilidade.

REGRAS DE PTAX (se a notícia mencionar câmbio/PTAX):
- PTAX tem horários fixos: consultas 10h, 11h, 12h, 13h (cada uma 10min) e divulgação final a partir de 13h30.
- Lembre o leitor que, nesses horários, o comportamento técnico pode distorcer o movimento natural - não é hora de seguir cegamente uma notícia.

CLASSIFICAÇÃO DE RELEVÂNCIA (escolha uma):
- "ALTA" — evento com potencial de mover o mercado de forma imediata e relevante (decisão de juros, dado de inflação acima/abaixo do esperado, escalada geopolítica, fala de autoridade monetária)
- "MEDIA" — contribui para o quadro mas não é gatilho isolado (comentário de analista, dado secundário, fala de político sem novidade)
- "BAIXA" — ruído, não vale alertar

FORMATO DE SAÍDA — responda APENAS em JSON válido, sem markdown, sem texto antes ou depois:
{
  "relevancia": "ALTA" | "MEDIA" | "BAIXA",
  "resumo": "resumo da notícia em 2-4 frases, em português, cobrindo o que aconteceu, o contexto (quem disse o quê, qual dado saiu, qual número), e por que isso é relevante agora. Direto e sem floreio, mas completo — não corte informação só para ser breve.",
  "correlacao_wdo": "direta" | "inversa" | "contextual" | "agenda_volatilidade" | "neutra",
  "leitura_critica": "1-2 frases explicando o que isso significa para o viés do WDO agora, no estilo direto e técnico do Trade System. Se for contextual, diga isso explicitamente e não force uma direção.",
  "ignorar": true | false
}

Marque "ignorar": true se a notícia for BAIXA relevância ou não tiver relação nenhuma com macro/câmbio/juros/commodities/geopolítica relevante para o Brasil.

Seja extremamente direto. Sem jargão de mercado genérico, sem "fique atento", sem hedge excessivo de linguagem. O leitor é um trader profissional que vai decidir uma operação com base no que você disser.
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

def parse_published_date(entry):
    """Extrai a data de publicacao do item RSS e converte para horario
    de Brasilia. Retorna string formatada ou None se nao disponivel."""
    for field in ("published_parsed", "updated_parsed"):
        time_struct = entry.get(field)
        if time_struct:
            try:
                dt_utc = datetime(*time_struct[:6], tzinfo=timezone.utc)
                dt_br = dt_utc.astimezone(TZ_BR)
                return dt_br.strftime("%d/%m %H:%M")
            except Exception:
                continue
    return None


def fetch_feed(name, url, timeout=10):
    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (compatible; PortulanasBot/1.0)"
        })
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        items = []
        for entry in parsed.entries[:15]:
            items.append({
                "source": name,
                "title": entry.get("title", "").strip(),
                "link": entry.get("link", "").strip(),
                "summary": entry.get("summary", "")[:1200],
                "published": parse_published_date(entry),
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


def pick_representative_item(group):
    """Escolhe o item mais informativo de um grupo (maior resumo)
    para servir de base da analise enviada ao Gemini."""
    return max(group, key=lambda it: len(it.get("summary", "")))


def analyze_with_gemini(item):
    user_content = f"""Notícia para análise:

Fonte: {item['source']}
Título: {item['title']}
Resumo: {item['summary']}
Link: {item['link']}
"""

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": PORTULANAS_SYSTEM_PROMPT + "\n\n" + user_content}]}
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 600,
        }
    }

    try:
        resp = call_gemini_with_retry(payload)
        if resp is None:
            print(f"[erro] sem resposta do gemini apos retries para '{item['title'][:50]}'")
            return None
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]

        # Limpar possiveis blocos de markdown ```json ... ```
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        parsed = json.loads(text)
        return parsed
    except Exception as e:
        print(f"[erro] analise gemini falhou para '{item['title'][:50]}': {e}")
        return None


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
        f"🧪 <b>HOMOLOGAÇÃO · TESTE DE PROMPT</b>\n\n"
        f"<b>{item['title']}</b>\n"
        f"<i>{item['source']} · {pub}</i>\n\n"
        f"<b>JSON retornado pelo Gemini:</b>\n"
        f"<pre>{raw_json}</pre>\n\n"
        f"🔗 {item['link']}"
    )
    return msg


def format_alert(group, representative_item, analysis):
    rel = analysis["relevancia"]
    emoji = {"ALTA": "🔴", "MEDIA": "🟡", "BAIXA": "⚪"}.get(rel, "⚪")

    corr_label = {
        "direta": "↑ Direta",
        "inversa": "↓ Inversa",
        "contextual": "↕ Contextual",
        "agenda_volatilidade": "⚡ Agenda / Volatilidade",
        "neutra": "— Neutra",
    }.get(analysis.get("correlacao_wdo", ""), "—")

    pub = representative_item.get("published") or "data não disponível"

    header = (
        f"{emoji} <b>PORTULANAS · ALERTA {rel}</b>\n\n"
        f"<b>{representative_item['title']}</b>\n"
        f"<i>{representative_item['source']} · {pub}</i>\n\n"
        f"📋 {analysis['resumo']}\n\n"
        f"🎯 <b>Correlação WDO:</b> {corr_label}\n"
        f"💡 {analysis['leitura_critica']}\n\n"
    )

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

    if HOMOLOGACAO:
        # Modo homologacao: ignora cache e filtro de palavras-chave.
        # Forca analise das N noticias mais recentes, so para auditoria
        # do formato e da fidelidade do prompt Portulanas.
        candidates = raw_items[:HOMOLOG_SAMPLE_SIZE]
        print(f"[info] modo homologacao: forcando analise de {len(candidates)} itens (sem filtro/cache)")

        sent_count = 0
        for item in candidates:
            analysis = analyze_with_gemini(item)
            if analysis is None:
                print(f"[aviso] gemini nao retornou analise valida para '{item['title'][:50]}'")
                continue
            msg = format_homolog_message(item, analysis)
            send_telegram(msg)
            sent_count += 1
            time.sleep(2)

        print(f"[info] homologacao: {sent_count} mensagens de teste enviadas")
        return  # nao salva cache em modo homologacao, para nao interferir no garimpo real

    new_items = []
    for item in raw_items:
        h = item_hash(item["title"], item["link"])
        if h in cache:
            continue
        cache[h] = time.time()
        new_items.append(item)

    print(f"[info] {len(new_items)} itens novos (nao vistos antes)")

    candidates = [it for it in new_items if quick_relevance_check(it)]
    print(f"[info] {len(candidates)} itens passaram no filtro de palavras-chave")

    groups = group_similar_items(candidates)
    multi_source_groups = sum(1 for g in groups if len(g) > 1)
    print(f"[info] {len(groups)} grupos formados ({multi_source_groups} com mais de uma fonte)")

    sent_count = 0
    for group in groups:
        representative = pick_representative_item(group)
        analysis = analyze_with_gemini(representative)
        if analysis is None:
            continue
        if analysis.get("ignorar", True):
            continue
        if analysis.get("relevancia") == "BAIXA":
            continue

        msg = format_alert(group, representative, analysis)
        send_telegram(msg)
        sent_count += 1
        time.sleep(1)  # respeitar rate limit do Gemini free tier

    print(f"[info] {sent_count} alertas enviados")

    save_cache(cache)


if __name__ == "__main__":
    main()
