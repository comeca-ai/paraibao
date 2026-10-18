"""Laço de coleta.

Ele é burro de propósito: não sabe extrair nada. Só pergunta a cada fonte qual
adaptador ela usa (`Source.kind`), pede os candidatos, completa o que falta e
grava o que é novo.

Toda a inteligência de extração mora em app/fontes/. Trocar uma fonte de RSS
para API é um UPDATE numa coluna, sem deploy.
"""

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from . import fontes
from .fontes import municipios as fontes_municipios
from .config import MAX_ITEMS_PER_SOURCE, USER_AGENT
from .models import Article, ArticleMunicipality, CrawlRun, Source

log = logging.getLogger("paraibao.collector")

# User-Agent de navegador não é frescura: o Portal Correio devolve 406 e o
# MaisPB não responde para user-agent de biblioteca. Isso é indistinguível de
# "o site bloqueia", e foi o que fez uma auditoria anterior reprovar uma fonte
# que estava boa.
CABECALHOS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "application/json, text/html;q=0.9, application/xml;q=0.8, */*;q=0.7",
}


def _slug(titulo: str, artigo_id_hint: str) -> str:
    """Slug legível e estável. O sufixo evita colisão entre títulos iguais."""
    base = unicodedata.normalize("NFKD", titulo).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()[:150].strip("-")
    return f"{base}-{artigo_id_hint[:8]}" if base else artigo_id_hint[:16]


def _fingerprint(fonte_slug: str, url: str) -> str:
    return hashlib.sha256(f"{fonte_slug}|{url}".encode()).hexdigest()


# Stopword que não ajuda a distinguir matéria. Tirar antes de comparar faz
# "Cinco trechos do litoral da Paraíba impróprios" casar com "Cinco trechos da
# Paraíba impróprios", que é a mesma matéria republicada com outra URL.
STOP = {
    "a","o","as","os","um","uma","de","do","da","dos","das","em","no","na","nos",
    "nas","por","para","com","e","ou","que","ao","aos","à","às","se","sua","seu",
}


def chave_titulo(titulo: str) -> str:
    """Assinatura do título: sem acento, sem stopword, ordenada.

    Ordenar as palavras é o truque: pega republicação com a ordem trocada, que
    o hash direto do texto deixaria passar.

    O que ESTE nível não pega, de propósito: manchete reescrita ("...confira
    quais" vs "...veja quais"). Medido em 2026-08-02: 2 casos em 2.644 artigos,
    0,08%. Pegar isso exigiria similaridade por trigrama, e aí o risco vira
    unir matérias DIFERENTES sobre a mesma cidade ("São Domingos do Cariri
    gasta com buffet" e "...com pneus" têm alta similaridade e são notícias
    distintas). Duplicata visível é menos danosa que matéria sumida.
    """
    base = unicodedata.normalize("NFKD", titulo).encode("ascii", "ignore").decode().lower()
    palavras = [p for p in re.findall(r"[a-z0-9]+", base) if p not in STOP and len(p) > 2]
    return hashlib.sha256(" ".join(sorted(palavras)).encode()).hexdigest()[:32]


def _titulos_conhecidos(session, chaves: list[str]) -> set[str]:
    if not chaves:
        return set()
    return set(
        session.scalars(
            select(Article.title_key).where(Article.title_key.in_(chaves))
        ).all()
    )


def _urls_conhecidas(session, urls: list[str]) -> set[str]:
    if not urls:
        return set()
    return set(
        session.scalars(
            select(Article.canonical_url).where(Article.canonical_url.in_(urls))
        ).all()
    )


def _gravar(session, source: Source, c) -> Article:
    fp = _fingerprint(source.slug or str(source.id), c.url)
    artigo = Article(
        title_key=chave_titulo(c.titulo),
        source_id=source.id,
        slug=_slug(c.titulo, fp),
        title=c.titulo[:700],
        canonical_url=c.url[:1000],
        summary=(c.resumo or "")[:4000],
        body=c.corpo,
        author=(c.autor or "")[:250] or None,
        image_url=(c.imagem or "")[:1000] or None,
        published_at=c.publicado_em,
        fingerprint=fp,
        status="published",
        layer="aggregate",
    )
    session.add(artigo)
    return artigo


def coletar_por_municipio(session, source: Source, client: httpx.Client, run: CrawlRun) -> int:
    """Busca ativa: pede as matérias de cada município em vez de esperar o feed.

    Matéria que vem por aqui já tem o município conhecido, então o vínculo entra
    com confiança 1.0 e método `tag`, sem passar pelo classificador de texto.
    """
    if source.kind != "wp_rest":
        return 0

    fontes_municipios.resolver_tags(session, source, client)
    pares = fontes_municipios.coletar_municipios(session, source, client)
    if not pares:
        return 0

    validos = [(c, mid) for c, mid in pares if c.valido()]
    conhecidas = _urls_conhecidas(session, [c.url for c, _ in validos])
    chaves = {c.url: chave_titulo(c.titulo) for c, _ in validos}
    vistos_titulo = _titulos_conhecidos(session, list(chaves.values()))

    novas = 0
    for c, municipality_id in validos:
        if c.url in conhecidas or chaves[c.url] in vistos_titulo:
            continue
        conhecidas.add(c.url)
        vistos_titulo.add(chaves[c.url])
        artigo = _gravar(session, source, c)
        session.flush()
        session.add(
            ArticleMunicipality(
                article_id=artigo.id,
                municipality_id=municipality_id,
                is_primary=True,
                detection_method="tag",
                confidence=1.0,
            )
        )
        novas += 1

    run.items_new += novas
    session.commit()
    if novas:
        log.info("%s: %s matérias novas por município", source.slug, novas)
    return novas


def coletar_fonte(session, source: Source, client: httpx.Client, run: CrawlRun) -> int:
    candidatos = fontes.coletar(source, client, limite=MAX_ITEMS_PER_SOURCE)
    run.items_seen += len(candidatos)

    validos = [c for c in candidatos if c.valido()]
    conhecidas = _urls_conhecidas(session, [c.url for c in validos])
    # Nível 2: mesma matéria republicada com outra URL. Acontece no ClickPB.
    chaves = {c.url: chave_titulo(c.titulo) for c in validos}
    vistos_titulo = _titulos_conhecidos(session, list(chaves.values()))

    # Abre a página da matéria só para o que é novo e está incompleto.
    completadas = fontes.completar(validos, client, conhecidas)
    if completadas:
        log.info("%s: %s imagens recuperadas na página", source.slug, completadas)

    novas = 0
    mais_recente = source.cursor_after
    for c in validos:
        if c.publicado_em and (mais_recente is None or c.publicado_em > mais_recente):
            mais_recente = c.publicado_em
        if c.url in conhecidas or chaves[c.url] in vistos_titulo:
            continue
        conhecidas.add(c.url)
        vistos_titulo.add(chaves[c.url])
        _gravar(session, source, c)
        novas += 1

    # O cursor só anda quando a rodada dá certo. Se andasse mesmo com falha,
    # uma coleta ruim faria o sistema pular matéria para sempre.
    source.cursor_after = mais_recente
    source.last_success_at = datetime.now(timezone.utc)
    source.last_error = None
    run.items_new += novas
    run.sources_ok += 1
    return novas


def crawl_all(session) -> CrawlRun:
    run = CrawlRun()
    session.add(run)
    session.commit()

    sources = session.scalars(select(Source).where(Source.enabled.is_(True))).all()
    try:
        with httpx.Client(headers=CABECALHOS, timeout=20, follow_redirects=True) as client:
            for source in sources:
                try:
                    novas = coletar_fonte(session, source, client, run)
                    session.commit()
                    log.info("%s [%s]: %s novas", source.slug, source.kind, novas)
                    # Busca ativa por município, só onde a fonte tem API.
                    coletar_por_municipio(session, source, client, run)
                except Exception as exc:
                    # Uma fonte fora do ar não derruba a rodada das outras.
                    session.rollback()
                    source = session.get(Source, source.id)
                    source.last_error = str(exc)[:2000]
                    run = session.get(CrawlRun, run.id)
                    run.sources_failed += 1
                    session.commit()
                    log.warning("%s falhou: %s", source.slug, str(exc)[:200])

        run = session.get(CrawlRun, run.id)
        run.status = "success" if run.sources_failed == 0 else "partial"
    except Exception as exc:
        session.rollback()
        run = session.get(CrawlRun, run.id)
        run.status = "failed"
        run.error = str(exc)[:4000]
    run.finished_at = datetime.now(timezone.utc)
    session.commit()
    return run
