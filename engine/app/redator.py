"""Reescreve a notícia com texto próprio e promove a matéria para camada `own`.

Por que isto muda o projeto:

  O fato noticioso não é protegido por direito autoral; a FORMA de expressão é
  (ver legal.py). Enquanto o texto é do jornal de origem, o Paraibão só pode
  citar um trecho, o canonical aponta para lá e o Google indexa a fonte, não a
  gente. Com texto próprio a matéria vira obra nossa: canonical nosso, entrada
  no news-sitemap, e o tráfego de busca deixa de ser doado.

O que este módulo NÃO faz, de propósito:

  Não inventa. A regra que sustenta tudo é que todo fato do texto final tem que
  estar no material de origem. Uma cifra alucinada numa matéria sobre um
  político real não é bug de software, é dano a terceiro. Por isso a saída do
  modelo passa por três verificações mecânicas antes de ir ao ar, e qualquer
  uma delas reprovando descarta a reescrita e mantém a matéria como agregada.

O texto original nunca é apagado: fica em `body` como prova do que foi
reescrito, e o texto próprio vai para `own_body`.
"""

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from . import ia, jornal
from .config import IA_POR_RODADA, IA_PROVEDOR, IA_TETO_DIA
from .models import Article, ArticleMunicipality, Municipality, Source

log = logging.getLogger("paraibao.redator")

MIN_CORPO = 400          # abaixo disso o material não dá notícia
MAX_ENTRADA = 6000       # corta corpo gigante: o fim quase nunca acrescenta
MIN_PARAGRAFOS = 3
LIMITE_COPIA = 0.12      # fração de trechos de 8 palavras repetidos do original


SISTEMA = f"""Você é redator de um portal de notícias {jornal.UF.uf_com_artigo}. Recebe a
apuração de outro veículo e escreve a notícia com texto próprio.

REGRAS INVIOLÁVEIS
1. Não invente nada. Todo fato, número, nome, cargo, data, valor e local do seu
   texto precisa estar no material recebido. Na dúvida, omita.
2. Não copie. Nenhuma frase pode ser reaproveitada literalmente. Reescreva a
   estrutura da informação, não troque sinônimos.
3. Não opine. Sem adjetivo de valor, sem julgamento, sem previsão, sem
   conclusão sua.
4. Só use aspas se a fala estiver no material. Se usar, reproduza exatamente e
   diga quem falou.
5. Credite a apuração uma vez, no corpo, de forma natural: "segundo o VEÍCULO",
   "de acordo com apuração do VEÍCULO".

ESTILO
- Português do Brasil, registro jornalístico, frases curtas e diretas.
- Primeiro parágrafo é o lide: o que aconteceu, com quem, onde e quando.
- De 3 a 6 parágrafos.
- Título de no máximo 90 caracteres, sem ponto final, sem sensacionalismo e
  sem ponto de exclamação.
- O título é uma frase normal: começa com letra maiúscula e mantém maiúscula
  em nome próprio (pessoa, cidade, órgão). NÃO escreva o título inteiro em
  maiúsculas nem o título inteiro em minúsculas.
- A linha de apoio acrescenta informação ao título, não o repete.
- Nunca use travessão. Use vírgula, ponto ou dois-pontos.

Se o material recebido não tiver informação suficiente para uma notícia
honesta, responda com suficiente=false e explique em uma linha."""


ESQUEMA = {
    "type": "object",
    "properties": {
        "suficiente": {
            "type": "boolean",
            "description": "false se o material não permite escrever a notícia",
        },
        "motivo": {"type": "string"},
        "titulo": {"type": "string"},
        "olho": {"type": "string"},
        "paragrafos": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["suficiente", "motivo", "titulo", "olho", "paragrafos"],
    "additionalProperties": False,
}


# ------------------------------------------------------------- verificações


def _fold(texto: str) -> str:
    sem = unicodedata.normalize("NFKD", texto.lower())
    sem = "".join(c for c in sem if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9\s]", " ", sem)


def _palavras(texto: str) -> list[str]:
    return _fold(texto).split()


def _fracao_copiada(original: str, novo: str, janela: int = 8) -> float:
    """Fração dos trechos de 8 palavras do texto novo que aparecem igual no
    original.

    Reescrita que reaproveita frases inteiras não resolve nada: continua sendo
    a forma de expressão do outro veículo, que é justamente o que a lei
    protege. Oito palavras é curto o bastante para pegar cópia e longo o
    bastante para não acusar coincidência ("de acordo com o prefeito de").
    """
    orig, nov = _palavras(original), _palavras(novo)
    if len(nov) < janela:
        return 0.0
    trechos_orig = {
        " ".join(orig[i : i + janela]) for i in range(max(0, len(orig) - janela + 1))
    }
    trechos_novos = [
        " ".join(nov[i : i + janela]) for i in range(len(nov) - janela + 1)
    ]
    if not trechos_novos:
        return 0.0
    return sum(1 for t in trechos_novos if t in trechos_orig) / len(trechos_novos)


def _numeros(texto: str) -> set[str]:
    """Números do texto, normalizados. Separador de milhar e decimal some para
    que 1.500 e 1500 contem como o mesmo número."""
    achados = re.findall(r"\d[\d.,]*", texto)
    return {re.sub(r"[.,]", "", n).lstrip("0") or "0" for n in achados}


def _caixa_do_titulo(titulo: str) -> str:
    """Rede de proteção para o título que volta todo em minúscula.

    A instrução "sem caixa alta" no prompt queria dizer "não grite"; o modelo
    entendeu "nenhuma maiúscula" e devolveu 4 de 6 títulos assim. O prompt já
    foi corrigido, mas isso aqui é barato e evita que uma manchete quebrada
    chegue na home se o modelo escorregar de novo.

    Só age quando NÃO existe nenhuma maiúscula no título: nome próprio no meio
    da frase não tem como ser recuperado aqui, e é justamente por isso que a
    correção de verdade é o prompt.
    """
    if not titulo or any(c.isupper() for c in titulo):
        return titulo
    log.warning("título voltou sem nenhuma maiúscula: %r", titulo[:80])
    return titulo[0].upper() + titulo[1:]


def verificar(original: str, saida: dict) -> tuple[bool, str]:
    """As três travas, na ordem em que custam menos."""
    paragrafos = [p.strip() for p in saida.get("paragrafos", []) if p.strip()]
    titulo = (saida.get("titulo") or "").strip()

    if len(paragrafos) < MIN_PARAGRAFOS:
        return False, f"só {len(paragrafos)} parágrafos"
    if not titulo or len(titulo) > 130:
        return False, f"título com {len(titulo)} caracteres"

    novo = f"{titulo}\n{saida.get('olho', '')}\n" + "\n".join(paragrafos)

    # 1. inventou número? Cifra, data ou percentual que não existe na origem é
    #    o erro mais perigoso que este módulo pode cometer.
    inventados = _numeros(novo) - _numeros(original)
    # números de 1 a 2 dígitos aparecem por reformulação legítima
    # ("três" vira "3", "duas semanas" vira "2 semanas")
    inventados = {n for n in inventados if len(n) > 2}
    if inventados:
        return False, f"número fora da origem: {sorted(inventados)[:4]}"

    # 2. copiou?
    copiado = _fracao_copiada(original, novo)
    if copiado > LIMITE_COPIA:
        return False, f"{copiado:.0%} de trechos idênticos à origem"

    # 3. encolheu demais? Texto de duas linhas não é matéria, é chamada.
    if len(" ".join(paragrafos)) < 350:
        return False, "texto curto demais"

    return True, ""


# ------------------------------------------------------------------ escrita


def _material(session, artigo: Article) -> tuple[str, str]:
    fonte = session.get(Source, artigo.source_id)
    municipio = session.scalar(
        select(Municipality)
        .join(ArticleMunicipality, ArticleMunicipality.municipality_id == Municipality.id)
        .where(
            ArticleMunicipality.article_id == artigo.id,
            ArticleMunicipality.is_primary.is_(True),
        )
    )
    corpo = (artigo.body or "")[:MAX_ENTRADA]
    nome_fonte = fonte.name if fonte else "veículo local"
    prompt = (
        f"VEÍCULO QUE APUROU: {nome_fonte}\n"
        f"MUNICÍPIO: {municipio.name if municipio else 'Paraíba'}\n"
        f"TÍTULO ORIGINAL: {artigo.title}\n"
        f"RESUMO ORIGINAL: {artigo.summary or ''}\n\n"
        f"TEXTO APURADO:\n{corpo}"
    )
    return prompt, nome_fonte


def reescrever(session, artigo: Article) -> bool:
    """Uma matéria. Devolve True se virou camada própria."""
    original = f"{artigo.title}\n{artigo.summary or ''}\n{artigo.body or ''}"
    prompt, _ = _material(session, artigo)

    try:
        resposta = ia.gerar_json(SISTEMA, prompt, ESQUEMA, max_tokens=2000)
    except ia.ErroIA as exc:
        log.warning("ia falhou (artigo %s): %s", artigo.id, str(exc)[:200])
        return False

    saida = resposta["dados"]
    artigo.rewrite_tokens_in = resposta["tokens_entrada"]
    artigo.rewrite_tokens_out = resposta["tokens_saida"]
    artigo.rewrite_model = ia.modelo_atual()
    # Marca a tentativa mesmo quando reprova: sem isso a mesma matéria ruim
    # seria reprocessada em toda rodada, queimando cota à toa.
    artigo.rewritten_at = datetime.now(timezone.utc)

    if not saida.get("suficiente"):
        artigo.rewrite_error = f"material insuficiente: {saida.get('motivo', '')[:200]}"
        session.commit()
        return False

    ok, motivo = verificar(original, saida)
    if not ok:
        artigo.rewrite_error = f"reprovado: {motivo}"
        session.commit()
        log.info("reescrita reprovada (artigo %s): %s", artigo.id, motivo)
        return False

    artigo.own_title = _caixa_do_titulo(saida["titulo"].strip())
    artigo.own_summary = (saida.get("olho") or "").strip()
    artigo.own_body = "\n\n".join(p.strip() for p in saida["paragrafos"] if p.strip())
    artigo.rewrite_error = None
    # A promoção de camada é o último passo: até aqui nada mudou no site.
    artigo.layer = "own"
    session.commit()
    return True


def fila(session, limite: int):
    """Quem entra na fila da reescrita.

    Mesma lógica editorial do publicador: matéria com município vinculado
    primeiro, porque é a que só existe aqui, depois por recência. Cota é
    recurso escasso e tem que ir para o que diferencia o portal.
    """
    e_local = select(1).where(ArticleMunicipality.article_id == Article.id).exists()
    return session.scalars(
        select(Article)
        .where(
            Article.layer == "aggregate",
            Article.rewritten_at.is_(None),
            Article.body.is_not(None),
            func.length(Article.body) >= MIN_CORPO,
            Article.status.in_(("published", "collected")),
        )
        .order_by(
            e_local.desc(),
            func.coalesce(Article.published_at, Article.collected_at).desc(),
        )
        .limit(limite)
    ).all()


def _hoje(session) -> int:
    inicio = datetime.now(timezone.utc) - timedelta(hours=24)
    return session.scalar(
        select(func.count(Article.id)).where(Article.rewritten_at >= inicio)
    ) or 0


def rodar(session) -> int:
    # Sair calado aqui custou meses de reescrita: a chave do provedor estava
    # vazia em produção, `disponivel()` dava falso, a função retornava 0 e o
    # log não registrava nem sucesso nem erro. Quem olhasse o worker via só a
    # coleta funcionando. Desligado por opção é INFO; configurado pela metade
    # é WARNING, porque é engano, não escolha.
    if IA_PROVEDOR == "off":
        log.info("redator desligado (IA_PROVEDOR=off)")
        return 0
    if not ia.disponivel():
        log.warning(
            "redator parado: provedor '%s' selecionado mas sem credencial. "
            "Nenhuma matéria vira texto próprio enquanto isso.", IA_PROVEDOR
        )
        return 0
    restam = IA_TETO_DIA - _hoje(session)
    if restam <= 0:
        log.info("redator no teto do dia (%s/%s)", _hoje(session), IA_TETO_DIA)
        return 0
    convertidas = 0
    for artigo in fila(session, min(IA_POR_RODADA, restam)):
        if reescrever(session, artigo):
            convertidas += 1
    return convertidas


def situacao(session) -> dict:
    """Números reais para o painel: quanto já rodou e quanto custou."""
    tentadas = session.scalar(
        select(func.count(Article.id)).where(Article.rewritten_at.is_not(None))
    ) or 0
    proprias = session.scalar(
        select(func.count(Article.id)).where(Article.layer == "own")
    ) or 0
    entrada = session.scalar(select(func.sum(Article.rewrite_tokens_in))) or 0
    saida = session.scalar(select(func.sum(Article.rewrite_tokens_out))) or 0
    erros = session.execute(
        select(Article.rewrite_error, func.count(Article.id))
        .where(Article.rewrite_error.is_not(None))
        .group_by(Article.rewrite_error)
        .order_by(func.count(Article.id).desc())
        .limit(5)
    ).all()
    # Consumo das últimas 24h, que é a janela em que a franquia da Cloudflare
    # é contada. Total acumulado não serve para saber se vai estourar hoje.
    desde = datetime.now(timezone.utc) - timedelta(hours=24)
    entrada_24h = session.scalar(
        select(func.sum(Article.rewrite_tokens_in)).where(Article.rewritten_at >= desde)
    ) or 0
    saida_24h = session.scalar(
        select(func.sum(Article.rewrite_tokens_out)).where(Article.rewritten_at >= desde)
    ) or 0
    return {
        "neuronios_24h": ia.neuronios(int(entrada_24h), int(saida_24h)),
        "neuronios_franquia": 10000 if IA_PROVEDOR == "cloudflare" else 0,
        "provedor": IA_PROVEDOR,
        "modelo": ia.modelo_atual(),
        "configurado": ia.disponivel(),
        "teto_dia": IA_TETO_DIA,
        "usadas_24h": _hoje(session),
        "tentadas": tentadas,
        "proprias": proprias,
        "aproveitamento": round(proprias / tentadas * 100) if tentadas else 0,
        "tokens_entrada": int(entrada),
        "tokens_saida": int(saida),
        "custo_usd": round(ia.custo_estimado(int(entrada), int(saida)), 4),
        "motivos_de_recusa": [{"motivo": m, "total": t} for m, t in erros],
    }
