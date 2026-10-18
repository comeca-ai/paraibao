"""Detecção de município em cascata.

Esse módulo é o principal controle de custo do sistema. A ordem importa:

  1. fonte tem município default (diário oficial de X)  -> ~40%, custo zero
  2. nome ou apelido do município no título ou no lead  -> ~40%, custo zero
  3. LLM decide entre os candidatos                     -> ~15%, custo baixo
  4. nada casou: matéria é estadual                     -> ~5%,  custo zero

Só o passo 3 chama modelo, e ele ainda não está ligado. Os passos 1, 2 e 4
resolvem a maior parte sem gastar um token.

O detalhe que faz isso funcionar sem gerar lixo: 83 dos 223 municípios têm
nome que colide com palavra comum ou sobrenome ("Areia", "Sousa", "Boa Vista",
"Conceição"). Esses estão marcados como `ambiguous` e só casam quando existe
marcador geográfico por perto ("em Areia", "prefeitura de Sousa", "Areia/PB").
Sem isso, qualquer matéria com a palavra "areia" viraria notícia do município
de Areia.
"""

import re

from .jornal import UF
import unicodedata

from sqlalchemy import select

from .models import ArticleMunicipality, Municipality


# Preposição ou expressão que indica lugar logo antes do nome.
CONTEXTO_ANTES = re.compile(
    r"(?:\b(?:em|de|do|da|no|na|pra|para|ate|entre|sobre)\s+"
    r"|\b(?:prefeitura|municipio|cidade|camara|comarca|zona rural|interior|regiao)\s+de\s+"
    r"|\b(?:prefeito|prefeita|vereador|vereadora)\s+de\s+)$"
)
# Marcador de UF logo depois do nome: "Areia/PB", "Areia - PB", "Areia (PB)".
# A sigla vem do perfil: em outra praça este mesmo padrão vale para "PE", "CE".
CONTEXTO_DEPOIS = re.compile(rf"^\s*[-/,(]?\s*{UF.uf.lower()}\b")

# Palavra anterior que continua valendo como lugar mesmo capitalizada
# ("Em Patos, ...", "Prefeitura De Sousa").
MARCADORES = {
    "em", "de", "do", "da", "no", "na", "pra", "para", "ate", "entre", "sobre",
    "prefeitura", "municipio", "cidade", "camara", "comarca", "interior", "regiao",
    "e", "com", "contra", "ou",
}
# Palavra anterior capitalizada, capturada no texto ORIGINAL. A dobra preserva
# comprimento justamente pra essa posição bater.
PALAVRA_ANTERIOR = re.compile(r"([A-Za-zÀ-ÿ][\wÀ-ÿ'-]*)\s+$")


# Substantivo que indica lugar DENTRO de uma cidade. "Praia de Manaíra" e
# "bairro de Manaíra" são João Pessoa, não o município de Manaíra no Sertão.
# Vence o marcador geográfico, porque "de" sozinho não distingue os dois casos.
NEGATIVO_ANTES = re.compile(
    r"\b(?:praia|bairro|shopping|hotel|avenida|av|rua|condominio|edificio|"
    r"estadio|ginasio|hospital|colegio|clube|mercado|terminal|aeroporto|"
    r"parque|distrito|povoado|sitio|feira|rodoviaria|orla|lagoa do)"
    r"(?:\s+d[aeo]s?)?\s+$"
)

# Bairro que tem o mesmo nome de um município do interior. Manaíra é bairro
# nobre de João Pessoa e também município do Sertão, e o bairro aparece muito
# mais no noticiário. Para esses, só um marcador FORTE ("município de X",
# "prefeitura de X", "X/PB") atribui ao município; qualquer outra menção é
# entendida como o bairro e vai para a cidade-mãe.
BAIRROS_HOMONIMOS = {"manaira": "joao-pessoa"}
MARCADOR_FORTE = re.compile(
    r"(?:\b(?:municipio|prefeitura|cidade|camara|comarca)\s+de\s+"
    r"|\b(?:no|na)\s+(?:municipio|cidade)\s+de\s+)$"
)

# Acima disso a matéria é entendida como lista de cidades, não matéria de uma.
# 4 já é lista: matéria sobre uma cidade raramente nomeia outras três.
LIMITE_LISTA = 3


def parece_nome_proprio(palavra: str) -> bool:
    """'Cícero' sim, 'em' não, 'PM' não (sigla em caixa alta não conta)."""
    if len(palavra) < 2 or not palavra[0].isupper():
        return False
    return not palavra.isupper()


def fold(value: str) -> str:
    """Minúscula sem acento, preservando o comprimento 1:1.

    O unicodedata.normalize seguido de encode/ignore encurta a string ('é' vira
    'e' mas passa por 2 code points), o que desalinharia as posições que a
    janela de contexto usa. Por isso a dobra é feita caractere a caractere.
    """
    saida = []
    for ch in value:
        decomposto = unicodedata.normalize("NFKD", ch)
        base = "".join(c for c in decomposto if not unicodedata.combining(c))
        saida.append((base[:1] or ch).lower())
    return "".join(saida)


class TerritoryIndex:
    """Uma regex só com todos os nomes e apelidos, do mais longo pro mais curto.

    Alternação em Python é leftmost-first, então ordenar por comprimento
    decrescente garante que "campina grande" ganhe de "campina" e que
    "sao jose de piranhas" ganhe de "sao jose".
    """

    def __init__(self, municipios: list[Municipality]):
        self.por_termo: dict[str, tuple[int, bool, str]] = {}
        self.id_por_slug = {m.slug: m.id for m in municipios}
        for m in municipios:
            self.por_termo[fold(m.name)] = (m.id, m.ambiguous, "name")
            for apelido in m.aliases or []:
                termo = fold(apelido)
                # Apelido não sobrescreve nome oficial de outro município.
                self.por_termo.setdefault(termo, (m.id, m.ambiguous, "alias"))

        termos = sorted(self.por_termo, key=len, reverse=True)
        self.regex = re.compile(r"\b(" + "|".join(re.escape(t) for t in termos) + r")\b")

    def detectar(self, titulo: str, resumo: str = "") -> list[dict]:
        """Devolve os municípios citados, o primeiro do título como primário."""
        achados: dict[int, dict] = {}

        for peso, texto in (("titulo", titulo), ("resumo", resumo)):
            if not texto:
                continue
            dobrado = fold(texto)
            for match in self.regex.finditer(dobrado):
                termo = match.group(1)
                municipio_id, ambiguo, metodo = self.por_termo[termo]
                antes = dobrado[max(0, match.start() - 40) : match.start()]
                depois = dobrado[match.end() : match.end() + 6]
                tem_marcador = bool(
                    CONTEXTO_ANTES.search(antes) or CONTEXTO_DEPOIS.match(depois)
                )

                # Lugar dentro de outra cidade nunca é município, mesmo com
                # marcador: "Praia de Manaíra" é João Pessoa.
                if NEGATIVO_ANTES.search(antes):
                    continue

                # Bairro homônimo: sem marcador forte, é o bairro, e o crédito
                # vai para a cidade-mãe.
                if termo in BAIRROS_HOMONIMOS and not MARCADOR_FORTE.search(antes):
                    mae = self.id_por_slug.get(BAIRROS_HOMONIMOS[termo])
                    if mae is None or mae in achados:
                        continue
                    municipio_id, ambiguo, metodo = mae, False, "bairro_homonimo"

                # Nome que colide com palavra comum exige marcador geográfico.
                if ambiguo and not tem_marcador:
                    continue

                # Sobrenome e estabelecimento: "Cícero Lucena" (prefeito de JP)
                # e "Shopping Manaíra" viravam município de Lucena e de Manaíra.
                # Se a palavra anterior é nome próprio e não é marcador de lugar,
                # o que veio antes manda: é pessoa ou lugar privado, não cidade.
                if not tem_marcador:
                    anterior = PALAVRA_ANTERIOR.search(texto[: match.start()])
                    if anterior and parece_nome_proprio(anterior.group(1)):
                        if fold(anterior.group(1)) not in MARCADORES:
                            continue

                if municipio_id in achados:
                    continue
                achados[municipio_id] = {
                    "municipality_id": municipio_id,
                    "detection_method": metodo,
                    "confidence": 0.75 if ambiguo else 0.95,
                    "_peso": 0 if peso == "titulo" else 1,
                    "_pos": match.start(),
                }

        ordenado = sorted(achados.values(), key=lambda a: (a["_peso"], a["_pos"]))

        # Matéria que cita muita cidade é lista ("Inmet emite alerta para as
        # cidades X, Y, Z"), não matéria da primeira delas. Fica sem primário e
        # aparece como estadual, ainda vinculada a cada município citado.
        lista_larga = len(ordenado) > LIMITE_LISTA
        for i, achado in enumerate(ordenado):
            achado["is_primary"] = i == 0 and not lista_larga
            if lista_larga:
                achado["confidence"] = round(achado["confidence"] * 0.6, 2)
            achado.pop("_peso")
            achado.pop("_pos")
        return ordenado


_index: TerritoryIndex | None = None


def get_index(session) -> TerritoryIndex:
    global _index
    if _index is None:
        municipios = session.scalars(
            select(Municipality).where(Municipality.active.is_(True))
        ).all()
        _index = TerritoryIndex(municipios)
    return _index


def reset_index():
    global _index
    _index = None


def classificar_territorio(session, article, source=None) -> list[dict]:
    """Cascata completa para um artigo. Não commita, só devolve os vínculos."""
    # Passo 1: a fonte já diz de qual município ela é.
    if source is not None and source.default_municipality_id:
        return [
            {
                "municipality_id": source.default_municipality_id,
                "is_primary": True,
                "detection_method": "source_default",
                "confidence": 1.0,
            }
        ]
    # Passo 2: nome ou apelido no texto.
    return get_index(session).detectar(article.title, article.summary or "")


def aplicar(session, article, source=None) -> int:
    """Grava os vínculos do artigo, substituindo o que houver. Devolve quantos.

    Exceção: vínculo com método `tag` veio da busca por município na API da
    fonte, então o município é conhecido, não adivinhado. Adivinhar por cima
    disso só pioraria.
    """
    existentes = session.scalars(
        select(ArticleMunicipality).where(ArticleMunicipality.article_id == article.id)
    ).all()
    if any(v.detection_method == "tag" for v in existentes):
        return len(existentes)

    achados = classificar_territorio(session, article, source)
    for antigo in existentes:
        session.delete(antigo)
    # O flush é obrigatório: a unit of work do SQLAlchemy ordena por tipo de
    # operação, não pela ordem em que foram pedidas, e emitiria os INSERT antes
    # dos DELETE. No reprocessamento isso viola o unique (article_id,
    # municipality_id) e derruba o lote inteiro.
    if existentes:
        session.flush()
    for achado in achados:
        session.add(ArticleMunicipality(article_id=article.id, **achado))
    return len(achados)
