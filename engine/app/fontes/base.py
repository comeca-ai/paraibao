"""Contrato único de extração.

Toda fonte, independente do método, devolve a mesma coisa: uma lista de
ArtigoCandidato. É isso que permite trocar RSS por API numa fonte sem tocar em
nada do resto do sistema.

A regra que organiza este módulo é a escada: cada adaptador usa o degrau mais
barato e mais estável que aquela fonte oferece, e nunca sobe além do
necessário. Ver docs/EXTRACAO.md.
"""

import html as htmlmod
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from dateutil import parser as date_parser


RASTREIO = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|igshid$)", re.I)

# Imagem que não é imagem de matéria.
IMAGEM_RUIM = re.compile(
    r"(\.svg($|\?)|^data:|/logo|logo\.|placeholder|avatar|blank|spacer|spinner|"
    r"default[-_]?(image|thumb))",
    re.I,
)

# Ruído que os portais grudam no fim do texto.
RUIDO_TEXTO = re.compile(
    r"(leia (também|mais)|veja também|compartilhe|siga o|receba as notícias|"
    r"clique aqui e|entre no nosso grupo|assine a newsletter)\b.*",
    re.I | re.S,
)


@dataclass
class ArtigoCandidato:
    """O que toda fonte entrega, seja por API, RSS ou HTML."""

    fonte_slug: str
    titulo: str
    url: str
    external_id: str | None = None
    imagem: str | None = None
    resumo: str = ""
    corpo: str | None = None
    autor: str | None = None
    publicado_em: datetime | None = None
    secao: str | None = None
    # Preenchido pelo enriquecimento quando o degrau principal não trouxe tudo.
    faltando: list[str] = field(default_factory=list)

    def valido(self) -> bool:
        return bool(self.titulo and len(self.titulo) > 12 and self.url.startswith("http"))


def limpar_texto(valor) -> str:
    """Tira HTML, decodifica entidade e normaliza espaço.

    Aceita qualquer tipo de propósito: API de terceiro devolve int, None e
    dict onde a documentação promete string. Converter aqui é mais barato que
    espalhar isinstance por todo adaptador.
    """
    if valor is None or valor == "":
        return ""
    valor = valor if isinstance(valor, str) else str(valor)
    texto = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", valor, flags=re.I | re.S)
    texto = re.sub(r"<figcaption[^>]*>.*?</figcaption>", " ", texto, flags=re.I | re.S)
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = htmlmod.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def limpar_corpo(valor: str | None) -> str | None:
    """Além de limpar, corta o rodapé de 'leia também' e afins."""
    texto = limpar_texto(valor)
    if not texto:
        return None
    texto = RUIDO_TEXTO.sub("", texto).strip()
    return texto or None


def normalizar_url(valor: str) -> str:
    """Absoluta, sem parâmetro de rastreio, sem âncora, sem barra final."""
    partes = urlsplit(valor.strip())
    query = [(k, v) for k, v in parse_qsl(partes.query) if not RASTREIO.match(k)]
    return urlunsplit(
        (
            partes.scheme.lower(),
            partes.netloc.lower(),
            partes.path.rstrip("/") or "/",
            urlencode(query),
            "",
        )
    )


def imagem_valida(url: str | None) -> str | None:
    if not url or not url.startswith("http") or IMAGEM_RUIM.search(url):
        return None
    return url


def parse_data(valor) -> datetime | None:
    if not valor:
        return None
    if isinstance(valor, datetime):
        dt = valor
    else:
        try:
            dt = date_parser.parse(str(valor))
        except (ValueError, TypeError, OverflowError):
            return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def resumo_de(corpo: str | None, resumo_bruto: str | None, limite: int = 320) -> str:
    """Prefere o começo do corpo ao resumo do feed.

    O <description> de vários portais vem contaminado com legenda de foto e
    nome do veículo antes do texto. O primeiro parágrafo do corpo é mais
    confiável do que o campo que deveria ser o resumo.
    """
    base = corpo or limpar_texto(resumo_bruto)
    if not base:
        return ""
    if len(base) <= limite:
        return base
    corte = base[:limite]
    ponto = corte.rfind(". ")
    return (corte[: ponto + 1] if ponto > limite * 0.5 else corte.rstrip()) + ""
