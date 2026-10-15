#!/usr/bin/env python3
"""
Vigia de navegador para Teams ou Slack web (DMs e menções a você), com
autoconserto assistido quando o layout da tela muda.

NÃO instala nada no Teams/Slack: abre a versão web num perfil de navegador
próprio e lê a tela. Usa o mesmo canal do Majordomo (Telegram direto, ou
GitHub/GitLab/Notion pela ponte, no nó "<node>-watch").

    pip install playwright
    playwright install chromium
    python browser_watch.py              # 1a vez: faça login na janela
    python browser_watch.py --headless   # depois, sem janela

Como detecta que "ficou cego": o Teams/Slack mostram o total de não lidos no
TÍTULO da aba (ex.: "(3) Microsoft Teams"). Se o título indica não lidos mas o
vigia não acha nenhum item na lista, provavelmente o layout mudou. Ele captura
print + HTML e te pergunta no Telegram. Você confere que há mensagem e manda
/consertar.

Fluxo do /consertar:
  1) chama o Claude Code (claude -p) apontando para o HTML salvo, pedindo para
     atualizar só o selectors.json (nunca a lógica);
  2) recarrega os seletores e faz um AUTOTESTE (acha lista + caixa de texto);
  3) passou -> volta a vigiar sozinho e avisa; falhou -> avisa e espera você.

Config (bloco "browser_watch" no config.json do Majordomo):
  "browser_watch": {
    "platform": "teams",
    "poll_min": 2,
    "my_names": ["Seu Nome", "Nome Completo"],
    "profile_dir": "C:/majordomo/brchrome",
    "claude_cmd": "claude",
    "claude_dir": "C:/majordomo"
  }
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Instale o Playwright: pip install playwright && playwright install chromium")
    sys.exit(1)

import channels as _channels

BASE = Path(__file__).resolve().parent
CFG = _channels.load_config(BASE / "config.json")
BW = CFG.get("browser_watch") or {}
SEL_FILE = BASE / "selectors.json"
STATE = BASE / "browser_state.json"
EVID = BASE / "evidencia"


def load_state():
    base = {"seen": [], "refs": {}, "ref_n": 0, "offset": 0, "mode": "ok"}
    if STATE.exists():
        base.update(json.loads(STATE.read_text(encoding="utf-8")))
    if "tg_offset" in base:  # nome antigo do offset do Telegram
        base["offset"] = base.pop("tg_offset")
    return base


st = load_state()


def save_state():
    st["seen"] = st["seen"][-500:]
    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")


# Mesmo canal do Majordomo (telegram direto ou intermediário, no nó "<node>-watch").
# tg_timeout=0: o laço de vigia não pode ficar preso no long-poll.
CHANNEL = _channels.open_channel(CFG, st, save_state, role="watch", tg_timeout=0)
# intervalo entre consultas aos comandos enquanto espera um /consertar
CMD_EVERY = 5 if _channels.is_direct(CFG) else max(5, CFG.get("poll_sec", 15))


def send(text):
    CHANNEL.send(text)


def load_sel():
    return json.loads(SEL_FILE.read_text(encoding="utf-8"))[BW["platform"]]


def poll_commands():
    respostas, acao = [], None
    try:
        msgs = CHANNEL.poll()
    except Exception as e:
        print("Canal:", e)
        return respostas, acao
    for text, meta in msgs:
        CHANNEL.ack(meta)
        low = text.split("@")[0].lower()
        if low == "/consertar":
            acao = "consertar"
        elif low == "/cancelar":
            acao = "cancelar"
        elif text.startswith("/br "):
            ref, _, body = text[4:].partition(" ")
            if body.strip():
                respostas.append((ref.strip(), body.strip()))
    save_state()
    return respostas, acao


def title_unread(page):
    try:
        t = page.title() or ""
    except Exception:
        return -1
    m = re.search(r"\((\d+)\)", t)
    if m:
        return int(m.group(1))
    return 1 if re.search(r"•|\*", t) else 0


def find_unread(page, s):
    return page.query_selector_all(s["unread"])


def open_and_read(page, s, handle, n=3):
    """Clica na conversa e devolve as últimas n mensagens como 'Autor: texto'."""
    handle.click()
    page.wait_for_timeout(1800)
    out, last_sender = [], ""
    conts = page.query_selector_all(s.get("msg_container", "[data-qa='message_container']"))
    for c in conts[-n:]:
        se = c.query_selector(s.get("msg_sender", "")) if s.get("msg_sender") else None
        sender = (se.inner_text().strip() if se else "") or last_sender
        last_sender = sender or last_sender
        te = c.query_selector(s.get("msg_text", "")) if s.get("msg_text") else None
        txt = (te.inner_text().strip() if te else "")
        if txt:
            out.append(f"{sender}: {short(txt, 280)}" if sender else short(txt, 280))
    return "\n".join(out) if out else "(não consegui ler o texto)"


def check_unread(page, s, names):
    for it in find_unread(page, s):
        label = ""
        if s.get("name_el"):
            el = it.query_selector(s["name_el"])
            if el:
                label = (el.inner_text() or "").strip()
        if not label:
            label = (it.get_attribute(s["name_attr"]) or it.inner_text() or "").strip()
        if not label:
            continue
        who = label.split(",")[0].split("\n")[0].strip()[:60]
        if BW["platform"] == "teams":
            lab = label.lower()
            if not (any(n in lab for n in names) or
                    not any(k in lab for k in ("channel", "canal", "equipe", "team", "general"))):
                continue
        fp = f"{BW['platform']}:{who}"
        if fp in st["seen"]:
            continue
        st["seen"].append(fp)
        st["ref_n"] += 1
        ref = f"b{st['ref_n']}"
        st["refs"][ref] = who
        for k in list(st["refs"])[:-50]:
            st["refs"].pop(k, None)
        corpo = "Mensagem nova (abra para ler)."
        if s.get("msg_text"):
            try:
                corpo = open_and_read(page, s, it)
            except Exception as e:
                print("Ler msg:", e)
        send(f"💬 {BW['platform'].capitalize()} — {who}\n{corpo}\n\n/br {ref} <resposta>")
    save_state()


def reply_to(page, s, who, body):
    handle = None
    if s.get("name_el"):
        for it in page.query_selector_all(s["unread"].split(":has")[0]):
            el = it.query_selector(s["name_el"])
            if el and who.lower() in (el.inner_text() or "").lower():
                handle = it
                break
    if handle is None:
        handle = page.query_selector(f'[{s["name_attr"]}^="{who}"]') or page.get_by_text(who, exact=False).first
    handle.click()
    page.wait_for_timeout(1500)
    box = page.wait_for_selector(s["compose"], timeout=15000)
    box.click()
    box.type(body, delay=15)
    page.keyboard.press("Enter")
    page.wait_for_timeout(800)
    st["seen"] = [x for x in st["seen"] if x != f"{BW['platform']}:{who}"]
    save_state()


def capture(page):
    EVID.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    htm = EVID / f"{BW['platform']}-{stamp}.html"
    try:
        page.screenshot(path=str(EVID / f"{BW['platform']}-{stamp}.png"))
    except Exception as e:
        print("screenshot:", e)
    try:
        htm.write_text(page.content(), encoding="utf-8")
    except Exception as e:
        print("html:", e)
    st["last_html"] = str(htm)
    save_state()


def self_test(page, s):
    try:
        page.reload()
        page.wait_for_selector(s["ready"], timeout=60000)
    except Exception:
        return False, "a página não carregou"
    lista = find_unread(page, s)
    if title_unread(page) > 0 and not lista:
        return False, "título mostra não lidos mas a lista não foi encontrada"
    if page.query_selector(s["compose"]) is None:
        return False, "caixa de texto não encontrada"
    return True, f"ok ({len(lista)} conversa(s) na lista, caixa de texto encontrada)"


def run_claude_fix():
    cmd = shutil.which(BW.get("claude_cmd", CFG.get("claude_cmd", "claude"))) or "claude"
    cwd = BW.get("claude_dir", str(BASE))
    html_path = st.get("last_html", "")
    prompt = (
        "O vigia de navegador parou de achar as mensagens no " + BW["platform"] + " web, "
        "provavelmente porque o layout mudou. O arquivo " + str(SEL_FILE) + " tem os seletores CSS "
        'em uso (bloco "' + BW["platform"] + '"). Abra o HTML real da tela salvo em ' + html_path + " "
        'e descubra os seletores atuais para: (1) "ready" (container principal carregado); '
        '(2) "unread" (itens da lista lateral com indicador de não lido / badge de menção); '
        '(3) "compose" (a caixa de texto contenteditable para digitar). '
        'Atualize APENAS esses valores no bloco "' + BW["platform"] + '" do ' + str(SEL_FILE) + ", "
        "mantendo o JSON válido e sem tocar em nenhum outro arquivo. Prefira seletores estáveis "
        "(data-tid, data-qa, role, aria-label) a classes ofuscadas. Responda em uma linha."
    )
    try:
        p = subprocess.run([cmd, "-p", prompt, "--output-format", "json"], cwd=cwd,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=900, stdin=subprocess.DEVNULL)
    except Exception as e:
        return False, f"falha ao chamar o Claude: {e}"
    if p.returncode != 0 and not p.stdout.strip():
        return False, (p.stderr or "")[-500:]
    try:
        data = json.loads(p.stdout)
        return (not data.get("is_error", False)), (data.get("result") or "")[-800:]
    except Exception:
        return True, (p.stdout or "")[-800:]


def do_fix(page):
    send("🔧 Chamando o Claude para atualizar os seletores…")
    ok, msg = run_claude_fix()
    if not ok:
        send(f"❌ O Claude não concluiu o conserto:\n{msg}\nVou aguardar — olhe quando puder.")
        return False
    try:
        s = load_sel()
    except Exception as e:
        send(f"❌ selectors.json ficou inválido após a edição: {e}\nAguardando você.")
        return False
    passou, detalhe = self_test(page, s)
    if passou:
        st["mode"] = "ok"
        save_state()
        send(f"✅ Consertado e testado — {detalhe}. Voltei a vigiar sozinho.")
        return True
    send(f"❌ Ainda não passou no teste: {detalhe}.\nNão vou tentar de novo sozinho — me avise quando olhar.")
    return False


def handle_actions(page, s, acao):
    """Trata /consertar e /cancelar. Retorna o selector possivelmente recarregado."""
    if acao == "consertar" and st.get("mode") == "cego":
        if do_fix(page):
            return load_sel()
    elif acao == "cancelar" and st.get("mode") == "cego":
        st["mode"] = "ok"
        save_state()
        send("Ok, continuo como está, com os seletores atuais.")
    return s


def run(headless):
    s = load_sel()
    names = [n.lower() for n in BW.get("my_names", [])]
    profile = BW.get("profile_dir", str(BASE / "brchrome"))
    every = BW.get("poll_min", 2) * 60
    confirm = BW.get("confirm_sec", 90)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            profile, headless=headless, args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(s["url"])
        try:
            page.wait_for_selector(s["ready"], timeout=120000)
        except Exception:
            send("⚠️ Vigia do navegador: não carregou. Rode sem --headless e faça login.")
        send(f"🟢 Vigia de navegador ({BW['platform']}) online.")
        print("Vigiando. Ctrl+C para sair.")
        blind_since = None

        while True:
            respostas, acao = poll_commands()
            for ref, body in respostas:
                who = st["refs"].get(ref)
                if not who:
                    send(f"Referência {ref} expirou. Abra pelo app desta vez.")
                    continue
                try:
                    reply_to(page, s, who, body)
                    send(f"📤 Respondido ({ref}).")
                except Exception as e:
                    send(f"❌ Não consegui responder ({ref}): {e}")
            s = handle_actions(page, s, acao)
            if acao == "consertar":
                blind_since = None

            if st.get("mode") != "cego":
                try:
                    if title_unread(page) > 0 and not find_unread(page, s):
                        blind_since = blind_since or time.time()
                        if time.time() - blind_since >= confirm:
                            capture(page)
                            st["mode"] = "cego"
                            save_state()
                            send("⚠️ Tem mensagem não lida no " + BW["platform"].capitalize() +
                                 " mas não estou conseguindo lê-la — o layout pode ter mudado.\n"
                                 "Confira se há mesmo mensagem nova e, se tiver, mande /consertar "
                                 "(ou /cancelar para eu seguir como está).")
                    else:
                        blind_since = None
                        check_unread(page, s, names)
                except Exception as e:
                    print("Leitura:", e)

            waited = 0
            while waited < every:
                time.sleep(5)
                waited += 5
                if st.get("mode") == "cego" and waited % CMD_EVERY < 5:
                    _, a2 = poll_commands()
                    s = handle_actions(page, s, a2)
                    if a2 in ("consertar", "cancelar"):
                        blind_since = None
                        break


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    if not BW:
        print('Falta o bloco "browser_watch" no config.json.')
        sys.exit(1)
    while True:
        try:
            run(args.headless)
        except KeyboardInterrupt:
            print("Encerrado.")
            break
        except Exception as e:
            print("Reiniciando após erro:", e)
            time.sleep(30)
