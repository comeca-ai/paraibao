"""Proxy e cache de imagem.

A home traz ~22 fotos vindas de 5 domínios diferentes (paraibaonline, maispb,
portalcorreio, files.clickpb, cdn.jornaldaparaiba). Cada domínio novo custa
DNS + handshake TLS + latência do CDN alheio, e o navegador não consegue
multiplexar entre eles. É por isso que o HTML chega em 260ms e a página parece
lenta mesmo assim.

Servindo tudo pelo nosso domínio:
  - uma conexão só, multiplexada por HTTP/2
  - cache em disco: a segunda visita não sai da VM
  - redimensionamento: foto de jornal vem em 1280px e o card usa 400px
  - para de consumir a banda do CDN de terceiro a cada visita

A URL é assinada com HMAC. Sem isso, qualquer um usaria o servidor como proxy
aberto para servir qualquer imagem da internet.
"""

import hashlib
import hmac
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from . import jornal

import httpx

from .config import IMAGE_SECRET

log = logging.getLogger("paraibao.imagens")

# Volume Docker, não /tmp do container: sem isso o cache é apagado a cada
# deploy e toda imagem é rebaixada do zero.
CACHE = Path(os.getenv("IMAGE_CACHE_DIR", "/var/cache/paraibao-img"))
CACHE.mkdir(parents=True, exist_ok=True)

# Só busca imagem de domínio de fonte conhecida. Vale como segunda barreira
# caso a assinatura vaze.
# Derivada do perfil do jornal: fonte nova entra sozinha, e os subdomínios
# de CDN vêm de `dominios_imagem_extra`. Ver app/jornal.py.
DOMINIOS = set(jornal.dominios_de_imagem())

LARGURAS = {400, 800, 1200}
MAX_BYTES = 8 * 1024 * 1024
TIPOS_OK = ("image/jpeg", "image/png", "image/webp", "image/avif", "image/gif")


def assinar(url: str, largura: int) -> str:
    return hmac.new(
        IMAGE_SECRET.encode(), f"{url}|{largura}".encode(), hashlib.sha256
    ).hexdigest()[:16]


def caminho_proxy(url: str | None, largura: int = 800) -> str | None:
    """Devolve a URL interna da imagem, ou None se ela não deve passar."""
    if not url or not url.startswith("http"):
        return None
    host = urlparse(url).netloc.lower()
    if host not in DOMINIOS:
        return url  # domínio desconhecido: serve direto, sem proxy
    from urllib.parse import quote

    return f"/img/{largura}/{assinar(url, largura)}?u={quote(url, safe='')}"


def _arquivo(url: str, largura: int) -> Path:
    chave = hashlib.sha256(f"{url}|{largura}".encode()).hexdigest()
    return CACHE / chave[:2] / f"{chave}.bin"


def buscar(url: str, largura: int) -> tuple[bytes, str] | None:
    """Devolve (conteúdo, content-type). Usa o cache em disco quando existir."""
    destino = _arquivo(url, largura)
    tipo_arq = destino.with_suffix(".type")
    if destino.exists() and tipo_arq.exists():
        return destino.read_bytes(), tipo_arq.read_text().strip()

    host = urlparse(url).netloc.lower()
    if host not in DOMINIOS:
        return None

    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            r = client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; ParaibaoBot/1.0; +https://paraibao.com.br)",
                    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                    "Referer": f"https://{host}/",
                },
            )
            r.raise_for_status()
    except Exception as exc:
        log.warning("imagem falhou %s: %s", url[:70], str(exc)[:80])
        return None

    tipo = r.headers.get("content-type", "").split(";")[0].strip().lower()
    if not tipo.startswith("image/") or len(r.content) > MAX_BYTES:
        return None

    conteudo = _redimensionar(r.content, largura) or r.content
    if conteudo is not r.content:
        tipo = "image/jpeg"

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(conteudo)
    tipo_arq.write_text(tipo)
    return conteudo, tipo


def _redimensionar(bruto: bytes, largura: int) -> bytes | None:
    """Foto de jornal vem em 1280px+ e o card usa 400px. Sem isso o leitor
    baixa 4x mais byte do que a tela mostra. Falha em silêncio: se não der
    para processar, serve o original."""
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(bruto))
        if img.width <= largura:
            return None
        altura = round(img.height * largura / img.width)
        img = img.convert("RGB").resize((largura, altura), Image.LANCZOS)
        saida = io.BytesIO()
        img.save(saida, format="JPEG", quality=82, optimize=True, progressive=True)
        return saida.getvalue()
    except Exception:
        return None
