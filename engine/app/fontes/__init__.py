"""Registro de adaptadores e o laço de coleta de uma fonte.

Cada fonte declara no banco qual degrau da escada ela usa (`Source.kind`), e
este módulo despacha para o adaptador certo. Trocar uma fonte de RSS para API
é um UPDATE numa coluna, não um deploy.
"""

import logging
import time

from . import rss, wp_rest
from .base import ArtigoCandidato
from .pagina import enriquecer

log = logging.getLogger("paraibao.fontes")

ADAPTADORES = {
    "wp_rest": wp_rest.coletar,
    "rss": rss.coletar,
}

# Campos que valem uma requisição extra na página da matéria.
ESSENCIAIS = ("imagem", "publicado_em")

# Educação com o servidor alheio: intervalo entre aberturas de matéria.
PAUSA_ENTRE_PAGINAS = 0.4
MAX_PAGINAS_POR_RODADA = 25


def coletar(source, client, limite: int = 60) -> list[ArtigoCandidato]:
    adaptador = ADAPTADORES.get(source.kind)
    if adaptador is None:
        raise ValueError(f"fonte {source.slug}: kind '{source.kind}' sem adaptador")
    return adaptador(source, client, limite=limite)


def completar(artigos: list[ArtigoCandidato], client, ja_conhecidas: set[str]) -> int:
    """Abre a página só das matérias novas às quais falta algo essencial.

    Nunca reabre matéria já no banco: o custo de uma coleta é proporcional ao
    que é novo, não ao tamanho do feed.
    """
    pendentes = [
        a
        for a in artigos
        if a.url not in ja_conhecidas and any(not getattr(a, c) for c in ESSENCIAIS)
    ]
    if not pendentes:
        return 0

    completadas = 0
    for artigo in pendentes[:MAX_PAGINAS_POR_RODADA]:
        try:
            resposta = client.get(artigo.url)
            resposta.raise_for_status()
            antes = artigo.imagem
            enriquecer(artigo, resposta.text)
            if artigo.imagem and not antes:
                completadas += 1
        except Exception as exc:
            # Falhar em completar não invalida a matéria: ela entra sem imagem.
            log.warning("nao completou %s: %s", artigo.url[:80], str(exc)[:120])
        time.sleep(PAUSA_ENTRE_PAGINAS)
    return completadas
