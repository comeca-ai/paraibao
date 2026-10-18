"""Adaptador de RSS. Degraus 3 e 4 da escada.

Usado quando a fonte não expõe API. Feed é estável, é publicado de propósito
pelo veículo e não quebra quando o site muda de tema, mas costuma vir
incompleto: falta imagem, às vezes falta corpo.

O que falta é completado pela página da matéria, uma requisição por matéria
nova. Ver fontes/pagina.py.

Medido no Jornal da Paraíba em 2026-08-01:
  50 itens, content:encoded em 100% (corpo completo), category em 100%,
  media:content em apenas 8%, sem autor.
"""

import logging

import feedparser

from .base import (
    ArtigoCandidato,
    imagem_valida,
    limpar_corpo,
    limpar_texto,
    normalizar_url,
    parse_data,
    resumo_de,
)

log = logging.getLogger("paraibao.fontes.rss")


def _imagem(entrada) -> str | None:
    for chave in ("media_content", "media_thumbnail"):
        midias = entrada.get(chave) or []
        if midias and midias[0].get("url"):
            url = imagem_valida(midias[0]["url"])
            if url:
                return url
    for anexo in entrada.get("enclosures") or []:
        if str(anexo.get("type", "")).startswith("image/"):
            url = imagem_valida(anexo.get("href") or anexo.get("url"))
            if url:
                return url
    return None


def _corpo(entrada) -> str | None:
    """content:encoded traz o texto completo; summary costuma vir contaminado."""
    conteudos = entrada.get("content") or []
    if conteudos and conteudos[0].get("value"):
        return limpar_corpo(conteudos[0]["value"])
    return limpar_corpo(entrada.get("summary"))


def _secao(entrada) -> str | None:
    marcas = entrada.get("tags") or []
    if marcas and marcas[0].get("term"):
        return limpar_texto(marcas[0]["term"]) or None
    return None


def coletar(source, client, limite: int = 60) -> list[ArtigoCandidato]:
    resposta = client.get(source.feed_url)
    resposta.raise_for_status()
    feed = feedparser.parse(resposta.content)
    if feed.bozo and not feed.entries:
        raise ValueError(str(feed.bozo_exception))

    artigos = []
    for entrada in feed.entries[:limite]:
        link = entrada.get("link")
        titulo = limpar_texto(entrada.get("title"))
        if not link or not titulo:
            continue
        corpo = _corpo(entrada)
        artigos.append(
            ArtigoCandidato(
                fonte_slug=source.slug,
                external_id=limpar_texto(entrada.get("id")) or None,
                titulo=titulo,
                url=normalizar_url(link),
                imagem=_imagem(entrada),
                corpo=corpo,
                # O resumo sai do corpo de propósito: o <description> de vários
                # portais começa com legenda de foto e nome do veículo.
                resumo=resumo_de(corpo, entrada.get("summary")),
                autor=limpar_texto(entrada.get("author")) or None,
                publicado_em=parse_data(entrada.get("published") or entrada.get("updated")),
                secao=_secao(entrada),
            )
        )
    return artigos
