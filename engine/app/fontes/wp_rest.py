"""Adaptador da API REST do WordPress. Degrau 1 da escada.

É o melhor caso possível: uma requisição devolve título, link, data, corpo,
autor, seção e imagem, tudo estruturado, sem seletor de CSS para quebrar
quando o portal trocar de tema.

Confirmado aberto em 2026-08-01: ClickPB, Paraíba Online e Portal Correio.
O ClickPB expõe 384.910 posts pelo header X-WP-Total, contra os 20 do RSS.

Dois parâmetros carregam esse adaptador:
  _embed=wp:featuredmedia  traz a imagem junto, em vez de 1 request por matéria
  after=ISO8601            coleta incremental: só o que saiu depois do cursor
"""

import logging

from .base import (
    ArtigoCandidato,
    limpar_corpo,
    limpar_texto,
    normalizar_url,
    imagem_valida,
    parse_data,
    resumo_de,
)

log = logging.getLogger("paraibao.fontes.wp")

CAMINHO = "/wp-json/wp/v2/posts"


def _imagem(post: dict) -> str | None:
    midias = (post.get("_embedded") or {}).get("wp:featuredmedia") or []
    if not midias or not isinstance(midias[0], dict):
        return None
    return imagem_valida(midias[0].get("source_url"))


def _autor(post: dict) -> str | None:
    """Prefere coauthors (plugin comum em portal) e cai no autor embutido."""
    # O ClickPB devolve coauthors como lista de IDs numéricos, não de objetos.
    # ID não é nome: só aproveita quando vem estruturado ou como texto.
    coautores = post.get("coauthors")
    if isinstance(coautores, list) and coautores:
        nomes = []
        for c in coautores:
            if isinstance(c, dict):
                nomes.append(limpar_texto(c.get("display_name") or c.get("name")))
            elif isinstance(c, str):
                nomes.append(limpar_texto(c))
        nomes = [n for n in nomes if n]
        if nomes:
            return "; ".join(nomes[:3])
    autores = (post.get("_embedded") or {}).get("author") or []
    if autores and isinstance(autores[0], dict):
        return limpar_texto(autores[0].get("name")) or None
    return None


def coletar(source, client, limite: int = 60) -> list[ArtigoCandidato]:
    params = {
        "per_page": min(limite, 100),
        "_embed": "wp:featuredmedia,author",
        "orderby": "date",
        "order": "desc",
    }
    # Coleta incremental. Sem isso, toda rodada rebaixa as mesmas matérias e
    # obriga o dedup a trabalhar à toa.
    if source.cursor_after:
        params["after"] = source.cursor_after.replace(tzinfo=None).isoformat()

    resposta = client.get(source.site_url.rstrip("/") + CAMINHO, params=params)
    resposta.raise_for_status()
    dados = resposta.json()
    if not isinstance(dados, list):
        raise ValueError(f"resposta inesperada da API: {type(dados).__name__}")

    artigos = []
    for post in dados:
        link = post.get("link")
        if not link:
            continue
        corpo = limpar_corpo((post.get("content") or {}).get("rendered"))
        # date_gmt é a data em UTC. Usar `date` traria 3h de erro.
        publicado = parse_data(
            (post.get("date_gmt") + "Z") if post.get("date_gmt") else post.get("date")
        )
        artigos.append(
            ArtigoCandidato(
                fonte_slug=source.slug,
                external_id=str(post.get("id") or ""),
                titulo=limpar_texto((post.get("title") or {}).get("rendered")),
                url=normalizar_url(link),
                imagem=_imagem(post),
                corpo=corpo,
                resumo=resumo_de(corpo, (post.get("excerpt") or {}).get("rendered")),
                autor=_autor(post),
                publicado_em=publicado,
                secao=None,
            )
        )
    return artigos
