"""Métricas do jornal, escolhidas para responder pergunta de dono.

A regra que define o que entra aqui: **se o número subir e nada melhorar na
operação nem no caixa, ele não entra.** Por isso não existe "visitante único",
"impressão" nem "seguidor" neste módulo. Todos sobem sozinhos com o tempo e
nenhum diz o que fazer amanhã.

Cada número abaixo existe para responder uma pergunta específica:

  cobertura      -> a promessa do produto ("todos os municípios") é verdade?
  frescor        -> chegamos antes ou depois do leitor já ter lido na fonte?
  camada própria -> quanto do acervo sai do risco jurídico e entra no Google News?
  aproveitamento -> a IA está pagando o que consome ou desperdiçando cota?
  saúde da fonte -> qual veículo parou de entregar sem ninguém perceber?
  custo          -> quanto custa cada matéria, para saber o preço da praça

O que ainda NÃO dá para medir e está declarado como buraco, e não escondido:
leitor que volta, conversão para assinante e receita. Falta a camada de
membros existir. Ver `docs/ARQUITETURA.md`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Float, cast, func, select

from .models import Article, ArticleMunicipality, CrawlRun, Municipality, Source

PUBLICADO = Article.status.in_(("published", "collected"))
QUANDO = func.coalesce(Article.published_at, Article.collected_at)


def _desde(horas: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=horas)


def cobertura(session, dias: int = 7) -> dict:
    """Quantos municípios tiveram matéria na janela.

    É a métrica que mede a promessa do produto. Um portal que diz cobrir 223
    municípios e cobre 12 não tem problema de audiência, tem problema de
    produto, e nenhum número de pageview mostra isso.
    """
    total = session.scalar(select(func.count(Municipality.id))) or 0
    com_materia = session.scalar(
        select(func.count(func.distinct(ArticleMunicipality.municipality_id)))
        .select_from(ArticleMunicipality)
        .join(Article, Article.id == ArticleMunicipality.article_id)
        .where(PUBLICADO, QUANDO >= _desde(dias * 24))
    ) or 0
    # Municípios que nunca tiveram nada: a lista de trabalho editorial.
    nunca = session.scalar(
        select(func.count(Municipality.id)).where(
            ~Municipality.id.in_(select(ArticleMunicipality.municipality_id))
        )
    ) or 0
    return {
        "dias": dias,
        "total": total,
        "com_materia": com_materia,
        "pct": round(com_materia / total * 100) if total else 0,
        "nunca_cobertos": nunca,
    }


def frescor(session, horas: int = 48) -> dict:
    """Minutos entre a fonte publicar e nós coletarmos.

    Se a mediana for de horas, o leitor já leu no veículo de origem antes de
    o item existir aqui, e todo o resto do funil perde o sentido.
    """
    atraso = cast(
        func.extract("epoch", Article.collected_at - Article.published_at) / 60.0, Float
    )
    linha = session.execute(
        select(
            func.percentile_cont(0.5).within_group(atraso.asc()),
            func.percentile_cont(0.9).within_group(atraso.asc()),
            func.count(Article.id),
        ).where(
            PUBLICADO,
            Article.published_at.is_not(None),
            Article.collected_at >= _desde(horas),
            # atraso negativo = relógio da fonte adiantado; não é medida nossa
            atraso >= 0,
        )
    ).first()
    mediana, p90, n = linha or (None, None, 0)
    return {
        "horas": horas,
        "mediana_min": round(mediana) if mediana is not None else None,
        "p90_min": round(p90) if p90 is not None else None,
        "amostra": n or 0,
    }


def camadas(session) -> dict:
    """Agregada contra própria.

    Matéria própria é a que tem canonical nosso, entra no news-sitemap e pode
    ser compartilhada sem mandar o leitor para o concorrente. É o indicador de
    quanto do acervo deixou de ser empréstimo.
    """
    por_camada = dict(
        session.execute(
            select(Article.layer, func.count(Article.id)).where(PUBLICADO).group_by(Article.layer)
        ).all()
    )
    total = sum(por_camada.values())
    propria = por_camada.get("own", 0)
    return {
        "total": total,
        "propria": propria,
        "agregada": por_camada.get("aggregate", 0),
        "pct_propria": round(propria / total * 100, 1) if total else 0.0,
    }


def maquina_ia(session, horas: int = 24) -> dict:
    """Aproveitamento e consumo da reescrita.

    Aproveitamento baixo significa cota queimada em material que as travas
    reprovam: o custo acontece, o resultado não. É o número que diz se vale
    subir o teto diário ou se o problema é a matéria de entrada.
    """
    desde = _desde(horas)
    tentadas = session.scalar(
        select(func.count(Article.id)).where(Article.rewritten_at >= desde)
    ) or 0
    aprovadas = session.scalar(
        select(func.count(Article.id)).where(
            Article.rewritten_at >= desde, Article.layer == "own"
        )
    ) or 0
    entrada = session.scalar(
        select(func.sum(Article.rewrite_tokens_in)).where(Article.rewritten_at >= desde)
    ) or 0
    saida = session.scalar(
        select(func.sum(Article.rewrite_tokens_out)).where(Article.rewritten_at >= desde)
    ) or 0
    recusas = session.execute(
        select(Article.rewrite_error, func.count(Article.id))
        .where(Article.rewrite_error.is_not(None), Article.rewritten_at >= desde)
        .group_by(Article.rewrite_error)
        .order_by(func.count(Article.id).desc())
        .limit(5)
    ).all()
    return {
        "horas": horas,
        "tentadas": tentadas,
        "aprovadas": aprovadas,
        "aproveitamento": round(aprovadas / tentadas * 100) if tentadas else 0,
        "tokens_entrada": int(entrada),
        "tokens_saida": int(saida),
        # Motivo de recusa agrupado: é a pauta de melhoria do prompt.
        "recusas": [{"motivo": (m or "")[:80], "total": t} for m, t in recusas],
    }


def fontes(session, horas_silencio: int = 12) -> list[dict]:
    """Saúde por veículo.

    `silenciosa` é o alarme que faltava: uma fonte que para de entregar não
    gera erro nenhum, ela só some do fluxo. Sem esta coluna, dá para perder
    um veículo por semanas e só notar pela queda do volume total.
    """
    linhas = session.execute(
        select(
            Source,
            func.count(Article.id),
            func.max(QUANDO),
        )
        .outerjoin(Article, Article.source_id == Source.id)
        .group_by(Source.id)
        .order_by(Source.name)
    ).all()
    corte = _desde(horas_silencio)
    saida = []
    for s, total, ultima in linhas:
        saida.append({
            "nome": s.name,
            "slug": s.slug,
            "ativa": s.enabled,
            "degrau": s.kind,
            "materias": total or 0,
            "ultima": ultima.isoformat() if ultima else None,
            "silenciosa": bool(ultima is None or ultima < corte),
            "erro": (s.last_error or "")[:140] or None,
        })
    return saida


def coleta(session, rodadas: int = 12) -> dict:
    """As últimas rodadas do worker: é isto que prova que a máquina roda."""
    runs = session.scalars(
        select(CrawlRun).order_by(CrawlRun.id.desc()).limit(rodadas)
    ).all()
    return {
        "rodadas": [
            {
                "id": r.id,
                "status": r.status,
                "quando": (r.finished_at or r.started_at).isoformat(),
                "vistas": r.items_seen,
                "novas": r.items_new,
                "falhas": r.sources_failed,
            }
            for r in runs
        ],
        "ultima_em": (runs[0].finished_at or runs[0].started_at).isoformat() if runs else None,
    }


def producao(session, dias: int = 14) -> list[dict]:
    """Matérias por dia, separando própria de agregada.

    Série curta de propósito: serve para ver tendência e queda, não para
    virar gráfico bonito de apresentação.
    """
    linhas = session.execute(
        select(
            func.date_trunc("day", QUANDO).label("dia"),
            func.count(Article.id),
            func.count(Article.id).filter(Article.layer == "own"),
        )
        .where(PUBLICADO, QUANDO >= _desde(dias * 24))
        .group_by("dia")
        .order_by("dia")
    ).all()
    return [
        {"dia": d.date().isoformat(), "total": t, "propria": p}
        for d, t, p in linhas
        if d
    ]


def resumo(session) -> dict:
    """Tudo que o painel precisa, numa sessão só."""
    return {
        "cobertura": cobertura(session),
        "frescor": frescor(session),
        "camadas": camadas(session),
        "ia": maquina_ia(session),
        "fontes": fontes(session),
        "coleta": coleta(session),
        "producao": producao(session),
        "gerado_em": datetime.now(timezone.utc).isoformat(),
    }
