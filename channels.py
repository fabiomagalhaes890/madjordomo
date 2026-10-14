#!/usr/bin/env python3
"""
Camada de canal do Majordomo.

O Majordomo sempre fala com o usuário por um "canal". O canal pode ser:

  telegram  — direto, pelo bot (padrão). Exige que a máquina alcance o Telegram.
  github    — intermediário: grava as mensagens de saída num repositório e lê as
              respostas de lá. Para máquinas que bloqueiam o Telegram mas acessam Git.
  notion    — intermediário: usa um banco de dados do Notion como ponto de encontro.

Com um canal intermediário, as mensagens não chegam sozinhas ao seu Telegram: um
segundo Majordomo rodando em modo PONTE (bridge.py), numa máquina sua que TEM
Telegram, lê o intermediário e repassa, e devolve as suas respostas para lá.

Cada canal implementa dois métodos:
  send(text)      -> entrega uma mensagem de saída (do Majordomo para o usuário)
  poll()          -> devolve [(text, meta), ...] de mensagens de entrada novas
                     (do usuário para o Majordomo); meta é opaco e devolvido no ack.

O formato de uma "mensagem" trocada pelos intermediários é um JSON:
  {"id": "...", "dir": "out"|"in", "text": "...", "ts": epoch}
"out" = Majordomo→usuário; "in" = usuário→Majordomo.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def make_channel(cfg, state, save_state):
    kind = (cfg.get("channel") or "telegram").lower()
    if kind == "telegram":
        return TelegramChannel(cfg, state, save_state)
    if kind == "github":
        return GitHubChannel(cfg, state, save_state)
    if kind == "notion":
        return NotionChannel(cfg, state, save_state)
    raise ValueError(f"channel desconhecido: {kind}")


# ======================================================================
# Telegram — direto (comportamento original)
# ======================================================================
class TelegramChannel:
    MAX = 3900

    def __init__(self, cfg, state, save_state):
        self.cfg = cfg
        self.state = state
        self.save_state = save_state
        self.api = f"https://api.telegram.org/bot{cfg['telegram_token']}"

    def _tg(self, method, **params):
        req = urllib.request.Request(
            f"{self.api}/{method}", data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())

    def send(self, text):
        text = text or "(sem resposta)"
        for i in range(0, len(text), self.MAX):
            params = {"chat_id": self.cfg["chat_id"], "text": text[i:i + self.MAX]}
            if self.cfg.get("topic_id"):
                params["message_thread_id"] = self.cfg["topic_id"]
            try:
                self._tg("sendMessage", **params)
            except Exception as e:
                print("Telegram enviar:", e)

    def poll(self):
        out = []
        try:
            r = self._tg("getUpdates", offset=self.state.get("offset", 0),
                         timeout=25, allowed_updates=["message"])
        except Exception as e:
            print("Telegram poll:", e)
            return out
        for u in r.get("result", []):
            self.state["offset"] = u["update_id"] + 1
            self.save_state()
            m = u.get("message", {})
            if m.get("from", {}).get("id") != self.cfg["allowed_user_id"]:
                continue
            if self.cfg.get("topic_id") and m.get("message_thread_id") != self.cfg["topic_id"]:
                continue
            text = (m.get("text") or "").strip()
            if text:
                out.append((text, None))
        return out

    def ack(self, meta):
        pass  # Telegram usa offset, não precisa de ack por mensagem


# ======================================================================
# GitHub — intermediário via Contents API (um arquivo-fila por direção)
# ======================================================================
class GitHubChannel:
    """Usa um repositório como caixa de mensagens. Grava saídas em outbox.jsonl e
    lê respostas de inbox.jsonl (uma linha JSON por mensagem). A ponte faz o inverso.
    Só precisa de acesso HTTPS à api.github.com (quase sempre liberado onde Git funciona)."""

    def __init__(self, cfg, state, save_state):
        c = cfg["github_channel"]
        self.repo = c["repo"]                      # "usuario/repo"
        self.token = c["token"]                    # PAT com escopo repo
        self.branch = c.get("branch", "main")
        self.outbox = c.get("outbox", "outbox.jsonl")   # Majordomo -> usuário
        self.inbox = c.get("inbox", "inbox.jsonl")      # usuário -> Majordomo
        self.state = state
        self.save_state = save_state
        self.base = f"https://api.github.com/repos/{self.repo}/contents"

    def _get(self, path):
        url = f"{self.base}/{urllib.parse.quote(path)}?ref={self.branch}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            import base64
            content = base64.b64decode(d.get("content", "")).decode("utf-8", "replace")
            return content, d.get("sha")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "", None
            raise

    def _put(self, path, content, sha, msg):
        import base64
        body = {"message": msg, "branch": self.branch,
                "content": base64.b64encode(content.encode("utf-8")).decode()}
        if sha:
            body["sha"] = sha
        req = urllib.request.Request(
            f"{self.base}/{urllib.parse.quote(path)}", data=json.dumps(body).encode(),
            method="PUT", headers={"Authorization": f"Bearer {self.token}",
                                   "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def _append_line(self, path, obj, msg):
        for _ in range(3):  # tenta de novo em conflito de sha
            content, sha = self._get(path)
            content = content + json.dumps(obj, ensure_ascii=False) + "\n"
            try:
                self._put(path, content, sha, msg)
                return
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    time.sleep(1); continue
                raise

    def send(self, text):
        obj = {"id": uuid.uuid4().hex[:12], "dir": "out", "text": text or "", "ts": time.time()}
        try:
            self._append_line(self.outbox, obj, "majordomo: msg")
        except Exception as e:
            print("GitHub enviar:", e)

    def poll(self):
        """Lê linhas novas do inbox (respostas do usuário vindas pela ponte)."""
        out = []
        try:
            content, _ = self._get(self.inbox)
        except Exception as e:
            print("GitHub poll:", e)
            return out
        seen = set(self.state.setdefault("ch_seen", []))
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("dir") != "in" or obj.get("id") in seen:
                continue
            seen.add(obj["id"])
            text = (obj.get("text") or "").strip()
            if text:
                out.append((text, obj["id"]))
        self.state["ch_seen"] = list(seen)[-500:]
        self.save_state()
        return out

    def ack(self, meta):
        pass  # o "seen" já serve de ack


# ======================================================================
# Notion — intermediário via base de dados (uma página por mensagem)
# ======================================================================
class NotionChannel:
    """Usa um database do Notion como caixa de mensagens. Cada mensagem é uma página
    com propriedades: Texto (title), Direcao (select: out/in), Lida (checkbox)."""

    def __init__(self, cfg, state, save_state):
        c = cfg["notion_channel"]
        self.token = c["token"]            # integration token (secret_...)
        self.db = c["database_id"]
        self.state = state
        self.save_state = save_state
        self.h = {"Authorization": f"Bearer {self.token}",
                  "Content-Type": "application/json",
                  "Notion-Version": "2022-06-28"}

    def _req(self, method, url, body=None):
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=self.h)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def send(self, text):
        body = {"parent": {"database_id": self.db},
                "properties": {
                    "Texto": {"title": [{"text": {"content": (text or "")[:1900]}}]},
                    "Direcao": {"select": {"name": "out"}},
                    "Lida": {"checkbox": False}}}
        try:
            self._req("POST", "https://api.notion.com/v1/pages", body)
        except Exception as e:
            print("Notion enviar:", e)

    def poll(self):
        out = []
        body = {"filter": {"and": [
            {"property": "Direcao", "select": {"equals": "in"}},
            {"property": "Lida", "checkbox": {"equals": False}}]}}
        try:
            res = self._req("POST", f"https://api.notion.com/v1/databases/{self.db}/query", body)
        except Exception as e:
            print("Notion poll:", e)
            return out
        for page in res.get("results", []):
            title = page.get("properties", {}).get("Texto", {}).get("title", [])
            text = "".join(t.get("plain_text", "") for t in title).strip()
            if text:
                out.append((text, page["id"]))
        return out

    def ack(self, meta):
        """Marca a página como lida."""
        try:
            self._req("PATCH", f"https://api.notion.com/v1/pages/{meta}",
                      {"properties": {"Lida": {"checkbox": True}}})
        except Exception as e:
            print("Notion ack:", e)
