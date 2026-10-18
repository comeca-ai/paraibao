"""Perfil do jornal: tudo que muda quando o motor roda para outra praça.

Este módulo existe por um motivo só: até aqui, "Paraíba" estava cravada no
código em dezenas de pontos, e um motor com o nome do primeiro cliente dentro
não é um produto, é aquele cliente. Nada abaixo desta linha deveria saber em
qual estado está rodando.

**Como se abre uma praça nova:** cria-se `app/perfis/<slug>.json` com
identidade, território, fontes e cidades da previsão, e sobe-se o serviço com
`JORNAL_PERFIL=<slug>`. Nenhum arquivo `.py` é tocado.

O perfil é carregado uma vez, na importação. Ele não muda em execução: um
processo serve um jornal. Isso é deliberado e combina com o modelo de
implantação (um contêiner por praça), onde o isolamento é físico e não
depende de nenhum filtro em consulta que alguém possa esquecer de escrever.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PERFIS = Path(__file__).parent / "perfis"
SLUG = os.getenv("JORNAL_PERFIL", "pb").strip().lower()


@dataclass(frozen=True)
class Identidade:
    nome: str
    dominio: str
    base_url: str
    slogan: str
    email: str
    hashtag: str
    cor_marca: str
    praca: str
    user_agent: str
    # Arte da praça. Opcional de propósito: praça nova entra no ar antes de ter
    # logo pronta, e o cabeçalho cai no letreiro em texto na fonte da casa em
    # vez de mostrar a marca do jornal errado.
    logo_marca: str | None = None
    logo_branca: str | None = None
    og_imagem: str | None = None
    # Domínio onde o painel de controle vive. Quando definido, as rotas de
    # administração só respondem nele, e o site público deixa de servi-las.
    # Vazio mantém o comportamento antigo (painel no mesmo domínio).
    painel_host: str | None = None


@dataclass(frozen=True)
class Territorio:
    uf: str
    uf_nome: str
    gentilico: str
    artigo: str          # "da" Paraíba, "de" Pernambuco, "do" Ceará
    mesorregioes: list[str]

    @property
    def uf_com_artigo(self) -> str:
        """"da Paraíba". Existe porque concordância não é opcional em texto
        que vai para o leitor, e o artigo varia com o nome do estado."""
        return f"{self.artigo} {self.uf_nome}"


@lru_cache(maxsize=1)
def _bruto() -> dict:
    caminho = PERFIS / f"{SLUG}.json"
    if not caminho.exists():
        disponiveis = ", ".join(sorted(p.stem for p in PERFIS.glob("*.json"))) or "nenhum"
        raise RuntimeError(
            f"perfil de jornal '{SLUG}' não existe em {PERFIS}. "
            f"Disponíveis: {disponiveis}. Defina JORNAL_PERFIL."
        )
    return json.loads(caminho.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def identidade() -> Identidade:
    return Identidade(**_bruto()["identidade"])


@lru_cache(maxsize=1)
def territorio() -> Territorio:
    t = dict(_bruto()["territorio"])
    t.pop("municipios", None)
    return Territorio(**t)


def municipios() -> list[dict]:
    """Lista completa da praça. Consumida só pelo seed da migração."""
    return _bruto()["territorio"]["municipios"]


def fontes() -> list[dict]:
    """Veículos que a praça agrega. Consumida só pelo seed do worker."""
    return _bruto()["fontes"]


def cidades_previsao() -> list[dict]:
    """Cidades da faixa de previsão do tempo."""
    return _bruto().get("previsao", [])


def dominios_de_imagem() -> list[str]:
    """Domínios cujas imagens passam pelo nosso proxy.

    Derivado das fontes, para que fonte nova entre na lista sozinha, MAIS os
    extras declarados no perfil. Os extras não são preguiça: vários veículos
    servem a foto de um subdomínio de CDN que não aparece em lugar nenhum da
    URL do site (`files.clickpb.com.br`, `cdn.jornaldaparaiba.com.br`). Só
    derivar deixaria essas imagens de fora, e é de uma delas que sai a arte de
    compartilhamento de boa parte das matérias.
    """
    saida: set[str] = set(_bruto().get("dominios_imagem_extra", []))
    for f in fontes():
        for chave in ("site_url", "feed_url", "api_url"):
            url = f.get(chave) or ""
            if "//" in url:
                host = url.split("//", 1)[1].split("/", 1)[0].lower()
                saida.add(host)
                saida.add(host[4:] if host.startswith("www.") else "www." + host)
    return sorted(saida)


# Chaves de Redis com o prefixo da praça. Dois jornais no mesmo Redis não
# podem disputar a mesma fila nem sobrescrever a previsão um do outro.
FILA_COLETA = f"{SLUG}:crawl_requests"
CHAVE_TEMPO = f"{SLUG}:tempo"

# Atalhos, para o resto do código não chamar função a toda hora.
JORNAL = identidade()
UF = territorio()
