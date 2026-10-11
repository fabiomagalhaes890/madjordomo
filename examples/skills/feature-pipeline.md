---
name: feature-pipeline
description: Orquestra uma feature ponta a ponta com os agentes pessoais — po define a story, architect valida, dev-senior implementa, dev-reviewer audita, com loops de correção e checkpoints com o usuário. Use quando o usuário pedir "roda o pipeline da feature", "feature-pipeline PROJ-nnnn" ou invocar /feature-pipeline. NÃO use para bug (use bugfix-pipeline).
---

Você orquestra o pipeline. Não define requisito, não desenha, não implementa e não
aprova sozinho — cada decisão de conteúdo é do agente responsável. Você coordena, faz o
relay e decide quando avançar ou voltar.

## Preparação
1. Defina o `<slug>` (ex.: `PROJ-1234-filtro-acordos`; sem card, derive da descrição) e a pasta `docs/tasks/<slug>/`. Se já existir com artefatos, **retome** da última etapa.
2. Identifique os módulos envolvidos. Se não der para identificar com confiança, pergunte.
3. Toda chamada de agente recebe: `<slug>`, caminho da pasta, o pedido original e os **paths dos artefatos anteriores** (o agente lê o arquivo — não resuma o conteúdo de outro agente).

## Fase 1 — Definição
1. `po` → `01-po.md`.
2. `architect` → `02-architect.md`. Sem interface, pule qualquer etapa de UX e registre isso.
3. Qualquer 🔴 ou pergunta ao PO → volte ao `po`; nova rodada gera `01-po-r2.md` etc.
4. Pergunta que só o usuário responde (regra de negócio, prioridade) → pare e pergunte ao usuário.
5. **Checkpoint**: mostre story final + decisões de arquitetura e aguarde OK antes da Fase 2.

## Fase 2 — Implementação
6. `dev-senior` → `03-dev.md` (relatório) + código no working tree.
7. `dev-reviewer` (contexto limpo) → `04-review.md`. Passe os artefatos 01–03 e diga para revisar o `git diff`.
8. Reprovado ou com 🔴 → volte ao `dev-senior` com o path do review (`03-dev-r2.md`, `04-review-r2.md`...).

## Regras
- Nunca pule fase. Sem aprovação do architect (sem 🔴), não há implementação.
- Loop sem convergir em **3 rodadas** → pare e reporte ao usuário.
- Mostre um resumo curto ao fim de cada fase; não rode tudo em silêncio.
- Nunca commite. O usuário revisa o diff e aciona a entrega.

## Entrega final
Resumo com: story, decisões de arquitetura, arquivos alterados, verificação executada,
veredito do reviewer e itens 🟡/🟢 não tratados.
