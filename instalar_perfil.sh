#!/bin/bash
# Copia o perfil (fonte: perfil/) para /root/.hermes/profiles/promptadmissoes/. Não liga nada.
set -euo pipefail
FONTE=/srv/prompt-admissoes/perfil
DEST=/root/.hermes/profiles/promptadmissoes
[ -d "$DEST" ] || { hermes profile create promptadmissoes --no-skills --no-alias; }
install -m 600 "$FONTE/config.yaml" "$DEST/config.yaml"
for f in SOUL.md AGENTS.md IDENTITY.md; do install -m 644 "$FONTE/$f" "$DEST/$f"; done
echo "perfil promptadmissoes atualizado em $DEST"
