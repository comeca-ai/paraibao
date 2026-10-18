"""Adaptação do banco para o que os templates do design consomem.

Concentrar aqui evita lógica de apresentação espalhada nas rotas, e garante
que a regra de camada (quem tem página interna e quem sai para a fonte) seja
aplicada num lugar só.
"""

from datetime import datetime, timezone

from markupsafe import Markup

from .editoria import rotulo as rotulo_editoria
from .imagens import caminho_proxy

MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]

# O portal é da Paraíba: hora exibida é a de Brasília, não UTC.
FUSO = -3


def _local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(__import__("datetime").timedelta(hours=FUSO)))


def tempo_relativo(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    minutos = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    if minutos < 1:
        return "agora"
    if minutos < 60:
        return f"há {minutos} min"
    if minutos < 60 * 24:
        horas = minutos // 60
        return f"há {horas} hora" + ("s" if horas > 1 else "")
    dias = minutos // (60 * 24)
    if dias == 1:
        return "ontem"
    if dias < 7:
        return f"há {dias} dias"
    loc = _local(dt)
    return f"{loc.day} {MESES[loc.month - 1]}"


def hora_curta(dt: datetime | None) -> str:
    loc = _local(dt)
    return loc.strftime("%Hh%M") if loc else ""


MESES_LONGOS = ["janeiro","fevereiro","março","abril","maio","junho",
                "julho","agosto","setembro","outubro","novembro","dezembro"]


def data_extenso(dt) -> str:
    loc = _local(dt)
    if not loc:
        return ""
    return f"{loc.day} de {MESES_LONGOS[loc.month - 1]} de {loc.year} · {loc.strftime('%Hh%M')}"


def artigo_view(article, fonte_nome: str, municipio=None) -> dict:
    """Um artigo no formato que os templates esperam.

    Toda matéria abre DENTRO do Paraibão. O que muda por camada é o que a
    página mostra: agregada exibe trecho citado com atribuição e o caminho
    para a obra; própria e oficial exibem o texto inteiro. Ver app/legal.py.
    """
    externo = article.layer == "aggregate"
    data = article.published_at or article.collected_at
    # Matéria com texto próprio mostra o título e o olho que a gente escreveu.
    # Os do veículo de origem continuam no banco, intocados, como referência.
    proprio = article.layer == "own" and bool(article.own_title)
    return {
        "id": article.id,
        "titulo": article.own_title if proprio else article.title,
        "resumo": (article.own_summary if proprio else article.summary) or "",
        "propria": proprio,
        # 400px cobre card e thumb; a matéria pede a versão maior.
        "imagem": caminho_proxy(article.image_url, 400),
        "imagem_grande": caminho_proxy(article.image_url, 1200),
        "imagem_original": article.image_url,
        "url": f"/materia/{article.slug}" if article.slug else article.canonical_url,
        "url_origem": article.canonical_url,
        "externo": externo,
        # Vazio: o card abre na própria aba, porque a página é nossa. A marca
        # de origem fica no crédito da fonte, visível no card e na matéria.
        "alvo": Markup(""),
        "seta": Markup(""),
        "editoria": rotulo_editoria(article.category),
        "editoria_slug": article.category,
        "fonte": fonte_nome,
        "municipio": municipio.name if municipio else None,
        "municipio_slug": municipio.slug if municipio else None,
        "tempo": tempo_relativo(data),
        "hora": hora_curta(data),
        "data": data,
    }
