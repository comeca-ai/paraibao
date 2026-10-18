"""Migração idempotente do schema e seed dos municípios.

Roda no startup da API e do worker. Tudo aqui precisa ser seguro de executar
N vezes em cima do banco de produção que já tem artigos coletados: só
ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS e upsert no seed.
Nada de DROP, nada de ALTER destrutivo.
"""

import logging
import re
import time
import unicodedata

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from .db import Base, SessionLocal, engine
from .jornal import municipios as MUNICIPIOS_DO_PERFIL

log = logging.getLogger("paraibao.migrate")


# Colunas novas em tabelas que já existiam em produção.
ADD_COLUMNS = [
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS layer VARCHAR(20) NOT NULL DEFAULT 'aggregate'",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS slug VARCHAR(240)",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS category VARCHAR(40) NOT NULL DEFAULT 'geral'",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS category_confidence DOUBLE PRECISION",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS body TEXT",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS title_key VARCHAR(32)",
    # Camada própria: o texto reescrito mora separado do original, que fica
    # em `body` como prova. Ver redator.py.
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS own_title VARCHAR(700)",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS own_summary TEXT",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS own_body TEXT",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS rewritten_at TIMESTAMPTZ",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS rewrite_model VARCHAR(60)",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS rewrite_error TEXT",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS rewrite_tokens_in INTEGER",
    "ALTER TABLE articles ADD COLUMN IF NOT EXISTS rewrite_tokens_out INTEGER",
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS content_policy VARCHAR(20) "
    "NOT NULL DEFAULT 'link_out'",
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS license_basis TEXT",
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'rss'",
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS trust_level INTEGER NOT NULL DEFAULT 50",
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS default_municipality_id INTEGER "
    "REFERENCES municipalities(id)",
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS slug VARCHAR(120)",
    "ALTER TABLE sources ADD COLUMN IF NOT EXISTS cursor_after TIMESTAMPTZ",
]

# Busca textual em português direto no Postgres. Coluna gerada, sempre em
# sincronia, sem trigger e sem serviço de busca separado.
SEARCH = [
    """
    ALTER TABLE articles ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('portuguese', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('portuguese', coalesce(summary, '')), 'B')
    ) STORED
    """,
    "CREATE INDEX IF NOT EXISTS ix_articles_search ON articles USING GIN (search_vector)",
]

INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_articles_slug ON articles (slug)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_sources_slug ON sources (slug)",
    "CREATE INDEX IF NOT EXISTS ix_articles_title_key ON articles (title_key)",
    "CREATE INDEX IF NOT EXISTS ix_articles_layer_pub ON articles "
    "(layer, coalesce(published_at, collected_at) DESC)",
    # A home filtra por status e ordena por recência.
    # O índice composto (status, data) NÃO é usado: a consulta faz
    # `status IN (dois valores)`, e o planejador prefere Seq Scan a fazer duas
    # buscas e merge. Medido: Seq Scan em 2.645 linhas mesmo com o índice lá.
    # Índice PARCIAL resolve, porque casa com o predicado exato e já vem na
    # ordem certa: o Postgres só percorre e para no LIMIT.
    "CREATE INDEX IF NOT EXISTS ix_articles_pub_desc ON articles "
    "(coalesce(published_at, collected_at) DESC) "
    "WHERE status IN ('published','collected')",
    # Mesma lógica para as páginas de editoria.
    "CREATE INDEX IF NOT EXISTS ix_articles_cat_pub_desc ON articles "
    "(category, coalesce(published_at, collected_at) DESC) "
    "WHERE status IN ('published','collected')",
    # Índices que a auditoria mostrou com zero uso ocupam espaço e custam em
    # cada INSERT. `articles_fingerprint_key` duplica `ix_articles_fingerprint`.
    "DROP INDEX IF EXISTS ix_articles_status_pub",
    "DROP INDEX IF EXISTS ix_articles_layer_pub",
    "DROP INDEX IF EXISTS ix_articles_status",
    "CREATE INDEX IF NOT EXISTS ix_articles_cat_pub ON articles "
    "(category, coalesce(published_at, collected_at) DESC)",
    # Fila da coleta por município: quem foi visitado há mais tempo vai primeiro.
    "CREATE INDEX IF NOT EXISTS ix_muntags_fila ON municipality_tags "
    "(source_id, last_run_at NULLS FIRST) WHERE tag_id IS NOT NULL",
    # A fila do redator pergunta "quem ainda não passou pela IA", e o teto
    # diário pergunta "quantas passaram nas últimas 24h".
    "CREATE INDEX IF NOT EXISTS ix_articles_reescrita ON articles "
    "(rewritten_at DESC NULLS FIRST)",
    # O publicador pergunta duas coisas a cada rodada: quantos posts saíram
    # hoje nessa rede e quando saiu o último. As duas leem por (rede, data).
    "CREATE INDEX IF NOT EXISTS ix_social_rede_pub ON social_posts "
    "(network, published_at DESC) WHERE status = 'published'",
]


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def seed_municipalities():
    """Insere os 223 municípios. Atualiza aliases e flag de ambiguidade se mudarem."""
    from .models import Municipality

    with SessionLocal() as session:
        existing = {m.ibge_code: m for m in session.scalars(select(Municipality)).all()}
        created = updated = 0
        for item in MUNICIPIOS_DO_PERFIL():
            current = existing.get(item["ibge"])
            if current is None:
                session.add(
                    Municipality(
                        ibge_code=item["ibge"],
                        name=item["nome"],
                        slug=item["slug"],
                        micro_region=item["micro"],
                        meso_region=item["meso"],
                        immediate_region=item["imediata"],
                        aliases=item["aliases"],
                        ambiguous=item["ambiguo"],
                    )
                )
                created += 1
            elif current.aliases != item["aliases"] or current.ambiguous != item["ambiguo"]:
                current.aliases = item["aliases"]
                current.ambiguous = item["ambiguo"]
                updated += 1
        session.commit()
        if created or updated:
            log.info("municípios: %s criados, %s atualizados", created, updated)


def backfill_source_slugs():
    from .models import Source

    with SessionLocal() as session:
        for source in session.scalars(select(Source).where(Source.slug.is_(None))).all():
            source.slug = slugify(source.name)
        session.commit()


def backfill_article_slugs():
    """Gera slug para as matérias coletadas antes da página interna existir."""
    from .models import Article

    with SessionLocal() as session:
        pendentes = session.scalars(
            select(Article).where(Article.slug.is_(None)).limit(5000)
        ).all()
        for artigo in pendentes:
            base = slugify(artigo.title)[:150].strip("-")
            artigo.slug = f"{base}-{artigo.fingerprint[:8]}" if base else artigo.fingerprint[:16]
        if pendentes:
            session.commit()
            log.info("slug gerado para %s matérias", len(pendentes))


def seed_dono():
    """Cria a primeira conta, uma única vez, e nunca mexe depois.

    Sem isto, ligar o login novo tranca o dono do lado de fora: a tabela nasce
    vazia e não existe tela de cadastro aberta, de propósito, porque painel de
    jornal com auto-cadastro é convite.

    A senha vem de `DONO_SENHA` e, se não houver, da `PANEL_PASSWORD` que já
    estava no `.env`. Assim a virada não pede nada de ninguém: entra-se com a
    senha de sempre e a tela obriga a trocar na hora, que é exatamente o que
    precisa acontecer com uma senha que já circulou.
    """
    import os

    from . import contas
    from .jornal import JORNAL
    from .models import Usuario

    with SessionLocal() as session:
        if contas.total(session):
            return
        email = (os.getenv("DONO_EMAIL", "") or f"admin@{JORNAL.dominio}").strip().lower()
        senha = os.getenv("DONO_SENHA", "") or os.getenv("PANEL_PASSWORD", "")
        if len(senha) < contas.MIN_SENHA:
            # Não inventa senha fraca nem cria conta sem senha utilizável: é
            # melhor o painel ficar inacessível e gritar no log do que nascer
            # com uma porta que qualquer um empurra.
            log.error(
                "nenhuma conta existe e não há senha válida em DONO_SENHA nem "
                "PANEL_PASSWORD (mínimo %s caracteres). O painel fica sem acesso "
                "até isso ser definido no .env.",
                contas.MIN_SENHA,
            )
            return
        usuario = Usuario(
            nome=os.getenv("DONO_NOME", "") or "Dono",
            email=email,
            senha_hash=contas.hash_senha(senha),
            papel=contas.DONO,
            precisa_trocar_senha=True,
        )
        session.add(usuario)
        session.commit()
        log.warning(
            "primeira conta criada: %s (papel dono). A troca de senha é "
            "obrigatória no primeiro acesso.",
            email,
        )


# Número arbitrário, mas fixo: é só um nome para o cadeado. Trocá-lo entre
# versões faria dois processos acharem que estão em filas diferentes.
TRAVA_MIGRACAO = 8172634


def _aplicar(statement: str, tentativas: int = 5) -> None:
    """Um DDL, uma transação, com prazo para conseguir a trava.

    Aqui está a correção de um deadlock real, e o motivo é contraintuitivo:
    o problema NÃO era duas migrações ao mesmo tempo, era a migração contra o
    trabalho normal do coletor. Rodando todos os `ALTER TABLE` numa transação
    só, a migração ficava segurando a trava exclusiva de `articles` enquanto
    pedia a de `sources`; o coletor, do outro lado, segurava `sources` e pedia
    `articles`. Ciclo fechado, e o Postgres matou a API.

    Uma transação por comando desfaz o ciclo, porque nenhuma delas segura duas
    tabelas ao mesmo tempo. O `lock_timeout` é a segunda rede: em vez de
    esperar para sempre por uma trava, desiste e tenta de novo daqui a pouco,
    que é o comportamento certo quando o outro lado é uma coleta que termina
    em segundos.

    O deploy reproduzia isso quase sempre porque o worker coleta assim que
    sobe, exatamente enquanto a API está migrando.
    """
    for tentativa in range(1, tentativas + 1):
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                conn.execute(text(statement))
            return
        except OperationalError:
            if tentativa == tentativas:
                raise
            resumo = " ".join(statement.split())[:90]
            log.warning(
                "DDL não conseguiu a trava (%s/%s), tentando de novo: %s",
                tentativa, tentativas, resumo,
            )
            time.sleep(1.5 * tentativa)


def run():
    """Migração serializada entre processos.

    A API e o worker sobem juntos e os DOIS chamam esta função. O cadeado
    consultivo evita que rodem a mesma coisa ao mesmo tempo; o que evita o
    deadlock contra o coletor é o `_aplicar`, acima.
    """
    with engine.connect() as trava:
        trava.execute(text("SELECT pg_advisory_lock(:k)"), {"k": TRAVA_MIGRACAO})
        trava.commit()
        try:
            Base.metadata.create_all(engine)
            for statement in ADD_COLUMNS + SEARCH + INDEXES:
                _aplicar(statement)
            seed_municipalities()
            backfill_source_slugs()
            backfill_article_slugs()
            seed_dono()
        finally:
            trava.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": TRAVA_MIGRACAO})
            trava.commit()
    log.info("migração concluída")
