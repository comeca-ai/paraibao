"""Enriquecimento pela página da matéria.

Só é chamado para o que o degrau principal não trouxe. Uma requisição por
matéria nova, nunca por matéria já conhecida.

Lê nesta ordem, do mais estruturado para o menos:
  1. JSON-LD @type NewsArticle   (imagem, data com timezone, seção, autor)
  2. meta property=article:*     (data, seção)
  3. meta property=og:image      (imagem)

Confirmado no Jornal da Paraíba em 2026-08-01: a página traz JSON-LD
NewsArticle completo, o que recupera os 92% de imagem que faltam no feed.
"""

import json
import logging
import re

from .base import ArtigoCandidato, imagem_valida, limpar_texto, parse_data

log = logging.getLogger("paraibao.fontes.pagina")

LD_JSON = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S
)
META = re.compile(
    r'<meta[^>]+(?:property|name)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\']',
    re.I,
)
META_INVERTIDA = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']([^"\']+)["\']',
    re.I,
)


def _achatar(dado):
    """JSON-LD vem como objeto, lista ou @graph. Devolve tudo num nível só."""
    if isinstance(dado, list):
        for item in dado:
            yield from _achatar(item)
    elif isinstance(dado, dict):
        if "@graph" in dado:
            yield from _achatar(dado["@graph"])
        else:
            yield dado


def _tipo_de(bloco) -> set[str]:
    tipo = bloco.get("@type", "")
    return {tipo} if isinstance(tipo, str) else set(tipo or [])


NOTICIA = {"NewsArticle", "Article", "ReportageNewsArticle", "BlogPosting"}


def ler_jsonld(html: str) -> dict:
    for bruto in LD_JSON.findall(html):
        try:
            dado = json.loads(bruto.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for bloco in _achatar(dado):
            if _tipo_de(bloco) & NOTICIA:
                return bloco
    return {}


def ler_metas(html: str) -> dict[str, str]:
    metas = {}
    for chave, valor in META.findall(html):
        metas.setdefault(chave.lower(), valor)
    for valor, chave in META_INVERTIDA.findall(html):
        metas.setdefault(chave.lower(), valor)
    return metas


def _imagem_do_jsonld(bloco: dict) -> str | None:
    img = bloco.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl")
    return imagem_valida(img if isinstance(img, str) else None)


def _autor_do_jsonld(bloco: dict, publisher_nome: str | None) -> str | None:
    autor = bloco.get("author")
    if isinstance(autor, list):
        autor = autor[0] if autor else None
    nome = autor.get("name") if isinstance(autor, dict) else autor
    if not isinstance(nome, str):
        return None
    nome = limpar_texto(nome)
    # Portal que assina tudo como a própria redação não tem autor de verdade.
    # Guardar "Jornal da Paraíba" no campo autor é ruído, não informação.
    if publisher_nome and nome.lower() in publisher_nome.lower():
        return None
    return nome or None


def enriquecer(artigo: ArtigoCandidato, html: str) -> ArtigoCandidato:
    """Preenche só o que está faltando. Nunca sobrescreve o que já veio."""
    bloco = ler_jsonld(html)
    metas = ler_metas(html)
    publisher = bloco.get("publisher") or {}
    publisher_nome = publisher.get("name") if isinstance(publisher, dict) else None

    if not artigo.imagem:
        artigo.imagem = (
            _imagem_do_jsonld(bloco)
            or imagem_valida(metas.get("og:image"))
            or imagem_valida(metas.get("twitter:image"))
        )

    if not artigo.autor:
        artigo.autor = _autor_do_jsonld(bloco, publisher_nome)

    if not artigo.publicado_em:
        artigo.publicado_em = parse_data(
            bloco.get("datePublished") or metas.get("article:published_time")
        )

    if not artigo.secao:
        secao = bloco.get("articleSection") or metas.get("article:section")
        if isinstance(secao, list):
            secao = secao[0] if secao else None
        artigo.secao = limpar_texto(secao) or None

    artigo.faltando = [
        campo for campo in ("imagem", "autor", "publicado_em") if not getattr(artigo, campo)
    ]
    return artigo
