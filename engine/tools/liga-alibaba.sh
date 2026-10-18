#!/usr/bin/env bash
# Grava as credenciais do Alibaba Model Studio no .env da VM e liga o provedor.
#
# Existe como script porque a chave não pode ser digitada num comando de duplo
# salto sem virar sopa de aspas, e porque ela não deve ficar no histórico do
# shell nem versionada: é lida do teclado, sem eco.
#
#   ./tools/liga-alibaba.sh              liga
#   ./tools/liga-alibaba.sh --voltar     volta para o Gemini
set -euo pipefail

KVM4_KEY=~/.ssh/certeiro_vps_ed25519
KVM4=wander@31.220.50.51
AZURE_KEY=/home/wander/.ssh/paraibao_migration_ed25519
AZURE=azureuser@191.235.110.51
ENV=/srv/paraibao-engine/.env
MODELO=${ALIBABA_MODELO:-qwen3.8-flash}

# Roda o script recebido por stdin dentro da VM, sem passar por aspas de ssh.
na_vm() { ssh -i "$KVM4_KEY" -o BatchMode=yes "$KVM4" \
  "ssh -i $AZURE_KEY -o BatchMode=yes $AZURE 'sudo bash -s'"; }

if [[ "${1:-}" == "--voltar" ]]; then
  na_vm <<VOLTA
set -euo pipefail
sed -i 's/^IA_PROVEDOR=.*/IA_PROVEDOR=gemini/' $ENV
cd /srv/paraibao-engine && docker compose up -d worker >/dev/null
sleep 5 && docker exec paraibao-engine-worker-1 printenv IA_PROVEDOR
VOLTA
  exit 0
fi

read -rsp "chave do Model Studio (sk-ws-...): " CHAVE; echo
[[ -n "$CHAVE" ]] || { echo "chave vazia, nada feito"; exit 1; }
read -rp "base URL [https://ws-1pvp9wq6k8pv0n4t.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1]: " BASE
BASE=${BASE:-https://ws-1pvp9wq6k8pv0n4t.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1}

na_vm <<LIGA
set -euo pipefail
cp $ENV $ENV.bak-\$(date +%Y%m%d-%H%M%S)
sed -i '/^ALIBABA_/d' $ENV
sed -i 's|^IA_PROVEDOR=.*|IA_PROVEDOR=alibaba|' $ENV
# heredoc com delimitador entre aspas: a chave já foi substituída na passagem
# local, e assim um \$ dentro dela não é expandido de novo do lado da VM.
cat >> $ENV <<'VARS'
ALIBABA_API_KEY=$CHAVE
ALIBABA_BASE_URL=$BASE
ALIBABA_MODELO=$MODELO
ALIBABA_PRECO_ENTRADA=0
ALIBABA_PRECO_SAIDA=0
VARS
cd /srv/paraibao-engine && docker compose up -d worker >/dev/null
sleep 6
echo "--- ambiente do worker"
docker exec paraibao-engine-worker-1 printenv IA_PROVEDOR ALIBABA_MODELO
echo "--- tamanho da chave que chegou (nao o valor)"
docker exec paraibao-engine-worker-1 sh -c 'echo \${#ALIBABA_API_KEY} caracteres'
LIGA

echo
echo "esperando a primeira rodada do worker..."
sleep 60
ssh -i "$KVM4_KEY" -o BatchMode=yes "$KVM4" \
  "ssh -i $AZURE_KEY -o BatchMode=yes $AZURE 'sudo docker logs --since 3m paraibao-engine-worker-1 2>&1 | grep -iE \"redator|reescrita|alibaba|parado\" | tail -8'"
