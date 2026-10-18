# vhosts do gateway

O gateway (`paraibao-gateway`) monta `/srv/paraibao-gateway/conf.d` em
`/etc/nginx/conf.d`. Estes arquivos são a **fonte da verdade** e vivem no
repositório para não existirem só dentro do servidor.

## Subir um vhost novo

1. **DNS primeiro.** Registro A do subdomínio apontando para a VM
   (`191.235.110.51`), em modo **DNS-only** na Cloudflare (nuvem cinza).
   Com o proxy ligado, o desafio HTTP do Let's Encrypt não fecha.
2. Copiar o `.conf` para `/srv/paraibao-gateway/conf.d/`.
3. Emitir o certificado (o vhost de porta 80 já serve o desafio):

       sudo docker run --rm \
         -v /srv/paraibao-gateway/letsencrypt:/etc/letsencrypt \
         -v /srv/paraibao-gateway/certbot:/var/www/certbot \
         certbot/certbot certonly --webroot -w /var/www/certbot \
         -d <subdominio> --agree-tos -m <email> --non-interactive

4. `sudo docker exec paraibao-gateway nginx -s reload`

⚠️ O bloco 443 referencia o certificado. Se ele for copiado **antes** de o
certificado existir, o nginx não sobe e derruba o site inteiro junto. Emitir
primeiro, ativar depois.
