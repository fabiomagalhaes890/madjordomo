# Exemplos

Conteúdo genérico, sem referência a nenhum cliente. Adapte aos seus projetos.

## personas/
Cinco agentes com papéis definidos e formato de saída fixo:
`po`, `architect`, `dev-senior`, `dev-reviewer`, `bug-investigator`.

Instale em `~/.claude/agents/` (todos os projetos) ou
`<project_dir>/.claude/agents/` (um projeto).

## skills/
Orquestradores que encadeiam as personas com checkpoints:
`feature-pipeline` e `bugfix-pipeline`.

Instale em `~/.claude/skills/` ou `<project_dir>/.claude/skills/`.

## configs/
`config.minimo.json` — o mínimo para rodar (só Telegram + agente, sem vigias).
Copie para a raiz como `config.json` e preencha.
