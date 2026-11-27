# Prompt Admissões

Admissão de terceirizados da Prompt Serviços: o DP cadastra a pessoa aprovada, um agente Hermes conduz o
candidato no WhatsApp (documentos → exame → guia → contrato → data de início) e a equipe aprova cada decisão no painel.

- `admissoes/motor.py` — etapas, aprovações, cobranças (3 por etapa: 24/48/72 h, seg–sáb 8h–21h SP), vagas e estoque.
- `admissoes/fluxo.py` + `agente.py` — turno de conversa: EVENTO → `hermes --profile promptadmissoes -z` → JSON fechado.
- `admissoes/ponte.py` — webhook do WA-AKG (HMAC, keyId único) + relógio (cobrança, véspera, pós-exame, clínicas 10 min).
- `admissoes/painel.py` — painel da equipe atrás de link secreto (sem login).
- `perfil/` — fonte do perfil Hermes (só assinatura Codex). `instalar_perfil.sh` copia.
- `GO-LIVE.md` — como ligar o WhatsApp.

Testes: `uv run pytest`. Ensaio sem WhatsApp: `.venv/bin/python simular.py <telefone> "<texto>" [foto]`.
