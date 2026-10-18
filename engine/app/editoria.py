"""Classificação de editoria determinística.

Vale a mesma lógica de custo do território: resolver o que dá sem modelo, e
deixar o LLM só para o que sobra. Aqui a regra é contagem de termo com peso,
sem dependência externa e sem latência.

Quando o enricher com LLM entrar, ele substitui apenas os casos em que
`confianca` vier abaixo do limiar, e a saída continua sendo o mesmo slug.
"""

import re

from .territory import fold


# peso 3 = termo que sozinho decide; peso 1 = indício
REGRAS: dict[str, list[tuple[str, int]]] = {
    "seguranca": [
        ("homicidio", 3), ("assassinad", 3), ("latrocinio", 3), ("feminicidio", 3),
        ("policia militar", 3), ("policia civil", 3), ("delegacia", 3), ("preso", 2),
        ("presa", 2), ("prisao", 2), ("assalto", 3), ("roubo", 2), ("furto", 2),
        ("trafico", 3), ("apreendid", 2), ("baleado", 3), ("crime", 2), ("vitima", 1),
        ("operacao policial", 3), ("bope", 3), ("suspeito", 2), ("acusado", 1),
    ],
    "politica": [
        ("prefeito", 3), ("prefeita", 3), ("vereador", 3), ("camara municipal", 3),
        ("deputado", 3), ("senador", 3), ("governador", 3), ("eleicao", 3),
        ("eleitoral", 3), ("candidat", 2), ("partido", 2), ("tse", 2), ("tre", 2),
        ("assembleia legislativa", 3), ("ministro", 2), ("prefeitura", 2),
        ("licitacao", 2), ("tcu", 2), ("tce", 2), ("mandato", 2), ("posse", 1),
    ],
    "esportes": [
        ("futebol", 3), ("campeonato", 2), ("botafogo", 3), ("treze", 3),
        ("campinense", 3), ("sousa ec", 3), ("nautico", 2), ("serie a", 2),
        ("serie b", 2), ("serie c", 2), ("serie d", 2), ("gol", 1), ("jogador", 2),
        ("tecnico", 1), ("partida", 1), ("estadio", 2), ("copa", 2), ("atleta", 2),
        ("torcida", 2), ("paraibano", 1),
    ],
    "saude": [
        ("hospital", 3), ("saude", 2), ("upa", 3), ("posto de saude", 3),
        ("vacina", 3), ("dengue", 3), ("surto", 2), ("epidemia", 3), ("medico", 2),
        ("enfermeir", 2), ("sus", 2), ("paciente", 2), ("cirurgia", 2),
        ("secretaria de saude", 3), ("covid", 3), ("samu", 3),
    ],
    "educacao": [
        ("escola", 3), ("aluno", 2), ("professor", 2), ("enem", 3), ("vestibular", 3),
        ("universidade", 3), ("ufpb", 3), ("uepb", 3), ("ifpb", 3), ("matricula", 2),
        ("creche", 3), ("merenda", 2), ("secretaria de educacao", 3), ("bolsa", 1),
        ("curso", 1), ("aula", 2),
    ],
    "economia": [
        ("emprego", 3), ("desemprego", 3), ("salario", 2), ("inflacao", 3),
        ("comercio", 2), ("empresa", 2), ("industria", 2), ("investimento", 2),
        ("pib", 3), ("mercado", 1), ("feira", 1), ("agricultura", 2),
        ("safra", 2), ("imposto", 2), ("financiamento", 2), ("concurso", 2),
    ],
    "entretenimento": [
        ("show", 3), ("festival", 3), ("cantor", 2), ("cantora", 2), ("forro", 3),
        ("sao joao", 3), ("carnaval", 3), ("banda", 2), ("filme", 2), ("serie", 1),
        ("novela", 2), ("bbb", 3), ("famoso", 1), ("cultura", 1), ("teatro", 2),
        ("festa", 2), ("turismo", 2),
    ],
    "cidades": [
        ("obra", 2), ("pavimentacao", 3), ("saneamento", 3), ("agua", 2),
        ("energia", 2), ("transito", 2), ("acidente", 2), ("chuva", 2),
        ("seca", 2), ("cagepa", 3), ("energisa", 3), ("coleta de lixo", 3),
        ("iluminacao", 2), ("rodovia", 2), ("br-", 2),
    ],
    "tecnologia": [
        ("inteligencia artificial", 3), ("aplicativo", 2), ("internet", 2),
        ("golpe", 2), ("celular", 1), ("startup", 3), ("dados", 1), ("digital", 1),
    ],
}

# Ordem de desempate quando dois placares empatam. Editoria mais específica ganha.
PRIORIDADE = [
    "seguranca", "politica", "saude", "educacao", "esportes",
    "economia", "entretenimento", "tecnologia", "cidades",
]

ROTULOS = {
    "seguranca": "Segurança",
    "politica": "Política",
    "esportes": "Esportes",
    "saude": "Saúde",
    "educacao": "Educação",
    "economia": "Economia",
    "entretenimento": "Entretenimento",
    "cidades": "Cidades",
    "tecnologia": "Tecnologia",
    "geral": "Geral",
}

_COMPILADO = {
    categoria: [(re.compile(r"\b" + re.escape(termo)), peso) for termo, peso in termos]
    for categoria, termos in REGRAS.items()
}


def classificar(titulo: str, resumo: str = "") -> tuple[str, float]:
    """Devolve (slug_da_editoria, confiança de 0 a 1).

    O título pesa o dobro do resumo: manchete de portal é escrita justamente
    para sinalizar o assunto.
    """
    t = fold(titulo or "")
    r = fold(resumo or "")

    placar: dict[str, int] = {}
    for categoria, termos in _COMPILADO.items():
        total = 0
        for regex, peso in termos:
            if regex.search(t):
                total += peso * 2
            elif regex.search(r):
                total += peso
        if total:
            placar[categoria] = total

    if not placar:
        return "geral", 0.0

    melhor = max(placar.values())
    empatados = [c for c, v in placar.items() if v == melhor]
    escolhida = min(empatados, key=lambda c: PRIORIDADE.index(c))

    # Confiança = quanto o vencedor se destaca do segundo colocado.
    segundo = max((v for c, v in placar.items() if c != escolhida), default=0)
    confianca = min(1.0, (melhor - segundo) / 6 + 0.35) if melhor else 0.0
    return escolhida, round(confianca, 2)


def rotulo(slug: str) -> str:
    return ROTULOS.get(slug, slug.capitalize())
