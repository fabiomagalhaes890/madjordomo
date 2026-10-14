#!/usr/bin/env python3
"""
Ponte do Majordomo.

Roda numa máquina SUA que tem Telegram. Fica lendo o intermediário (GitHub ou
Notion) onde um Majordomo de máquina bloqueada deixa as mensagens, e:
  - repassa cada mensagem de SAÍDA (do Majordomo) para o seu Telegram;
  - pega o que você responde no Telegram e grava como ENTRADA no intermediário,
    para o Majordomo bloqueado ler.

Assim o Telegram continua sendo o ponto central, mesmo para a máquina que não o
alcança. Use o MESMO intermediário nos dois lados.

Config: reaproveita o config.json, no bloco "bridge":
  "bridge": {
    "intermediary": "github",        // "github" ou "notion" (igual ao da máquina bloqueada)
    "telegram_token": "TOKEN_DO_BOT_DA_PONTE",
    "allowed_user_id": 123456789,
    "chat_id": -1001234567890,
    "topic_id": 7,
    "poll_sec": 15
  }
E o bloco do intermediário (github_channel OU notion_channel), idêntico ao da
máquina bloqueada, para apontar para o mesmo repositório/database.

    python bridge.py
"""
import json
import time
import urllib.request
from pathlib import Path

import channels as _channels

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
BR = CFG.get("bridge") or {}
STATE = BASE / "bridge_state.json"
API = f"https://api.telegram.org/bot{BR['telegram_token']}"


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"offset": 0, "seen_out": []}


state = load_state()


def save_state():
    state["seen_out"] = state.get("seen_out", [])[-500:]
    STATE.write_text(json.dumps(state), encoding="utf-8")


def tg(method, **params):
    req = urllib.request.Request(f"{API}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def tg_send(text):
    for i in range(0, len(text or " "), 3900):
        p = {"chat_id": BR["chat_id"], "text": (text or " ")[i:i + 3900]}
        if BR.get("topic_id"):
            p["message_thread_id"] = BR["topic_id"]
        try:
            tg("sendMessage", **p)
        except Exception as e:
            print("Telegram enviar:", e)


def make_intermediary():
    """Constrói o canal intermediário com a MESMA config da máquina bloqueada,
    mas aqui a ponte lê o OUTBOX (saídas do Majordomo) e escreve no INBOX."""
    kind = BR.get("intermediary", "github")
    fake_cfg = dict(CFG)
    fake_cfg["channel"] = kind
    return _channels.make_channel(fake_cfg, state, save_state), kind


def read_outbox(ch, kind):
    """Lê mensagens de SAÍDA (dir=out) ainda não repassadas ao Telegram."""
    msgs = []
    seen = set(state.get("seen_out", []))
    if kind == "github":
        content, _ = ch._get(ch.outbox)
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("dir") == "out" and o.get("id") not in seen:
                msgs.append((o["id"], o.get("text", "")))
    elif kind == "notion":
        body = {"filter": {"and": [
            {"property": "Direcao", "select": {"equals": "out"}},
            {"property": "Lida", "checkbox": {"equals": False}}]}}
        res = ch._req("POST", f"https://api.notion.com/v1/databases/{ch.db}/query", body)
        for page in res.get("results", []):
            title = page.get("properties", {}).get("Texto", {}).get("title", [])
            text = "".join(t.get("plain_text", "") for t in title)
            msgs.append((page["id"], text))
    return msgs


def mark_out_seen(ch, kind, mid):
    seen = set(state.get("seen_out", []))
    seen.add(mid)
    state["seen_out"] = list(seen)
    save_state()
    if kind == "notion":  # marca a página como lida
        ch.ack(mid)


def write_inbox(ch, kind, text):
    """Grava uma resposta sua como ENTRADA (dir=in) para o Majordomo bloqueado ler."""
    if kind == "github":
        import uuid
        ch._append_line(ch.inbox, {"id": uuid.uuid4().hex[:12], "dir": "in",
                                   "text": text, "ts": time.time()}, "ponte: resposta")
    elif kind == "notion":
        body = {"parent": {"database_id": ch.db},
                "properties": {"Texto": {"title": [{"text": {"content": text[:1900]}}]},
                               "Direcao": {"select": {"name": "in"}},
                               "Lida": {"checkbox": False}}}
        ch._req("POST", "https://api.notion.com/v1/pages", body)


def poll_telegram():
    """Suas respostas no Telegram -> viram entradas no intermediário."""
    try:
        r = tg("getUpdates", offset=state.get("offset", 0), timeout=20, allowed_updates=["message"])
    except Exception as e:
        print("Telegram poll:", e)
        return []
    out = []
    for u in r.get("result", []):
        state["offset"] = u["update_id"] + 1
        save_state()
        m = u.get("message", {})
        if m.get("from", {}).get("id") != BR["allowed_user_id"]:
            continue
        if BR.get("topic_id") and m.get("message_thread_id") != BR["topic_id"]:
            continue
        t = (m.get("text") or "").strip()
        if t:
            out.append(t)
    return out


def main():
    ch, kind = make_intermediary()
    tg_send(f"🌉 Ponte online (intermediário: {kind}).")
    print(f"Ponte rodando. Intermediário: {kind}. Ctrl+C para sair.")
    while True:
        try:
            # 1) saídas do Majordomo -> Telegram
            for mid, text in read_outbox(ch, kind):
                tg_send(text)
                mark_out_seen(ch, kind, mid)
            # 2) suas respostas no Telegram -> intermediário
            for text in poll_telegram():
                write_inbox(ch, kind, text)
            time.sleep(BR.get("poll_sec", 15))
        except KeyboardInterrupt:
            print("Encerrado.")
            return
        except Exception as e:
            print("Erro:", e)
            time.sleep(5)


if __name__ == "__main__":
    if not BR:
        print('Falta o bloco "bridge" no config.json.')
        raise SystemExit(1)
    main()
