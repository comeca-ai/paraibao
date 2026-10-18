"""Escolhe o que vai para a rede social, monta a peça e publica.

A parte difícil aqui não é publicar: é decidir o que NÃO publicar. O motor
coleta algumas centenas de matérias por dia, e um perfil que despeja tudo isso
no feed perde alcance — o ranqueamento trata repetição em rajada como spam, e
o seguidor cansa antes disso. Então a máquina é feita de freios:

  janela   — notícia parada há mais de X horas perdeu a hora
  teto     — no máximo N por dia, contado no fuso de Brasília
  intervalo— espaçamento mínimo entre uma publicação e a seguinte
  horário  — não publica de madrugada, quando ninguém está no feed

Cada rodada do worker publica no máximo UMA matéria. Como o worker roda de 10
em 10 minutos, o espaçamento sai natural, sem agendador separado.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from . import cartao, social
from . import jornal
from .config import (
    BASE_URL,
    SOCIAL_HORA_FIM,
    SOCIAL_HORA_INICIO,
    SOCIAL_INTERVALO_MIN,
    SOCIAL_JANELA_HORAS,
    SOCIAL_LAYOUT,
    SOCIAL_MODE,
    SOCIAL_POSTS_POR_DIA,
)
from .editoria import rotulo as rotulo_editoria
from .models import Article, ArticleMunicipality, Municipality, SocialPost, Source
from .territory import fold

log = logging.getLogger("paraibao.publicador")

BRASILIA = timezone(timedelta(hours=-3))
REDES = ("instagram", "facebook")


# ------------------------------------------------------------------ legendas


def _hashtags(editoria: str, municipio: str | None) -> str:
    """Poucas e específicas. Nuvem de hashtag genérica não entrega mais nada
    desde que a busca do Instagram passou a ranquear por texto da legenda."""
    # Do perfil: a praça define a própria etiqueta de estado.
    tags = [f"#{jornal.UF.uf_nome.replace('á','a').replace('í','i')}", jornal.JORNAL.hashtag]
    if municipio:
        tags.append("#" + "".join(p.capitalize() for p in fold(municipio).split()))
    rotulo = rotulo_editoria(editoria)
    if rotulo and rotulo.lower() != "geral":
        tags.append("#" + fold(rotulo).replace(" ", "").capitalize())
    tags.append("#Noticias")
    return " ".join(dict.fromkeys(tags))


def legenda_instagram(dados: dict) -> str:
    """Legenda de feed não aceita link clicável: escrever a URL no meio do
    texto só polui. O caminho é a bio, e é isso que a chamada diz."""
    partes = [dados["titulo"]]
    if dados.get("resumo"):
        partes.append(dados["resumo"])

    local = f"{dados['municipio']} · " if dados.get("municipio") else ""
    partes.append(f"{local}Apuração: {dados['fonte']}")
    partes.append(f"A matéria completa está no {jornal.JORNAL.dominio}, no link da bio.")
    partes.append(_hashtags(dados["editoria"], dados.get("municipio")))
    return "\n\n".join(partes)[:2200]


def legenda_facebook(dados: dict) -> str:
    """Aqui o link é clicável e vai no campo próprio, com preview automático a
    partir das meta tags da matéria. A mensagem não repete a URL."""
    partes = [dados["titulo"]]
    if dados.get("resumo"):
        partes.append(dados["resumo"])
    local = f"{dados['municipio']} · " if dados.get("municipio") else ""
    partes.append(f"{local}Apuração: {dados['fonte']}")
    return "\n\n".join(partes)[:5000]


# ------------------------------------------------------------------ seleção


def _dados(session, artigo: Article) -> dict:
    fonte = session.get(Source, artigo.source_id)
    municipio = session.scalar(
        select(Municipality)
        .join(ArticleMunicipality, ArticleMunicipality.municipality_id == Municipality.id)
        .where(
            ArticleMunicipality.article_id == artigo.id,
            ArticleMunicipality.is_primary.is_(True),
        )
    )
    return {
        "id": artigo.id,
        "titulo": artigo.title,
        "resumo": (artigo.summary or "").strip(),
        "editoria": artigo.category,
        "municipio": municipio.name if municipio else None,
        "fonte": fonte.name if fonte else "",
        "slug": artigo.slug,
        "imagem": artigo.image_url,
    }


def _publicados_hoje(session, rede: str) -> int:
    inicio = datetime.now(BRASILIA).replace(hour=0, minute=0, second=0, microsecond=0)
    return session.scalar(
        select(func.count(SocialPost.id)).where(
            SocialPost.network == rede,
            SocialPost.status.in_(("published", "dry")),
            SocialPost.published_at >= inicio.astimezone(timezone.utc),
        )
    ) or 0


def _ultimo_em(session, rede: str) -> datetime | None:
    quando = session.scalar(
        select(func.max(SocialPost.published_at)).where(
            SocialPost.network == rede, SocialPost.status.in_(("published", "dry"))
        )
    )
    if quando and quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return quando


def candidata(session, rede: str) -> Article | None:
    """A próxima matéria a publicar nessa rede, ou None.

    Duas ordens, nesta sequência:

      1. tem município da Paraíba vinculado
      2. mais recente

    Só recência não basta. O motor coleta matéria de agência que os jornais
    paraibanos republicam ("Congresso volta do recesso"), e num perfil chamado
    Paraibão isso é conteúdo genérico competindo com portal nacional, que é a
    briga que a gente perde. Notícia com município vinculado é a que só existe
    aqui. Dentro de cada grupo vale a recência, e a janela de 20h garante que
    o critério local nunca ressuscite notícia velha.
    """
    corte = datetime.now(timezone.utc) - timedelta(hours=SOCIAL_JANELA_HORAS)
    ja_foram = select(SocialPost.article_id).where(SocialPost.network == rede)
    e_local = (
        select(1)
        .where(ArticleMunicipality.article_id == Article.id)
        .exists()
    )
    return session.scalar(
        select(Article)
        .where(
            Article.status.in_(("published", "collected")),
            Article.slug.is_not(None),
            func.length(Article.title) > 25,
            func.coalesce(Article.published_at, Article.collected_at) >= corte,
            Article.id.not_in(ja_foram),
        )
        .order_by(
            e_local.desc(),
            func.coalesce(Article.published_at, Article.collected_at).desc(),
        )
        .limit(1)
    )


def _pode_publicar(session, rede: str, forcar_intervalo: bool = False) -> tuple[bool, str]:
    """Os freios, em ordem de custo: os baratos primeiro.

    `forcar_intervalo` pula só o espaçamento, e é usado pelo botão do painel.
    O teto diário e a janela de horário continuam valendo: são eles que
    protegem o alcance do perfil, e não faria sentido dar um botão que os
    ignora.
    """
    if SOCIAL_MODE == "off":
        return False, "modo off"

    agora = datetime.now(BRASILIA)
    if not (SOCIAL_HORA_INICIO <= agora.hour < SOCIAL_HORA_FIM):
        return False, f"fora da janela ({SOCIAL_HORA_INICIO}h-{SOCIAL_HORA_FIM}h)"

    hoje = _publicados_hoje(session, rede)
    if hoje >= SOCIAL_POSTS_POR_DIA:
        return False, f"teto do dia atingido ({hoje}/{SOCIAL_POSTS_POR_DIA})"

    ultimo = _ultimo_em(session, rede)
    if ultimo and not forcar_intervalo:
        faltam = SOCIAL_INTERVALO_MIN - (
            datetime.now(timezone.utc) - ultimo
        ).total_seconds() / 60
        if faltam > 0:
            return False, f"aguardando intervalo ({int(faltam)} min)"
    return True, ""


# -------------------------------------------------------------- publicação


def url_cartao(slug: str) -> str:
    """URL pública do card. A Meta baixa a imagem daqui na criação do
    container, então isto precisa responder de fora, sem autenticação."""
    return f"{BASE_URL}/cartao/{slug}.jpg?v={cartao.VERSAO}"


def _foto(dados: dict) -> bytes | None:
    if SOCIAL_LAYOUT != "foto" or not dados.get("imagem"):
        return None
    from . import imagens

    resultado = imagens.buscar(dados["imagem"], 1200)
    return resultado[0] if resultado else None


def publicar_uma(session, rede: str, forcar_intervalo: bool = False) -> SocialPost | None:
    """Publica no máximo uma matéria. Devolve o registro, ou None se nada era
    elegível agora."""
    liberado, motivo = _pode_publicar(session, rede, forcar_intervalo)
    if not liberado:
        log.debug("%s: %s", rede, motivo)
        return None

    artigo = candidata(session, rede)
    if artigo is None:
        return None

    dados = _dados(session, artigo)
    registro = SocialPost(
        article_id=artigo.id, network=rede, layout=SOCIAL_LAYOUT, status="queued"
    )
    session.add(registro)
    # Grava ANTES de chamar a Meta: se a chamada publicar e a resposta se
    # perder, a unicidade (article_id, rede) impede a republicação na próxima
    # rodada. Perder um post é aceitável; publicar duas vezes não é.
    session.commit()
    registro_id = registro.id

    try:
        if rede == "instagram":
            legenda = legenda_instagram(dados)
            # Gera o card antes de chamar a Meta, senão ela busca a URL e
            # encontra 404 no meio da criação do container.
            cartao.obter(dados, SOCIAL_LAYOUT, _foto(dados))
            registro.caption = legenda
            if SOCIAL_MODE == "on":
                saida = social.publicar_instagram(url_cartao(dados["slug"]), legenda)
            else:
                saida = {"id": None, "permalink": None}
        else:
            mensagem = legenda_facebook(dados)
            registro.caption = mensagem
            if SOCIAL_MODE == "on":
                saida = social.publicar_facebook(
                    mensagem, f"{BASE_URL}/materia/{dados['slug']}"
                )
            else:
                saida = {"id": None, "permalink": None}

        registro.status = "published" if SOCIAL_MODE == "on" else "dry"
        registro.external_id = saida.get("id")
        registro.permalink = saida.get("permalink")
        registro.published_at = datetime.now(timezone.utc)
        registro.attempts += 1
        session.commit()
        log.info(
            "%s %s: %s", rede, registro.status, dados["titulo"][:70]
        )
        return registro
    except Exception as exc:
        session.rollback()
        registro = session.get(SocialPost, registro_id)
        if registro:
            registro.status = "failed"
            registro.attempts += 1
            registro.error = str(exc)[:600]
            session.commit()
        log.warning("%s falhou: %s", rede, str(exc)[:200])
        return registro


def rodar(session) -> int:
    """Uma passada por rede. Chamada pelo worker a cada ciclo."""
    if SOCIAL_MODE == "off":
        return 0
    total = 0
    for rede in REDES:
        if publicar_uma(session, rede) is not None:
            total += 1
    return total


def situacao(session) -> dict:
    """Estado da máquina para o painel."""
    resumo = {}
    for rede in REDES:
        liberado, motivo = _pode_publicar(session, rede)
        proxima = candidata(session, rede)
        ultimo = _ultimo_em(session, rede)
        resumo[rede] = {
            "hoje": _publicados_hoje(session, rede),
            "teto": SOCIAL_POSTS_POR_DIA,
            "liberado": liberado,
            "motivo": motivo,
            "ultimo_em": ultimo.isoformat() if ultimo else None,
            "proxima": proxima.title if proxima else None,
            "total": session.scalar(
                select(func.count(SocialPost.id)).where(SocialPost.network == rede)
            ) or 0,
        }
    return {
        "modo": SOCIAL_MODE,
        "layout": SOCIAL_LAYOUT,
        "janela": f"{SOCIAL_HORA_INICIO}h-{SOCIAL_HORA_FIM}h",
        "intervalo_min": SOCIAL_INTERVALO_MIN,
        "credenciais": social.configurado(),
        "redes": resumo,
    }
