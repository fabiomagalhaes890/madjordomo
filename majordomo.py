#!/usr/bin/env python3
"""
Majordomo — ponte Telegram <-> Claude Code / Cursor CLI (modo headless),
com vigias de Jira ou Azure DevOps, GitHub ou Azure Repos, Gmail/Google Agenda
ou Outlook/Agenda/Teams (Microsoft 365) e Slack.
Só usa a biblioteca padrão do Python (3.10+). Sem pip install.

Comandos no Telegram:
  (os comandos de trabalho vêm do "commands" do config.json — veja /ajuda)
  /tasks [filtro]   seus cards abertos por status (filtro: status, chave ou título)
  /prs              status dos seus PRs abertos (GitHub ou Azure Repos)
  /agenda [dia]     reuniões: hoje, amanha, seg..dom, 15/10 ou semana
  /emails [busca]   últimos e-mails não lidos (ou busca)
  /tr t3 <texto>    responde no Teams a notificação [t3]
  /briefing         resumo do dia (agenda, cards e PRs)
  /sr s3 <texto>    responde no Slack a notificação [s3]
  /aprovacao <PR>   pede aprovação do PR no canal do Slack
  /claude <texto>   pedido livre ao Claude (nova sessão)
  <texto normal>    continua a thread ativa; sem thread ativa, abre uma nova
  /threads          lista as threads abertas e a ativa
  /ir <rótulo>      troca a thread ativa
  /resp <rótulo> …  responde uma thread específica sem trocar a ativa
  /ligar <ant> CARD vincula uma investigação sem-card ao card quando ele nascer
  /uso [hoje|semana|mes]  consumo das tarefas (custo e tokens)
  /status           o que está rodando agora
  /parar            cancela a tarefa em andamento
  /nova             encerra a thread ativa
  /ping             teste de vida
"""
import base64
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import channels as _channels

BASE = Path(__file__).resolve().parent
CFG = _channels.load_config(BASE / "config.json")
DIRECT_TELEGRAM = (CFG.get("channel") or "telegram").lower() == "telegram"
STATE_FILE = BASE / "state.json"
API = f"https://api.telegram.org/bot{CFG['telegram_token']}"
MAX_MSG = 3900  # limite do Telegram é 4096

# ---------------------------------------------------------------- estado
state_lock = threading.Lock()


def load_state():
    st = {"offset": 0, "jira_seen": None,
          "threads": {},        # rótulo -> {"session_id": ..., "label": ..., "updated": ...}
          "active": None,       # rótulo da thread ativa
          "inv_n": 0,           # contador de rótulos automáticos inv-N
          "usage": {}}          # "YYYY-MM-DD" -> {"cost": float, "in": int, "out": int, "runs": int}
    if STATE_FILE.exists():
        st.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return st


state = load_state()


def save_state():
    with state_lock:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- telegram
# ---------------------------------------------------------------- canal
CHANNEL = None  # criado em main(), depois que state existe


def tg(method, **params):
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/{method}", data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def send(text, chat_id=None, thread_id=None):
    """Envia pelo canal configurado (telegram direto, ou github/notion intermediário)."""
    if CHANNEL is not None and not chat_id:
        CHANNEL.send(text or "(sem resposta)")
        return
    if chat_id:  # resposta a um chat específico (modo configuração)
        try:
            params = {"chat_id": chat_id, "text": (text or "")[:MAX_MSG]}
            if thread_id:
                params["message_thread_id"] = thread_id
            tg("sendMessage", **params)
        except Exception as e:
            print("Erro ao enviar mensagem:", e)
        return
    # fallback (antes de main criar o canal): tenta telegram direto
    try:
        params = {"chat_id": CFG["chat_id"], "text": (text or "")[:MAX_MSG]}
        if CFG.get("topic_id"):
            params["message_thread_id"] = CFG["topic_id"]
        tg("sendMessage", **params)
    except Exception as e:
        print("Erro ao enviar mensagem:", e)


# ---------------------------------------------------------------- jira
JIRA = CFG.get("jira") or {}


def jira_search():
    """Retorna lista de (key, status, prioridade, resumo) dos cards do JQL configurado."""
    base = JIRA["base_url"].rstrip("/")
    jql = JIRA.get("jql", "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC")
    fields = "summary,status,priority,issuetype,parent"
    if JIRA.get("pat"):  # Jira Server / Data Center
        url = f"{base}/rest/api/2/search?" + urllib.parse.urlencode(
            {"jql": jql, "fields": fields, "maxResults": 50})
        auth = f"Bearer {JIRA['pat']}"
    else:  # Jira Cloud
        url = f"{base}/rest/api/3/search/jql?" + urllib.parse.urlencode(
            {"jql": jql, "fields": fields, "maxResults": 50})
        token = base64.b64encode(f"{JIRA['email']}:{JIRA['api_token']}".encode()).decode()
        auth = f"Basic {token}"
    req = urllib.request.Request(url, headers={"Authorization": auth, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    out = []
    for it in data.get("issues", []):
        f = it.get("fields", {})
        st = f.get("status") or {}
        STATUS_CAT[st.get("name", "?")] = (st.get("statusCategory") or {}).get("key", "")
        tp = f.get("issuetype") or {}
        par = f.get("parent") or {}
        TASK_META[it["key"]] = {"type": tp.get("name", ""), "sub": bool(tp.get("subtask")),
                                "parent": par.get("key"),
                                "parent_title": (par.get("fields") or {}).get("summary", "")}
        out.append((
            it["key"],
            (f.get("status") or {}).get("name", "?"),
            (f.get("priority") or {}).get("name", "-"),
            f.get("summary", ""),
        ))
    return out


STATUS_CAT = {}  # nome do status -> categoria do Jira ("new", "indeterminate", "done")
TASK_META = {}   # chave -> {"type", "sub" (é subtarefa), "parent", "parent_title"}
TODO_STATUS = {"backlog", "to do", "todo", "a fazer", "novo", "new", "aberto", "open", "proposed",
               "pendente", "selecionado para desenvolvimento", "selected for development"}
HIGH_PRIO = {"highest", "high", "alta", "altíssima", "muito alta", "crítica", "critical", "blocker", "1"}
TODO_MAX = 8  # itens listados por status "a fazer"; o resto vira contagem


def is_todo(status):
    cat = STATUS_CAT.get(status)
    return cat == "new" if cat else status.strip().lower() in TODO_STATUS


def format_tasks(items, filtro=""):
    """Agrupa por status: em andamento primeiro, 'a fazer' por último (resumido).
    filtro: trecho do status, da chave ou do título (ex.: /tasks qa)."""
    f = filtro.strip().lower()
    if f:
        items = [i for i in items if f in i[1].lower() or f in i[0].lower() or f in i[3].lower()
                 or f in TASK_META.get(i[0], {}).get("type", "").lower()]
    if not items:
        return f"Nenhum card com \"{filtro}\"." if f else "Nenhum card aberto em seu nome."

    def meta(k):
        return TASK_META.get(k, {})

    def alta(pr):
        return " ❗" if str(pr).strip().lower() in HIGH_PRIO else ""

    def sub_line(k, st, pr, s):
        return f"   ↳ {k} · {st}{alta(pr)} — {short(s, 60)}"

    keys = {i[0] for i in items}
    children, orphans, tops = {}, {}, []
    for it in items:
        par = meta(it[0]).get("parent")
        if par and par in keys:                 # pai também está na lista: vai embaixo dele
            children.setdefault(par, []).append(it)
        elif par and meta(it[0]).get("sub"):    # subtarefa de card que não é seu / já fechado
            orphans.setdefault(par, []).append(it)
        else:
            tops.append(it)

    groups = {}
    for it in tops:
        groups.setdefault(it[1], []).append(it)
    order = sorted(groups, key=lambda st: (is_todo(st), -len(groups[st])))
    nsub = len(items) - len(tops)
    head = f"📋 {len(tops)} card(s)" + (f" + {nsub} subtarefa(s)" if nsub else "")
    if order:
        head += "\n" + " · ".join(f"{len(groups[st])} {st}" for st in order)
    blocks = [head]
    for st in order:
        todo = is_todo(st)
        rows = groups[st]
        lines = [f"{'📥' if todo else '🔧'} {st} ({len(rows)})"]
        shown = rows[:TODO_MAX] if todo and not f else rows
        for k, _, pr, s in shown:
            tipo = meta(k).get("type")
            lines.append(f"• {k}{' · ' + tipo if tipo else ''}{alta(pr)} — {short(s, 70)}")
            lines += [sub_line(*c) for c in children.get(k, [])]
        if len(shown) < len(rows):
            lines.append(f"  … +{len(rows) - len(shown)} (veja com /tasks {st.lower()})")
        blocks.append("\n".join(lines))
    if orphans:
        lines = ["🧩 Subtarefas em cards de outros"]
        for par, subs in orphans.items():
            lines.append(f"• {par} — {short(meta(subs[0][0]).get('parent_title', ''), 60)}")
            lines += [sub_line(*c) for c in subs]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- azure devops
AZDO = CFG.get("azure_devops") or {}
_azdo_me = {}


def azdo(path, method="GET", body=None, org_level=False, api="7.1"):
    base = f"https://dev.azure.com/{urllib.parse.quote(AZDO['org'])}"
    if not org_level:
        base += "/" + urllib.parse.quote(AZDO["project"])
    url = base + path + ("&" if "?" in path else "?") + f"api-version={api}"
    tok = base64.b64encode(f":{AZDO['pat']}".encode()).decode()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def azdo_me():
    if "id" not in _azdo_me:
        cd = azdo("/_apis/connectionData", org_level=True, api="7.1-preview")
        _azdo_me["id"] = cd["authenticatedUser"]["id"]
    return _azdo_me["id"]


def azdo_tasks():
    wiql = AZDO.get("wiql", "SELECT [System.Id] FROM WorkItems WHERE [System.AssignedTo] = @Me "
                    "AND [System.State] NOT IN ('Closed', 'Done', 'Removed', 'Resolved') "
                    "ORDER BY [System.ChangedDate] DESC")
    res = azdo("/_apis/wit/wiql", "POST", {"query": wiql})
    ids = [str(w["id"]) for w in res.get("workItems", [])][:50]
    if not ids:
        return []
    items = azdo(f"/_apis/wit/workitems?ids={','.join(ids)}", org_level=True).get("value", [])
    out = []
    for it in items:
        f = it.get("fields", {})
        par = f.get("System.Parent")
        TASK_META[str(it["id"])] = {"type": f.get("System.WorkItemType", ""), "sub": False,
                                    "parent": str(par) if par else None, "parent_title": ""}
        out.append((str(it["id"]), f.get("System.State", "?"),
                    str(f.get("Microsoft.VSTS.Common.Priority", "-")),
                    f.get("System.Title", "")))
    return out


TASKS = JIRA or AZDO


def tasks_search():
    return jira_search() if JIRA else azdo_tasks()


def tasks_poller():
    """A cada poll_min minutos, avisa quando aparecer card/work item novo em seu nome."""
    every = (TASKS.get("poll_min") or 10) * 60
    while True:
        try:
            items = tasks_search()
            keys = [i[0] for i in items]
            if state["jira_seen"] is None:  # primeira execução: só memoriza
                state["jira_seen"] = keys
            else:
                for k, st, pr, s in [i for i in items if i[0] not in state["jira_seen"]]:
                    send(f"🆕 Card novo em seu nome\n{k} · {st} · {pr}\n{s}\n\n/analisar {k}")
                state["jira_seen"] = keys
            save_state()
        except Exception as e:
            print("Tarefas:", e)
        time.sleep(every)


# ---------------------------------------------------------------- slack
SLACK = CFG.get("slack") or {}
_slack_names = {}


def slack(method, **params):
    url = f"https://slack.com/api/{method}"
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {SLACK['user_token']}",
        "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
    if not res.get("ok"):
        raise RuntimeError(f"Slack {method}: {res.get('error')}")
    return res


def slack_get(method, **params):
    url = f"https://slack.com/api/{method}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK['user_token']}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.loads(r.read())
    if not res.get("ok"):
        raise RuntimeError(f"Slack {method}: {res.get('error')}")
    return res


def slack_name(uid):
    if not uid:
        return "?"
    if uid not in _slack_names:
        try:
            u = slack_get("users.info", user=uid)["user"]
            _slack_names[uid] = u.get("profile", {}).get("display_name") or u.get("real_name") or uid
        except Exception:
            _slack_names[uid] = uid
    return _slack_names[uid]


def slack_ref(channel, ts, thread_ts):
    refs = state.setdefault("slack_refs", {})
    n = state.get("slack_ref_n", 0) + 1
    state["slack_ref_n"] = n
    key = f"s{n}"
    refs[key] = {"channel": channel, "thread_ts": thread_ts}
    for old in list(refs)[:-50]:  # guarda só as 50 últimas
        refs.pop(old, None)
    return key


def short(text, n=350):
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[:n] + "…"


def slack_watcher():
    every = SLACK.get("poll_min", 2) * 60
    while True:
        try:
            me = slack_get("auth.test")["user_id"]
            break
        except Exception as e:
            print("Slack (login):", e)
            time.sleep(60)
    first = "slack_mention_ts" not in state
    while True:
        try:
            # menções
            last = float(state.get("slack_mention_ts", 0))
            res = slack_get("search.messages", query=f"<@{me}>", sort="timestamp", sort_dir="desc", count=20)
            matches = [m for m in res.get("messages", {}).get("matches", []) if float(m["ts"]) > last]
            if matches:
                state["slack_mention_ts"] = max(m["ts"] for m in matches)
            if not first:
                for m in reversed(matches):
                    if m.get("user") == me:
                        continue
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(m.get("permalink", "")).query)
                    thread_ts = q.get("thread_ts", [m["ts"]])[0]
                    key = slack_ref(m["channel"]["id"], m["ts"], thread_ts)
                    send(f"💬 Slack #{m['channel'].get('name', '?')} — {slack_name(m.get('user'))}\n"
                         f"{short(m.get('text'))}\n\n/sr {key} <resposta>")
            state.setdefault("slack_mention_ts", str(time.time()))

            # DMs
            dm_last = state.setdefault("slack_dm_ts", {})
            ims = slack_get("conversations.list", types="im", limit=200).get("channels", [])
            for im in ims:
                ch = im["id"]
                oldest = dm_last.get(ch)
                if oldest is None:
                    dm_last[ch] = str(time.time())
                    continue
                hist = slack_get("conversations.history", channel=ch, oldest=oldest, limit=10).get("messages", [])
                if not hist:
                    continue
                dm_last[ch] = max(h["ts"] for h in hist)
                for h in reversed(hist):
                    if h.get("user") == me or h.get("subtype"):
                        continue
                    key = slack_ref(ch, h["ts"], None)
                    send(f"✉️ DM Slack — {slack_name(h.get('user'))}\n{short(h.get('text'))}\n\n/sr {key} <resposta>")
                time.sleep(1.2)  # respeita o rate limit
            first = False
            save_state()
        except Exception as e:
            print("Slack:", e)
        time.sleep(every)


# ---------------------------------------------------------------- github
GH = CFG.get("github") or {}
GH_CMD = shutil.which("gh") or "gh"


def gh_json(args):
    p = subprocess.run([GH_CMD] + args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip()[:300])
    return json.loads(p.stdout or "null")


def ci_status(rollup):
    if not rollup:
        return "sem checks"
    pend = fail = False
    for c in rollup:
        if "status" in c:  # CheckRun
            if c.get("status") != "COMPLETED":
                pend = True
            elif c.get("conclusion") in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"):
                fail = True
        else:  # StatusContext
            if c.get("state") in ("FAILURE", "ERROR"):
                fail = True
            elif c.get("state") in ("PENDING", "EXPECTED"):
                pend = True
    return "falhou" if fail else ("rodando" if pend else "ok")


REVIEW_PT = {"APPROVED": "aprovado", "CHANGES_REQUESTED": "mudanças pedidas",
             "REVIEW_REQUIRED": "aguardando review", "": "sem review", None: "sem review"}


def pr_event(icon, what, name, url, detail=""):
    """Um bloco de evento de PR: título na 1a linha, detalhe e link recuados."""
    return f"{icon} {what} — {name}" + (f"\n   {detail}" if detail else "") + f"\n   {url}"


def comments_detail(qtd, authors=()):
    """'3 comentários novos · claude[bot] ×2, cursor[bot]'"""
    txt = f"{qtd} comentário{'s' if qtd > 1 else ''} novo{'s' if qtd > 1 else ''}"
    if authors:
        cont = {}
        for a in authors:
            cont[a] = cont.get(a, 0) + 1
        txt += " · " + ", ".join(a + (f" ×{c}" if c > 1 else "") for a, c in cont.items())
    return txt


def send_pr_digest(events):
    """Todos os eventos do ciclo numa mensagem só, separados por linha em branco."""
    if len(events) == 1:
        send(events[0])
    elif events:
        send("🔀 Atualizações de PR\n\n" + "\n\n".join(events))


def gh_check_prs(notify=True):
    tracked = state.setdefault("gh_prs", {})
    events = []
    found = gh_json(["search", "prs", "--author=@me", "--state=open", "--limit", "30",
                     "--json", "url,title,repository"] + ([f"--owner={GH['owner']}"] if GH.get("owner") else []))
    for pr in found or []:
        if pr["url"] not in tracked:
            tracked[pr["url"]] = {"title": pr["title"], "repo": pr["repository"]["name"],
                                  "ci": None, "review": None, "n": 0}
            if notify:
                events.append(pr_event("👀", "Acompanhando", f"{pr['repository']['name']}: {short(pr['title'], 80)}",
                                       pr["url"]))
    for url, old in list(tracked.items()):
        v = gh_json(["pr", "view", url, "--json", "state,statusCheckRollup,reviewDecision,comments,reviews"])
        name = f"{old['repo']}: {short(old['title'], 80)}"
        if v["state"] != "OPEN":
            if notify:
                events.append(pr_event("🚀" if v["state"] == "MERGED" else "🗑️",
                                       "Mergeado" if v["state"] == "MERGED" else "Fechado", name, url))
            tracked.pop(url)
            continue
        ci = ci_status(v.get("statusCheckRollup"))
        review = REVIEW_PT.get(v.get("reviewDecision"), v.get("reviewDecision"))
        posts = sorted([(c.get("createdAt", ""), (c.get("author") or {}).get("login", "?"))
                        for c in v.get("comments") or []] +
                       [(r.get("submittedAt", ""), (r.get("author") or {}).get("login", "?"))
                        for r in v.get("reviews") or []])
        n = len(posts)
        if notify and old["ci"] is not None:
            if ci != old["ci"] and ci in ("ok", "falhou"):
                extra = f"/aprovacao {url}" if ci == "ok" and SLACK.get("approval_channel") else ""
                events.append(pr_event("✅" if ci == "ok" else "❌", "CI passou" if ci == "ok" else "CI falhou",
                                       name, url, extra))
            if review != old["review"] and review in ("aprovado", "mudanças pedidas"):
                events.append(pr_event("👍" if review == "aprovado" else "✏️",
                                       "Aprovado" if review == "aprovado" else "Pediram mudanças", name, url))
            elif n > old["n"]:
                events.append(pr_event("🗨️", "Comentários", name, url,
                                       comments_detail(n - old["n"], [a for _, a in posts[old["n"]:]])))
        old.update(ci=ci, review=review, n=n)
    save_state()
    send_pr_digest(events)
    return tracked


VOTE_REVIEW = {10: "aprovado", 5: "aprovado", -5: "mudanças pedidas", -10: "mudanças pedidas"}


def azdo_check_prs(notify=True):
    tracked = state.setdefault("gh_prs", {})
    events = []
    org, proj = AZDO["org"], AZDO["project"]
    prs = azdo(f"/_apis/git/pullrequests?searchCriteria.creatorId={azdo_me()}"
               f"&searchCriteria.status=active&$top=50").get("value", [])
    for pr in prs:
        repo = pr["repository"]
        url = (f"https://dev.azure.com/{urllib.parse.quote(org)}/{urllib.parse.quote(proj)}"
               f"/_git/{urllib.parse.quote(repo['name'])}/pullrequest/{pr['pullRequestId']}")
        if url not in tracked:
            tracked[url] = {"title": pr["title"], "repo": repo["name"], "ci": None, "review": None, "n": 0,
                            "id": pr["pullRequestId"], "repo_id": repo["id"],
                            "project_id": repo.get("project", {}).get("id")}
            if notify:
                events.append(pr_event("👀", "Acompanhando", f"{repo['name']}: {short(pr['title'], 80)}", url))
    for url, old in list(tracked.items()):
        if "repo_id" not in old:
            continue  # PR de outro provedor
        rid, pid = old["repo_id"], old["id"]
        v = azdo(f"/_apis/git/repositories/{rid}/pullrequests/{pid}")
        name = f"{old['repo']}: {short(old['title'], 80)}"
        if v["status"] != "active":
            if notify:
                events.append(pr_event("🚀" if v["status"] == "completed" else "🗑️",
                                       "Concluído" if v["status"] == "completed" else "Abandonado", name, url))
            tracked.pop(url)
            continue
        votes = [r.get("vote", 0) for r in v.get("reviewers", [])]
        if any(x < 0 for x in votes):
            review = "mudanças pedidas"
        elif any(x > 0 for x in votes):
            review = "aprovado"
        else:
            review = "aguardando review"
        ci = "sem checks"
        try:
            art = f"vstfs:///CodeReview/CodeReviewId/{old['project_id']}/{pid}"
            ev = azdo("/_apis/policy/evaluations?artifactId=" + urllib.parse.quote(art, safe=""),
                      api="7.1-preview.1").get("value", [])
            builds = [e for e in ev if (e.get("configuration", {}).get("type", {}).get("displayName") == "Build")]
            st = [e.get("status") for e in builds]
            if st:
                ci = ("falhou" if any(s in ("rejected", "broken") for s in st)
                      else "rodando" if any(s in ("queued", "running") for s in st) else "ok")
        except Exception as e:
            print("Azure DevOps (policies):", e)
        threads = azdo(f"/_apis/git/repositories/{rid}/pullRequests/{pid}/threads").get("value", [])
        posts = sorted((c.get("publishedDate", ""), (c.get("author") or {}).get("displayName", "?"))
                       for t in threads for c in t.get("comments", [])
                       if c.get("commentType") == "text" and not c.get("isDeleted"))
        n = len(posts)
        if notify and old["ci"] is not None:
            if ci != old["ci"] and ci in ("ok", "falhou"):
                events.append(pr_event("✅" if ci == "ok" else "❌", "Build passou" if ci == "ok" else "Build falhou",
                                       name, url))
            if review != old["review"] and review in ("aprovado", "mudanças pedidas"):
                events.append(pr_event("👍" if review == "aprovado" else "✏️",
                                       "Aprovado" if review == "aprovado" else "Pediram mudanças", name, url))
            elif n > old["n"]:
                events.append(pr_event("🗨️", "Comentários", name, url,
                                       comments_detail(n - old["n"], [a for _, a in posts[old["n"]:]])))
        old.update(ci=ci, review=review, n=n)
    save_state()
    send_pr_digest(events)
    return tracked


# ---------------------------------------------------------------- gitlab
GL = CFG.get("gitlab") or {}


def gl_api(path, params=None):
    base = GL.get("base_url", "https://gitlab.com").rstrip("/") + "/api/v4"
    url = base + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": GL["token"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


GL_PIPE = {"success": "ok", "failed": "falhou", "running": "rodando", "pending": "rodando",
           "created": "rodando", "canceled": "falhou", "manual": "sem checks", "skipped": "sem checks"}


def gl_check_prs(notify=True):
    tracked = state.setdefault("gh_prs", {})
    events = []
    me = gl_api("/user")["id"]
    mrs = gl_api("/merge_requests", {"author_id": me, "state": "opened", "scope": "all", "per_page": 50})
    for mr in mrs:
        url = mr["web_url"]
        if url not in tracked:
            tracked[url] = {"title": mr["title"], "repo": mr["references"]["full"].split("!")[0],
                            "ci": None, "review": None, "n": 0,
                            "pid": mr["project_id"], "iid": mr["iid"]}
            if notify:
                events.append(pr_event("👀", "Acompanhando MR", f"{tracked[url]['repo']}: {short(mr['title'], 80)}",
                                       url))
    for url, old in list(tracked.items()):
        if "pid" not in old or "iid" not in old:
            continue  # MR só tem pid/iid; pula PRs de outro provedor
        mr = gl_api(f"/projects/{old['pid']}/merge_requests/{old['iid']}")
        name = f"{old['repo']}: {short(old['title'], 80)}"
        if mr["state"] != "opened":
            if notify:
                events.append(pr_event("🚀" if mr["state"] == "merged" else "🗑️",
                                       "MR mergeado" if mr["state"] == "merged" else "MR fechado", name, url))
            tracked.pop(url)
            continue
        ps = (mr.get("head_pipeline") or mr.get("pipeline") or {}).get("status")
        ci = GL_PIPE.get(ps, "sem checks")
        # aprovações
        review = "aguardando review"
        try:
            ap = gl_api(f"/projects/{old['pid']}/merge_requests/{old['iid']}/approvals")
            if ap.get("approved") or ap.get("approved_by"):
                review = "aprovado"
        except Exception as e:
            print("GitLab (approvals):", e)
        if mr.get("blocking_discussions_resolved") is False:
            review = "mudanças pedidas"
        n = mr.get("user_notes_count", 0)
        if notify and old["ci"] is not None:
            if ci != old["ci"] and ci in ("ok", "falhou"):
                events.append(pr_event("✅" if ci == "ok" else "❌",
                                       "Pipeline passou" if ci == "ok" else "Pipeline falhou", name, url))
            if review != old["review"] and review in ("aprovado", "mudanças pedidas"):
                events.append(pr_event("👍" if review == "aprovado" else "✏️",
                                       "Aprovado" if review == "aprovado" else "Pediram mudanças", name, url))
            elif n > old["n"]:
                events.append(pr_event("🗨️", "Comentários no MR", name, url, comments_detail(n - old["n"])))
        old.update(ci=ci, review=review, n=n)
    save_state()
    send_pr_digest(events)
    return tracked


PRS = GH or GL or (AZDO if AZDO.get("watch_prs", True) else {})


def prs_check(notify=True):
    if GH:
        return gh_check_prs(notify)
    if GL:
        return gl_check_prs(notify)
    return azdo_check_prs(notify)


def prs_watcher():
    every = (PRS.get("pr_poll_min") or PRS.get("poll_min") or 5) * 60
    first = "gh_prs" not in state
    while True:
        try:
            prs_check(notify=not first)
            first = False
        except Exception as e:
            print("PRs:", e)
        time.sleep(every)


# ---------------------------------------------------------------- e-mail e agenda
GOOGLE = CFG.get("google") or {}
MS = CFG.get("microsoft") or {}


def mailcal():
    if GOOGLE and (BASE / "google_token.json").exists():
        return "google"
    if MS and (BASE / "ms_token.json").exists():
        return "microsoft"
    return None


MAILCAL = mailcal()
MCFG = GOOGLE if MAILCAL == "google" else MS


# ---------- google
_gtok = {"access": None, "exp": 0}


def google_access():
    if _gtok["access"] and time.time() < _gtok["exp"] - 60:
        return _gtok["access"]
    t = json.loads((BASE / "google_token.json").read_text(encoding="utf-8"))
    data = urllib.parse.urlencode({
        "client_id": t["client_id"], "client_secret": t["client_secret"],
        "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request(t["token_uri"], data=data), timeout=30) as r:
        res = json.loads(r.read())
    _gtok.update(access=res["access_token"], exp=time.time() + res.get("expires_in", 3600))
    return _gtok["access"]


def gapi(url, **params):
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {google_access()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_DEFAULT = "is:unread in:inbox -category:promotions -category:social newer_than:2d"


def google_mail(query, n, skip=()):
    q = query or GOOGLE.get("gmail_query", GMAIL_DEFAULT)
    out = []
    for m0 in gapi(f"{GMAIL}/messages", q=q, maxResults=n).get("messages", []):
        if m0["id"] in skip:
            out.append({"id": m0["id"]})
            continue
        m = gapi(f"{GMAIL}/messages/{m0['id']}", format="metadata", metadataHeaders=["From", "Subject"])
        h = {x["name"]: x["value"] for x in m.get("payload", {}).get("headers", [])}
        out.append({"id": m0["id"], "from": h.get("From", "?"), "subject": h.get("Subject", "(sem assunto)"),
                    "snippet": m.get("snippet", "")})
    return out


def google_events(start, end):
    res = gapi(CAL, timeMin=start.isoformat(), timeMax=end.isoformat(),
               singleEvents="true", orderBy="startTime", maxResults=50)
    out = []
    for ev in res.get("items", []):
        if "dateTime" not in ev.get("start", {}):
            continue
        me = next((a for a in ev.get("attendees", []) if a.get("self")), None)
        if me and me.get("responseStatus") == "declined":
            continue
        st = dt.datetime.fromisoformat(ev["start"]["dateTime"].replace("Z", "+00:00")).astimezone()
        out.append({"key": f"{ev['id']}@{ev['start']['dateTime']}", "start": st,
                    "title": ev.get("summary", "(sem título)"),
                    "link": ev.get("hangoutLink") or ev.get("location") or ""})
    return out


# ---------- microsoft 365 (Outlook, Agenda, Teams)
_mstok = {"access": None, "exp": 0}


def ms_access():
    if _mstok["access"] and time.time() < _mstok["exp"] - 60:
        return _mstok["access"]
    path = BASE / "ms_token.json"
    t = json.loads(path.read_text(encoding="utf-8"))
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token", "client_id": t["client_id"],
        "refresh_token": t["refresh_token"], "scope": " ".join(t["scopes"])}).encode()
    url = f"https://login.microsoftonline.com/{t['tenant']}/oauth2/v2.0/token"
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
        res = json.loads(r.read())
    if res.get("refresh_token") and res["refresh_token"] != t["refresh_token"]:
        t["refresh_token"] = res["refresh_token"]
        path.write_text(json.dumps(t, indent=2), encoding="utf-8")
    _mstok.update(access=res["access_token"], exp=time.time() + res.get("expires_in", 3600))
    return _mstok["access"]


def graph(path, method="GET", body=None, params=None, headers=None):
    url = "https://graph.microsoft.com/v1.0" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="$/:,'()")
    h = {"Authorization": f"Bearer {ms_access()}", "Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def utc_iso(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms_mail(query, n, skip=()):
    sel = "id,from,subject,bodyPreview"
    if query:
        res = graph("/me/messages", params={"$search": f'"{query}"', "$top": n, "$select": sel})
    else:
        since = utc_iso(dt.datetime.now().astimezone() - dt.timedelta(days=MS.get("mail_days", 2)))
        res = graph("/me/mailFolders/inbox/messages", params={
            "$filter": f"receivedDateTime ge {since} and isRead eq false",
            "$orderby": "receivedDateTime desc", "$top": n, "$select": sel})
    out = []
    for m in res.get("value", []):
        ea = (m.get("from") or {}).get("emailAddress", {})
        out.append({"id": m["id"], "from": f"{ea.get('name', '')} <{ea.get('address', '')}>",
                    "subject": m.get("subject") or "(sem assunto)", "snippet": m.get("bodyPreview", "")})
    return out


def ms_events(start, end):
    res = graph("/me/calendarView", params={
        "startDateTime": utc_iso(start), "endDateTime": utc_iso(end), "$orderby": "start/dateTime",
        "$top": 50, "$select": "id,subject,start,isAllDay,isCancelled,responseStatus,onlineMeeting,location"},
        headers={"Prefer": 'outlook.timezone="UTC"'})
    out = []
    for ev in res.get("value", []):
        if ev.get("isAllDay") or ev.get("isCancelled"):
            continue
        if (ev.get("responseStatus") or {}).get("response") == "declined":
            continue
        raw = ev["start"]["dateTime"]
        st = dt.datetime.fromisoformat(raw[:19] + "+00:00").astimezone()
        out.append({"key": f"{ev['id']}@{raw}", "start": st, "title": ev.get("subject") or "(sem título)",
                    "link": (ev.get("onlineMeeting") or {}).get("joinUrl")
                    or (ev.get("location") or {}).get("displayName") or ""})
    return out


def html_text(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def teams_ref(chat_id):
    refs = state.setdefault("teams_refs", {})
    n = state.get("teams_ref_n", 0) + 1
    state["teams_ref_n"] = n
    refs[f"t{n}"] = chat_id
    for old in list(refs)[:-50]:
        refs.pop(old, None)
    return f"t{n}"


def teams_watcher():
    """Avisa mensagens novas em chats do Teams (DMs e grupos). Menções em canais não entram."""
    every = MS.get("teams_poll_min", 2) * 60
    me = None
    first = "teams_last" not in state
    while True:
        try:
            me = me or graph("/me", params={"$select": "id"})["id"]
            last = state.setdefault("teams_last", {})
            chats = graph("/me/chats", params={"$expand": "lastMessagePreview", "$top": 50}).get("value", [])
            for c in chats:
                p = c.get("lastMessagePreview")
                if not p or p.get("isDeleted"):
                    continue
                created = p.get("createdDateTime", "")
                if created <= last.get(c["id"], ""):
                    continue
                last[c["id"]] = created
                user = (p.get("from") or {}).get("user") or {}
                if first or user.get("id") == me:
                    continue
                onde = c.get("topic") or ("DM" if c.get("chatType") == "oneOnOne" else "grupo")
                key = teams_ref(c["id"])
                send(f"💬 Teams ({onde}) — {user.get('displayName', '?')}\n"
                     f"{short(html_text((p.get('body') or {}).get('content')))}\n\n/tr {key} <resposta>")
            first = False
            save_state()
        except Exception as e:
            print("Teams:", e)
        time.sleep(every)


# ---------- genérico
def mail_fetch(query="", n=10, skip=()):
    return google_mail(query, n, skip) if MAILCAL == "google" else ms_mail(query, n, skip)


def events_between(start, end):
    return google_events(start, end) if MAILCAL == "google" else ms_events(start, end)


def mail_ignore_list():
    """Remetentes ignorados: os do config + as notificações de PR quando o vigia de PRs já cobre."""
    ignore = [s.lower() for s in (MCFG.get("gmail_ignore_from") or MCFG.get("mail_ignore_from") or [])]
    if PRS and MCFG.get("skip_pr_emails", True):
        if GH:
            ignore.append("notifications@github.com")
        elif GL:
            ignore.append("gitlab@")
        else:
            ignore.append("azuredevops@microsoft.com")
    return ignore


def sender_name(addr):
    """'"Junior (JIRA)" <jira@x.com>' -> 'Junior (JIRA)'; sem nome, usa o e-mail."""
    name = (addr or "").split("<")[0].strip().strip('"').strip()
    return name or (addr or "?").strip("<> ")


def mail_digest(msgs):
    """Uma mensagem só por ciclo: agrupa por remetente e junta assuntos repetidos (Re:, Fwd:)."""
    groups = {}
    for m in msgs:
        subj = re.sub(r"^\s*((re|res|fwd?|enc)\s*:\s*)+", "", html.unescape(m.get("subject") or ""),
                      flags=re.I).strip() or "(sem assunto)"
        groups.setdefault(sender_name(m.get("from")), {}).setdefault(subj, []).append(m)
    snippet = MCFG.get("mail_snippet", False)
    blocks = [f"📧 {len(msgs)} e-mail(s) novo(s)"]
    for name, subjects in groups.items():
        slack_mail = any("slack.com" in m.get("from", "").lower() for ms in subjects.values() for m in ms)
        lines = [f"{'💬' if slack_mail else '✉️'} {short(name, 60)}"]
        for subj, ms in subjects.items():
            lines.append(f"• {short(subj, 120)}" + (f" ({len(ms)})" if len(ms) > 1 else ""))
            if snippet or slack_mail:  # no Slack o trecho é a própria mensagem
                lines.append(f"  {short(html.unescape(ms[-1].get('snippet') or ''), 160)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def mail_watcher():
    every = (MCFG.get("gmail_poll_min") or MCFG.get("mail_poll_min") or 3) * 60
    ignore = mail_ignore_list()
    first = "gmail_seen" not in state
    while True:
        try:
            seen = state.setdefault("gmail_seen", [])
            new = []
            for m in reversed(mail_fetch("", 20, skip=set(seen))):
                if m["id"] in seen:
                    continue
                seen.append(m["id"])
                if first or any(s in m.get("from", "").lower() for s in ignore):
                    continue
                new.append(m)
            if new:
                send(mail_digest(new))
            state["gmail_seen"] = seen[-300:]
            first = False
            save_state()
        except Exception as e:
            print("E-mail:", e)
        time.sleep(every)


DIAS = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5, "dom": 6}
DIAS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def fmt_events(evs, com_dia=False):
    fmt = "%d/%m %H:%M" if com_dia else "%H:%M"
    return "\n".join(f"{e['start']:{fmt}} {short(e['title'], 70)}" for e in evs)


def agenda(arg=""):
    a = (arg or "hoje").strip().lower()
    now = dt.datetime.now().astimezone()
    hoje = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if a == "semana":
        evs = events_between(now, hoje + dt.timedelta(days=7))
        return "📅 Próximos 7 dias:\n" + fmt_events(evs, True) if evs else "📅 Nada nos próximos 7 dias."
    if a == "hoje":
        dia = hoje
    elif a in ("amanha", "amanhã"):
        dia = hoje + dt.timedelta(days=1)
    elif a == "ontem":
        dia = hoje - dt.timedelta(days=1)
    elif a[:3] in DIAS:
        dia = hoje + dt.timedelta(days=(DIAS[a[:3]] - hoje.weekday()) % 7)
    else:
        try:
            d, m = (int(x) for x in a.split("/")[:2])
            dia = hoje.replace(month=m, day=d)
            if dia < hoje - dt.timedelta(days=60):
                dia = dia.replace(year=dia.year + 1)
        except Exception:
            return "Uso: /agenda [hoje | amanha | seg..dom | 15/10 | semana]"
    evs = events_between(dia, dia + dt.timedelta(days=1))
    titulo = f"📅 {DIAS_PT[dia.weekday()]} {dia:%d/%m}"
    return f"{titulo}:\n{fmt_events(evs)}" if evs else f"{titulo}: nenhuma reunião."


def emails(query=""):
    ms = mail_fetch(query.strip(), 10)
    if not ms:
        return "📭 Nenhum e-mail encontrado."
    return f"📧 {len(ms)} e-mail(s):\n\n" + "\n".join(
        f"• {short(m['from'], 60)}\n  {short(m['subject'], 100)}" for m in ms)


def briefing():
    parts = []
    if MAILCAL:
        try:
            parts.append(agenda("hoje"))
        except Exception as e:
            parts.append(f"Agenda: erro ({e})")
    if TASKS:
        try:
            parts.append(format_tasks(tasks_search()))
        except Exception as e:
            parts.append(f"Tarefas: erro ({e})")
    prs = state.get("gh_prs") or {}
    if prs:
        parts.append("🔀 PRs abertos:\n" + "\n".join(
            f"{p['repo']}: {short(p['title'], 60)} — CI {p['ci']} · {p['review']}" for p in prs.values()))
    return "☀️ Bom dia!\n\n" + "\n\n".join(parts)


def calendar_watcher():
    remind = MCFG.get("remind_min", 10)
    hour = MCFG.get("briefing_hour", 8)
    while True:
        try:
            now = dt.datetime.now().astimezone()
            notified = state.setdefault("cal_notified", [])
            for ev in events_between(now, now + dt.timedelta(minutes=remind + 1)):
                if ev["key"] in notified:
                    continue
                notified.append(ev["key"])
                mins = max(0, int((ev["start"] - now).total_seconds() // 60))
                send(f"⏰ Em {mins} min: {short(ev['title'], 100)}\n{ev['link']}".strip())
            state["cal_notified"] = notified[-100:]
            today = now.date().isoformat()
            if hour is not None and now.weekday() < 5 and now.hour >= hour and state.get("briefing_date") != today:
                state["briefing_date"] = today
                send(briefing())
            save_state()
        except Exception as e:
            print("Agenda:", e)
        time.sleep(60)


# ---------------------------------------------------------------- claude
CLAUDE = shutil.which(CFG.get("claude_cmd", "claude")) or CFG.get("claude_cmd", "claude")
job = {"proc": None, "desc": None, "started": None, "cancelled": False}
job_lock = threading.Lock()


def kill_tree(proc):
    """Encerra o processo e os filhos (no Windows o claude.cmd abre um node filho)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


def record_usage(data):
    """Acumula custo e tokens da chamada no contador diário."""
    try:
        day = dt.datetime.now().astimezone().date().isoformat()
        u = state.setdefault("usage", {}).setdefault(day, {"cost": 0.0, "in": 0, "out": 0, "runs": 0})
        u["cost"] += float(data.get("total_cost_usd") or 0)
        us = data.get("usage") or {}
        u["in"] += int(us.get("input_tokens") or 0) + int(us.get("cache_read_input_tokens") or 0)
        u["out"] += int(us.get("output_tokens") or 0)
        u["runs"] += 1
    except Exception as e:
        print("Uso:", e)


def run_claude(prompt, label, new_session):
    pre = CFG.get("remote_preamble", "")
    adir = CFG.get("artifacts_dir")
    if adir:
        pre = (pre + " ").strip() + (
            f" Salve TODOS os artefatos de trabalho (docs/tasks, relatórios, rascunhos .md) "
            f"em {adir}/{label}/ — nunca dentro do repositório do projeto.")
    prompt = f"{pre}\n\n{prompt}" if pre else prompt
    args = [CLAUDE, "-p", prompt, "--output-format", "json"] + CFG.get("claude_args", [])
    th = state.setdefault("threads", {}).get(label, {})
    if not new_session and th.get("session_id"):
        args += ["--resume", th["session_id"]]

    print("Executando:", CLAUDE, "| thread:", label, "| sessão:", th.get("session_id"))
    proc = subprocess.Popen(
        args,
        cwd=CFG["project_dir"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    job["proc"] = proc
    try:
        out, err = proc.communicate(timeout=CFG.get("timeout_min", 60) * 60)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        return "⏱️ Tempo limite atingido; tarefa encerrada."

    if job["cancelled"]:
        return None
    if proc.returncode != 0 and not out.strip():
        return f"❌ Claude saiu com código {proc.returncode}:\n{err[-1500:]}"

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out[-MAX_MSG:]

    record_usage(data)
    if data.get("session_id"):
        t = state.setdefault("threads", {}).setdefault(label, {"label": label})
        t["session_id"] = data["session_id"]
        t["updated"] = time.time()
        save_state()
    if data.get("is_error"):
        return f"❌ {data.get('result') or 'Erro sem descrição.'}"
    return data.get("result") or "(Claude terminou sem texto de resposta)"


def start_job(desc, prompt, label, new_session):
    with job_lock:
        if job["desc"]:
            send(f"⏳ Ainda estou em: {job['desc']}\nUse /parar para cancelar (uma tarefa por vez).")
            return
        job.update(desc=desc, started=time.time(), cancelled=False, proc=None)

    th = state.setdefault("threads", {})
    if new_session:
        th[label] = {"label": label, "session_id": None, "updated": time.time()}
    th.setdefault(label, {"label": label, "session_id": None, "updated": time.time()})
    state["active"] = label
    save_state()

    def worker():
        send(f"▶️ {desc}")
        try:
            result = run_claude(prompt, label, new_session)
        except Exception as e:
            result = f"❌ Falha ao executar o Claude: {e}"
        with job_lock:
            job.update(proc=None, desc=None, started=None)
        send("🛑 Tarefa cancelada." if result is None else f"✅ {desc}\n\n{result}")

    threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- comandos
def find_thread(label):
    """Acha o rótulo real de uma thread, ignorando maiúsc/minúsc. None se não existe."""
    th = state.get("threads", {})
    if label in th:
        return label
    low = label.lower()
    for k in th:
        if k.lower() == low:
            return k
    return None


def fmt_usage(arg=""):
    u = state.get("usage", {})
    if not u:
        return "Sem consumo registrado ainda."
    now = dt.datetime.now().astimezone().date()
    period = (arg or "hoje").strip().lower()
    if period in ("semana", "7d"):
        days = [(now - dt.timedelta(days=i)).isoformat() for i in range(7)]
        titulo = "7 dias"
    elif period in ("mes", "mês", "30d"):
        days = [(now - dt.timedelta(days=i)).isoformat() for i in range(30)]
        titulo = "30 dias"
    else:
        days = [now.isoformat()]
        titulo = "hoje"
    cost = sum(u.get(d, {}).get("cost", 0) for d in days)
    tin = sum(u.get(d, {}).get("in", 0) for d in days)
    tout = sum(u.get(d, {}).get("out", 0) for d in days)
    runs = sum(u.get(d, {}).get("runs", 0) for d in days)
    return (f"📊 Consumo ({titulo}):\n"
            f"US$ {cost:.2f} · {runs} execuç{'ão' if runs == 1 else 'ões'}\n"
            f"tokens: {tin/1000:.0f}k entrada · {tout/1000:.0f}k saída\n\n"
            "Este é o gasto das tarefas do Majordomo, não o saldo da sua assinatura.")


def thread_label(arg):
    """Deriva o rótulo de uma thread a partir do argumento de um comando.
    Card (PROJ-123) vira maiúsculo; nome livre vira slug; vazio gera inv-N."""
    import re as _re
    a = (arg or "").strip()
    if not a:
        state["inv_n"] = state.get("inv_n", 0) + 1
        return f"inv-{state['inv_n']}", ""
    first, _, rest = a.partition(" ")
    # parece um card? (letras-digitos, ex. PROJ-123, DSR-6428)
    if _re.fullmatch(r"[A-Za-z]{2,10}-\d{1,7}", first) or _re.fullmatch(r"[A-Za-z]{2,10}\d{2,7}", first):
        return first.upper(), rest.strip()
    # nome livre: slug do primeiro "token" significativo
    slug = _re.sub(r"[^a-z0-9-]+", "-", first.lower()).strip("-")[:30] or f"t{int(time.time())%10000}"
    return slug, rest.strip()


def fmt_threads():
    th = state.get("threads", {})
    if not th:
        return "Nenhuma thread aberta."
    act = state.get("active")
    rows = sorted(th.values(), key=lambda t: t.get("updated", 0), reverse=True)
    out = []
    for t in rows:
        mark = "▶ " if t["label"] == act else "  "
        out.append(f"{mark}{t['label']}")
    return "🧵 Threads abertas:\n" + "\n".join(out) + "\n\nAtiva: " + (act or "nenhuma")


def build_help():
    lines = []
    for name, c in CFG.get("commands", {}).items():
        uso = f"{name} CARD" if c.get("needs_card") else name
        lines.append(f"  {uso:<16} {c.get('desc', '')}")
    base = __doc__.split("Comandos no Telegram:")[1].split("\n", 2)[2]
    return "Comandos de trabalho:\n" + "\n".join(lines) + "\n\nGerais:\n" + base.rstrip()


def handle(msg):
    user = msg.get("from", {}).get("id")
    chat = msg["chat"]["id"]
    thread = msg.get("message_thread_id")
    text = (msg.get("text") or "").strip()

    # Canais intermediários: quem filtra o usuário é a ponte (bridge.py); IDs do Telegram não se aplicam aqui
    if DIRECT_TELEGRAM:
        # Modo configuração: responde com os IDs para você preencher o config.json
        if not CFG.get("allowed_user_id"):
            send(f"Modo configuração\nuser_id: {user}\nchat_id: {chat}\ntopic_id: {thread}", chat, thread)
            return

        # Segurança: só você, só no grupo/tópico configurado
        if user != CFG["allowed_user_id"] or chat != CFG["chat_id"]:
            return
        if CFG.get("topic_id") and thread != CFG["topic_id"]:
            return
    if not text:
        return

    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    tpl = CFG.get("templates", {})

    if cmd == "/ping":
        send("🏓 pong")
    elif cmd in ("/ajuda", "/help", "/start"):
        send(build_help())
    elif cmd == "/status":
        if job["desc"]:
            mins = int((time.time() - job["started"]) / 60)
            send(f"⏳ {job['desc']} — há {mins} min")
        else:
            send(f"💤 Livre. Thread ativa: {state.get('active') or 'nenhuma'}")
    elif cmd == "/parar":
        if job["proc"]:
            job["cancelled"] = True
            kill_tree(job["proc"])
        else:
            send("Nada rodando.")
    elif cmd == "/nova":
        act = state.get("active")
        if act and act in state.get("threads", {}):
            state["threads"].pop(act, None)
        state["active"] = None
        save_state()
        send(f"🧹 Thread {act or ''} encerrada." if act else "Nenhuma thread ativa.")
    elif cmd == "/tasks":
        if not TASKS:
            send("Jira/Azure DevOps não configurado no config.json.")
            return
        try:
            send(format_tasks(tasks_search(), arg))
        except Exception as e:
            send(f"❌ Erro ao consultar as tarefas: {e}")
    elif cmd == "/prs":
        if not PRS:
            send("GitHub/GitLab/Azure Repos não configurado no config.json.")
            return
        try:
            prs = prs_check(notify=False)
            if not prs:
                send("Nenhum PR aberto seu.")
            else:
                send("🔀 Seus PRs:\n\n" + "\n\n".join(
                    f"{p['repo']}: {short(p['title'], 80)}\nCI: {p['ci']} · {p['review']}\n{u}"
                    for u, p in prs.items()))
        except Exception as e:
            send(f"❌ Erro ao consultar PRs: {e}")
    elif cmd == "/sr":
        key, _, resp = arg.partition(" ")
        ref = state.get("slack_refs", {}).get(key)
        if not ref or not resp.strip():
            send("Uso: /sr s3 sua resposta  (o código vem na notificação)")
            return
        try:
            params = {"channel": ref["channel"], "text": resp.strip()}
            if ref.get("thread_ts"):
                params["thread_ts"] = ref["thread_ts"]
            slack("chat.postMessage", **params)
            send("📤 Enviado no Slack.")
        except Exception as e:
            send(f"❌ Erro ao enviar no Slack: {e}")
    elif cmd == "/aprovacao":
        if not SLACK.get("approval_channel"):
            send("Configure slack.approval_channel no config.json.")
            return
        pr, _, obs = arg.partition(" ")
        if not pr:
            send("Uso: /aprovacao <link do PR> [observação]")
            return
        text = SLACK.get("approval_text", "Pessoal, PR pronto para revisão: {pr} {obs}").format(pr=pr, obs=obs).strip()
        try:
            slack("chat.postMessage", channel=SLACK["approval_channel"], text=text)
            send("📤 Pedido de aprovação enviado no Slack.")
        except Exception as e:
            send(f"❌ Erro ao enviar no Slack: {e}")
    elif cmd == "/tr":
        key, _, resp = arg.partition(" ")
        cid = state.get("teams_refs", {}).get(key)
        if not cid or not resp.strip():
            send("Uso: /tr t3 sua resposta  (o código vem na notificação)")
            return
        try:
            graph(f"/chats/{cid}/messages", "POST", {"body": {"content": resp.strip()}})
            send("📤 Enviado no Teams.")
        except Exception as e:
            send(f"❌ Erro ao enviar no Teams: {e}")
    elif cmd in ("/agenda", "/emails") and not MAILCAL:
        send("E-mail/agenda não configurados (Google ou Microsoft) ou sem token.")
    elif cmd in ("/agenda", "/briefing", "/emails"):
        try:
            if cmd == "/agenda":
                send(agenda(arg))
            elif cmd == "/emails":
                send(emails(arg))
            else:
                send(briefing())
        except Exception as e:
            send(f"❌ Erro no Google: {e}")
    elif cmd == "/threads":
        send(fmt_threads())
    elif cmd == "/ir":
        label = arg.strip()
        if not label:
            send("Uso: /ir <rótulo>  (veja /threads)")
            return
        key = find_thread(label)
        if key:
            state["active"] = key; save_state(); send(f"▶ Thread ativa: {key}")
        else:
            send(f"Não achei a thread '{label}'. Veja /threads.")
    elif cmd == "/ligar":
        old, _, new_label = arg.partition(" ")
        old, new_label = old.strip(), new_label.strip()
        th = state.get("threads", {})
        src_key = find_thread(old)
        if not src_key or not new_label:
            send("Uso: /ligar <rótulo-antigo> <CARD>  (ex.: /ligar login-lento PROJ-457)")
            return
        new_key = new_label.upper()
        th[new_key] = {**th.pop(src_key), "label": new_key}
        if state.get("active") == src_key:
            state["active"] = new_key
        save_state()
        send(f"🔗 Thread '{src_key}' agora é {new_key} (contexto mantido).")
    elif cmd == "/resp":
        label, _, body = arg.partition(" ")
        key = find_thread(label.strip())
        if not key or not body.strip():
            send("Uso: /resp <rótulo> <texto>  (responde uma thread específica sem trocar a ativa)")
            return
        start_job(f"continuação de {key}", body.strip(), key, new_session=False)
    elif cmd == "/uso":
        send(fmt_usage(arg))
    elif cmd in CFG.get("commands", {}):
        c = CFG["commands"][cmd]
        new = c.get("new_session", False)
        if new:
            # /analisar, /investigar etc.: aceita card OU nome livre OU nada (gera inv-N)
            label, obs = thread_label(arg)
            card = label
        else:
            # /ok, /corrigir, /pr: operam na thread ativa
            label = state.get("active")
            if not label:
                send("Não há thread ativa. Comece com /analisar ou /investigar.")
                return
            card, obs = label, arg
        prompt = c["template"].format(card=card, obs=obs or "nenhuma")
        start_job(f"{cmd[1:]} {label}".strip(), prompt, label, new_session=new)
    elif cmd == "/claude":
        if not arg:
            send("Uso: /claude <pedido>")
            return
        label, _ = thread_label("")  # pedido livre abre thread própria
        start_job(f"pedido: {arg[:40]}", arg, label, new_session=True)
    elif cmd.startswith("/"):
        send("Comando desconhecido. /ajuda")
    else:
        label = state.get("active")
        if not label:
            # sem thread ativa (ex.: depois de /nova): texto livre abre sessão nova, como /claude
            label, _ = thread_label("")
            start_job(f"pedido: {text[:40]}", text, label, new_session=True)
            return
        start_job(f"continuação de {label}", text, label, new_session=False)


# ---------------------------------------------------------------- loop
def main():
    global CHANNEL
    CHANNEL = _channels.make_channel(CFG, state, save_state)
    print(f"Majordomo rodando (canal: {CFG.get('channel', 'telegram')}). Para sair, feche a janela.")
    if CFG.get("allowed_user_id") or not DIRECT_TELEGRAM:
        send("🟢 Majordomo online.")
        if TASKS.get("poll_min"):
            threading.Thread(target=tasks_poller, daemon=True).start()
        if SLACK.get("user_token"):
            threading.Thread(target=slack_watcher, daemon=True).start()
        if PRS and (GH.get("poll_min") or GL.get("poll_min") or AZDO.get("pr_poll_min")):
            threading.Thread(target=prs_watcher, daemon=True).start()
        if MAILCAL:
            if MCFG.get("gmail_poll_min") or MCFG.get("mail_poll_min"):
                threading.Thread(target=mail_watcher, daemon=True).start()
            if MCFG.get("calendar", True):
                threading.Thread(target=calendar_watcher, daemon=True).start()
            if MAILCAL == "microsoft" and MS.get("teams"):
                threading.Thread(target=teams_watcher, daemon=True).start()
    while True:
        try:
            for text, meta in CHANNEL.poll():
                # monta uma "mensagem" no formato que handle() espera
                if DIRECT_TELEGRAM and not CFG.get("allowed_user_id") and meta:
                    # modo configuração: usa os IDs reais de quem escreveu
                    print(f"Modo configuração: user_id={meta['from']} chat_id={meta['chat']} "
                          f"topic_id={meta['thread']}")
                    handle({"from": {"id": meta["from"]}, "chat": {"id": meta["chat"]},
                            "message_thread_id": meta["thread"], "text": text})
                    CHANNEL.ack(meta)
                    continue
                handle({"from": {"id": CFG.get("allowed_user_id")},
                        "chat": {"id": CFG.get("chat_id")},
                        "message_thread_id": CFG.get("topic_id"),
                        "text": text})
                CHANNEL.ack(meta)
            # canais intermediários não têm long-poll: aguarda um pouco entre ciclos
            if CFG.get("channel", "telegram") != "telegram":
                time.sleep(CFG.get("poll_sec", 15))
        except KeyboardInterrupt:
            if job["proc"]:
                kill_tree(job["proc"])
            print("Encerrado.")
            return
        except urllib.error.URLError as e:
            print("Rede:", e)
            time.sleep(5)
        except Exception as e:
            print("Erro:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
