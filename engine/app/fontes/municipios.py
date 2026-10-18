"""Coleta por município: busca ativa em vez de espera.

O coletor normal lê os 20 itens mais recentes de cada portal e torce para
alguma ser de Itaporanga. Resultado: 44 das 90 primeiras matérias eram de João
Pessoa, e o interior não aparecia.

Aqui é o contrário: para cada município, PEDE as matérias dele. A API do
WordPress expõe as tags, e quase todo município da Paraíba tem a sua.

Medido no ClickPB em 2026-08-02:
  Campina Grande 1.224 · Patos 300 · Sousa 256 · Cajazeiras 172 · Monteiro 54
  Zabelê 7 · Coxixola 9 · Cacimba de Areia 10
  15 de 16 municípios da amostra tinham tag exata.

Duas armadilhas que os testes revelaram:
  - `?search=` está desabilitado nessa instalação (devolve 0). `?tags=<id>` funciona.
  - as tags são caóticas: junto de "Cajazeiras" existe "4ª Vara Federal de
    Campina Grande". Só casa nome exato, normalizando acento e caixa.
"""

import logging
import time
import unicodedata
from datetime import datetime, timezone

from sqlalchemy import select

from ..models import Municipality, MunicipalityTag, Source
from .base import ArtigoCandidato
from .wp_rest import CAMINHO, _autor, _imagem
from .base import limpar_corpo, limpar_texto, normalizar_url, parse_data, resumo_de

log = logging.getLogger("paraibao.fontes.municipios")

# Quantos municípios visitar por rodada. Baixo de propósito: a coleta é
# contínua, não precisa varrer os 223 de uma vez, e o portal é de terceiro.
POR_RODADA = 12
RESOLVER_POR_RODADA = 15
PAUSA = 0.5


def _dobrar(valor: str) -> str:
    sem_acento = "".join(
        c for c in unicodedata.normalize("NFKD", valor) if not unicodedata.combining(c)
    )
    return sem_acento.lower().strip()


def resolver_tags(session, source: Source, client, limite: int = RESOLVER_POR_RODADA) -> int:
    """Descobre o id da tag de cada município. Roda uma vez por município."""
    ja_tem = select(MunicipalityTag.municipality_id).where(
        MunicipalityTag.source_id == source.id
    )
    pendentes = session.scalars(
        select(Municipality)
        .where(Municipality.active.is_(True), Municipality.id.notin_(ja_tem))
        .order_by(Municipality.name)
        .limit(limite)
    ).all()
    if not pendentes:
        return 0

    base = source.site_url.rstrip("/")
    achados = 0
    for m in pendentes:
        registro = MunicipalityTag(municipality_id=m.id, source_id=source.id)
        try:
            resposta = client.get(
                f"{base}/wp-json/wp/v2/tags",
                params={"search": m.name, "per_page": 30, "_fields": "id,name,count"},
            )
            resposta.raise_for_status()
            candidatas = resposta.json()
            alvo = _dobrar(m.name)
            exatas = [
                t
                for t in candidatas
                if isinstance(t, dict) and _dobrar(str(t.get("name", ""))) == alvo
            ]
            # Mais de uma tag com o mesmo nome acontece; fica com a mais usada.
            if exatas:
                melhor = max(exatas, key=lambda t: t.get("count", 0))
                registro.tag_id = melhor["id"]
                registro.tag_name = str(melhor.get("name"))[:200]
                registro.post_count = melhor.get("count", 0)
                achados += 1
        except Exception as exc:
            log.warning("tag de %s falhou: %s", m.name, str(exc)[:100])
        registro.resolved_at = datetime.now(timezone.utc)
        session.add(registro)
        time.sleep(PAUSA)

    session.commit()
    log.info(
        "%s: %s municípios resolvidos, %s com tag", source.slug, len(pendentes), achados
    )
    return achados


def coletar_municipios(
    session, source: Source, client, limite: int = POR_RODADA
) -> list[tuple[ArtigoCandidato, int]]:
    """Puxa matérias novas de N municípios. Devolve (artigo, municipality_id).

    A fila é por `last_run_at` ascendente: quem foi visitado há mais tempo (ou
    nunca) vai primeiro, então em algumas horas o rodízio cobre os 223 sem
    precisar varrer tudo de uma vez.
    """
    fila = session.scalars(
        select(MunicipalityTag)
        .where(MunicipalityTag.source_id == source.id, MunicipalityTag.tag_id.is_not(None))
        .order_by(MunicipalityTag.last_run_at.asc().nullsfirst())
        .limit(limite)
    ).all()
    if not fila:
        return []

    base = source.site_url.rstrip("/")
    saida = []
    for registro in fila:
        params = {
            "tags": registro.tag_id,
            "per_page": 20,
            "_embed": "wp:featuredmedia,author",
            "orderby": "date",
            "order": "desc",
        }
        if registro.cursor_after:
            params["after"] = registro.cursor_after.replace(tzinfo=None).isoformat()
        try:
            resposta = client.get(f"{base}{CAMINHO}", params=params)
            resposta.raise_for_status()
            posts = resposta.json()
            if not isinstance(posts, list):
                raise ValueError("resposta inesperada")
        except Exception as exc:
            log.warning("municipio tag=%s falhou: %s", registro.tag_id, str(exc)[:100])
            registro.last_run_at = datetime.now(timezone.utc)
            continue

        mais_recente = registro.cursor_after
        for post in posts:
            link = post.get("link")
            if not link:
                continue
            corpo = limpar_corpo((post.get("content") or {}).get("rendered"))
            publicado = parse_data(
                (post.get("date_gmt") + "Z") if post.get("date_gmt") else post.get("date")
            )
            if publicado and (mais_recente is None or publicado > mais_recente):
                mais_recente = publicado
            saida.append(
                (
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
                    ),
                    registro.municipality_id,
                )
            )
        registro.cursor_after = mais_recente
        registro.last_run_at = datetime.now(timezone.utc)
        time.sleep(PAUSA)

    session.commit()
    return saida
