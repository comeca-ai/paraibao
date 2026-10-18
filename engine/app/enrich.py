"""Enriquecimento: território e editoria.

Roda depois de cada coleta, em cima do que ainda não foi processado. Hoje é
100% determinístico e não chama modelo nenhum. Quando o LLM entrar, ele vai
tratar só os casos em que a confiança determinística ficou abaixo do limiar,
que pelo teste nos artigos reais é a minoria.

`category_confidence IS NULL` é o marcador de "nunca enriquecido". Depois de
processar, o campo sempre recebe valor (mesmo 0.0), então o backfill converge
e não reprocessa a mesma matéria para sempre.
"""

import logging

from sqlalchemy import select

from .editoria import classificar
from .models import Article, Source
from .territory import aplicar as aplicar_territorio

log = logging.getLogger("paraibao.enrich")

LOTE = 200


def enriquecer(session, article: Article, source: Source | None = None) -> None:
    categoria, confianca = classificar(article.title, article.summary or "")
    article.category = categoria
    article.category_confidence = confianca
    aplicar_territorio(session, article, source)


def enriquecer_pendentes(session, limite: int = LOTE) -> int:
    """Processa o que ainda não passou pelo enriquecimento. Idempotente."""
    pendentes = session.execute(
        select(Article, Source)
        .join(Source, Source.id == Article.source_id)
        .where(Article.category_confidence.is_(None))
        .limit(limite)
    ).all()
    for article, source in pendentes:
        enriquecer(session, article, source)
    if pendentes:
        session.commit()
        log.info("enriquecidas %s matérias", len(pendentes))
    return len(pendentes)
