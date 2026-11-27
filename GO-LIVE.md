# Go-live do Prompt Admissões (V1, 27/11/2026)

Estado: painel e ponte NO AR em modo seco. Nenhum número conectado, nenhuma mensagem saiu.

## Peças
| Peça | Onde | Porta | Estado |
|---|---|---|---|
| Painel da equipe | `admissoes/painel.py`, systemd `prompt-admissoes-painel` | 4133 (127.0.0.1) → https://admissoes.187-77-240-228.sslip.io/p/<segredo>/ | no ar |
| Ponte WA-AKG → Hermes | `admissoes/ponte.py`, systemd `prompt-admissoes-ponte` | 4134 (127.0.0.1) | no ar, modo seco |
| WhatsApp | WA-AKG `wa-akg-prompt` em /srv/wa-akg-prompt (pm2) | 4135 (127.0.0.1) | no ar no pm2, API key pronta, SEM sessão (falta o QR) |
| Agente | perfil Hermes `promptadmissoes` (fonte `perfil/`), só assinatura Codex | — | instalado, login ok |
| Dados e segredos | /srv/prompt-admissoes-dados (chmod 700): `admissoes.db`, `arquivos/`, `painel-segredo.txt`, `webhook-segredo.txt`, `chaves.json` | — | fora do repo |

Chaves (`/srv/prompt-admissoes-dados/chaves.json`, a ponte relê a cada 2 s):
- `envio_ligado` (false): false = modo seco, tudo vai para a Caixa de saída e NADA sai no WhatsApp.
- `cobranca_ligada` (true): cobranças 24/48/72 h (só saem de verdade com o envio ligado).

## Aviso antes de começar
- O WA-AKG usa Baileys (API NÃO oficial). Use um chip só da Prompt Admissões, sem conversas pessoais.
  Não use o número comercial (11) 99830-6824: um erro do robô fala com clientes reais.
- O robô responde a QUALQUER número que escrever (decisão do Lucca). Número sem cadastro vai para "Precisa de humano".

## Passos do Lucca
1. Separe o chip/celular do robô com WhatsApp (de preferência Business). Nome do perfil: "Prompt Serviços – DP".
2. Me avise. Eu (Claude) faço T1–T3 e mostro o QR.
3. Leia o QR: no celular, WhatsApp → Aparelhos conectados → Conectar aparelho.
4. Teste em modo seco (T4): cadastre você mesmo no painel e responda pelo seu celular. Veja na Caixa de saída o que o robô mandaria.
5. Diga "pode ligar o envio". Só então o robô manda mensagens de verdade (T5).

## Passos técnicos (Claude roda, com o ok do Lucca)
T0. AGORA PELO PAINEL: aba "Agente e WhatsApp" → "Conectar WhatsApp (gerar QR)" faz T2 e T3 sozinho e mostra o QR; "Ligar envio" faz T5 (zera as cobranças).
T1. API key: JÁ GERADA em 27/11 (CREDENCIAIS.txt, `API_KEY=`). Depois de criar a sessão (T2), no painel do WA-AKG
    por túnel (`ssh -L 4135:127.0.0.1:4135 root@187.77.240.228` → http://127.0.0.1:4135, login em CREDENCIAIS.txt):
    Bot Settings da sessão `prompt`: "Always Online" e "Auto Read" DESLIGADOS.
T2. Criar a sessão `prompt` e mostrar o QR:
```
KEY=$(grep '^API_KEY=' /srv/wa-akg-prompt/CREDENCIAIS.txt | cut -d= -f2)
curl -s -X POST http://127.0.0.1:4135/api/sessions -H "x-api-key: $KEY" -H 'Content-Type: application/json' -d '{"name":"prompt","sessionId":"prompt"}'
curl -s http://127.0.0.1:4135/api/sessions/prompt/qr -H "x-api-key: $KEY"
```
T3. Webhook do WA-AKG para a ponte:
```
SEG=$(cat /srv/prompt-admissoes-dados/webhook-segredo.txt)
curl -s -X POST http://127.0.0.1:4135/api/webhooks/prompt -H "x-api-key: $KEY" -H 'Content-Type: application/json' \
  -d "{\"name\":\"ponte prompt-admissoes\",\"url\":\"http://127.0.0.1:4134/webhook\",\"secret\":\"$SEG\",\"events\":[\"message.received\",\"message.sent\"]}"
```
T4. Conferir: `curl -s 127.0.0.1:4134/healthz`; mensagem do Lucca aparece na ficha; resposta na Caixa de saída (seco).
T5. Ligar o envio. ANTES, zere o relógio das cobranças de quem foi cadastrado em modo seco (senão todos recebem a cobrança 1 no mesmo minuto):
```
sqlite3 /srv/prompt-admissoes-dados/admissoes.db "UPDATE pessoas SET pedido_em=datetime('now','localtime'), ultima_cobranca=NULL, cobrancas=0 WHERE status='ativo'"
```
   Depois, em chaves.json, `"envio_ligado": true`. Desligar = voltar para false (vale na hora, painel e ponte).
   (`cobranca_em_seco` existe só para os testes; não use em produção.)

## Planilhas e arquivos que faltam (do Fabio Mansur)
- Lista de clínicas: crie/compartilhe a planilha com colunas `nome | cidade | endereco | dias (seg,ter,...) | inicio | fim | orientacoes | clientes`
  e ponha o ID em `config.json → clinicas.planilha_id`; `systemctl restart prompt-admissoes-ponte`. Até lá vale a clínica de Guarulhos (Rua Abaiara, 194).
- Modelo do contrato (PDF): suba em Clientes → Aerovip. Sem PDF o painel não deixa liberar contrato remoto (presencial funciona).
- Planilha de vagas e de admissões: só modelo (decisão: não importar dados antigos). Hoje as vagas entram pelo painel.

## Antes de ligar o envio
- Cadastros feitos em modo seco NÃO recebem a mensagem inicial depois (ela ficou na Caixa de saída). Reenvie pela ficha ("Mandar mensagem da equipe") ou recadastre.
- O WA-AKG não confere se o número tem WhatsApp antes de enviar. Erro de envio vai para "Precisa de humano".
- Clínicas (documento do Fabio, 27/11): PRESET usa o endereço da mensagem do DP (Rua Abaiara, 194), porque o documento não traz endereço; D2 está sem horário (marcada "exige agendamento"). Confirmar com o Fabio.
- Carregar/atualizar clínicas por documento: `.venv/bin/python scripts/carregar_clinicas.py /srv/prompt-admissoes-dados/clinicas-AAAA-MM-DD.json`.

## Parar tudo
`systemctl stop prompt-admissoes-ponte` (o robô para de ler e de cobrar). Painel: `systemctl stop prompt-admissoes-painel`.
