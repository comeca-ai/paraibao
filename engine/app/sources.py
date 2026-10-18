"""Declaração das fontes.

`kind` é o degrau da escada de extração que a fonte usa. Medido em 2026-08-01,
ver docs/EXTRACAO.md:

  ClickPB          /wp-json aberto, imagem em 100%, 384.910 posts alcançáveis
  Jornal da PB     sem /wp-json (404 em todos os caminhos). O RSS traz corpo em
                   100% e imagem em 8%; o resto vem do JSON-LD da página
  Paraíba Online   /wp-json aberto     -> pode virar wp_rest
  Portal Correio   /wp-json aberto     -> pode virar wp_rest
  MaisPB           /wp-json instável   -> manter em rss

Mudar o degrau de uma fonte é editar `kind` aqui (ou um UPDATE na tabela),
sem tocar em mais nada.
"""

SOURCES = [
    {
        "name": "ClickPB",
        "feed_url": "https://www.clickpb.com.br/feed/",
        "site_url": "https://www.clickpb.com.br",
        "kind": "wp_rest",
    },
    {
        "name": "Jornal da Paraíba",
        "feed_url": "https://jornaldaparaiba.com.br/feed/",
        "site_url": "https://jornaldaparaiba.com.br",
        "kind": "rss",
    },
    {
        "name": "Paraíba Online",
        "feed_url": "https://paraibaonline.com.br/feed/",
        "site_url": "https://paraibaonline.com.br",
        "kind": "rss",
    },
    {
        "name": "Portal Correio",
        "feed_url": "https://portalcorreio.com.br/feed/",
        "site_url": "https://portalcorreio.com.br",
        "kind": "rss",
    },
    {
        "name": "MaisPB",
        "feed_url": "https://www.maispb.com.br/feed/",
        "site_url": "https://www.maispb.com.br",
        "kind": "rss",
    },
]
