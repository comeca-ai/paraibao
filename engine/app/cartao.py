"""Card próprio para publicação em rede social.

Por que gerar em vez de repostar a foto do jornal: no site a exibição da foto
de terceiro se sustenta como citação, com crédito visível e link para a obra
(ver legal.py). No nosso perfil de Instagram a foto vira peça de conteúdo
nossa, sem o contexto que sustenta a citação. Card próprio resolve três
problemas de uma vez:

  1. jurídico   — a imagem publicada é nossa, o texto é manchete + crédito
  2. técnico    — a API do Instagram exige URL pública de JPEG, e o CDN do
                  jornal de origem bloqueia hotlink de terceiro
  3. de marca   — o feed fica com a identidade do Paraibão, não com a colcha
                  de retalhos visual de cinco jornais diferentes

Formato 1080x1350 (4:5): é o retrato máximo que o feed aceita, ou seja, o que
ocupa mais tela no celular.
"""

import hashlib
import logging
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import jornal
from .editoria import rotulo as rotulo_editoria

log = logging.getLogger("paraibao.cartao")

FONTES = Path(__file__).parent / "assets" / "fonts"
CACHE = Path(os.getenv("CARD_CACHE_DIR", "/var/cache/paraibao-img")) / "cartao"

L, A = 1080, 1350
MARGEM = 72

# Mesma paleta do site (styles.css). Mudar aqui sem mudar lá quebra a
# continuidade entre o card e a página que ele abre.
PRETO = (18, 16, 15)
BASE = (240, 240, 240)
CREME = (245, 241, 232)
MARCA = (34, 74, 233)    # azul da marca, o mesmo do site
CINZA = (107, 104, 102)
BRANCO = (252, 252, 252)

# Entra no nome do arquivo em cache. Mexeu no desenho, sobe o número: sem
# isso o card velho fica servido para sempre.
VERSAO = 7


def _fonte(nome: str, tamanho: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTES / nome), tamanho)


def _largura(desenho: ImageDraw.ImageDraw, texto: str, fonte) -> int:
    caixa = desenho.textbbox((0, 0), texto, font=fonte)
    return caixa[2] - caixa[0]


def _quebrar(desenho, texto: str, fonte, largura_max: int) -> list[str]:
    linhas: list[str] = []
    atual = ""
    for palavra in texto.split():
        teste = f"{atual} {palavra}".strip()
        if _largura(desenho, teste, fonte) <= largura_max or not atual:
            atual = teste
        else:
            linhas.append(atual)
            atual = palavra
    if atual:
        linhas.append(atual)
    return linhas


def _cortar(linhas: list[str], maximo: int) -> list[str]:
    """Corta em N linhas fechando a última.

    Sem isso o resumo termina no meio da frase ("...prioritária pelo Estatuto
    da Criança e") e o card parece defeito, não recorte.
    """
    if len(linhas) <= maximo:
        return linhas
    cortadas = linhas[:maximo]
    ultima = cortadas[-1]
    # Volta até o fim de frase mais próximo; se não houver, tira a última
    # palavra (que pode ser justamente a que ficou pela metade) e reticencia.
    for marca in (". ", "; ", ", "):
        pos = ultima.rfind(marca)
        if pos > len(ultima) * 0.5:
            cortadas[-1] = ultima[:pos + 1]
            return cortadas
    cortadas[-1] = ultima.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return cortadas


def _ajustar(desenho, texto: str, arquivo: str, largura_max: int, altura_max: int,
             maior: int, menor: int) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Maior corpo de texto que cabe na caixa.

    Manchete de jornal varia de 40 a 140 caracteres. Tamanho fixo ou estoura o
    card ou deixa buraco. Desce de 4 em 4 até caber e devolve o que coube.
    """
    for tamanho in range(maior, menor - 1, -4):
        fonte = _fonte(arquivo, tamanho)
        linhas = _quebrar(desenho, texto, fonte, largura_max)
        entrelinha = int(tamanho * 1.06)
        if len(linhas) * entrelinha <= altura_max:
            return fonte, linhas, entrelinha
    fonte = _fonte(arquivo, menor)
    entrelinha = int(menor * 1.06)
    cabem = max(1, altura_max // entrelinha)
    linhas = _quebrar(desenho, texto, fonte, largura_max)[:cabem]
    if linhas:
        linhas[-1] = linhas[-1].rstrip(" .,;:") + "…"
    return fonte, linhas, entrelinha


def _marca(desenho, y: int, cor_texto, cor_traco):
    """Assinatura do topo. O logo é wordmark, então sai da própria fonte:
    carregar SVG exigiria mais uma dependência para desenhar o mesmo texto."""
    desenho.rectangle([MARGEM, y, MARGEM + 14, y + 44], fill=cor_traco)
    desenho.text((MARGEM + 30, y - 4), jornal.JORNAL.nome.upper(),
                 font=_fonte("NeueHelvena-Extrabold.ttf", 44), fill=cor_texto)


def _rodape(desenho, fonte_nome: str, cor_texto, cor_fraca):
    """Crédito da apuração e o endereço do site.

    O crédito não é cortesia: é o que o art. 46, I, "a" exige de quem
    reproduz notícia alheia, e é o que sustenta a chamada como citação.
    """
    y = A - MARGEM - 58
    desenho.text((MARGEM, y), f"via {fonte_nome}",
                 font=_fonte("DMSans-Medium.ttf", 30), fill=cor_fraca)
    endereco = jornal.JORNAL.dominio
    fonte_end = _fonte("DMSans-Bold.ttf", 30)
    desenho.text((L - MARGEM - _largura(desenho, endereco, fonte_end), y),
                 endereco, font=fonte_end, fill=cor_texto)


def _chapeu(desenho, y: int, editoria: str, municipio: str | None) -> int:
    """Chip da marca com a editoria + município, igual ao rótulo do site.

    "GERAL" é o rótulo de quem não casou com nenhuma editoria, e como chapéu
    do card não informa nada. Nesse caso o lugar entra no lugar do tema, que é
    a informação que o leitor de portal local realmente usa para se orientar.
    """
    rotulo = rotulo_editoria(editoria).upper()
    lado = municipio.upper() if municipio else None
    if rotulo == "GERAL":
        rotulo, lado = (municipio.upper() if municipio else jornal.UF.uf_nome.upper()), None

    fonte = _fonte("DMSans-Bold.ttf", 28)
    largura = _largura(desenho, rotulo, fonte)
    desenho.rectangle([MARGEM, y, MARGEM + largura + 36, y + 52], fill=MARCA)
    desenho.text((MARGEM + 18, y + 10), rotulo, font=fonte, fill=CREME)
    if lado:
        desenho.text((MARGEM + largura + 60, y + 10), lado, font=fonte, fill=CINZA)
    return y + 52


def _tipografico(artigo: dict) -> Image.Image:
    """Só tipografia sobre fundo claro. É o formato 100% nosso: nenhum pixel
    de terceiro entra no card."""
    tela = Image.new("RGB", (L, A), BASE)
    d = ImageDraw.Draw(tela)

    _marca(d, MARGEM, PRETO, MARCA)

    topo_util = MARGEM + 130 + 52 + 56  # abaixo da marca e do chapéu
    base_util = A - MARGEM - 120        # acima da divisória do rodapé
    tem_resumo = bool(artigo.get("resumo"))

    fonte, linhas, entrelinha = _ajustar(
        d, artigo["titulo"], "NeueHelvena-Extrabold.ttf",
        L - MARGEM * 2, (base_util - topo_util) - (150 if tem_resumo else 0), 92, 48,
    )
    linhas_resumo = []
    if tem_resumo:
        fonte_r = _fonte("DMSans-Regular.ttf", 32)
        linhas_resumo = _cortar(_quebrar(d, artigo["resumo"], fonte_r, L - MARGEM * 2), 3)

    # Manchete curta ancorada no topo deixa metade do card vazia; colada na
    # base fica sufocando o rodapé. A folga vai 62% para cima, que é onde o
    # olho espera o peso num cartaz. O teto de 200px impede que um título
    # longo empurre o chapéu para longe da marca.
    altura_bloco = len(linhas) * entrelinha + (34 + len(linhas_resumo) * 44 if linhas_resumo else 0)
    y = topo_util + min(200, max(0, int(((base_util - topo_util) - altura_bloco) * 0.62)))
    # O chapéu acompanha o bloco, sempre colado no título.
    _chapeu(d, y - 108, artigo["editoria"], artigo.get("municipio"))

    for linha in linhas:
        d.text((MARGEM, y), linha, font=fonte, fill=PRETO)
        y += entrelinha
    if linhas_resumo:
        y += 34
        for linha in linhas_resumo:
            d.text((MARGEM, y), linha, font=fonte_r, fill=CINZA)
            y += 44

    d.line([MARGEM, A - MARGEM - 96, L - MARGEM, A - MARGEM - 96], fill=PRETO, width=1)
    _rodape(d, artigo["fonte"], PRETO, CINZA)
    return tela


def _com_foto(artigo: dict, foto: bytes | None) -> Image.Image:
    """Foto da fonte no topo, título embaixo em bloco escuro.

    Rende mais no feed, mas republica pixel de terceiro. O crédito fica
    impresso na própria imagem, que é o mínimo defensável.
    """
    if not foto:
        return _tipografico(artigo)

    tela = Image.new("RGB", (L, A), PRETO)
    d = ImageDraw.Draw(tela)

    altura_foto = 760
    try:
        import io

        img = Image.open(io.BytesIO(foto)).convert("RGB")
        escala = max(L / img.width, altura_foto / img.height)
        img = img.resize((round(img.width * escala), round(img.height * escala)),
                         Image.LANCZOS)
        esq = (img.width - L) // 2
        topo = (img.height - altura_foto) // 2
        tela.paste(img.crop((esq, topo, esq + L, topo + altura_foto)), (0, 0))
    except Exception as exc:
        log.warning("foto do card falhou: %s", str(exc)[:80])
        return _tipografico(artigo)

    # Faixa preta por cima da base da foto, para o crédito ter contraste
    # qualquer que seja a imagem.
    faixa = Image.new("RGBA", (L, 70), (18, 16, 15, 190))
    tela.paste(faixa, (0, altura_foto - 70), faixa)
    d.text((MARGEM, altura_foto - 52), f"Foto: Reprodução / {artigo['fonte']}",
           font=_fonte("DMSans-Medium.ttf", 26), fill=(200, 196, 190))

    _marca(d, 40, BRANCO, MARCA)

    y = _chapeu(d, altura_foto + 48, artigo["editoria"], artigo.get("municipio")) + 40
    fonte, linhas, entrelinha = _ajustar(
        d, artigo["titulo"], "NeueHelvena-Extrabold.ttf",
        L - MARGEM * 2, (A - MARGEM - 110) - y, 74, 40,
    )
    for linha in linhas:
        d.text((MARGEM, y), linha, font=fonte, fill=BRANCO)
        y += entrelinha

    _rodape(d, artigo["fonte"], BRANCO, (150, 146, 140))
    return tela


LAYOUTS = {"tipografico": _tipografico, "foto": _com_foto}


def gerar(artigo: dict, layout: str = "tipografico", foto: bytes | None = None) -> bytes:
    """JPEG do card. O Instagram só aceita JPEG em publicação por API."""
    import io

    desenhar = LAYOUTS.get(layout, _tipografico)
    tela = desenhar(artigo, foto) if desenhar is _com_foto else desenhar(artigo)
    saida = io.BytesIO()
    tela.save(saida, format="JPEG", quality=88, optimize=True, progressive=True)
    return saida.getvalue()


def chave(artigo_id: int, layout: str) -> str:
    return hashlib.sha256(f"{artigo_id}|{layout}|{VERSAO}".encode()).hexdigest()[:20]


def caminho(artigo_id: int, layout: str) -> Path:
    return CACHE / f"{chave(artigo_id, layout)}.jpg"


def obter(artigo: dict, layout: str = "tipografico", foto: bytes | None = None) -> bytes:
    """Card do cache, ou gera e guarda.

    O cache existe porque a Meta busca a URL pelo menos uma vez na criação do
    container e o Facebook rebusca ao montar o preview; e porque a mesma
    imagem é servida de novo em cada visita à página /ig.
    """
    destino = caminho(artigo["id"], layout)
    if destino.exists():
        return destino.read_bytes()
    dados = gerar(artigo, layout, foto)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(dados)
    return dados
