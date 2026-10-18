# Paraibão

Reconstrução do código que roda na VM `paraibao`. Esta árvore foi montada a partir de `/srv` e dos arquivos de operação do servidor, em 18/10/2026.

## Estrutura

| Pasta | O que é | Origem no servidor |
|---|---|---|
| `engine/` | Motor: API FastAPI, worker, templates, perfis por praça, Dockerfile e compose | `/srv/paraibao-engine` |
| `gateway/` | nginx de entrada, vhosts de `paraibao.com.br` e `painel.paraibao.com.br` | `/srv/paraibao-gateway` |
| `site/` | Site estático (HTML, CSS, imagens, fontes) | `/srv/paraibao-site/site` |
| `site-container/` | Compose e `default.conf` que servem o `site/` | `/srv/paraibao-site` |
| `ops/` | Scripts e cron de backup e de renovação de certificado | `/usr/local/sbin`, `/etc/cron.d` |
| `docs/` | Plano de migração e de franquia | `~/migracao-paraibao/plano.md` |

## O que não entra no repositório

- `.env`, `.env.antes-migracao`: contêm credenciais. Use `engine/.env.example` como modelo.
- Certificados e chaves (`letsencrypt/`, `*.pem`) e o token da Cloudflare.
- Dumps do banco (`/srv/backups`).
- Arquivos `.orig`, `.pre-domain-fix`, backups de `conf.d` e os arquivos com `?` no nome dentro de `site/`.
- Caches do Python e a pasta `engine/tools/_previa/`, que tem um link simbólico para um Mac.

## Pontos a revisar antes de publicar

- `engine/app/config.py` tem valores padrão inseguros, como `PANEL_PASSWORD="change-me"` e `IMAGE_SECRET="troque-isto"`, usados se a variável não estiver definida.
- `engine/tools/liga-alibaba.sh` grava credenciais no `.env` da VM. Não contém a chave, mas revise antes de publicar.

## Subir localmente

Os três projetos têm compose próprio. A rede `edge_certeiro` precisa existir antes:

```bash
docker network create edge_certeiro
cp engine/.env.example engine/.env   # preencha os valores
cd engine && docker compose up -d
```
