import hashlib
import json
import logging
import re
import secrets
from pathlib import Path
from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis import Redis
from sqlalchemy import desc, func, select, text

from . import contas, contatos, imagens, jornal, legal, metricas, migrate, publicador, redator, social, tempo
from .jornal import FILA_COLETA
from .config import BASE_URL, REDIS_URL, SOCIAL_LAYOUT
from .db import SessionLocal
from .editoria import ROTULOS, rotulo as rotulo_editoria
from .models import (
    Contato,
    Article,
    ArticleMunicipality,
    CrawlRun,
    Municipality,
    SocialPost,
    Source,
    Usuario,
)
from .views import artigo_view, data_extenso


log = logging.getLogger("paraibao.main")
redis = Redis.from_url(REDIS_URL, decode_responses=True)
templates = Jinja2Templates(directory="app/templates")


def _versao_asset(caminho: str) -> str:
    """Hash do conteúdo, para invalidar cache quando o arquivo muda.

    Sem isso o `max-age=604800` do nginx segura o CSS velho por uma semana e
    a correção não chega em quem já visitou o site.
    """
    try:
        return hashlib.sha256(Path(caminho).read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


ASSETS_V = {
    "css": _versao_asset("app/static/styles.css"),
    "fontes": _versao_asset("app/static/fontes.css"),
    # A prévia do WhatsApp e do Facebook é cacheada por eles do lado de lá.
    # Sem a versão na URL, trocar a arte não muda o que já foi compartilhado.
    "og": _versao_asset("app/static/img/og.jpg"),
    # O nginx serve estático com `max-age=604800`. Sem versão na URL, quem já
    # visitou o site fica uma semana com a logo antiga: foi o que aconteceu ao
    # tirar o ® e o arquivo novo já estar no ar.
    "logo": _versao_asset("app/static/img/logo-marca.svg"),
}

POR_PAGINA = 24

# Editorias da barra de navegação.
EDITORIAS_NAV = [
    {"slug": "politica", "rotulo": "Política"},
    {"slug": "seguranca", "rotulo": "Segurança"},
    {"slug": "esportes", "rotulo": "Esportes"},
    {"slug": "entretenimento", "rotulo": "Entretenimento"},
    {"slug": "saude", "rotulo": "Saúde"},
    {"slug": "educacao", "rotulo": "Educação"},
    {"slug": "economia", "rotulo": "Economia"},
]
# Do perfil: cada praça tem as suas.
MESORREGIOES = jornal.UF.mesorregioes
# Contado do perfil, e não escrito à mão: "223" aparecia em 9 lugares.
TOTAL_MUNICIPIOS = len(jornal.municipios())

PUBLICADO = Article.status.in_(("published", "collected"))
RECENCIA = desc(func.coalesce(Article.published_at, Article.collected_at))


def somente_painel(request: Request):
    """Restringe a rota ao domínio do painel, quando existe um.

    Duas razões, e a segunda é a que importa:

    1. Separa o que é público do que é administração. Quem varre a internet
       procurando `/painel` no domínio do jornal passa a não achar nada.
    2. No modelo white label, cada praça ganha o seu próprio subdomínio de
       painel. Manter a administração dentro do domínio do jornal
       misturaria as duas superfícies em todas elas de uma vez.

    Devolve 404, e não 403: 403 confirma que o caminho existe.
    """
    alvo = jornal.JORNAL.painel_host
    if not alvo:
        return
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host != alvo.lower():
        raise HTTPException(status_code=404, detail="não encontrado")


# --------------------------------------------------------------- quem entra
# Substituiu a senha básica do navegador. O porquê está em contas.py; aqui
# ficam só as três portas: página do painel, chamada de API e tela de dono.


def _ip_da_requisicao(request: Request) -> str:
    """IP real de quem chamou. Atrás do nginx, `request.client` é o proxy.

    Só o primeiro item do X-Forwarded-For é confiável, e mesmo assim porque o
    nginx à frente reescreve o cabeçalho. Sem proxy, cai no cliente direto.
    """
    encaminhado = request.headers.get("x-forwarded-for", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return request.client.host if request.client else ""


def _sessao(request: Request) -> dict | None:
    return contas.ler_sessao(request.cookies.get(contas.COOKIE))


def _pode_no_painel(sessao: dict | None) -> bool:
    return bool(sessao) and sessao.get("papel") in contas.PAPEIS_DE_PAINEL


def exigir_painel(request: Request) -> dict:
    """Páginas HTML do painel: sem sessão, vai para a tela de entrada.

    Redirecionar em vez de devolver 401 é o que faz a diferença entre um
    painel e um popup: quem chega sem sessão vê uma tela que explica onde
    está, e volta para onde queria ir depois de entrar.
    """
    sessao = _sessao(request)
    if not _pode_no_painel(sessao):
        destino = request.url.path
        if request.url.query:
            destino = f"{destino}?{request.url.query}"
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="entre para continuar",
            headers={"Location": f"/entrar?de={quote(destino, safe='')}"},
        )
    if sessao.get("precisa_trocar_senha"):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail="troque a senha para continuar",
            headers={"Location": "/senha"},
        )
    return sessao


def exigir_painel_api(request: Request) -> dict:
    """Rotas de API: 401 em JSON, porque quem chama é o JavaScript do painel e
    um redirecionamento viraria HTML no meio de um `fetch`."""
    sessao = _sessao(request)
    if not _pode_no_painel(sessao):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="não autenticado")
    return sessao


def exigir_dono(sessao: dict = Depends(exigir_painel)) -> dict:
    # 404 e não 403, pela mesma razão de `somente_painel`: 403 confirma que a
    # tela existe para quem não deveria saber disso.
    if sessao.get("papel") != contas.DONO:
        raise HTTPException(status_code=404, detail="não encontrado")
    return sessao


def _gravar_cookie(resposta: Response, request: Request, token: str) -> None:
    resposta.set_cookie(
        contas.COOKIE,
        token,
        max_age=contas.VALIDADE,
        httponly=True,
        samesite="strict",
        # Em HTTPS o cookie nunca sai em claro. Local, sem TLS, ele precisa
        # poder ser gravado, senão não dá para testar a tela antes do deploy.
        secure=(request.headers.get("x-forwarded-proto") or request.url.scheme) == "https",
        path="/",
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    migrate.run()
    yield


app = FastAPI(title="Paraibão", version="0.3.0", lifespan=lifespan)


@app.middleware("http")
async def separar_painel_do_jornal(request: Request, call_next):
    """No domínio do painel só existe painel; no do jornal, só jornal.

    Sem isto o subdomínio servia o site inteiro, o que dá dois problemas:
    confunde quem abre `painel.` esperando o painel, e cria **conteúdo
    duplicado num segundo hostname**, que é justamente o que se evita num
    projeto cujo tráfego depende de busca.

    O caminho raiz do painel vai para a tela da máquina; qualquer outro
    caminho público é devolvido para o domínio do jornal, com 301, para que
    um link errado que vaze não vire uma URL indexável.
    """
    alvo = jornal.JORNAL.painel_host
    if not alvo:
        return await call_next(request)

    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host != alvo.lower():
        return await call_next(request)

    caminho = request.url.path
    if caminho == "/":
        return Response(status_code=307, headers={"Location": "/painel/maquina"})
    # `/static` fica porque o painel usa a mesma folha de estilo e as fontes.
    # `/entrar`, `/sair` e `/senha` são a porta do painel: sem elas aqui, o
    # 301 para o jornal mandaria quem tenta entrar para a home do site.
    permitido = ("/painel", "/api/", "/static/", "/health", "/favicon",
                 "/entrar", "/sair", "/senha")
    if not caminho.startswith(permitido):
        return Response(
            status_code=301,
            headers={"Location": f"{BASE_URL}{caminho}"},
        )
    return await call_next(request)


@app.middleware("http")
async def responder_head(request: Request, call_next):
    """HEAD responde igual ao GET, sem corpo.

    O FastAPI não registra HEAD junto com GET, então toda página devolvia
    405. Isso é violação do HTTP e, na prática, quebrava a prévia de link:
    vários geradores de prévia fazem um HEAD antes do GET para conferir
    tipo e tamanho, e desistem no 405 mostrando só o texto.
    """
    if request.method != "HEAD":
        return await call_next(request)

    request.scope["method"] = "GET"
    resposta = await call_next(request)
    # O corpo é consumido e descartado: sem isso o gerador da resposta fica
    # pendurado. Os cabeçalhos vão inteiros, inclusive o content-length, que
    # é justamente o que o robô veio buscar.
    if hasattr(resposta, "body_iterator"):
        async for _ in resposta.body_iterator:
            pass
    return Response(status_code=resposta.status_code, headers=dict(resposta.headers))
# Assets do design original (styles.css, logo.svg, Neue Helvena, DM Sans).
app.mount("/static", StaticFiles(directory="app/static"), name="static")
# Mídia própria (vídeos). StaticFiles responde Range, que é o que o <video>
# usa para streaming. O diretório pode não existir em dev local.
if Path("/app/media").is_dir():
    app.mount("/media", StaticFiles(directory="/app/media"), name="media")



# ---------------------------------------------------------------- consultas


def _carregar(session, where=None, limite=POR_PAGINA, offset=0, ordem=RECENCIA):
    """Artigos + fonte + município primário, sem N+1."""
    consulta = (
        select(Article, Source.name, Source.slug)
        .join(Source, Source.id == Article.source_id)
        .where(PUBLICADO)
    )
    if where is not None:
        consulta = consulta.where(where)
    linhas = session.execute(consulta.order_by(ordem).offset(offset).limit(limite)).all()
    if not linhas:
        return []

    ids = [a.id for a, _, _ in linhas]
    primarios = session.execute(
        select(ArticleMunicipality.article_id, Municipality)
        .join(Municipality, Municipality.id == ArticleMunicipality.municipality_id)
        .where(
            ArticleMunicipality.article_id.in_(ids),
            ArticleMunicipality.is_primary.is_(True),
        )
    ).all()
    por_artigo = {artigo_id: m for artigo_id, m in primarios}
    return [
        artigo_view(a, fonte_nome, por_artigo.get(a.id))
        for a, fonte_nome, _ in linhas
    ]


def _menu(session) -> dict:
    """Editorias e cidades do mega menu, com contagem real do banco."""
    contagens = dict(
        session.execute(
            select(Article.category, func.count(Article.id))
            .where(PUBLICADO)
            .group_by(Article.category)
        ).all()
    )
    editorias = [
        {"slug": slug, "rotulo": rotulo_editoria(slug), "total": contagens.get(slug, 0)}
        for slug in ROTULOS
        if slug != "geral"
    ]
    editorias.sort(key=lambda e: e["total"], reverse=True)

    cidades = session.execute(
        select(Municipality.slug, Municipality.name)
        .join(ArticleMunicipality, ArticleMunicipality.municipality_id == Municipality.id)
        .group_by(Municipality.id)
        .order_by(desc(func.count(func.distinct(ArticleMunicipality.article_id))))
        .limit(4)
    ).all()
    return {
        "menu_editorias_a": editorias[:5],
        "menu_editorias_b": editorias[5:10],
        "menu_cidades": [{"slug": s, "nome": n} for s, n in cidades],
    }


# Painel que abre ao passar o mouse numa editoria da barra: 1 destaque com
# foto + 6 chamadas. Ver `templates/pb/base.html`.
MENU_POR_EDITORIA = 7
# O painel é igual para todo visitante e não muda de minuto a minuto. Sem
# esse cache, cada página do site pagaria as consultas do menu inteiro.
_MENU_TTL = timedelta(seconds=90)
_menu_cache: dict[str, object] = {"em": None, "dados": None}


def _paineis(session) -> dict[str, list[dict]]:
    """As mais recentes de cada editoria da barra, em UMA consulta.

    `row_number` particionado por editoria evita o laço de uma consulta por
    editoria, que rodaria em toda página do site.
    """
    slugs = [e["slug"] for e in EDITORIAS_NAV]
    ranking = (
        select(
            Article.id.label("id"),
            Article.category.label("cat"),
            func.row_number()
            .over(partition_by=Article.category, order_by=RECENCIA)
            .label("pos"),
        )
        .where(PUBLICADO, Article.category.in_(slugs))
        .subquery()
    )
    ids = [
        linha[0]
        for linha in session.execute(
            select(ranking.c.id).where(ranking.c.pos <= MENU_POR_EDITORIA)
        ).all()
    ]
    if not ids:
        return {}

    por_editoria: dict[str, list[dict]] = {}
    for a in _carregar(session, Article.id.in_(ids), limite=len(ids)):
        por_editoria.setdefault(a["editoria_slug"], []).append(a)
    # "Últimas" também abre painel: é o primeiro item da barra e o que o
    # leitor toca primeiro no celular. Sem isso ele seria o único da régua
    # que não responde ao toque.
    por_editoria["ultimas"] = _carregar(session, limite=MENU_POR_EDITORIA)
    return por_editoria


def _menu_completo(session) -> dict:
    agora = datetime.now(timezone.utc)
    em = _menu_cache["em"]
    if em is not None and agora - em < _MENU_TTL:
        return _menu_cache["dados"]
    dados = _menu(session)
    # O painel é enfeite de navegação e roda em TODA página. Se a consulta
    # falhar, o certo é a barra ficar sem painel, não o site inteiro cair.
    try:
        dados["paineis"] = _paineis(session)
    except Exception:
        log.exception("painel do menu falhou; barra segue sem painel")
        dados["paineis"] = {}
    _menu_cache["em"] = agora
    _menu_cache["dados"] = dados
    return dados


def _contexto(request: Request, session=None, secao: str = "", q: str | None = None) -> dict:
    ctx = {
        "request": request,
        "base_url": BASE_URL,
        "caminho": request.url.path,
        "editorias_nav": EDITORIAS_NAV,
        "secao": secao,
        "q": q,
        "ano": datetime.now(timezone.utc).year,
        "menu_editorias_a": [],
        "menu_editorias_b": [],
        "menu_cidades": [],
        "paineis": {},
        "destaque_menu": None,
        "canonical_externo": None,
        "assets_v": ASSETS_V,
        # Identidade e território do perfil, para o template não cravar
        # "Paraibão" nem "223" no HTML.
        "jornal": jornal.JORNAL,
        "uf": jornal.UF,
        "total_municipios": TOTAL_MUNICIPIOS,
    }
    if session is not None:
        ctx.update(_menu_completo(session))
    return ctx


def _pagina(request: Request) -> int:
    try:
        return max(1, int(request.query_params.get("pagina", 1)))
    except (TypeError, ValueError):
        return 1


def _render_lista(
    request, session, where, *, h1, descricao, chapeu="", subtitulo="", secao="",
    vazio_titulo="Nada por aqui ainda",
    vazio_texto="Assim que sair notícia sobre esse tema, ela aparece aqui.",
    q=None, vizinhos=None, vizinhos_titulo="", ordem=RECENCIA, municipio_fixo=None,
):
    pagina = _pagina(request)
    offset = (pagina - 1) * POR_PAGINA
    artigos = _carregar(session, where, limite=POR_PAGINA + 1, offset=offset, ordem=ordem)
    tem_mais = len(artigos) > POR_PAGINA
    # Numa página de município, a tag do card mostra o município da página. Sem
    # isso um card que cita duas cidades exibe a outra, e o leitor estranha ver
    # "João Pessoa" numa página de Campina Grande.
    if municipio_fixo is not None:
        for a in artigos:
            a["municipio"] = municipio_fixo
    contexto = _contexto(request, session, secao, q)
    contexto.update(
        artigos=artigos[:POR_PAGINA],
        h1=h1,
        chapeu=chapeu,
        subtitulo=subtitulo,
        descricao_pagina=descricao,
        pagina=pagina,
        tem_mais=tem_mais,
        caminho_base=request.url.path,
        vazio_titulo=vazio_titulo,
        vazio_texto=vazio_texto,
        vizinhos=vizinhos or [],
        vizinhos_titulo=vizinhos_titulo,
    )
    return templates.TemplateResponse("pb/lista.html", contexto)


# ------------------------------------------------------------------ páginas


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Home no layout do design original, alimentada pelo banco.

    Os blocos são preenchidos por fatias sem sobreposição da mesma lista
    ordenada por recência, então nenhuma matéria aparece duas vezes na página.
    Bloco sem matéria suficiente não é preenchido com exemplo: ele some.
    """
    with SessionLocal() as session:
        recentes = _carregar(session, limite=40)
        com_foto = [a for a in recentes if a["imagem"]]

        # A manchete precisa de foto: a dobra é montada em torno dela.
        manchete = com_foto[0] if com_foto else (recentes[0] if recentes else None)
        usados = {manchete["id"]} if manchete else set()

        def proximos(quantos, exigir_foto=False):
            saida = []
            for a in recentes:
                if a["id"] in usados:
                    continue
                if exigir_foto and not a["imagem"]:
                    continue
                usados.add(a["id"])
                saida.append(a)
                if len(saida) == quantos:
                    break
            return saida

        chamadas = proximos(4, exigir_foto=True)
        em_alta = proximos(4)
        ultimas_destaque = (proximos(1, exigir_foto=True) or proximos(1) or [None])[0]
        ultimas = proximos(4)

        # Bloco "Próximo jogo": o HTML está pronto no template, com escudo e VS.
        # Não existe fonte de calendário do Paraibano (fora de temporada em
        # agosto), então fica None e o bloco não renderiza. Preencher com
        # placar fixo seria dado inventado no ar.
        jogo = None

        # Coluna esquerda do bloco duplo: cards .video, que pedem imagem.
        video_artigos = [
            a for a in _carregar(session, limite=40)
            if a["imagem"] and a["id"] not in usados
        ][:3]
        for a in video_artigos:
            usados.add(a["id"])
        coluna_video = (
            {"titulo": "Em vídeo e imagem", "href": "/ultimas", "artigos": video_artigos}
            if len(video_artigos) >= 2
            else None
        )

        # Coluna direita: a .tabela do design, alimentada com o ranking real de
        # municípios. Mesmo componente visual, dado que existe.
        ranking = session.execute(
            select(Municipality.name, Municipality.slug, func.count(func.distinct(ArticleMunicipality.article_id)))
            .join(ArticleMunicipality, ArticleMunicipality.municipality_id == Municipality.id)
            .group_by(Municipality.id)
            .order_by(desc(func.count(func.distinct(ArticleMunicipality.article_id))))
            .limit(6)
        ).all()
        tabela = (
            {
                "titulo": "Municípios na semana",
                "href": "/municipios",
                "colunas": ["Município", "Notícias"],
                "linhas": [{"nome": nome, "valores": [total]} for nome, _, total in ranking],
            }
            if ranking
            else None
        )

        # O bloco que diferencia o projeto: matéria com município identificado.
        com_municipio = [
            a for a in _carregar(session, limite=60)
            if a["municipio"] and a["id"] not in usados
        ][:4]

        contexto = _contexto(request, session, "home")
        contexto.update(
            # Quem busca a previsão é o worker; a API só lê do Redis, para a
            # home nunca depender de requisição externa para abrir.
            tempo=tempo.atual(redis),
            manchete=manchete,
            chamadas=chamadas,
            em_alta=em_alta,
            ultimas_destaque=ultimas_destaque,
            ultimas=ultimas,
            jogo=jogo,
            coluna_video=coluna_video,
            tabela=tabela,
            por_municipio=com_municipio,
            destaque_menu=manchete,
        )
        return templates.TemplateResponse("pb/home.html", contexto)


@app.get("/ultimas", response_class=HTMLResponse)
def ultimas(request: Request):
    with SessionLocal() as session:
        return _render_lista(
            request, session, None,
            h1="Últimas notícias",
            chapeu="Paraíba",
            descricao="Todas as últimas notícias da Paraíba, em ordem cronológica.",
            secao="ultimas",
        )


@app.get("/editoria/{slug}", response_class=HTMLResponse)
def editoria(request: Request, slug: str):
    if slug not in ROTULOS:
        raise HTTPException(status_code=404, detail="Editoria não encontrada")
    with SessionLocal() as session:
        nome = rotulo_editoria(slug)
        return _render_lista(
            request, session, Article.category == slug,
            h1=nome,
            chapeu="Editoria",
            descricao=f"Notícias de {nome} na Paraíba.",
            secao=f"editoria:{slug}",
        )


@app.get("/municipio/{slug}", response_class=HTMLResponse)
def municipio(request: Request, slug: str):
    with SessionLocal() as session:
        m = session.scalar(select(Municipality).where(Municipality.slug == slug))
        if m is None:
            raise HTTPException(status_code=404, detail="Município não encontrado")

        vizinhos = session.scalars(
            select(Municipality)
            .where(Municipality.micro_region == m.micro_region, Municipality.id != m.id)
            .order_by(Municipality.name)
            .limit(24)
        ).all()

        where = Article.id.in_(
            select(ArticleMunicipality.article_id).where(
                ArticleMunicipality.municipality_id == m.id
            )
        )
        return _render_lista(
            request, session, where,
            h1=f"Notícias de {m.name}",
            chapeu=f"{m.micro_region} · {m.meso_region}",
            subtitulo=f"Código IBGE {m.ibge_code}.",
            descricao=(
                f"Notícias de {m.name}, na Paraíba: política, segurança, saúde, "
                f"educação e prefeitura. Microrregião de {m.micro_region}."
            ),
            secao="municipios",
            vazio_titulo=f"Ainda sem notícia de {m.name}",
            vazio_texto=(
                "Nenhuma das fontes acompanhadas publicou sobre esse município no "
                "período coletado. A página segue no ar e se atualiza sozinha."
            ),
            vizinhos=vizinhos,
            vizinhos_titulo=f"Outros municípios da microrregião de {m.micro_region}",
            municipio_fixo=m,
        )


@app.get("/municipios", response_class=HTMLResponse)
def municipios(request: Request):
    with SessionLocal() as session:
        contagens = dict(
            session.execute(
                select(
                    ArticleMunicipality.municipality_id,
                    func.count(func.distinct(ArticleMunicipality.article_id)),
                ).group_by(ArticleMunicipality.municipality_id)
            ).all()
        )
        todos = session.scalars(select(Municipality).order_by(Municipality.name)).all()
        regioes = []
        for nome in MESORREGIOES:
            do_grupo = [m for m in todos if m.meso_region == nome]
            if do_grupo:
                regioes.append(
                    {
                        "nome": nome,
                        "municipios": [
                            {"slug": m.slug, "name": m.name, "total": contagens.get(m.id, 0)}
                            for m in do_grupo
                        ],
                    }
                )
        contexto = _contexto(request, session, "municipios")
        contexto.update(regioes=regioes, com_noticia=len(contagens))
        return templates.TemplateResponse("pb/municipios.html", contexto)


@app.get("/fonte/{slug}", response_class=HTMLResponse)
def fonte(request: Request, slug: str):
    with SessionLocal() as session:
        s = session.scalar(select(Source).where(Source.slug == slug))
        if s is None:
            raise HTTPException(status_code=404, detail="Fonte não encontrada")
        return _render_lista(
            request, session, Article.source_id == s.id,
            h1=s.name,
            chapeu="Veículo",
            subtitulo=f"Chamadas coletadas de {s.site_url}. O texto integral fica na origem.",
            descricao=f"Notícias da Paraíba publicadas por {s.name}.",
        )


# ------------------------------------------------- anuncie / escreva

# Textos das duas portas. Ficam juntos porque a página é a mesma e assim dá
# para ler as duas propostas lado a lado ao ajustar copy.
PORTAS = {
    "anuncio": {
        "acao": "/anuncie",
        "chapeu": "Publicidade",
        "titulo": f"Anuncie no {jornal.JORNAL.nome}",
        "subtitulo": f"Fale com quem lê notícia {jornal.UF.uf_com_artigo} todo dia, "
                     "no município onde o seu cliente está.",
        "descricao": f"Anuncie no {jornal.JORNAL.nome} e fale com o leitor de notícia "
                     f"{jornal.UF.uf_com_artigo}, por município, seção ou no site inteiro.",
        "lado_titulo": "Por que aqui",
        "argumentos": [
            f"Cobertura dos {TOTAL_MUNICIPIOS} municípios {jornal.UF.uf_com_artigo}, não só da capital.",
            "Seu anúncio pode aparecer só na cidade ou só na editoria que interessa.",
            "Leitor que chega pelo nome do próprio município, com intenção local.",
            "Formatos e valores combinados caso a caso, sem pacote engessado.",
        ],
        "nota": "Conte o que você vende e para qual cidade. A gente responde com "
                "os formatos que fazem sentido e os valores.",
        "rotulo_mensagem": "O que você quer divulgar",
        "rotulo_botao": "Quero anunciar",
        "texto_ok": "Vamos responder no WhatsApp ou no e-mail que você deixou, "
                    "normalmente em até um dia útil.",
    },
    "colaborador": {
        "acao": "/escreva",
        "chapeu": "Faça parte",
        "titulo": "Escreva com a gente",
        "subtitulo": f"O {jornal.JORNAL.nome} cobre {TOTAL_MUNICIPIOS} municípios. "
                     "Ninguém conhece a sua cidade melhor do que quem mora nela.",
        "descricao": f"Quer escrever para o {jornal.JORNAL.nome}? Conte de onde você é "
                     "e sobre o que quer escrever.",
        "lado_titulo": "Quem procuramos",
        "argumentos": [
            f"Gente de qualquer município {jornal.UF.uf_com_artigo}, principalmente fora da capital.",
            "Não precisa ser jornalista formado nem ter publicado antes.",
            "Precisa apurar antes de escrever e não inventar o que não conferiu.",
            "Pauta que só quem está lá enxerga vale mais que texto bonito.",
        ],
        "nota": "Se você já escreveu alguma coisa, manda o link. Se nunca escreveu, "
                "conte que assunto da sua cidade ninguém está cobrindo.",
        "rotulo_mensagem": "Sobre o que você quer escrever",
        "rotulo_botao": "Quero escrever",
        "texto_ok": "Vamos ler com calma e responder no contato que você deixou.",
    },
}


def _ip_do_pedido(request: Request) -> str:
    """IP real do leitor, e não o do nginx que está na frente."""
    encaminhado = request.headers.get("x-forwarded-for", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return request.client.host if request.client else ""


def _pagina_formulario(request, tipo, *, erro=None, valores=None, enviado=False,
                       status_code=200):
    porta = PORTAS[tipo]
    with SessionLocal() as session:
        contexto = _contexto(request, session, f"porta:{tipo}")
    contexto.update(
        porta, tipo=tipo, areas=contatos.AREAS,
        erro=erro, valores=valores or {}, enviado=enviado,
    )
    return templates.TemplateResponse(
        "pb/formulario.html", contexto, status_code=status_code
    )


@app.get("/anuncie", response_class=HTMLResponse)
def anuncie(request: Request):
    return _pagina_formulario(request, "anuncio")


@app.get("/escreva", response_class=HTMLResponse)
def escreva(request: Request):
    return _pagina_formulario(request, "colaborador")


async def _receber(request: Request, tipo: str):
    form = await request.form()
    dados = {k: (v if isinstance(v, str) else "") for k, v in form.items()}

    # Armadilha invisível: humano não enxerga o campo, robô preenche. Responde
    # como sucesso de propósito, para não ensinar o robô a contornar.
    if dados.get("site", "").strip():
        log.info("formulário %s barrado pela armadilha", tipo)
        return _pagina_formulario(request, tipo, enviado=True)

    try:
        with SessionLocal() as session:
            contatos.registrar(
                session, tipo, dados,
                ip=_ip_do_pedido(request),
                user_agent=request.headers.get("user-agent", ""),
                origem=str(request.url.path),
            )
    except contatos.ErroContato as e:
        return _pagina_formulario(
            request, tipo, erro=str(e), valores=dados, status_code=422
        )
    except Exception:
        log.exception("falha ao gravar contato tipo=%s", tipo)
        return _pagina_formulario(
            request, tipo,
            erro="Não conseguimos registrar agora. Tente de novo em alguns minutos.",
            valores=dados, status_code=500,
        )
    return _pagina_formulario(request, tipo, enviado=True)


@app.post("/anuncie", response_class=HTMLResponse)
async def anuncie_post(request: Request):
    return await _receber(request, "anuncio")


@app.post("/escreva", response_class=HTMLResponse)
async def escreva_post(request: Request):
    return await _receber(request, "colaborador")


@app.get("/busca", response_class=HTMLResponse)
def busca(request: Request, q: str = Query("", max_length=120)):
    termo = q.strip()
    with SessionLocal() as session:
        if not termo:
            return _render_lista(
                request, session, Article.id < 0,
                h1="Busca", chapeu="Paraíba",
                descricao="Busque notícias da Paraíba por palavra, cidade ou tema.",
                vazio_titulo="Digite o que você procura",
                vazio_texto="Nome de cidade, tema ou palavra que apareça na manchete.",
                q=termo,
            )
        # websearch_to_tsquery aceita a sintaxe que o usuário já conhece de
        # buscador ("a b", "a OR b", aspas para frase) e nunca lança erro de
        # sintaxe com entrada arbitrária, diferente de to_tsquery.
        where = Article.id.in_(
            select(Article.id).where(
                text("search_vector @@ websearch_to_tsquery('portuguese', :termo)").bindparams(
                    termo=termo
                )
            )
        )
        ordem = text(
            "ts_rank(search_vector, websearch_to_tsquery('portuguese', :termo)) DESC, "
            "coalesce(published_at, collected_at) DESC"
        ).bindparams(termo=termo)
        return _render_lista(
            request, session, where,
            h1=f"Busca: {termo}",
            chapeu="Resultados",
            descricao=f"Resultados da busca por {termo} nas notícias da Paraíba.",
            vazio_titulo=f"Nada encontrado para “{termo}”",
            vazio_texto="Tente outra palavra, ou procure pelo nome do município.",
            q=termo,
            ordem=ordem,
        )


@app.get("/materia/{slug}", response_class=HTMLResponse)
def materia(request: Request, slug: str):
    """Página da matéria.

    Camada `aggregate`: título, resumo próprio e um trecho citado com
    atribuição, mais o caminho para a obra original. Nunca o texto integral.
    Camadas `own` e `official`: texto inteiro, porque é nosso ou é ato oficial.
    Ver app/legal.py para a base na Lei 9.610/98.
    """
    with SessionLocal() as session:
        linha = session.execute(
            select(Article, Source.name, Source.slug)
            .join(Source, Source.id == Article.source_id)
            .where(Article.slug == slug, PUBLICADO)
        ).one_or_none()
        if linha is None:
            raise HTTPException(status_code=404, detail="Matéria não encontrada")
        article, fonte_nome, fonte_slug = linha

        municipio = session.scalar(
            select(Municipality)
            .join(ArticleMunicipality, ArticleMunicipality.municipality_id == Municipality.id)
            .where(
                ArticleMunicipality.article_id == article.id,
                ArticleMunicipality.is_primary.is_(True),
            )
        )
        a = artigo_view(article, fonte_nome, municipio)
        a["fonte_slug"] = fonte_slug
        a["url_origem"] = article.canonical_url
        a["autor"] = article.author
        a["data_extenso"] = data_extenso(a["data"])

        modo = legal.modo_exibicao(article.layer)
        # Camada própria exibe o NOSSO texto por inteiro. Camada agregada
        # exibe um trecho citado do texto do veículo. Trocar essas duas
        # origens é justamente o erro que a arquitetura existe para impedir.
        bruto = (
            legal.trecho_citavel(article.body)
            if modo == "citacao"
            else (article.own_body or article.body or "")
        )
        paragrafos = [t.strip() for t in re.split(r"\n{2,}|(?<=\.)\s{2,}", bruto) if len(t.strip()) > legal.MIN_PARAGRAFO]
        if not paragrafos and bruto:
            paragrafos = [bruto]

        relacionadas = _carregar(
            session,
            (Article.category == article.category) & (Article.id != article.id),
            limite=4,
        )

        url_absoluta = f"{BASE_URL}/materia/{article.slug}"
        jsonld = json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "NewsArticle",
                "headline": a["titulo"][:110],
                "datePublished": a["data"].isoformat() if a["data"] else None,
                "image": [article.image_url] if article.image_url else [],
                # A autoria acompanha quem escreveu o TEXTO. Na camada agregada
                # o texto é do veículo, e declarar como nossa contradiria a
                # própria página. Na camada própria o texto é nosso, e o
                # veículo continua declarado em isBasedOn.
                "author": {
                    "@type": "Organization",
                    "name": "Paraibão" if a.get("propria") else fonte_nome,
                },
                "publisher": {"@type": "Organization", "name": "Paraibão"},
                "isBasedOn": article.canonical_url,
                "mainEntityOfPage": url_absoluta,
            },
            ensure_ascii=False,
        )

        contexto = _contexto(request, session, f"editoria:{article.category}")
        contexto.update(
            a=a,
            modo=modo,
            paragrafos=paragrafos,
            relacionadas=relacionadas,
            credito_imagem=legal.credito_imagem(fonte_nome),
            url_absoluta=url_absoluta,
            compartilhar_wa=quote(f"{article.title} {url_absoluta}"),
            jsonld=jsonld,
            # A obra original é do veículo. O canonical aponta para lá: é o
            # sinal de boa-fé que sustenta o resto do argumento.
            canonical_externo=article.canonical_url if modo == "citacao" else None,
        )
        return templates.TemplateResponse("pb/materia.html", contexto)


# --------------------------------------------------------------------- SEO


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /painel\n"
        "Disallow: /api/\n"
        # Página de serviço para o link da bio: repete matéria que já está
        # indexada em outro endereço. /cartao NÃO entra aqui: o robô do
        # Facebook precisa alcançar a imagem para montar o preview.
        "Disallow: /ig\n\n"
        f"Sitemap: {BASE_URL}/sitemap.xml\n"
        f"Sitemap: {BASE_URL}/news-sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap():
    with SessionLocal() as session:
        urls = [
            f"<url><loc>{BASE_URL}/</loc><changefreq>hourly</changefreq>"
            f"<priority>1.0</priority></url>\n",
            f"<url><loc>{BASE_URL}/ultimas</loc><changefreq>hourly</changefreq></url>\n",
            f"<url><loc>{BASE_URL}/municipios</loc><changefreq>daily</changefreq>"
            f"<priority>0.8</priority></url>\n",
        ]
        for slug in ROTULOS:
            urls.append(
                f"<url><loc>{BASE_URL}/editoria/{slug}</loc>"
                f"<changefreq>hourly</changefreq></url>\n"
            )
        # As 223 páginas municipais são o ativo de SEO do projeto. Todas entram,
        # inclusive as sem notícia: a busca de cauda longa ("notícias de X PB")
        # existe antes de o conteúdo existir.
        for slug in session.scalars(select(Municipality.slug).order_by(Municipality.slug)):
            urls.append(
                f"<url><loc>{BASE_URL}/municipio/{slug}</loc>"
                f"<changefreq>daily</changefreq><priority>0.7</priority></url>\n"
            )
        for slug in session.scalars(select(Source.slug).where(Source.slug.is_not(None))):
            urls.append(f"<url><loc>{BASE_URL}/fonte/{slug}</loc></url>\n")
        corpo = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "".join(urls)
            + "</urlset>\n"
        )
        return Response(content=corpo, media_type="application/xml")


@app.get("/news-sitemap.xml")
def news_sitemap():
    """Google News só aceita as últimas 48h, e só conteúdo próprio entra."""
    corte = datetime.now(timezone.utc) - timedelta(hours=48)
    with SessionLocal() as session:
        artigos = session.scalars(
            select(Article)
            .where(
                PUBLICADO,
                Article.layer.in_(("own", "official")),
                Article.slug.is_not(None),
                func.coalesce(Article.published_at, Article.collected_at) >= corte,
            )
            .order_by(RECENCIA)
            .limit(1000)
        ).all()
        itens = []
        for a in artigos:
            data = (a.published_at or a.collected_at).isoformat()
            titulo = a.title.replace("&", "&amp;").replace("<", "&lt;")
            itens.append(
                f"<url><loc>{BASE_URL}/materia/{a.slug}</loc>"
                f"<news:news><news:publication><news:name>Paraibão</news:name>"
                f"<news:language>pt</news:language></news:publication>"
                f"<news:publication_date>{data}</news:publication_date>"
                f"<news:title>{titulo}</news:title></news:news></url>\n"
            )
        corpo = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">\n'
            + "".join(itens)
            + "</urlset>\n"
        )
        return Response(content=corpo, media_type="application/xml")


# ----------------------------------------------------------- API e operação


@app.get("/img/{largura}/{assinatura}")
def imagem(largura: int, assinatura: str, u: str = Query(...)):
    """Serve imagem de fonte por um domínio só, com cache e redimensionada.

    A assinatura impede que isso vire proxy aberto: sem ela, qualquer um
    poderia usar o servidor para servir qualquer imagem da internet.
    """
    if largura not in imagens.LARGURAS:
        raise HTTPException(status_code=400, detail="largura inválida")
    if not secrets.compare_digest(assinatura, imagens.assinar(u, largura)):
        raise HTTPException(status_code=403, detail="assinatura inválida")

    resultado = imagens.buscar(u, largura)
    if resultado is None:
        raise HTTPException(status_code=404, detail="imagem indisponível")
    conteudo, tipo = resultado
    return Response(
        content=conteudo,
        media_type=tipo,
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


@app.get("/cartao/{slug}.jpg")
def cartao_jpg(slug: str):
    """Card da matéria em JPEG, no formato de feed.

    Precisa ser público e sem autenticação: a Meta baixa esta URL do servidor
    dela na hora de criar a publicação. Também é o que a página /ig exibe.
    """
    with SessionLocal() as session:
        linha = session.execute(
            select(Article, Source.name)
            .join(Source, Source.id == Article.source_id)
            .where(Article.slug == slug)
        ).first()
        if linha is None:
            raise HTTPException(status_code=404, detail="matéria não encontrada")
        artigo, fonte_nome = linha
        municipio = session.scalar(
            select(Municipality)
            .join(ArticleMunicipality, ArticleMunicipality.municipality_id == Municipality.id)
            .where(
                ArticleMunicipality.article_id == artigo.id,
                ArticleMunicipality.is_primary.is_(True),
            )
        )

    dados = {
        "id": artigo.id,
        "titulo": artigo.title,
        "resumo": (artigo.summary or "").strip(),
        "editoria": artigo.category,
        "municipio": municipio.name if municipio else None,
        "fonte": fonte_nome,
    }
    from . import cartao as cartao_mod

    layout = SOCIAL_LAYOUT
    foto = None
    if layout == "foto" and artigo.image_url:
        resultado = imagens.buscar(artigo.image_url, 1200)
        foto = resultado[0] if resultado else None
    return Response(
        content=cartao_mod.obter(dados, layout, foto),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.get("/ig", response_class=HTMLResponse)
def link_da_bio(request: Request):
    """Destino do link da bio do Instagram.

    Existe por uma limitação da plataforma: legenda de publicação de feed não
    aceita link clicável. Quem viu o card no Instagram chega aqui e encontra a
    mesma notícia em primeiro lugar, sem ter que procurar na home.
    """
    with SessionLocal() as session:
        publicadas = select(SocialPost.article_id).where(
            SocialPost.network == "instagram",
            SocialPost.status.in_(("published", "dry")),
        )
        tem = session.scalar(select(func.count()).select_from(publicadas.subquery())) or 0
        return _render_lista(
            request,
            session,
            Article.id.in_(publicadas) if tem else None,
            h1="Do nosso Instagram",
            chapeu="Paraibão",
            subtitulo="As notícias que você viu no feed, abertas aqui.",
            descricao="As matérias publicadas no Instagram do Paraibão.",
            secao="",
            vazio_titulo="Ainda não publicamos por aqui",
            vazio_texto="Veja as últimas notícias da Paraíba na home.",
        )


@app.get("/health")
def health():
    with SessionLocal() as session:
        session.execute(select(1))
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/public/articles")
def api_articles(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    with SessionLocal() as session:
        artigos = _carregar(session, limite=limit, offset=offset)
        total = session.scalar(select(func.count(Article.id)).where(PUBLICADO)) or 0
        return {
            "articles": [
                {
                    "id": a["id"],
                    "title": a["titulo"],
                    "summary": a["resumo"],
                    "image_url": a["imagem"],
                    "url": a["url"],
                    "origin_url": a["url_origem"],
                    "category": a["editoria_slug"],
                    "source": a["fonte"],
                    "municipality": a["municipio"],
                    "published_at": a["data"].isoformat() if a["data"] else None,
                }
                for a in artigos
            ],
            "pagination": {"limit": limit, "offset": offset, "total": total},
        }


# --------------------------------------------------------------- entrar/sair


def _tela_de_entrada(request: Request, **extra):
    base = {
        "request": request,
        "jornal": jornal.JORNAL,
        "assets_v": ASSETS_V,
        "min_senha": contas.MIN_SENHA,
        "modo": "entrar",
        "erro": "",
        "recado": "",
        "de": "",
        "email_digitado": "",
        "forcado": False,
        "usuario": None,
    }
    base.update(extra)
    return templates.TemplateResponse("pb/entrar.html", base)


def _destino_seguro(de: str) -> str:
    """Só aceita caminho interno do painel.

    Sem isto, `/entrar?de=https://site-do-golpe` transforma a tela de login do
    jornal em trampolim de phishing: a pessoa entra de verdade, no domínio
    certo, e sai do outro lado em outro site.
    """
    de = (de or "").strip()
    if de.startswith("/painel") and "//" not in de and "\\" not in de:
        return de
    return "/painel/maquina"


@app.get("/entrar", response_class=HTMLResponse,
         dependencies=[Depends(somente_painel)], include_in_schema=False)
def entrar_tela(request: Request, de: str = Query("", max_length=300)):
    sessao = _sessao(request)
    if _pode_no_painel(sessao) and not sessao.get("precisa_trocar_senha"):
        return RedirectResponse(_destino_seguro(de), status_code=303)
    return _tela_de_entrada(request, de=de)


@app.post("/entrar", response_class=HTMLResponse,
          dependencies=[Depends(somente_painel)], include_in_schema=False)
def entrar_enviar(
    request: Request,
    email: str = Form("", max_length=200),
    senha: str = Form("", max_length=200),
    de: str = Form("", max_length=300),
):
    ip_hash = contas.hash_ip(_ip_da_requisicao(request))
    espera = contas.bloqueado(email, ip_hash)
    if espera:
        minutos = max(1, round(espera / 60))
        return _tela_de_entrada(
            request, de=de, email_digitado=email[:200],
            erro=f"Tentativas demais. Espere {minutos} minuto(s) e tente de novo.",
        )

    with SessionLocal() as session:
        try:
            usuario = contas.autenticar(session, email, senha)
        except contas.ErroConta as e:
            contas.registrar_falha(email, ip_hash)
            log.warning("login recusado para %r", (email or "")[:80])
            return _tela_de_entrada(
                request, de=de, email_digitado=email[:200], erro=str(e)
            )

        contas.limpar_falhas(email, ip_hash)
        usuario.ultimo_acesso_em = datetime.now(timezone.utc)
        usuario.ultimo_ip_hash = ip_hash
        session.commit()
        token = contas.abrir_sessao(usuario, ip_hash)
        precisa_trocar = usuario.precisa_trocar_senha

    destino = "/senha" if precisa_trocar else _destino_seguro(de)
    resposta = RedirectResponse(destino, status_code=303)
    _gravar_cookie(resposta, request, token)
    return resposta


@app.get("/sair", dependencies=[Depends(somente_painel)], include_in_schema=False)
def sair(request: Request):
    contas.fechar_sessao(request.cookies.get(contas.COOKIE))
    resposta = RedirectResponse("/entrar", status_code=303)
    resposta.delete_cookie(contas.COOKIE, path="/")
    return resposta


@app.get("/senha", response_class=HTMLResponse,
         dependencies=[Depends(somente_painel)], include_in_schema=False)
def senha_tela(request: Request, de: str = Query("", max_length=300)):
    """Trocar a própria senha. Serve para os dois casos: a troca obrigatória
    do primeiro acesso e a troca voluntária de quem já está dentro."""
    sessao = _sessao(request)
    if not _pode_no_painel(sessao):
        return RedirectResponse("/entrar", status_code=303)
    return _tela_de_entrada(
        request, modo="trocar", de=de,
        forcado=bool(sessao.get("precisa_trocar_senha")),
        usuario=sessao,
    )


@app.post("/senha", response_class=HTMLResponse,
          dependencies=[Depends(somente_painel)], include_in_schema=False)
def senha_enviar(
    request: Request,
    nova: str = Form("", max_length=200),
    repetida: str = Form("", max_length=200),
    atual: str = Form("", max_length=200),
    de: str = Form("", max_length=300),
):
    sessao = _sessao(request)
    if not _pode_no_painel(sessao):
        return RedirectResponse("/entrar", status_code=303)
    forcado = bool(sessao.get("precisa_trocar_senha"))

    with SessionLocal() as session:
        usuario = session.get(Usuario, sessao["usuario_id"])
        if not usuario or not usuario.ativo:
            return RedirectResponse("/sair", status_code=303)
        # Na troca voluntária a senha atual é exigida; na obrigatória não,
        # porque a senha atual é justamente a que já circulou.
        if not forcado and not contas.conferir_senha(atual, usuario.senha_hash):
            return _tela_de_entrada(
                request, modo="trocar", de=de, forcado=forcado, usuario=sessao,
                erro="A senha atual está incorreta.",
            )
        try:
            contas.trocar_senha(session, usuario, nova, repetida)
        except contas.ErroConta as e:
            return _tela_de_entrada(
                request, modo="trocar", de=de, forcado=forcado, usuario=sessao,
                erro=str(e),
            )
        # `trocar_senha` derruba TODAS as sessões, inclusive esta. Abrir uma
        # nova aqui é o que evita mandar quem acabou de trocar a senha de
        # volta para a tela de entrada.
        token = contas.abrir_sessao(usuario, contas.hash_ip(_ip_da_requisicao(request)))

    resposta = RedirectResponse(_destino_seguro(de), status_code=303)
    _gravar_cookie(resposta, request, token)
    return resposta


# ----------------------------------------------------------------- painel


@app.get("/painel", dependencies=[Depends(somente_painel), Depends(exigir_painel)], include_in_schema=False)
def painel():
    return FileResponse("app/painel.html")


@app.get("/painel/maquina", response_class=HTMLResponse,
         dependencies=[Depends(somente_painel)], include_in_schema=False)
def painel_maquina(request: Request, eu: dict = Depends(exigir_painel)):
    """Painel de controle: a máquina rodando e as métricas que decidem algo.

    Renderizado no servidor de propósito. É a tela que se abre no meio da rua
    para saber se o jornal está de pé, e uma tela dessas não pode depender de
    JavaScript carregar.
    """
    with SessionLocal() as session:
        dados = metricas.resumo(session)
        situacao_ia = redator.situacao(session)
    return templates.TemplateResponse(
        "pb/maquina.html",
        {
            "request": request,
            "m": dados,
            "ia_config": situacao_ia,
            "jornal": jornal.JORNAL,
            "uf": jornal.UF,
            "assets_v": ASSETS_V,
            "eu": eu,
        },
    )


@app.get("/api/metricas", dependencies=[Depends(somente_painel), Depends(exigir_painel_api)])
def api_metricas():
    """Mesmas métricas em JSON, para o dia em que existir painel do dono de
    várias praças e ele precisar somar isto de N jornais."""
    with SessionLocal() as session:
        return metricas.resumo(session)


@app.get("/painel/contatos", response_class=HTMLResponse,
         dependencies=[Depends(somente_painel)], include_in_schema=False)
def painel_contatos(request: Request, tipo: str = Query("", max_length=20),
                    eu: dict = Depends(exigir_painel)):
    """Caixa de entrada de quem chegou por /anuncie e /escreva."""
    filtro = tipo if tipo in contatos.TIPOS else ""
    with SessionLocal() as session:
        consulta = select(Contato).order_by(desc(Contato.created_at)).limit(300)
        if filtro:
            consulta = consulta.where(Contato.tipo == filtro)
        linhas = session.scalars(consulta).all()
        por_tipo = dict(
            session.execute(
                select(Contato.tipo, func.count(Contato.id)).group_by(Contato.tipo)
            ).all()
        )
        itens = [
            {
                "tipo": c.tipo, "nome": c.nome, "empresa": c.empresa, "area": c.area,
                "cidade": c.cidade, "email": c.email, "telefone": c.telefone,
                "telefone_digitado": c.telefone_digitado, "link": c.link,
                "mensagem": c.mensagem, "quando": data_extenso(c.created_at),
            }
            for c in linhas
        ]
    return templates.TemplateResponse(
        "pb/contatos.html",
        {
            "request": request,
            "contatos": itens,
            "filtro": filtro,
            "totais": {
                "todos": sum(por_tipo.values()),
                "anuncio": por_tipo.get("anuncio", 0),
                "colaborador": por_tipo.get("colaborador", 0),
            },
            "assets_v": ASSETS_V,
            "jornal": jornal.JORNAL,
            "eu": eu,
        },
    )


# ------------------------------------------------------------------ equipe


@app.get("/painel/equipe", response_class=HTMLResponse,
         dependencies=[Depends(somente_painel)], include_in_schema=False)
def painel_equipe(request: Request, eu: dict = Depends(exigir_dono),
                  recado: str = Query("", max_length=200),
                  erro: str = Query("", max_length=200)):
    """Quem tem acesso ao painel. Só o dono abre."""
    with SessionLocal() as session:
        linhas = session.scalars(
            select(Usuario).order_by(Usuario.ativo.desc(), Usuario.criado_em)
        ).all()
        usuarios = [
            {
                "id": u.id,
                "nome": u.nome,
                "email": u.email,
                "papel": u.papel,
                "papel_rotulo": contas.PAPEIS.get(u.papel, u.papel),
                "ativo": u.ativo,
                "ultimo_acesso": data_extenso(u.ultimo_acesso_em) if u.ultimo_acesso_em else "nunca",
                "sessoes": contas.sessoes_ativas(u.id),
            }
            for u in linhas
        ]
    # A senha temporária vive UM pedido no Redis e some ao ser lida. Não vai
    # na URL nem fica no banco: quem tirar print da barra de endereço ou ler o
    # log do nginx depois não encontra nada.
    temporaria = None
    chave_1x = f"{jornal.SLUG}:senha1x:{eu['usuario_id']}"
    try:
        cru = redis.getdel(chave_1x)
        if cru:
            temporaria = json.loads(cru)
    except Exception:
        log.warning("não foi possível ler a senha temporária", exc_info=True)
    return templates.TemplateResponse(
        "pb/equipe.html",
        {
            "request": request,
            "usuarios": usuarios,
            "eu": {"id": eu["usuario_id"], "nome": eu["nome"], "papel": eu["papel"]},
            "senha_temporaria": temporaria,
            "recado": RECADOS_EQUIPE.get(recado, ""),
            "erro": ERROS_EQUIPE.get(erro, ""),
            "assets_v": ASSETS_V,
            "jornal": jornal.JORNAL,
        },
    )


def _guardar_senha_temporaria(dono_id: int, email: str, senha: str) -> None:
    try:
        redis.setex(
            f"{jornal.SLUG}:senha1x:{dono_id}", 300,
            json.dumps({"email": email, "senha": senha}),
        )
    except Exception:
        log.error("Redis fora: a senha temporária de %s se perdeu", email, exc_info=True)


# Texto de recado e de erro sai daqui, e nunca da query string. Deixar a
# mensagem viajar na URL permitiria montar um link que mostra o aviso que
# quiser para quem está logado, que é phishing com o domínio certo na barra.
RECADOS_EQUIPE = {
    "criada": "Conta criada. A senha temporária está logo acima.",
    "senha": "Senha redefinida. A temporária está logo acima.",
    "sessoes": "Sessões encerradas.",
    "ativada": "Conta reativada.",
    "desativada": "Conta desativada e sessões encerradas.",
}
ERROS_EQUIPE = {
    "existe": "Já existe uma conta com esse e-mail.",
    "email": "E-mail inválido.",
    "papel": "Papel inválido.",
    "senha_curta": "A senha gerada não passou na validação. Tente de novo.",
    "senha_fraca": "A senha gerada não passou na validação. Tente de novo.",
    "nao_encontrada": "Conta não encontrada.",
    "unico_dono": "Este é o único dono ativo. Promova outro antes de desativar.",
    "eu_mesmo": "Você não pode desativar a própria conta.",
    "falhou": "Não foi possível concluir a ação.",
}


def _voltar_equipe(recado: str = "", erro: str = ""):
    # PRG: a tela de equipe é recarregada por GET depois de cada ação, para
    # que F5 não repita a ação anterior.
    destino = "/painel/equipe"
    if recado:
        destino += f"?recado={quote(recado, safe='')}"
    elif erro:
        destino += f"?erro={quote(erro, safe='')}"
    return RedirectResponse(destino, status_code=303)


@app.post("/painel/equipe/criar", dependencies=[Depends(somente_painel)], include_in_schema=False)
def equipe_criar(request: Request, eu: dict = Depends(exigir_dono),
                 nome: str = Form("", max_length=160),
                 email: str = Form("", max_length=200),
                 papel: str = Form("editor", max_length=20)):
    senha = secrets.token_urlsafe(9)
    with SessionLocal() as session:
        try:
            usuario = contas.criar(
                session, nome=nome, email=email, senha=senha,
                papel=papel if papel in (contas.DONO, contas.EDITOR) else contas.EDITOR,
                precisa_trocar=True,
            )
        except contas.ErroConta as e:
            return _voltar_equipe(erro=e.codigo)
    _guardar_senha_temporaria(eu["usuario_id"], usuario.email, senha)
    return _voltar_equipe(recado="criada")


@app.post("/painel/equipe/senha", dependencies=[Depends(somente_painel)], include_in_schema=False)
def equipe_nova_senha(request: Request, eu: dict = Depends(exigir_dono),
                      id: int = Form(...)):
    senha = secrets.token_urlsafe(9)
    with SessionLocal() as session:
        usuario = session.get(Usuario, id)
        if not usuario:
            return _voltar_equipe(erro="nao_encontrada")
        usuario.senha_hash = contas.hash_senha(senha)
        usuario.precisa_trocar_senha = True
        usuario.senha_alterada_em = datetime.now(timezone.utc)
        session.commit()
        email = usuario.email
    # Senha nova derruba as sessões antigas; é justamente o caso de uso.
    contas.encerrar_todas(id)
    _guardar_senha_temporaria(eu["usuario_id"], email, senha)
    log.warning("senha redefinida por %s para %s", eu["email"], email)
    return _voltar_equipe(recado="senha")


@app.post("/painel/equipe/sessoes", dependencies=[Depends(somente_painel)], include_in_schema=False)
def equipe_encerrar_sessoes(request: Request, eu: dict = Depends(exigir_dono),
                            id: int = Form(...)):
    contas.encerrar_todas(id)
    return _voltar_equipe(recado="sessoes")


@app.post("/painel/equipe/estado", dependencies=[Depends(somente_painel)], include_in_schema=False)
def equipe_estado(request: Request, eu: dict = Depends(exigir_dono),
                  id: int = Form(...), ativo: str = Form("1")):
    ligar = ativo == "1"
    if not ligar and id == eu["usuario_id"]:
        return _voltar_equipe(erro="eu_mesmo")
    with SessionLocal() as session:
        usuario = session.get(Usuario, id)
        if not usuario:
            return _voltar_equipe(erro="nao_encontrada")
        # Sem esta trava o jornal pode ficar sem nenhum dono ativo, e aí não
        # existe mais ninguém capaz de reabrir esta tela.
        if not ligar and usuario.papel == contas.DONO:
            donos = session.scalar(
                select(func.count(Usuario.id)).where(
                    Usuario.papel == contas.DONO, Usuario.ativo.is_(True)
                )
            ) or 0
            if donos <= 1:
                return _voltar_equipe(erro="unico_dono")
        usuario.ativo = ligar
        session.commit()
        email = usuario.email
    if not ligar:
        contas.encerrar_todas(id)
    log.warning("conta %s por %s: %s", "reativada" if ligar else "desativada", eu["email"], email)
    return _voltar_equipe(recado="ativada" if ligar else "desativada")


@app.get("/api/dashboard", dependencies=[Depends(somente_painel), Depends(exigir_painel_api)])
def dashboard(limit: int = Query(50, ge=1, le=200)):
    with SessionLocal() as session:
        artigos = session.execute(
            select(Article, Source.name).join(Source).order_by(RECENCIA).limit(limit)
        ).all()
        fontes = session.scalars(select(Source).order_by(Source.name)).all()
        runs = session.scalars(select(CrawlRun).order_by(desc(CrawlRun.id)).limit(10)).all()
        return {
            "stats": {
                "articles": session.scalar(select(func.count(Article.id))) or 0,
                "articles_with_municipality": session.scalar(
                    select(func.count(func.distinct(ArticleMunicipality.article_id)))
                ) or 0,
                "municipalities": session.scalar(select(func.count(Municipality.id))) or 0,
                "sources": len(fontes),
                "healthy_sources": sum(1 for s in fontes if not s.last_error),
                "last_run": runs[0].finished_at.isoformat()
                if runs and runs[0].finished_at
                else None,
            },
            "articles": [
                {
                    "id": a.id,
                    "title": a.title,
                    "url": a.canonical_url,
                    "source": nome,
                    "category": a.category,
                    "layer": a.layer,
                    "status": a.status,
                    "published_at": a.published_at.isoformat() if a.published_at else None,
                }
                for a, nome in artigos
            ],
            "sources": [
                {
                    "id": s.id,
                    "name": s.name,
                    "slug": s.slug,
                    "content_policy": s.content_policy,
                    "enabled": s.enabled,
                    "last_success_at": s.last_success_at.isoformat()
                    if s.last_success_at
                    else None,
                    "last_error": s.last_error,
                }
                for s in fontes
            ],
            "runs": [
                {
                    "id": r.id,
                    "status": r.status,
                    "started_at": r.started_at.isoformat(),
                    "items_seen": r.items_seen,
                    "items_new": r.items_new,
                    "sources_failed": r.sources_failed,
                }
                for r in runs
            ],
        }


@app.get("/api/social", dependencies=[Depends(somente_painel), Depends(exigir_painel_api)])
def social_status(limit: int = Query(30, ge=1, le=100)):
    """Estado da máquina de publicação e as últimas peças que ela montou."""
    with SessionLocal() as session:
        estado = publicador.situacao(session)
        linhas = session.execute(
            select(SocialPost, Article.title, Article.slug)
            .join(Article, Article.id == SocialPost.article_id)
            .order_by(desc(SocialPost.id))
            .limit(limit)
        ).all()
        estado["posts"] = [
            {
                "id": p.id,
                "network": p.network,
                "status": p.status,
                "title": titulo,
                "card": f"/cartao/{slug}.jpg" if slug else None,
                "caption": p.caption,
                "permalink": p.permalink,
                "error": p.error,
                "published_at": p.published_at.isoformat() if p.published_at else None,
            }
            for p, titulo, slug in linhas
        ]
        return estado


@app.get("/api/redator", dependencies=[Depends(somente_painel), Depends(exigir_painel_api)])
def redator_status(limit: int = Query(12, ge=1, le=60)):
    """Estado da reescrita: aproveitamento, gasto e as últimas convertidas."""
    with SessionLocal() as session:
        estado = redator.situacao(session)
        recentes = session.scalars(
            select(Article)
            .where(Article.layer == "own")
            .order_by(desc(Article.rewritten_at))
            .limit(limit)
        ).all()
        estado["recentes"] = [
            {
                "id": a.id,
                "titulo_origem": a.title,
                "titulo_proprio": a.own_title,
                "olho": a.own_summary,
                "url": f"/materia/{a.slug}" if a.slug else None,
                "fonte_url": a.canonical_url,
                "quando": a.rewritten_at.isoformat() if a.rewritten_at else None,
            }
            for a in recentes
        ]
        return estado


@app.post("/api/redator/rodar", dependencies=[Depends(somente_painel), Depends(exigir_painel_api)], status_code=202)
def redator_rodar(quantas: int = Query(1, ge=1, le=10)):
    """Reescreve agora, sem esperar o ciclo do worker. Respeita o teto do dia.

    Devolve o motivo quando não faz nada: um botão que falha em silêncio é
    pior do que um botão que não existe.
    """
    from . import ia

    if not ia.disponivel():
        return {"tentadas": 0, "convertidas": 0, "motivo": "falta a chave da API"}
    with SessionLocal() as session:
        if redator._hoje(session) >= redator.IA_TETO_DIA:
            return {"tentadas": 0, "convertidas": 0, "motivo": "teto do dia atingido"}
        convertidas, tentadas, erros = 0, 0, []
        for artigo in redator.fila(session, quantas):
            tentadas += 1
            if redator.reescrever(session, artigo):
                convertidas += 1
            elif artigo.rewrite_error:
                erros.append(artigo.rewrite_error)
        motivo = "" if convertidas else (erros[0] if erros else "nada elegível na fila")
        return {"tentadas": tentadas, "convertidas": convertidas, "motivo": motivo}


@app.get("/api/social/diagnostico", dependencies=[Depends(somente_painel), Depends(exigir_painel_api)])
def social_diagnostico():
    """Diz exatamente o que falta configurar, sem precisar tentar publicar."""
    return {"meta": social.diagnostico(), "limite_24h": social.limite_publicacao()}


@app.post("/api/social/publicar", dependencies=[Depends(somente_painel), Depends(exigir_painel_api)], status_code=202)
def social_publicar(rede: str = Query("instagram")):
    """Dispara uma publicação agora, ignorando o intervalo mas não o teto.

    Serve para o primeiro teste com credencial nova: em vez de esperar a
    próxima janela do worker, o operador vê o resultado na hora.
    """
    if rede not in publicador.REDES:
        raise HTTPException(status_code=400, detail="rede inválida")
    with SessionLocal() as session:
        registro = publicador.publicar_uma(session, rede, forcar_intervalo=True)
        if registro is None:
            return {"publicado": False, "motivo": publicador.situacao(session)["redes"][rede]["motivo"]}
        return {
            "publicado": registro.status in ("published", "dry"),
            "status": registro.status,
            "external_id": registro.external_id,
            "permalink": registro.permalink,
            "error": registro.error,
        }


@app.post("/api/crawl", dependencies=[Depends(somente_painel), Depends(exigir_painel_api)], status_code=202)
def request_crawl():
    redis.rpush(FILA_COLETA, datetime.now(timezone.utc).isoformat())
    return {"queued": True}


@app.exception_handler(404)
async def nao_encontrado(request: Request, _exc):
    if request.url.path.startswith("/api/"):
        return Response(
            content='{"detail":"não encontrado"}', status_code=404,
            media_type="application/json",
        )
    with SessionLocal() as session:
        contexto = _contexto(request, session, "")
    contexto.update(
        artigos=[], h1="Página não encontrada", chapeu="Erro 404", subtitulo="",
        descricao_pagina="Página não encontrada no Paraibão.",
        pagina=1, tem_mais=False, caminho_base="/", vizinhos=[], vizinhos_titulo="",
        vazio_titulo="Essa página não existe",
        vazio_texto="Use a busca acima ou veja as últimas notícias.",
    )
    return templates.TemplateResponse("pb/lista.html", contexto, status_code=404)
