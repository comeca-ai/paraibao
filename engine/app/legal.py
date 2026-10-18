"""Regras de uso de conteúdo de terceiro.

Base na Lei 9.610/98. Não é parecer jurídico: é a implementação conservadora
das permissões que a lei dá, para ser validada por advogado antes de escalar.

O que a lei permite, e o que isso vira aqui:

  Art. 46, I, "a" — é livre a reprodução, "na imprensa diária ou periódica, de
  notícia ou de artigo informativo, publicado em diários ou periódicos, com a
  menção do nome do autor, se assinados, e da publicação de onde foram
  transcritos". É a base do crédito + link.

  Art. 46, III — é livre "a citação (...) de passagens de qualquer obra (...)
  na medida justificada para o fim a atingir, indicando-se o nome do autor e a
  origem da obra". É a base do trecho citado, não do texto inteiro.

  Entendimento consolidado — o FATO noticioso não é protegido; protegida é a
  FORMA de expressão. Por isso texto reescrito é seguro e cópia não é.

Daí a arquitetura da página de matéria, em três camadas de risco crescente:

  1. título + resumo PRÓPRIO      risco baixo    (fato + forma nossa)
  2. trecho citado com aspas       risco baixo    (Art. 46, III, medida justificada)
  3. texto integral copiado        NÃO FAZEMOS    (substituiria a obra)

O CTA para o veículo de origem não é cortesia: é o que sustenta o argumento de
que a página remete à obra em vez de substituí-la.
"""

# Quanto do original pode ser citado. Dois ou três parágrafos remetem à
# matéria; o texto inteiro a substituiria, e aí a permissão do art. 46 não
# alcança mais.
LIMITE_CITACAO = 1200
MIN_PARAGRAFO = 40


def trecho_citavel(corpo: str | None, limite: int = LIMITE_CITACAO) -> str:
    """Devolve o começo do corpo, cortado em fim de parágrafo ou de frase.

    Cortar no meio da frase, além de feio, enfraquece o argumento de citação:
    a citação tem que ser inteligível por si.
    """
    if not corpo:
        return ""
    texto = corpo.strip()
    if len(texto) <= limite:
        return texto

    corte = texto[:limite]
    for marca in ("\n\n", ". ", "? ", "! "):
        pos = corte.rfind(marca)
        if pos > limite * 0.55:
            return corte[: pos + len(marca)].strip()
    return corte.rsplit(" ", 1)[0].strip()


def credito_imagem(fonte_nome: str) -> str:
    """Foto de jornal tem autoria própria, do fotógrafo, separada do texto.

    O crédito visível é o mínimo. O correto mesmo é hospedar cópia própria só
    quando houver licença; enquanto isso a exibição é sempre creditada.
    """
    return f"Foto: Reprodução / {fonte_nome}"


def pode_ter_pagina_interna(layer: str) -> bool:
    """Toda camada tem página; o que muda é o que a página mostra."""
    return layer in ("aggregate", "own", "official")


def modo_exibicao(layer: str) -> str:
    """  citacao = resumo próprio + trecho citado + CTA para a origem
      integral = texto é nosso ou é ato oficial, exibe inteiro"""
    return "integral" if layer in ("own", "official") else "citacao"
