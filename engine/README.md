# Paraibão Engine

MVP operacional do motor de coleta editorial do Paraibão.

## Serviços

- `api`: API FastAPI e painel SPA.
- `worker`: agenda, coleta e normaliza feeds.
- `db`: PostgreSQL.
- `redis`: fila de comandos e base para expansão dos workers.

## Operação

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f worker
```

O painel usa HTTP Basic Auth e fica na porta definida por `PUBLIC_PORT`.

## Arquitetura de expansão

Os serviços já estão separados. Ao crescer, `db`, `redis`, `worker` e `api` podem
ser movidos para hosts diferentes alterando apenas as URLs no ambiente.
# Publicação automática

Cada item novo e não duplicado entra com status `published` e fica imediatamente
disponível para o portal nestas rotas públicas:

- `GET /api/public/articles`
- `GET /api/public/articles/{id}`

O conteúdo público é limitado aos metadados e ao resumo fornecido pelo feed, com
crédito e link para a matéria original.

