#!/usr/bin/env python3
"""
Autorização única do Google para o Majordomo (Gmail e Agenda, SOMENTE LEITURA).
Rode uma vez: python google_auth.py
Precisa do arquivo google_client.json (baixado do Google Cloud Console) nesta pasta.
Gera google_token.json, que o Majordomo usa daqui pra frente. Não compartilhe esse arquivo.
"""
import base64
import hashlib
import http.server
import json
import secrets
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
client = json.loads((BASE / "google_client.json").read_text(encoding="utf-8"))
c = client.get("installed") or client.get("web")
SCOPES = " ".join([
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
])
PORT = 8765
REDIRECT = f"http://127.0.0.1:{PORT}"

verifier = secrets.token_urlsafe(64)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
csrf = secrets.token_urlsafe(16)
auth_url = c["auth_uri"] + "?" + urllib.parse.urlencode({
    "client_id": c["client_id"],
    "redirect_uri": REDIRECT,
    "response_type": "code",
    "scope": SCOPES,
    "access_type": "offline",
    "prompt": "consent",
    "code_challenge": challenge,
    "code_challenge_method": "S256",
    "state": csrf,
})

result = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [None])[0] == csrf:
            result["code"] = q.get("code", [None])[0]
            result["error"] = q.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        msg = "Pronto! Pode fechar esta aba." if result.get("code") else "Falhou. Veja o terminal."
        self.wfile.write(f"<h2>{msg}</h2>".encode("utf-8"))

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
print("Abrindo o navegador para autorizar. Se não abrir, acesse:\n", auth_url)
webbrowser.open(auth_url)
while "code" not in result and "error" not in result:
    server.handle_request()

if not result.get("code"):
    raise SystemExit(f"Autorização negada ou bloqueada: {result.get('error')}")

data = urllib.parse.urlencode({
    "code": result["code"],
    "client_id": c["client_id"],
    "client_secret": c.get("client_secret", ""),
    "redirect_uri": REDIRECT,
    "grant_type": "authorization_code",
    "code_verifier": verifier,
}).encode()
with urllib.request.urlopen(urllib.request.Request(c["token_uri"], data=data), timeout=30) as r:
    tok = json.loads(r.read())

if "refresh_token" not in tok:
    raise SystemExit("O Google não devolveu refresh_token. Remova o acesso do app na sua conta e rode de novo.")

(BASE / "google_token.json").write_text(json.dumps({
    "refresh_token": tok["refresh_token"],
    "client_id": c["client_id"],
    "client_secret": c.get("client_secret", ""),
    "token_uri": c["token_uri"],
}, indent=2), encoding="utf-8")
print("✅ google_token.json criado. Agora reinicie o Majordomo.")
