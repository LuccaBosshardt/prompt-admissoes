#!/bin/bash
# Instala o WA-AKG PRÓPRIO do Prompt Admissões: wa-akg-prompt (:4135, só 127.0.0.1). Não conecta número.
# Rodado em 27/11/2026 (V1). Passos seguintes no GO-LIVE.md.
# Modelo: /srv/wa-akg-compra (fork do WA-AKG com as correções locais: presença, RawMsg, escuta só no HOSTNAME).
# Banco: o MySQL Docker já existente `wa-akg-db` (127.0.0.1:3316), base NOVA `wa_akg_prompt`.
# Não mexe nas outras instâncias do WA-AKG.
set -euo pipefail
: "${ADMIN_EMAIL:?rode com ADMIN_EMAIL=<e-mail do admin do WA-AKG> bash instalar-gateway.sh}"

DEST=/srv/wa-akg-prompt
PORTA=4135
MODELO=/srv/wa-akg-compra
DB=wa_akg_prompt

[ -e "$DEST" ] && { echo "Já existe $DEST. Pare aqui e confira."; exit 1; }
if ss -tln | grep -q ":$PORTA "; then echo "Porta $PORTA ocupada. Escolha outra no vps_port_mapping."; exit 1; fi

# 1) Código: clone do repositório local do modelo (só o que está commitado; sem .env, sem dados, sem credenciais)
git clone --quiet "$MODELO" "$DEST"
cd "$DEST"
npm ci --silent

# 2) Base nova no MySQL existente (senha root lida do .env do wa-akg-db, nunca impressa)
V=$(grep -E 'MYSQL_ROOT_PASSWORD' /srv/wa-akg-db/docker-compose.yml | head -1 | sed -E 's/.*\$\{([A-Za-z0-9_]+).*/\1/')
ROOT_PASS=$(grep -E "^$V=" /srv/wa-akg-db/.env | head -1 | cut -d= -f2- | tr -d "\"'")
docker exec -i wa-akg-db mysql -uroot -p"$ROOT_PASS" -e "CREATE DATABASE IF NOT EXISTS $DB CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" 2>/dev/null

# 3) .env a partir do modelo, trocando porta, base, segredos e URLs
umask 077
AUTH_SECRET=$(openssl rand -hex 32)
SWAG_PASS=$(openssl rand -hex 12)
sed -E \
  -e "s#^PORT=.*#PORT=$PORTA#" \
  -e "s#^(DATABASE_URL=\"?mysql://[^/]+/)[^\"]+#\1$DB#" \
  -e "s#^AUTH_SECRET=.*#AUTH_SECRET=$AUTH_SECRET#" \
  -e "s#^(BASE_URL|NEXTAUTH_URL|AUTH_URL|NEXT_PUBLIC_APP_URL|NEXT_PUBLIC_API_URL)=.*#\1=http://127.0.0.1:$PORTA#" \
  -e "s#^APP_NAME=.*#APP_NAME=\"WA-AKG Prompt Admissões\"#" \
  -e "s#^NEXT_PUBLIC_SWAGGER_PASSWORD=.*#NEXT_PUBLIC_SWAGGER_PASSWORD=$SWAG_PASS#" \
  -e "s#^MEDIA_STORAGE_PATH=.*#MEDIA_STORAGE_PATH=$DEST/data/media#" \
  "$MODELO/.env" > .env
mkdir -p data/media logs

# 4) Esquema + admin + build
set -a; . ./.env; set +a   # node puro não lê o .env sozinho
npx prisma db push --skip-generate >/dev/null
ADMIN_PASS=$(openssl rand -base64 18 | tr -d '/+=')
npm run make-admin "$ADMIN_EMAIL" "$ADMIN_PASS" >/dev/null
npm run build >/dev/null
# Cadastro público DESLIGADO e fuso de SP (vem ligado / Asia/Jakarta por padrão)
docker exec -i wa-akg-db mysql -uroot -p"$ROOT_PASS" $DB -e "INSERT INTO SystemConfig (id, appName, timezone, enableRegistration, updatedAt) VALUES ('default','WA-AKG Prompt Admissões','America/Sao_Paulo',0,NOW()) ON DUPLICATE KEY UPDATE enableRegistration=0, timezone='America/Sao_Paulo';" 2>/dev/null

# 5) pm2 (arquivo próprio; PORT e HOSTNAME fixos, igual ao wa-akg-compra)
cat > ecosystem.prompt.config.js <<EOF2
// pm2 do WA-AKG do Prompt Admissões (porta $PORTA, só 127.0.0.1).
module.exports = { apps: [{
  name: "wa-akg-prompt", cwd: "$DEST", script: "npx", args: "tsx src/server/index.ts",
  interpreter: "none", watch: false, autorestart: true, instances: 1, exec_mode: "fork", max_memory_restart: "1G",
  env: { NODE_ENV: "production", PORT: "$PORTA", HOSTNAME: "127.0.0.1" },
  error_file: "$DEST/logs/pm2-error.log", out_file: "$DEST/logs/pm2-out.log",
  log_date_format: "YYYY-MM-DD HH:mm:ss" }] };
EOF2

# 6) Credenciais (chmod 600). A API_KEY do usuário entra aqui depois (painel → Profile → API Key).
cat > CREDENCIAIS.txt <<EOF3
# WA-AKG wa-akg-prompt — criado $(date +%F)
PAINEL=http://127.0.0.1:$PORTA (túnel SSH: ssh -L $PORTA:127.0.0.1:$PORTA root@187.77.240.228)
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=$ADMIN_PASS
SWAGGER_PASSWORD=$SWAG_PASS
API_KEY=PREENCHER_DEPOIS_DO_LOGIN
EOF3
chmod 600 CREDENCIAIS.txt .env

echo "Instalado em $DEST (ainda PARADO). Próximo passo: pm2 start $DEST/ecosystem.prompt.config.js"
