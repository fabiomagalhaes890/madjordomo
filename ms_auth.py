#!/usr/bin/env python3
"""
Autorização única do Microsoft 365 para o Majordomo (Outlook, Agenda e chats do Teams).
Usa o "device code": o script mostra um código, você abre microsoft.com/devicelogin
em qualquer navegador, entra com a conta do CLIENTE e digita o código.

Requer no config.json:
  "microsoft": {"client_id": "...", "tenant": "organizations"}
Gera ms_token.json (não compartilhe) e testa o que a conta consegue acessar.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
MS = json.loads((BASE / "config.json").read_text(encoding="utf-8"))["microsoft"]
TENANT = MS.get("tenant", "organizations")
SCOPES = MS.get("scopes", [
    "offline_access", "User.Read", "Mail.Read", "Calendars.Read", "Chat.Read", "ChatMessage.Send",
])
AUTH = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0"


def post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


dc = post(f"{AUTH}/devicecode", {"client_id": MS["client_id"], "scope": " ".join(SCOPES)})
if "device_code" not in dc:
    raise SystemExit(f"Erro ao iniciar login: {dc.get('error_description', dc)}")
print("\n" + dc["message"] + "\n")

interval = dc.get("interval", 5)
while True:
    time.sleep(interval)
    tok = post(f"{AUTH}/token", {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": MS["client_id"],
        "device_code": dc["device_code"],
    })
    err = tok.get("error")
    if err == "authorization_pending":
        continue
    if err == "slow_down":
        interval += 5
        continue
    if err:
        raise SystemExit(f"Login falhou: {err}\n{tok.get('error_description', '')}")
    break

(BASE / "ms_token.json").write_text(json.dumps({
    "refresh_token": tok["refresh_token"], "client_id": MS["client_id"], "tenant": TENANT,
    "scopes": SCOPES}, indent=2), encoding="utf-8")
print("✅ ms_token.json criado.\n\nTestando acessos:")


def graph(path):
    req = urllib.request.Request("https://graph.microsoft.com/v1.0" + path,
                                 headers={"Authorization": f"Bearer {tok['access_token']}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


tests = [
    ("Conta", "/me?$select=displayName,mail,userPrincipalName"),
    ("Outlook (e-mail)", "/me/mailFolders/inbox/messages?$top=1&$select=subject"),
    ("Agenda", "/me/events?$top=1&$select=subject"),
    ("Teams (chats)", "/me/chats?$top=1"),
]
for name, path in tests:
    try:
        r = graph(path)
        extra = f" — {r.get('mail') or r.get('userPrincipalName')}" if name == "Conta" else ""
        print(f"  ✅ {name}{extra}")
    except urllib.error.HTTPError as e:
        print(f"  ❌ {name}: HTTP {e.code} {e.read()[:200]!r}")
