"""
Portulanas Macro Bot — Rodada Programada
RIVOOS WEALTH · DG

Gera um panorama macro obrigatorio (abertura do dia ou checkpoint
hora a hora), mesmo que nao haja noticia nova de alta relevancia.

Roda via GitHub Actions em horarios fixos:
- 08:30 BRT -> panorama de abertura
- 09:00, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 16:00 BRT -> checkpoint hora a hora
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]

# Migrado de Gemini para Groq (mesma razao do portulanas_bot.py: cota
# do Gemini ficou limitada a 20 req/dia na conta atual).
GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"

TZ_BR = timezone(timedelta(hours=-3))

ROUND_TYPE = sys.argv[1] if len(sys.argv) > 1 else "checkpoint"  # "abertura" ou "checkpoint"

PANORAMA_PROMPT = """Você é o motor do PORTULANAS, sistema de leitura macro da RIVOOS WEALTH para WDO (mini dólar futuro, B3).

Gere um {tipo} curto e direto, em português, para um trader profissional que está operando WDO agora.

Estrutura da resposta (texto simples, sem markdown, pode usar emojis simples):
1. Uma linha de abertura com o horário e o tipo de rodada
2. Panorama rápido: como estão DXY, treasuries, bolsas internacionais e petróleo nas últimas horas (responda com base no seu conhecimento mais recente de contexto de mercado, sem inventar números específicos de preço — fale em termos de direção e força: "subindo com força", "estável", "pressionado", etc.)
3. Viés sugerido para WDO no momento: alta, baixa ou neutro, com uma frase de justificativa
4. Um lembrete rápido se houver evento de agenda relevante próximo (CPI, payroll, Copom, Fed, PTAX) — se não souber de nenhum evento específico, diga "sem evento de agenda conhecido nas próximas horas, mas confirme no calendário oficial"

Seja direto, sem floreio, sem "fique atento" genérico. Máximo 6 linhas de texto corrido.
"""


def call_groq_with_retry(payload, max_retries=3, base_wait=15):
    """Chama a API do Groq com retry automatico em caso de rate limit (429).
    Espera progressiva: 15s, 30s, 60s entre tentativas."""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(GROQ_URL, json=payload, headers=headers, timeout=20)
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


def ask_groq_panorama(tipo_label):
    prompt = PANORAMA_PROMPT.format(tipo=tipo_label)
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 350,
    }
    try:
        resp = call_groq_with_retry(payload)
        if resp is None:
            print("[erro] sem resposta do groq apos retries")
            return None
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[erro] groq panorama falhou: {e}")
        return None


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[erro] envio telegram falhou: {e}")


def main():
    now_br = datetime.now(TZ_BR)
    hora = now_br.strftime("%H:%M")

    if ROUND_TYPE == "abertura":
        tipo_label = "panorama de abertura do dia"
        header = f"PORTULANAS - PANORAMA DE ABERTURA - {hora}\n\n"
    else:
        tipo_label = "checkpoint horário obrigatório"
        header = f"PORTULANAS - CHECKPOINT {hora}\n\n"

    texto = ask_groq_panorama(tipo_label)
    if texto is None:
        texto = "Não foi possível gerar o panorama agora — falha na consulta ao motor de análise. Verifique manualmente o painel macro."

    send_telegram(header + texto)
    print(f"[info] rodada '{ROUND_TYPE}' enviada às {hora}")


if __name__ == "__main__":
    main()
