"""Cliente da Graph API para publicar no Instagram e na Página do Facebook.

Isto usa a API oficial de publicação de conteúdo da Meta, não automação de
navegador. É o caminho previsto pela plataforma: não há risco de bloqueio por
"comportamento automatizado", desde que os limites sejam respeitados.

Três coisas que a API impõe e que moldam o resto do código:

  1. O Instagram publica em DOIS passos: primeiro cria um "container" com a
     URL da imagem, depois publica o container. Entre um e outro a Meta baixa
     a imagem do nosso servidor — ou seja, a URL do card tem que estar pública
     e acessível de fora ANTES de o container ser criado.

  2. O container pode não ficar pronto na hora. Publicar sem checar o estado
     devolve erro; por isso o polling em `_aguardar_container`.

  3. O teto é de 25 publicações por conta do Instagram a cada 24 horas, e ele
     é contado pela Meta, não por nós. `limite_publicacao()` lê o consumo real
     para a máquina nunca bater a parede sem saber.

Legenda de feed do Instagram NÃO aceita link clicável — é regra do produto e
não tem contorno por API. Por isso o tráfego para o site vem da Página do
Facebook (que aceita link), do link da bio e, na etapa seguinte, da resposta
automática em comentário. Ver docs/INSTAGRAM.md.
"""

import logging
import time

import httpx

from .config import (
    FB_PAGE_ID,
    IG_USER_ID,
    META_ACCESS_TOKEN,
    META_API_VERSION,
)

log = logging.getLogger("paraibao.social")

TIMEOUT = 60
# Teto da própria Meta para publicação por API. Não é escolha editorial.
TETO_META_24H = 25


class ErroMeta(Exception):
    """Erro devolvido pela Graph API, já com a mensagem legível."""


def _base() -> str:
    return f"https://graph.facebook.com/{META_API_VERSION}"


def configurado() -> dict:
    """O que falta para a máquina poder publicar. Vai para o painel."""
    return {
        "token": bool(META_ACCESS_TOKEN),
        "instagram": bool(IG_USER_ID),
        "facebook": bool(FB_PAGE_ID),
    }


def _chamar(metodo: str, caminho: str, dados: dict) -> dict:
    dados = {**dados, "access_token": META_ACCESS_TOKEN}
    with httpx.Client(timeout=TIMEOUT) as client:
        if metodo == "GET":
            r = client.get(f"{_base()}/{caminho}", params=dados)
        else:
            r = client.post(f"{_base()}/{caminho}", data=dados)
    try:
        corpo = r.json()
    except Exception:
        raise ErroMeta(f"resposta ilegível ({r.status_code}): {r.text[:200]}")
    if "error" in corpo:
        erro = corpo["error"]
        raise ErroMeta(
            f"{erro.get('type', 'erro')} {erro.get('code', '')}: "
            f"{erro.get('message', '')}"[:400]
        )
    if r.status_code >= 400:
        raise ErroMeta(f"HTTP {r.status_code}: {r.text[:200]}")
    return corpo


def _aguardar_container(container_id: str, tentativas: int = 12) -> None:
    """A Meta baixa e processa a imagem de forma assíncrona.

    Publicar antes de `FINISHED` devolve erro. O tempo normal é de 1 a 3
    segundos; o limite aqui dá quase um minuto antes de desistir.
    """
    for tentativa in range(tentativas):
        estado = _chamar("GET", container_id, {"fields": "status_code,status"})
        codigo = estado.get("status_code")
        if codigo == "FINISHED":
            return
        if codigo in ("ERROR", "EXPIRED"):
            raise ErroMeta(f"container {codigo}: {estado.get('status', '')}"[:300])
        time.sleep(min(2 + tentativa, 8))
    raise ErroMeta("container não ficou pronto a tempo")


def publicar_instagram(image_url: str, legenda: str) -> dict:
    """Publica no feed. Devolve {'id', 'permalink'}."""
    if not (META_ACCESS_TOKEN and IG_USER_ID):
        raise ErroMeta("credenciais do Instagram ausentes")

    container = _chamar(
        "POST", f"{IG_USER_ID}/media", {"image_url": image_url, "caption": legenda}
    )
    container_id = container.get("id")
    if not container_id:
        raise ErroMeta(f"container sem id: {container}")

    _aguardar_container(container_id)
    publicado = _chamar("POST", f"{IG_USER_ID}/media_publish", {"creation_id": container_id})
    media_id = publicado.get("id")

    permalink = None
    try:
        permalink = _chamar("GET", media_id, {"fields": "permalink"}).get("permalink")
    except ErroMeta:
        pass  # o post já está no ar; o link é conveniência do painel
    return {"id": media_id, "permalink": permalink}


def publicar_facebook(mensagem: str, link: str) -> dict:
    """Publica na Página com link clicável.

    O preview vem das meta tags Open Graph da própria matéria, que já existem
    em /materia/{slug}. Não mandamos imagem aqui: mandar `picture` junto com
    `link` faz a Meta ignorar um dos dois e o resultado fica imprevisível.
    """
    if not (META_ACCESS_TOKEN and FB_PAGE_ID):
        raise ErroMeta("credenciais do Facebook ausentes")

    resposta = _chamar("POST", f"{FB_PAGE_ID}/feed", {"message": mensagem, "link": link})
    post_id = resposta.get("id", "")
    return {
        "id": post_id,
        "permalink": f"https://www.facebook.com/{post_id.replace('_', '/posts/')}"
        if "_" in post_id
        else None,
    }


def limite_publicacao() -> dict | None:
    """Consumo real do teto de 24h, contado pela Meta."""
    if not (META_ACCESS_TOKEN and IG_USER_ID):
        return None
    try:
        dados = _chamar(
            "GET", f"{IG_USER_ID}/content_publishing_limit", {"fields": "quota_usage,config"}
        )
        item = (dados.get("data") or [{}])[0]
        return {
            "usado": item.get("quota_usage", 0),
            "teto": (item.get("config") or {}).get("quota_total", TETO_META_24H),
        }
    except ErroMeta as exc:
        log.warning("limite de publicação indisponível: %s", exc)
        return None


def diagnostico() -> dict:
    """Checa token, conta do Instagram e Página. Usado pelo painel para dizer
    exatamente o que está faltando, em vez de só falhar na hora de publicar."""
    resultado = {"token": None, "instagram": None, "facebook": None}
    if not META_ACCESS_TOKEN:
        resultado["token"] = "ausente"
        return resultado
    try:
        _chamar("GET", "me", {"fields": "id"})
        resultado["token"] = "ok"
    except ErroMeta as exc:
        resultado["token"] = str(exc)
        return resultado
    for chave, ident, campos in (
        ("instagram", IG_USER_ID, "username,followers_count"),
        ("facebook", FB_PAGE_ID, "name,fan_count"),
    ):
        if not ident:
            resultado[chave] = "id não configurado"
            continue
        try:
            resultado[chave] = _chamar("GET", ident, {"fields": campos})
        except ErroMeta as exc:
            resultado[chave] = str(exc)
    return resultado
