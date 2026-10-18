#!/usr/bin/env bash
# Deploy do Paraibão na VM Azure.
#
# A Azure não é alcançável direto: a chave dela mora na KVM4, então a KVM4 é
# usada como salto e como área de estágio do rsync.
#
#   local  --rsync-->  KVM4 (/tmp/paraibao-deploy)  --rsync-->  Azure (/srv/paraibao-engine)
#
# Uso:
#   ./deploy.sh          build + restart de api e worker
#   ./deploy.sh --check  só valida o que está no ar, não mexe em nada
set -euo pipefail

KVM4_KEY=~/.ssh/certeiro_vps_ed25519
KVM4=wander@31.220.50.51
AZURE_KEY=/home/wander/.ssh/paraibao_migration_ed25519
AZURE=azureuser@191.235.110.51
DESTINO=/srv/paraibao-engine
ESTAGIO=/tmp/paraibao-deploy

kvm4() { ssh -i "$KVM4_KEY" -o BatchMode=yes "$KVM4" "$@"; }
azure() { kvm4 "ssh -i $AZURE_KEY -o BatchMode=yes $AZURE \"$1\""; }

verificar() {
  echo "--- containers"
  # O comando atravessa dois níveis de ssh, e cada um come um par de aspas.
  # Por isso o formato não tem espaço, aspas nem pipe: qualquer um dos três
  # chega quebrado do outro lado.
  azure 'sudo docker ps --format {{.Names}}={{.Status}}'
  echo "--- health interno"
  azure 'curl -sS --max-time 10 http://127.0.0.1:8787/health'
  echo
  echo "--- externo"
  curl -sS -o /dev/null -w 'home    %{http_code}  %{time_total}s\n' --max-time 20 https://paraibao.com.br/
  curl -sS -o /dev/null -w 'municip %{http_code}\n' --max-time 20 https://paraibao.com.br/municipio/campina-grande
  curl -sS -o /dev/null -w 'sitemap %{http_code}\n' --max-time 20 https://paraibao.com.br/sitemap.xml
}

if [[ "${1:-}" == "--check" ]]; then
  verificar
  exit 0
fi

echo "### 1. enviando código para a KVM4 (estágio)"
# O rsync do macOS é o openrsync e não aceita --chmod, então a permissão é
# normalizada do lado da KVM4. Sem isso os arquivos chegam 700 (herança do
# pós-formatação do Mac) e quebram leitura dentro do container.
kvm4 "mkdir -p $ESTAGIO"
rsync -az --delete \
  --exclude '__pycache__' --exclude '.git' --exclude '*.pyc' --exclude 'docs' \
  --exclude '.venv*' --exclude '.DS_Store' \
  -e "ssh -i $KVM4_KEY -o BatchMode=yes" \
  ./ "$KVM4:$ESTAGIO/"
kvm4 "chmod -R a+rX $ESTAGIO"

echo "### 2. KVM4 -> Azure"
kvm4 "rsync -az --delete --chmod=D755,F644 \
  --exclude '.env' \
  -e 'ssh -i $AZURE_KEY -o BatchMode=yes' \
  $ESTAGIO/ $AZURE:/tmp/paraibao-novo/"
azure "sudo rsync -a --delete --exclude '.env' /tmp/paraibao-novo/ $DESTINO/"

echo "### 3. build e restart (db e redis não são tocados)"
azure "cd $DESTINO && sudo docker compose up -d --build api worker"

echo "### 4. esperando o health responder"
for i in $(seq 1 30); do
  if azure 'curl -fsS --max-time 5 http://127.0.0.1:8787/health' >/dev/null 2>&1; then
    echo "    ok em ${i}0s"
    break
  fi
  sleep 3
done

echo "### 5. verificação"
verificar
