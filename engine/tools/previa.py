"""Prévia local dos templates, sem banco e sem Docker.

Por que existe: este Mac não tem mais Docker (formatação de julho), e subir
Postgres + Redis só para olhar um ajuste de CSS custa mais do que o ajuste.
Aqui os templates são renderizados com o MESMO formato de contexto que
`app/main.py` monta, só que alimentado por conteúdo real raspado da produção.

Uso:
    .venv-previa/bin/python tools/previa.py            # renderiza e serve em :4173
    .venv-previa/bin/python tools/previa.py --recolher # rebusca o conteúdo da prod

O conteúdo fica em cache em tools/_previa/dados.json, então a segunda rodada
não bate na produção. As imagens continuam apontando para o proxy da prod, que
é o que faz a página parecer com a página.

Isto é ferramenta de desenvolvimento. Não entra no container e não é servido.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import re
import socketserver
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "tools" / "_previa"
CACHE = SAIDA / "dados.json"
PROD = "https://paraibao.com.br"

sys.path.insert(0, str(RAIZ))


# ----------------------------------------------------------------- coleta

def _absoluto(url: str | None) -> str | None:
    """A prévia roda em localhost, então caminho relativo de imagem morre."""
    if not url:
        return None
    if url.startswith("http"):
        return url
    return PROD + url


def recolher() -> dict:
    """Raspa a home e uma matéria da produção para servir de conteúdo real."""
    cli = httpx.Client(timeout=30, follow_redirects=True,
                       headers={"user-agent": "paraibao-previa/1.0"})

    sopa = BeautifulSoup(cli.get(PROD).text, "html.parser")
    artigos: list[dict] = []
    vistos: set[str] = set()

    # O destaque do mega-menu também é um <article>, e o rótulo dele é
    # "Agora", não uma editoria. Colhido junto, ele entra na prévia como
    # manchete com chapéu errado.
    for art in sopa.select("article"):
        if art.find_parent(class_=["mega", "agora"]):
            continue
        h = art.find(["h1", "h2", "h3"])
        a = h.find("a") if h else None
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if href in vistos or not href.startswith("/materia/"):
            continue
        vistos.add(href)

        img = art.find("img")
        rot = art.select_one(".rotulo")
        resumo = art.select_one(".resumo, .dobra__olho")
        tempo = art.select_one(".tempo")
        artigos.append({
            "titulo": h.get_text(" ", strip=True),
            "url": href,
            "imagem": _absoluto(img.get("src")) if img else None,
            "editoria": rot.get_text(strip=True).title() if rot else "Geral",
            "resumo": resumo.get_text(" ", strip=True) if resumo else "",
            "tempo": tempo.get_text(" ", strip=True) if tempo else "há 1 hora",
        })

    # Duas matérias, porque o template tem dois modos e eles não se parecem:
    # `citacao` mostra um trecho curto e o convite para a origem; `proprio`
    # mostra o texto inteiro. Olhar só um esconde metade do problema de leitura.
    def ler(url: str) -> dict | None:
        m = BeautifulSoup(cli.get(PROD + url).text, "html.parser")
        corpo = m.select_one(".texto")
        if not corpo:
            return None
        paras = [p.get_text(" ", strip=True)
                 for p in corpo.find_all("p")
                 if "materia__origem" not in (p.parent.get("class") or [])
                 and "materia__apuracao" not in (p.get("class") or [])]
        figura = m.select_one(".materia__figura img")
        h1 = m.find("h1")
        olho = m.select_one(".materia__olho")
        return {
            "titulo": h1.get_text(" ", strip=True) if h1 else "",
            "resumo": olho.get_text(" ", strip=True) if olho else "",
            "imagem": _absoluto(figura.get("src")) if figura else None,
            "paragrafos": [p for p in paras if len(p) > 60][:14],
            "url": url,
            "propria": bool(m.select_one(".materia__apuracao")),
        }

    lidas = [x for x in (ler(a["url"]) for a in artigos[:10]) if x and x["paragrafos"]]
    materia = next((x for x in lidas if len(x["paragrafos"]) <= 2),
                   lidas[0] if lidas else {})

    # A camada própria quase não tem matéria no ar, e uma matéria de um
    # parágrafo não testa leitura de texto corrido. Para a PRÉVIA, o corpo
    # longo é a costura dos parágrafos reais das outras: é prosa de notícia
    # de verdade, no comprimento que a página precisa exibir. Não é conteúdo
    # publicável, e não sai daqui.
    longa = max(lidas, key=lambda x: len(x["paragrafos"]), default={})
    if longa:
        longa = {**longa, "paragrafos": [p for x in lidas for p in x["paragrafos"]][:12]}

    cli.close()
    dados = {"artigos": artigos, "materia": materia, "materia_longa": longa,
             "colhido_em": datetime.now(timezone.utc).isoformat()}
    SAIDA.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(dados, ensure_ascii=False, indent=1))
    print(f"colhidos {len(artigos)} artigos · corpo longo com "
          f"{len(longa.get('paragrafos', []))} parágrafos")
    return dados


# ------------------------------------------------------------- contexto

EDITORIAS = [
    ("politica", "Política"), ("seguranca", "Segurança"), ("cidades", "Cidades"),
    ("economia", "Economia"), ("esportes", "Esportes"), ("educacao", "Educação"),
    ("saude", "Saúde"), ("cultura", "Cultura"), ("geral", "Geral"),
]
CIDADES = [("joao-pessoa", "João Pessoa"), ("campina-grande", "Campina Grande"),
           ("patos", "Patos"), ("sousa", "Sousa")]


def _hash(caminho: Path) -> str:
    try:
        return hashlib.sha256(caminho.read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


def _artigo(bruto: dict, i: int) -> dict:
    """Mesmo dicionário que `app/views.py:artigo_view` devolve."""
    slug = re.sub(r"[^a-z0-9]+", "-", bruto["titulo"].lower())[:60].strip("-")
    return {
        "id": i,
        "titulo": bruto["titulo"],
        "resumo": bruto.get("resumo", ""),
        "propria": False,
        "imagem": bruto.get("imagem"),
        "imagem_grande": bruto.get("imagem"),
        "imagem_original": bruto.get("imagem"),
        "url": bruto.get("url") or f"/materia/{slug}",
        "url_origem": "https://portalcorreio.com.br/exemplo",
        "externo": False,
        "alvo": Markup(""),
        "seta": Markup(""),
        "editoria": bruto.get("editoria", "Geral"),
        "editoria_slug": re.sub(r"[^a-z]", "", bruto.get("editoria", "geral").lower()),
        "fonte": ["Portal Correio", "ClickPB", "G1 Paraíba", "Jornal da Paraíba"][i % 4],
        "municipio": ["João Pessoa", "Campina Grande", None, "Patos"][i % 4],
        "municipio_slug": ["joao-pessoa", "campina-grande", None, "patos"][i % 4],
        "tempo": bruto.get("tempo", "há 1 hora"),
        "hora": (datetime.now(timezone.utc) - timedelta(hours=i)).strftime("%Hh%M"),
        "data": datetime.now(timezone.utc) - timedelta(hours=i),
    }


def _base(caminho: str, artigos: list[dict]) -> dict:
    contagens = {}
    for a in artigos:
        contagens[a["editoria_slug"]] = contagens.get(a["editoria_slug"], 0) + 1
    eds = [{"slug": s, "rotulo": r, "total": contagens.get(s, 3 + i)}
           for i, (s, r) in enumerate(EDITORIAS) if s != "geral"]
    # Painéis do hover: no app são as N mais recentes de cada editoria. Aqui
    # o conteúdo colhido é fatiado por editoria, e o que sobrar vazio é
    # completado para o painel não abrir com duas chamadas.
    paineis: dict[str, list[dict]] = {}
    for i, (slug, _) in enumerate(EDITORIAS):
        if slug == "geral":
            continue
        fatia = [a for a in artigos if a["editoria_slug"] == slug]
        if len(fatia) < 7:
            fatia = (fatia + artigos[i:] + artigos)[:7]
        paineis[slug] = fatia[:7]

    return {
        "request": None,
        "base_url": PROD,
        "caminho": caminho,
        "editorias_nav": [{"slug": s, "rotulo": r} for s, r in EDITORIAS if s != "geral"],
        "paineis": paineis,
        "secao": "",
        "q": None,
        "ano": datetime.now().year,
        "menu_editorias_a": eds[:5],
        "menu_editorias_b": eds[5:10],
        "menu_cidades": [{"slug": s, "nome": n} for s, n in CIDADES],
        "destaque_menu": None,
        "canonical_externo": None,
        # Versão pelo hash do arquivo, igual ao app faz. Com valor fixo o
        # navegador segura o CSS antigo e a prévia mostra o design de antes
        # da última edição, o que já me fez perseguir bug que não existia.
        "assets_v": {"css": _hash(RAIZ / "app" / "static" / "styles.css"),
                     "fontes": _hash(RAIZ / "app" / "static" / "fontes.css")},
    }


def montar(dados: dict) -> dict[str, dict]:
    arts = [_artigo(b, i) for i, b in enumerate(dados["artigos"])]
    if len(arts) < 20:  # repete para encher a grade quando a prod estiver magra
        arts = (arts * 4)[:20]
        for i, a in enumerate(arts):
            a["id"] = i

    home = _base("/", arts)
    home.update(
        ticker=arts[:6], manchete=arts[0], chamadas=arts[1:5],
        em_alta=arts[5:9], ultimas_destaque=arts[9], ultimas=arts[10:14],
        jogo=None,
        coluna_video={"titulo": "Em vídeo e imagem", "href": "/ultimas", "artigos": arts[14:17]},
        tabela={"titulo": "Municípios na semana", "href": "/municipios",
                "colunas": ["Município", "Notícias"],
                "linhas": [{"nome": n, "valores": [40 - i * 6]} for i, (_, n) in enumerate(CIDADES)]},
        por_municipio=arts[17:21] or arts[:4],
        destaque_menu=arts[0],
    )

    lista = _base("/editoria/politica", arts)
    lista.update(
        artigos=arts[:16], h1="Política", chapeu="Paraíba", subtitulo="",
        descricao_pagina="Notícias de política da Paraíba.",
        pagina=1, tem_mais=True, caminho_base="/editoria/politica",
        vazio_titulo="", vazio_texto="", vizinhos=[], vizinhos_titulo="",
    )

    def pagina_materia(bruta: dict, modo: str) -> dict:
        art = _artigo({**(dados["artigos"][0] if dados["artigos"] else {}),
                       "titulo": bruta.get("titulo") or "Sem título",
                       "resumo": bruta.get("resumo", ""),
                       "imagem": bruta.get("imagem")}, 0)
        art["data_extenso"] = "3 de agosto de 2026 · 11h02"
        art["autor"] = "Redação"
        art["fonte_slug"] = "portal-correio"
        art["propria"] = modo != "citacao"
        ctx = _base(art["url"], arts)
        ctx.update(
            a=art, modo=modo,
            paragrafos=bruta.get("paragrafos") or ["Conteúdo de exemplo."],
            relacionadas=arts[1:5],
            credito_imagem=f"Foto: divulgação / {art['fonte']}",
            url_absoluta=PROD + art["url"],
            compartilhar_wa="exemplo",
            jsonld="{}",
        )
        return ctx

    municipios = _base("/municipios", arts)
    municipios.update(
        com_noticia=221,
        regioes=[{"nome": nome,
                  "municipios": [{"slug": s, "name": n, "total": (7 - i) if i < 3 else 0}
                                 for i, (s, n) in enumerate(CIDADES * 4)]}
                 for nome in ["Mata Paraibana", "Agreste Paraibano", "Borborema", "Sertão Paraibano"]],
    )

    return {
        "index.html": ("pb/home.html", home),
        "municipios.html": ("pb/municipios.html", municipios),
        "materia.html": ("pb/materia.html",
                         pagina_materia(dados.get("materia") or {}, "citacao")),
        "materia-propria.html": ("pb/materia.html",
                                 pagina_materia(dados.get("materia_longa") or {}, "proprio")),
        "lista.html": ("pb/lista.html", lista),
    }


# -------------------------------------------------------------- render

def render(dados: dict) -> None:
    env = Environment(
        loader=FileSystemLoader(str(RAIZ / "app" / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    SAIDA.mkdir(parents=True, exist_ok=True)
    for arquivo, (template, ctx) in montar(dados).items():
        html = env.get_template(template).render(**ctx)
        # Na prévia o CSS é servido da própria pasta, não pela rota /static.
        html = html.replace('href="/static/', 'href="static/').replace('src="/static/', 'src="static/')
        (SAIDA / arquivo).write_text(html)
        print(f"  {arquivo}")

    destino = SAIDA / "static"
    if destino.exists() or destino.is_symlink():
        destino.unlink() if destino.is_symlink() else None
    if not destino.exists():
        destino.symlink_to(RAIZ / "app" / "static")


def servir(porta: int) -> None:
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(SAIDA))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", porta), handler) as s:
        print(f"prévia em http://127.0.0.1:{porta}/  (ctrl+c para sair)")
        s.serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--recolher", action="store_true", help="rebusca conteúdo da produção")
    p.add_argument("--porta", type=int, default=4173)
    p.add_argument("--so-render", action="store_true", help="renderiza e sai, sem servir")
    args = p.parse_args()

    dados = recolher() if (args.recolher or not CACHE.exists()) else json.loads(CACHE.read_text())
    render(dados)
    if not args.so_render:
        servir(args.porta)
