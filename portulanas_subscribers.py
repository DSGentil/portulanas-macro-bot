"""
Portulanas Macro Bot — Gerenciador de Assinantes
RIVOOS WEALTH · DG

Faz polling na API do Telegram (getUpdates) para detectar quando
alguem aperta /start ou manda qualquer mensagem pro bot. Cada pessoa
nova e adicionada a uma lista persistente de assinantes
(subscribers.json) e recebe uma mensagem de boas-vindas na hora.

Essa lista e usada pelo portulanas_bot.py e portulanas_panorama.py
para enviar as notificacoes a TODOS os assinantes, nao mais a um
unico chat_id fixo.

Roda via GitHub Actions com frequencia propria (ex: a cada 15-30 min),
de forma independente do garimpo de noticias - assim alguem que aperta
Start e reconhecido rapido, mesmo entre as janelas fixas do garimpo.
"""

import os
import json
import requests

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

SUBSCRIBERS_FILE = "subscribers.json"
OFFSET_FILE = "telegram_update_offset.json"

WELCOME_MESSAGE = (
    "🧭 <b>Bem-vindo ao Portulanas</b>\n\n"
    "Sou o farol macro da RIVOOS WEALTH. Garimpo notícias de mercado o dia inteiro "
    "e te aviso, em janelas fixas, o que realmente importa para quem acompanha "
    "o dólar futuro (WDO).\n\n"
    "Cada rodada traz até 6 notícias: as mais relevantes do momento em juros, "
    "inflação, política fiscal, fluxo de capital — e sempre um bloco dedicado "
    "a ações e fundos imobiliários.\n\n"
    "Não precisa de nenhum comando seu. As mensagens chegam automaticamente, "
    "nos horários programados."
)

RETURNING_MESSAGE = (
    "👋 Você já está inscrito no Portulanas — as notificações continuam chegando "
    "normalmente nos horários programados. Não é necessário fazer mais nada."
)


def load_subscribers():
    if not os.path.exists(SUBSCRIBERS_FILE):
        return []
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_subscribers(subscribers):
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(subscribers, f, indent=2)


def load_offset():
    if not os.path.exists(OFFSET_FILE):
        return 0
    try:
        with open(OFFSET_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("offset", 0)
    except Exception:
        return 0


def save_offset(offset):
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)


def get_updates(offset):
    """Busca atualizacoes novas do Telegram a partir do offset salvo.
    O parametro offset garante que updates ja processados nao voltem
    a aparecer (comportamento padrao da API getUpdates)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 5}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", [])
    except Exception as e:
        print(f"[erro] falha ao buscar updates: {e}")
        return []


def send_telegram_to(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[erro] falha ao enviar mensagem para {chat_id}: {e}")


def main():
    subscribers = load_subscribers()
    offset = load_offset()

    print(f"[info] assinantes atuais: {len(subscribers)}")
    print(f"[info] buscando updates a partir do offset {offset}")

    updates = get_updates(offset)
    print(f"[info] {len(updates)} updates novos encontrados")

    new_subscribers = 0
    max_update_id = offset - 1 if offset > 0 else 0

    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None:
            max_update_id = max(max_update_id, update_id)

        message = update.get("message")
        if not message:
            continue

        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is None:
            continue

        chat_id_str = str(chat_id)

        if chat_id_str in subscribers:
            # Ja e assinante - se mandou /start de novo, confirma que
            # ja esta inscrito (evita silencio total, mas nao duplica
            # na lista).
            text = message.get("text", "")
            if text.strip() == "/start":
                send_telegram_to(chat_id, RETURNING_MESSAGE)
            continue

        # Assinante novo - adiciona a lista e manda boas-vindas
        subscribers.append(chat_id_str)
        new_subscribers += 1
        send_telegram_to(chat_id, WELCOME_MESSAGE)
        print(f"[info] novo assinante adicionado: {chat_id_str}")

    if new_subscribers > 0:
        save_subscribers(subscribers)
        print(f"[info] {new_subscribers} novo(s) assinante(s) salvos. Total agora: {len(subscribers)}")
    else:
        print("[info] nenhum assinante novo nesta rodada")

    if updates:
        save_offset(max_update_id + 1)


if __name__ == "__main__":
    main()
