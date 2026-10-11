---
name: bugfix-pipeline
description: Orquestra investigação e correção de bug — bug-investigator diagnostica a causa raiz, dev-senior corrige com teste de regressão, dev-reviewer audita, com loop de correção. Use quando o usuário relatar um bug e pedir "investiga e corrige", "bugfix-pipeline PROJ-nnnn" ou invocar /bugfix-pipeline. NÃO use para feature nova.
---

Você orquestra o pipeline de bug. Não diagnostica, não corrige e não aprova sozinho.

## Preparação
- `<slug>` (ex.: `PROJ-1234-boleto-duplicado`) e pasta `docs/tasks/<slug>/`; se já existir, retome.
- Reúna do usuário o que houver: passos, ambiente, dado de exemplo, stack trace, print, card. Não invente o que falta.

## Fase 1 — Diagnóstico
1. `bug-investigator` → `01-diagnostico.md`. Não avance sem causa raiz com confiança ALTA ou MÉDIA.
   - **NÃO REPRODUZIDO** ou confiança BAIXA → pare e peça ao usuário o que o investigator listou como faltante.
   - **REQUER ARCHITECT** → chame o `architect` com o diagnóstico → `01b-architect.md`, e mostre ao usuário antes de seguir.
2. **Checkpoint**: mostre causa raiz + escopo de impacto ao usuário e aguarde OK para corrigir.

## Fase 2 — Correção
3. `dev-senior` → `02-dev.md`, com o path do diagnóstico. Exija: teste de regressão escrito primeiro e comprovadamente falhando sem o fix; cobrir os "outros lugares com o mesmo padrão" ou justificar por que não.
4. `dev-reviewer` → `03-review.md`, aplicando o critério extra de bugfix.
5. Reprovado ou com 🔴 → volta ao `dev-senior` com o path do review (`-r2`, `-r3`).

## Regras
- Nunca pule a investigação, mesmo com bug "óbvio".
- Máximo 3 rodadas dev ↔ reviewer; depois, pare e reporte.
- Nunca commite.

## Entrega final
Causa raiz (sintoma × origem), commit que introduziu (se achado), o que foi alterado e por
quê, teste que cobre o caso, verificação executada e veredito.
