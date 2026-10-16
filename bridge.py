#!/usr/bin/env python3
"""
Ponte do Majordomo (multi-PC).

Roda numa máquina SUA que tem Telegram. Monitora um ou mais "nós" (cada nó é um
Majordomo numa máquina bloqueada) através de um intermediário (GitHub, GitLab ou
Notion) e repassa as mensagens para o seu Telegram, cada nó no seu próprio TÓPICO.
As suas respostas, dadas no tópico de cada nó, voltam para o nó certo.

Assim o Telegram segue sendo o ponto central mesmo para máquinas que não o alcançam,
e vários PCs convivem sem se misturar.

Config (bloco "bridge" no config.json):
  "bridge": {
    "intermediary": "github",            // "github" | "gitlab" | "notion"
    "telegram_token": "TOKEN_DO_BOT_DA_PONTE",
    "allowed_user_id": 123456789,
    "chat_id": -1001234567890,
    "poll_sec": 15,
    "nodes": [                           // um por PC bloqueado; topic_id = tópico no grupo
      {"node": "node-1", "topic_id": 11},
      {"node": "node-2", "topic_id": 12}
    ]
  }
E o bloco do intermediário (github_channel | gitlab_channel | notion_channel),
apontando para o MESMO repositório/database que os nós usam.

    python bridge.py
"""
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import channels as _channels

BASE = Path(__file__).resolve().parent
CFG = _channels.load_config(BASE / "config.json")
BR = CFG.get("bridge") or {}
STATE = BASE / "bridge_state.json"
API = f"https://api.telegram.org/bot{BR.get('telegram_token', '')}"


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {"offset": 0, "seen_out": {}}


state = load_state()


def save_state():
    for k in state.get("seen_out", {}):
        state["seen_out"][k] = state["seen_out"][k][-500:]
    STATE.write_text(json.dumps(state), encoding="utf-8")


def tg(method, **params):
    req = urllib.request.Request(f"{API}/{method}", data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def tg_send(text, topic_id):
    for i in range(0, len(text or " "), 3900):
        p = {"chat_id": BR["chat_id"], "text": (text or " ")[i:i + 3900]}
        if topic_id:
            p["message_thread_id"] = topic_id
        try:
            tg("sendMessage", **p)
        except urllib.error.HTTPError as e:
            print("Telegram enviar:", e, e.read().decode("utf-8", "replace"))
        except Exception as e:
            print("Telegram enviar:", e)


def discover_ids():
    """chat_id/allowed_user_id ainda zerados: mostra os IDs das mensagens que chegarem ao bot."""
    print("chat_id/allowed_user_id não configurados no bloco \"bridge\".\n"
          "Mande uma mensagem ao bot da ponte (no grupo/tópico desejado) e copie os IDs abaixo. Ctrl+C para sair.")
    offset = 0
    while True:
        try:
            r = tg("getUpdates", offset=offset, timeout=20, allowed_updates=["message"])
        except KeyboardInterrupt:
            return
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message", {})
            chat = m.get("chat", {})
            print(f'allowed_user_id: {m.get("from", {}).get("id")}  chat_id: {chat.get("id")}  '
                  f'topic_id: {m.get("message_thread_id")}  ({chat.get("title") or chat.get("type")})')


def channel_for(node):
    """Cria o canal intermediário apontando para o nó dado (lê outbox, escreve inbox)."""
    kind = BR.get("intermediary", "github")
    cfg = dict(CFG)
    cfg["channel"] = kind
    cfg["node"] = node
    return _channels.make_channel(cfg, state, save_state), kind


def read_outbox(ch, kind):
    """Mensagens de SAÍDA (dir=out) do nó ainda não repassadas."""
    msgs = []
    if kind in ("github", "gitlab"):
        content, _ = ch._get(ch.outbox)
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("dir") == "out":
                msgs.append((o.get("id"), o.get("text", "")))
    elif kind == "notion":
        body = {"filter": {"and": [
            {"property": "Direcao", "select": {"equals": "out"}},
            {"property": "No", "rich_text": {"equals": ch.node}},
            {"property": "Lida", "checkbox": {"equals": False}}]}}
        res = ch._req("POST", f"https://api.notion.com/v1/databases/{ch.db}/query", body)
        for page in res.get("results", []):
            title = page.get("properties", {}).get("Texto", {}).get("title", [])
            text = "".join(t.get("plain_text", "") for t in title)
            msgs.append((page["id"], text))
    return msgs


def mark_seen(ch, kind, node, mid):
    seen = state.setdefault("seen_out", {}).setdefault(node, [])
    if mid not in seen:
        seen.append(mid)
    save_state()
    if kind == "notion":
        ch.ack(mid)


def write_inbox(ch, kind, text):
    """Grava a sua resposta como ENTRADA (dir=in) para o nó ler."""
    if kind in ("github", "gitlab"):
        ch._append_line(ch.inbox, {"id": uuid.uuid4().hex[:12], "dir": "in",
                                   "text": text, "ts": time.time()}, "ponte: resposta")
    elif kind == "notion":
        body = {"parent": {"database_id": ch.db},
                "properties": {"Texto": {"title": [{"text": {"content": text[:1900]}}]},
                               "Direcao": {"select": {"name": "in"}},
                               "No": {"rich_text": [{"text": {"content": ch.node}}]},
                               "Lida": {"checkbox": False}}}
        ch._req("POST", "https://api.notion.com/v1/pages", body)


# Comandos que pertencem aos scripts auxiliares, pelo sufixo do nó. Num tópico com vários
# nós, a resposta vai para o nó dono do comando; o resto (texto livre inclusive) vai para o
# nó principal (o que não tem "commands" nem sufixo conhecido).
ROLE_COMMANDS = {
    "-watch": ["/consertar", "/cancelar", "/br"],
    "-reuniao": ["/gravar", "/parar_gravacao", "/status_reuniao"],
}


def node_commands(n):
    if "commands" in n:
        return [c.lower() for c in n["commands"]]
    for suffix, cmds in ROLE_COMMANDS.items():
        if n["node"].endswith(suffix):
            return cmds
    return []


def route(nodes_in_topic, text):
    """Escolhe o nó do tópico que recebe a mensagem, pelo comando."""
    cmd = text.split(" ", 1)[0].split("@")[0].lower()
    default = None
    for node, cmds in nodes_in_topic:
        if cmd in cmds:
            return node
        if not cmds and default is None:
            default = node
    return default or nodes_in_topic[0][0]


def poll_telegram(topic_to_node):
    """Suas respostas no Telegram -> (node, texto). Usa o tópico (e o comando) para saber o nó."""
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
        tid = m.get("message_thread_id")
        nodes_in_topic = topic_to_node.get(tid)
        if not nodes_in_topic:
            print(f"Ignorada: mensagem fora de um tópico de nó (topic_id={tid})")
            continue
        t = (m.get("text") or "").strip()
        if t:
            out.append((route(nodes_in_topic, t), t))
    return out


def main():
    nodes = BR.get("nodes") or []
    if not nodes:
        print('Bloco "bridge" precisa de "nodes": [{"node": "...", "topic_id": N}, ...]')
        raise SystemExit(1)
    # um canal por nó + mapa tópico->[(nó, comandos)] (vários nós podem dividir um tópico)
    chans = {}
    topic_to_node = {}
    for n in nodes:
        ch, kind = channel_for(n["node"])
        chans[n["node"]] = (ch, kind, n.get("topic_id"))
        topic_to_node.setdefault(n.get("topic_id"), []).append((n["node"], node_commands(n)))

    kind0 = BR.get("intermediary", "github")
    tg_send(f"🌉 Ponte online · {kind0} · {len(nodes)} nó(s): " +
            ", ".join(n["node"] for n in nodes), None)
    print(f"Ponte rodando. Intermediário: {kind0}. Nós: {[n['node'] for n in nodes]}")

    while True:
        try:
            # 1) saídas de cada nó -> seu tópico no Telegram
            for node, (ch, kind, topic) in chans.items():
                for mid, text in read_outbox(ch, kind):
                    seen = state.get("seen_out", {}).get(node, [])
                    if mid in seen:
                        continue
                    tg_send(text, topic)
                    mark_seen(ch, kind, node, mid)
            # 2) suas respostas -> inbox do nó certo (pelo tópico)
            for node, text in poll_telegram(topic_to_node):
                ch, kind, _ = chans[node]
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
    if not BR.get("chat_id") or not BR.get("allowed_user_id"):
        discover_ids()
        raise SystemExit(0)
    main()
