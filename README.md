# Majordomo

**Orquestre agentes de IA (Claude Code ou Cursor CLI) pelo Telegram, com vigias que avisam o que precisa da sua atenção.**

O **Majordomo** é uma ponte entre o Telegram e um agente de IA rodando em modo headless no seu
computador. Você manda um comando pelo celular, ele executa o agente na pasta do projeto e
devolve a resposta no mesmo tópico do Telegram. Em paralelo, "vigias" opcionais monitoram
Jira, GitHub/GitLab, e-mail, agenda e chat, e avisam só quando algo precisa de você.

Suporta **múltiplos projetos em paralelo**, cada um isolado: um projeto por bot e por tópico,
tudo chegando num único grupo do Telegram. Assim dá para coordenar tudo de um lugar só, sem
ficar alternando entre janelas.

> Só usa a **biblioteca padrão do Python** (3.10+). Sem `pip install` para o núcleo.
> O vigia de navegador (opcional) precisa do Playwright.

> **Funciona com qualquer linguagem.** O Majordomo não conhece a sua stack — ele só dispara o
> agente (`claude -p` / `agent -p`) na pasta e repassa o seu comando. Quem decide o que rodar
> (build, testes, deploy) são as suas skills e os seus templates. Os exemplos deste repositório
> usam .NET e Node por conveniência, mas servem de molde para Python, Go, Java, Rust, o que for.

---

## Índice

- [Como funciona](#como-funciona)
- [Arquitetura](#arquitetura)
- [Instalação rápida](#instalação-rápida)
- [Passo a passo: bot do Telegram](#passo-a-passo-bot-do-telegram)
- [O arquivo config.json](#o-arquivo-configjson)
- [Permissões do agente](#permissões-do-agente)
- [Comandos](#comandos)
- [Agentes (personas) e pipelines](#agentes-personas-e-pipelines)
- [Como criar sua própria skill](#como-criar-sua-própria-skill)
- [Vigias (integrações)](#vigias-integrações)
  - [Jira / Azure DevOps](#jira--azure-devops)
  - [GitHub / GitLab / Azure Repos](#github--gitlab--azure-repos)
  - [Google (Gmail e Agenda)](#google-gmail-e-agenda)
  - [Microsoft 365 (Outlook, Agenda e Teams)](#microsoft-365-outlook-agenda-e-teams)
  - [Slack](#slack)
  - [Vigia de navegador (Teams/Slack sem app)](#vigia-de-navegador-teamsslack-sem-app)
- [Resumo de reuniões (Whisper local)](#resumo-de-reuniões-whisper-local)
- [Rodar como serviço (Windows)](#rodar-como-serviço-windows)
- [Vários projetos](#vários-projetos)
- [Segurança e privacidade](#segurança-e-privacidade)
- [Solução de problemas](#solução-de-problemas)
- [Aviso legal](#aviso-legal)

---

## Como funciona

1. Você cria um bot no Telegram e um grupo com um tópico por projeto.
2. O `majordomo.py` roda no seu computador, escuta o Telegram e só obedece a você (whitelist por `user_id`).
3. Quando você manda um comando (`/analisar PROJ-123`), ele dispara o agente de IA em modo
   headless (`claude -p "..."` ou `agent -p "..."`) na pasta do projeto, usando as **skills e
   personas** que você já configurou.
4. Quando o agente precisa de uma decisão sua (um checkpoint, uma pergunta), ele **para** e te
   manda a pergunta no Telegram. Você responde por texto, e ele **retoma a mesma sessão**.
5. Os vigias checam suas ferramentas de tempos em tempos e avisam no mesmo lugar.

A ideia central é trocar **validação em cada passo** por **dois portões humanos**: um para
responder perguntas/aprovar o plano, outro para autorizar a entrega (o commit/PR). Todo o
resto roda sozinho e você só lê o resumo no final.

---

## Arquitetura

```
  Telegram (seu celular)
        │  comandos / respostas
        ▼
  ┌─────────────┐      headless        ┌──────────────────┐
  │ majordomo.py │ ───────────────────▶ │  Claude Code      │
  │ (1 por bot) │   claude -p / agent  │  ou Cursor CLI    │
  └─────────────┘ ◀─────────────────── │  + suas skills    │
        ▲   ▲        resultado (JSON)   └──────────────────┘
        │   │
        │   └── vigias (threads): Jira/DevOps, GitHub/GitLab, Gmail/Outlook, Agenda, Teams
        │
        └── browser_watch.py (processo à parte): lê Teams/Slack web pela tela
```

- **Um bot por instância.** O Telegram só deixa um processo ler as mensagens de um bot. Para
  rodar várias instâncias (vários projetos), crie um bot para cada — todos podem ficar no
  mesmo grupo, em tópicos diferentes.
- **Estado em disco** (`state.json`): sessão atual, o que cada vigia já avisou. Sobrevive a
  reinícios.

---

## Instalação rápida

Pré-requisitos:

- **Python 3.10+** — descubra o caminho com `python -c "import sys; print(sys.executable)"`
- **Claude Code** já logado (rode `claude` uma vez na pasta do projeto e aceite confiar nela),
  **ou** **Cursor CLI** (`agent`) logado.

```bash
git clone https://github.com/<voce>/majordomo.git
cd majordomo
cp config.example.json config.json
# edite config.json (veja abaixo)
python majordomo.py
```

Na primeira execução, com `allowed_user_id` ainda em `0`, o bot entra em **modo configuração**
e responde com os IDs que você precisa preencher.

---

## Passo a passo: bot do Telegram

1. **Criar o bot:** no [@BotFather](https://t.me/botfather), mande `/newbot`, escolha um nome e
   um username terminado em `bot`. Guarde o **token**.
2. **Grupo e tópico:** crie um grupo, ative **Tópicos** nas configurações, crie o tópico do
   projeto e adicione o bot.
3. **Privacidade:** promova o bot a **administrador** do grupo (não precisa marcar nenhuma
   permissão). Admin recebe todas as mensagens. _Alternativa:_ no BotFather, `/setprivacy` →
   **Disable**, depois remova e adicione o bot de novo ao grupo.
4. **Modo configuração:** com `telegram_token` preenchido e `allowed_user_id`/`chat_id`/
   `topic_id` ainda em `0`/`null`, rode `python majordomo.py` e mande qualquer mensagem no
   tópico. O bot responde com os três IDs.
5. **Fechar:** preencha os três IDs no `config.json` (números sem aspas; `chat_id` de grupo
   começa com `-100`) e reinicie. Deve chegar **"🟢 Majordomo online."**. Teste com `/ping`.

---

## O arquivo config.json

O `config.example.json` vem com **todos os blocos opcionais comentados** (linhas iniciadas com
`//`). Para ligar um, apague os `//` das linhas dele. Escolha **um** provedor de cada tipo:

| Precisa de | Opções (use uma) |
|---|---|
| Agente de IA | Claude Code (`"claude"`) **ou** Cursor CLI (`"agent"`) |
| Tarefas | Jira **ou** Azure DevOps |
| PRs / MRs | GitHub **ou** GitLab **ou** Azure Repos |
| E-mail e agenda | Google **ou** Microsoft 365 |
| Chat | Slack (app) **ou** vigia de navegador |

Campos principais:

| Campo | O que é |
|---|---|
| `telegram_token` | Token do bot desta instância (único por instância). |
| `allowed_user_id` / `chat_id` / `topic_id` | Você, o grupo e o tópico. Só você, nesse tópico, comanda. |
| `project_dir` | Pasta onde o agente roda. Com Claude, use a pasta que tem `.claude/` (agentes, skills, MCP). Barras normais: `C:/Projetos/cliente/workspace`. |
| `claude_cmd` / `claude_args` | Claude: `"claude"` + `["--permission-mode","acceptEdits"]`. Cursor: `"agent"` + `["--force"]`. |
| `timeout_min` | Limite por tarefa. Pipelines completos: 90. |
| `remote_preamble` | Texto injetado em toda chamada: avisa o agente que você está no celular e pede resumos curtos nos checkpoints. |
| `artifacts_dir` | Pasta **fora do repositório** onde os pipelines gravam os `.md` de trabalho (um subdiretório por thread). Evita poluir o projeto. |
| `commands` | Seus comandos de trabalho. Veja [Comandos](#comandos). |

---

## Permissões do agente

Em modo headless **ninguém aprova pedidos de permissão na hora**: o que não estiver na lista
`allow` é negado. Por isso um MCP pode funcionar no Claude interativo e falhar pelo Telegram.

1. Descubra o nome do servidor MCP (ex.: Jira): dentro do `project_dir`, `claude mcp list`. A
   permissão é `mcp__NOME` (libera todas as ferramentas dele).
2. Crie `<project_dir>/.claude/settings.local.json` (pessoal, fora do git) com o bloco de
   `permissions`. Veja `claude-settings.example.json`.
3. Teste antes do Telegram: `claude -p "leia o card XXX-123 e diga só o título"`.
4. Quando um pipeline for bloqueado, o erro diz a ferramenta (ex.: `Bash(dotnet test:*)`):
   acrescente em `allow`.

Merge, `git push --force` e ações destrutivas ficam no `deny` de propósito.

**Os comandos do `allow` dependem da sua stack.** O `claude-settings.example.json` traz um
conjunto comum (git, `gh`, mais exemplos de build/teste de .NET e Node) — mantenha os do seu
projeto e remova o resto. Exemplos por stack:

| Stack | Comandos típicos no `allow` |
|---|---|
| .NET | `Bash(dotnet build:*)`, `Bash(dotnet test:*)`, `Bash(dotnet restore:*)`, `Bash(msbuild:*)` |
| Node / Angular | `Bash(npm run build:*)`, `Bash(npm run test:*)`, `Bash(npx ng test:*)`, `Bash(pnpm:*)` |
| Python | `Bash(pytest:*)`, `Bash(ruff:*)`, `Bash(python -m build:*)` |
| Go | `Bash(go build:*)`, `Bash(go test:*)` |

Regra geral: qualquer comando que suas skills rodem precisa estar no `allow`, senão o agente
headless é bloqueado sem pedir confirmação.

---

### Ambiente do projeto

O agente roda os comandos de build/teste do **seu** projeto. Garanta que eles funcionam **via
linha de comando** na máquina antes de automatizar pelo Majordomo — se não rodam no terminal,
também não vão rodar pelo Telegram.

- **.NET:** o `dotnet`/`msbuild` precisa estar no PATH, e os pacotes restaurados
  (`dotnet restore` / `nuget restore`). Feed privado exige autenticação configurada na máquina.
- **Node / Angular e afins:** dependências instaladas (`npm ci`). Testes que abrem um navegador
  headless (Karma/ChromeHeadless, Cypress, Playwright) podem exigir configuração extra de
  ambiente — e a versão do navegador instalado precisa ser compatível com a do runner de teste.
  Confirme que o teste roda headless no terminal antes de pô-lo numa skill.
- **Qualquer stack:** variáveis de ambiente, segredos de build e serviços locais (banco, cache)
  precisam estar disponíveis para o agente, que roda com o seu usuário.

## Comandos

Os comandos de trabalho ficam no `config.json`, em `"commands"`. Cada um tem `desc`,
`needs_card`, `new_session` e um `template` (com `{card}` e `{obs}`). Criar um comando novo é
só acrescentar um item — sem mexer no código.

Comandos de controle (fixos):

| Comando | O que faz |
|---|---|
| `/claude <texto>` | Pedido livre ao agente (nova sessão). |
| `<texto normal>` | Continua a **thread ativa** (responder perguntas/checkpoints). |
| `/threads` | Lista as threads abertas e qual está ativa. |
| `/ir <rótulo>` | Troca a thread ativa. |
| `/resp <rótulo> <texto>` | Responde uma thread específica sem trocar a ativa. |
| `/ligar <antigo> <CARD>` | Vincula uma investigação sem-card ao card quando ele for criado. |
| `/uso [hoje\|semana\|mes]` | Consumo das tarefas (custo e tokens). |
| `/status` | O que está rodando agora. |
| `/parar` | Cancela a tarefa em andamento. |
| `/nova` | Encerra a thread ativa. |
| `/ping`, `/ajuda` | Teste de vida e lista de comandos. |

Comandos dos vigias (quando configurados):

| Comando | O que faz |
|---|---|
| `/tasks` | Cards/work items abertos em seu nome (Jira ou Azure DevOps). |
| `/prs` | Status dos seus PRs/MRs (GitHub, GitLab ou Azure Repos). |
| `/agenda [hoje\|amanha\|seg..dom\|15/10\|semana]` | Reuniões. |
| `/emails [busca]` | Últimos não lidos, ou uma busca. |
| `/briefing` | Agenda + cards + PRs (também automático de manhã). |
| `/sr <ref> <texto>` / `/tr <ref> <texto>` | Responder no Slack / Teams a uma notificação. |

O conjunto de comandos de trabalho do exemplo (`config.example.json`) é:

- `/analisar PROJ-123` — roda a `feature-pipeline` até o primeiro checkpoint (não mexe no código).
- `/investigar PROJ-123` — roda só o diagnóstico da `bugfix-pipeline` (sem corrigir).
- `/ok [obs]` — aprova o checkpoint e segue o pipeline.
- `/corrigir [obs]` — depois do `/investigar`, segue para o fix.
- `/pr [obs]` — depois que **você revisou o diff**: commit, push, abre o PR (sem merge) e move o card.

### Threads: vários itens em paralelo

Cada trabalho em andamento é uma **thread** com um rótulo e a sua própria sessão de IA — então
você alterna entre itens sem perder o contexto de nenhum.

- O rótulo pode ser um **card** (`/analisar PROJ-123`), um **nome livre** (`/investigar
  login-lento`) ou **nada** — aí o Majordomo gera um `inv-1`, `inv-2`… Útil para começar uma
  investigação **antes de existir o card**.
- `/threads` mostra o que está aberto; `/ir <rótulo>` troca a ativa; texto normal vai para a ativa.
- Criou o card depois? `/ligar login-lento PROJ-457` renomeia a thread para o card, **mantendo
  todo o histórico** da investigação.
- `/resp <rótulo> <texto>` responde uma thread específica sem trocar a ativa.

> O Majordomo roda **uma tarefa por vez** (se você disparar outra com uma em andamento, ele
> avisa). As threads guardam o contexto de cada item; elas não rodam duas execuções no mesmo
> instante.

---

## Agentes (personas) e pipelines

A inteligência do fluxo não está no Majordomo — está nas suas **skills e personas** do agente de
IA. O Majordomo só decide *quando* chamar o agente e entrega o resultado no Telegram.

Em `examples/personas/` há cinco personas de exemplo (genéricas, sem referência a nenhum
cliente), cada uma com um papel e um formato de saída fixo:

| Persona | Papel | Modelo sugerido |
|---|---|---|
| `po` | Transforma pedido em user story + critérios de aceite (Gherkin). | médio (sonnet) |
| `architect` | Decisões estruturais antes de codar; aponta riscos. | forte (opus) |
| `dev-senior` | Implementa a partir da definição aprovada, com testes. | forte/médio |
| `dev-reviewer` | Audita o diff contra a definição, veredito claro. | forte (opus) |
| `bug-investigator` | Acha a causa raiz de um bug **sem** corrigir. | forte (opus) |

Em `examples/skills/` há dois orquestradores que encadeiam essas personas com checkpoints:

- `feature-pipeline.md` — PO → Architect → Dev → Reviewer, com loops de correção.
- `bugfix-pipeline.md` — Investigator → (Architect, se necessário) → Dev → Reviewer.

**Como instalar as personas/skills (Claude Code):**

- Para valer em **todos os projetos**: copie para `~/.claude/agents/` (personas) e
  `~/.claude/skills/` (skills).
- Para um projeto só: copie para `<project_dir>/.claude/agents/` e
  `<project_dir>/.claude/skills/`.

No Cursor, instale as personas como subagentes/custom modes e as pipelines como regras/comandos,
conforme a documentação da sua versão.

**A regra de ouro:** as personas **nunca commitam**. O pipeline termina com o código no working
tree; você revisa o `git diff` e só o comando `/pr` autoriza o commit e a abertura do PR.

---

## Como criar sua própria skill

Uma skill é um arquivo `SKILL.md` com um cabeçalho YAML e instruções em Markdown. O agente
decide usá-la pela `description`, então escreva a descrição pensando em **quando** ela deve ser
acionada.

Modelo mínimo:

```markdown
---
name: minha-skill
description: O que faz e QUANDO usar. Ex.: "Gera release notes a partir dos commits desde a última tag. Use quando o usuário pedir 'release notes' ou invocar /release-notes."
---

Instruções passo a passo para o agente seguir.

## Passos
1. Rode `git log <ultima-tag>..HEAD --oneline`.
2. Agrupe por tipo (feat, fix, chore) ...
3. Escreva em PT-BR, uma linha por item.

## Formato de saída
...
```

Dicas:

- **Descrição é um gatilho.** Inclua as palavras que você usaria e o nome do comando (ex.:
  "invocar /release-notes").
- **Uma skill pode chamar personas.** Veja como as pipelines de exemplo invocam `po`, `architect`
  etc. e passam os artefatos por caminho de arquivo (não por resumo).
- **Checkpoints** são o que deixa o fluxo seguro pelo Telegram: faça a skill **parar** e pedir
  confirmação antes de qualquer passo irreversível.
- **Ligue ao Majordomo** acrescentando um comando no `"commands"` do `config.json`:

```jsonc
"/release": {
  "desc": "gera release notes do projeto",
  "needs_card": false,
  "new_session": true,
  "template": "Use a skill minha-skill para gerar as release notes. Observações: {obs}."
}
```

---

## Vigias (integrações)

Cada vigia é opcional e ligado pelo seu bloco no `config.json`. Todos usam APIs oficiais (exceto
o vigia de navegador). As ações que mandam algo em seu nome (responder, pedir aprovação) sempre
passam por um comando seu.

> **Vigias não gastam token de IA.** Eles falam direto com as APIs dos serviços (ou leem a tela,
> no caso do navegador). Monitorar Jira, e-mail, chat etc. o dia todo tem custo zero de IA — o
> consumo vem apenas das tarefas do agente. Acompanhe com `/uso`.

### Jira / Azure DevOps

Use **um** dos dois para `/tasks` e o aviso de card novo.

- **Jira:** gere um token em id.atlassian.com → Tokens de API. Bloco `jira` com `base_url`,
  `email`, `api_token` (ou `pat` para Server/Data Center) e `jql`.
- **Azure DevOps:** PAT com escopos *Work Items (Read)* e *Code (Read)*. Bloco `azure_devops`
  com `org`, `project`, `pat`. Também cobre PRs do Azure Repos.

### GitHub / GitLab / Azure Repos

Use **um** dos três para `/prs` e os avisos de CI/pipeline, aprovação, comentário e merge.

- **GitHub:** `gh auth login` (CLI oficial). Bloco `github` com `owner`.
- **GitLab:** PAT com escopo `api`. Bloco `gitlab` com `base_url` e `token`. (MR = Merge Request.)
- **Azure Repos:** incluso no bloco `azure_devops`.

### Google (Gmail e Agenda)

Somente leitura. Precisa de um projeto no Google Cloud (pode ser com sua conta pessoal):

1. Ative **Gmail API** e **Google Calendar API**.
2. Em **Google Auth Platform** (antiga "Tela de permissão OAuth"), tipo **Externo**; adicione a
   conta em *Usuários de teste* (e **Publicar** para o token não expirar em 7 dias).
3. Crie uma credencial **App para computador**, baixe como `google_client.json`.
4. Rode `python google_auth.py` e autorize com a conta certa. Gera `google_token.json`.
5. Preencha o bloco `google` no config.

### Microsoft 365 (Outlook, Agenda e Teams)

Cobre e-mail, agenda e chats do Teams:

1. Em entra.microsoft.com, registre um app **multilocatário**, com **fluxos de cliente público**
   habilitados.
2. Permissões delegadas (Graph): `User.Read`, `Mail.Read`, `Calendars.Read`, `Chat.Read`,
   `ChatMessage.Send`, `offline_access`.
3. Copie o *client id* para o bloco `microsoft`.
4. Rode `python ms_auth.py`, abra microsoft.com/devicelogin, entre com a conta e digite o código.
   O script testa conta, Outlook, Agenda e Teams.

> Muitos tenants exigem aprovação de admin para apps de terceiros. Se bloquear, use as
> notificações nativas no celular, ou o vigia de navegador.

### Slack

Se o workspace permite apps: crie um app via manifest (escopos de **usuário**: `search:read`,
`im:read`, `im:history`, `users:read`, `chat:write`), instale e copie o *User OAuth Token*
(`xoxp-...`) para o bloco `slack`. Dá avisos de menção/DM e o `/sr` para responder.

Se o workspace **não** permite apps: use o [vigia de navegador](#vigia-de-navegador-teamsslack-sem-app).

### Vigia de navegador (Teams/Slack sem app)

Quando não dá para instalar app, o `browser_watch.py` abre a versão web num navegador próprio e
**lê a tela**. Roda como processo separado, mas usa o mesmo bot/tópico.

```bash
python -m pip install playwright
python -m playwright install chromium
python browser_watch.py              # 1ª vez: faça login na janela
python browser_watch.py --headless   # depois, sem janela
```

- Os seletores de tela ficam em `selectors.json` (editável — o layout do Teams/Slack muda com o
  tempo).
- **Autoconserto:** se o título da aba indica mensagem não lida mas a lista não é encontrada, o
  vigia salva um print + HTML, pausa e te pergunta no Telegram. Você confere e manda
  `/consertar`: o agente lê o HTML e atualiza só os seletores, roda um autoteste e volta a
  vigiar se passar.
- Comandos: `/br <ref> <texto>` (responder), `/consertar`, `/cancelar`.

> **Atenção:** ler o conteúdo do chat por automação de tela e enviá-lo ao Telegram pode ferir a
> política de uso da ferramenta na sua empresa. Use só onde é permitido e nunca trafegue dados
> sensíveis.

---

## Resumo de reuniões (Whisper local)

O `meeting_watch.py` grava o **áudio do sistema** durante a reunião (o que você ouve), transcreve
**localmente** com [faster-whisper](https://github.com/SYSTRAN/faster-whisper) e pede ao agente um
resumo, que chega no Telegram. **Nada de áudio sai da máquina** — a transcrição é toda local.

Funciona em qualquer plataforma (Meet, Teams, Zoom, telefone no viva-voz), porque capta o som que
sai do computador, não a plataforma.

```bash
python -m pip install faster-whisper soundcard numpy
```

Config (bloco `meeting` no `config.json`):

```jsonc
"meeting": {
  "model": "medium",            // tiny|base|small|medium|large-v3 — pt: medium ou maior
  "device": "auto",            // auto|cpu|cuda
  "language": "pt",
  "out_dir": "C:/majordomo/reunioes",
  "summarize": true             // false = só transcrição, sem gastar token
}
```

Uso pelo Telegram (rode `python meeting_watch.py --serve`, ou junte ao Agendador):

| Comando | O que faz |
|---|---|
| `/gravar [rótulo]` | Começa a gravar o áudio do sistema. |
| `/parar` | Para, transcreve e envia o resumo (assunto, decisões, pendências, o que pediram a você, próximos passos). |
| `/status_reuniao` | Diz se está gravando e há quanto tempo. |

Ou pela linha de comando: `python meeting_watch.py start`, depois `Ctrl+C` para encerrar; ou
`python meeting_watch.py file reuniao.wav` para processar um áudio já gravado.

**Captura de áudio (loopback).** Usa a biblioteca `soundcard` para gravar o *loopback* do
alto-falante (o som que sai do PC). No Windows e no Linux costuma funcionar direto. Se a gravação
sair muda, confira o dispositivo de saída selecionado e, no Windows, que o "Stereo Mix" ou
equivalente não está desabilitado.

**Desempenho.** Com GPU NVIDIA (`device: "cuda"`) a transcrição roda muitas vezes mais rápido que
o tempo real; na CPU funciona, mas é bem mais lento. O modelo `medium` é um bom equilíbrio para
português; `large-v3` é mais preciso e mais pesado. Esse módulo precisa de `pip install`, então só
roda nos seus computadores próprios.

**Privacidade e consentimento.** Gravar uma conversa da qual você participa costuma ser aceito,
mas contrato, NDA ou política do cliente podem exigir aviso aos participantes. Avise que está
transcrevendo para suas anotações. Não é aconselhamento jurídico — confira as regras de cada
cliente. Áudios e transcrições ficam só na sua máquina (e no `.gitignore`).

## Rodar como serviço (Windows)

Use o **Agendador de Tarefas** (Criar Tarefa, não a básica):

- **Geral:** "Executar somente quando o usuário estiver conectado".
- **Disparadores:** Ao fazer logon, atrasar 30s.
- **Ações:** Programa = caminho completo do `pythonw.exe`; Argumentos = `majordomo.py`; Iniciar
  em = a pasta do Majordomo.
- **Condições:** desmarque "só na rede elétrica".
- **Configurações:** desmarque "interromper após 3 dias"; marque reiniciar a cada 1 min (3×).

Alternativa simples: um `.bat` com `cd /d` + `python majordomo.py` e um atalho em `shell:startup`
com "Executar: Minimizada".

---

## Vários projetos

O mesmo `majordomo.py` serve para todos. O que muda é o `config.json` de cada um.

1. Crie um **bot novo** e um **tópico novo** no mesmo grupo.
2. Copie a pasta do Majordomo (sem `state.json`, `config.json` nem os arquivos de token).
3. Crie o `config.json`, pegue os IDs no modo configuração (`chat_id` é o mesmo; muda o
   `topic_id`).
4. Ajuste `project_dir`, `commands` e os blocos de vigia daquele projeto.
5. Crie a tarefa no Agendador apontando para a pasta.

Duas instâncias podem rodar no mesmo computador, cada um com sua pasta, bot, tópico e tarefa.

---

## Segurança e privacidade

- **Whitelist sempre preenchida:** o bot obedece só ao seu `user_id` e só no tópico configurado.
- **Telegram não é ponta-a-ponta em bots:** trafegue resumos, IDs de card e links — nunca dados
  de clientes, nem segredos.
- **Somente leitura onde dá:** Google (escopos `readonly`), banco de dados (usuário read-only),
  provedores de nuvem (perfil read-only).
- **Nada sai em seu nome sem você ver:** `/pr`, `/sr`, `/tr` e respostas exigem comando seu.
- **Segredos nunca entram no git:** `config.json`, `state.json`, `google_token.json`,
  `ms_token.json`, `browser_state.json`, `brchrome/` e `evidencia/` estão no `.gitignore`.
  Confira com `git status` antes do primeiro push.

---

## Solução de problemas

| Sintoma | Causa e solução |
|---|---|
| Bot não responde; console só diz "Majordomo rodando" | Mensagens do grupo não chegam ao bot. Promova o bot a admin (ou `setprivacy` Disable e re-adicione). Teste `/ping@nome_do_bot`. |
| `HTTP Error 401/404` | Token errado ou incompleto. |
| `CERTIFICATE_VERIFY_FAILED` | Certificados do Python ou proxy. Rode o *Install Certificates* do Python ou atualize `certifi`. |
| Fica "em execução" e nunca responde | Agente esperando stdin/login/confiança na pasta. Rode `claude` interativo uma vez no `project_dir`; teste `claude -p "diga oi" --output-format json`. |
| `Ctrl+C` não para (Windows) | Espera do Telegram em andamento. Feche a janela ou `taskkill /IM python.exe /F`. |
| Permissão negada no MCP ou git | Falta em `allow` do `settings.local.json`. Use `mcp__NOME` do `claude mcp list`. |
| Build falha (.NET) | `dotnet`/`msbuild` não está no PATH, pacotes não restaurados (`dotnet restore`/`nuget restore`) ou o comando não está no `allow`. Teste `dotnet build` no terminal primeiro. |
| Teste de front falha ao rodar headless | Testes com navegador (Angular/Karma, Cypress, Playwright) exigem ambiente preparado e versão de navegador compatível com o runner. Confirme que rodam headless no terminal antes de automatizar. |
| `/emails` vazio mas a agenda funciona | Confirme a conta autorizada e que o `/emails in:inbox` também vem vazio; o e-mail pode já ter sido lido. |
| Vigia de navegador não acha as mensagens | O layout mudou. Confira o login na janela; use `/consertar` ou ajuste `selectors.json`. |

---

## Aviso legal

Este projeto automatiza o seu próprio trabalho usando as suas credenciais. **Antes de usar com
qualquer cliente ou empregador**, confirme que a automação, a leitura de chats e o tráfego de
informações para fora do ambiente estão de acordo com o contrato, a política de uso e a
legislação aplicável. O autor não se responsabiliza pelo uso indevido. Nenhuma linha deste
repositório contém dados de clientes — todos os exemplos são genéricos. Licenciado sob AGPL-3.0.

---

## Apoie o projeto

O Majordomo é gratuito e open source. Se ele te economiza tempo, considere apoiar pelo
[GitHub Sponsors](https://github.com/sponsors/SEU_USUARIO) — ajuda a manter o
desenvolvimento, a documentação e o site.

## Licença

**AGPL-3.0.** O Majordomo é livre e de código aberto — você pode usar, estudar, modificar e
distribuir. A AGPL acrescenta uma garantia: quem usar este código (inclusive modificado) num
produto ou serviço acessível pela rede precisa **disponibilizar o código-fonte** dessas
modificações sob a mesma licença. Na prática, isso mantém o projeto aberto e impede que alguém
o feche para revender. Veja [LICENSE](LICENSE).
