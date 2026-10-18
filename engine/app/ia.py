"""Camada de IA, com o provedor escolhido por variável de ambiente.

Existe por dois motivos práticos, não por elegância:

  1. O custo é dinheiro saindo todo mês. Poder trocar de provedor sem mexer em
     código é o que permite começar num plano gratuito, medir o resultado e só
     então decidir se vale pagar.
  2. Toda chamada devolve o consumo em tokens. Sem isso a conta só aparece na
     fatura, quando já não dá para reagir.

Os dois provedores recebem o MESMO esquema JSON e devolvem o MESMO dicionário,
então quem chama (redator.py) não sabe nem precisa saber qual está ligado.
"""

import json
import logging

import httpx

from .config import (
    ALIBABA_API_KEY,
    ALIBABA_BASE_URL,
    ALIBABA_MODELO,
    ALIBABA_PRECO_ENTRADA,
    ALIBABA_PRECO_SAIDA,
    ANTHROPIC_API_KEY,
    CF_ACCOUNT_ID,
    CF_API_TOKEN,
    CF_MODELO,
    GEMINI_API_KEY,
    GEMINI_MODELO,
    IA_MODELO,
    IA_PROVEDOR,
)

log = logging.getLogger("paraibao.ia")

TIMEOUT = 120

# US$ por milhão de tokens. Serve só para o painel mostrar quanto já custou;
# a cobrança real é a da fatura do provedor.
PRECOS = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "gemini": (0.0, 0.0),
    "cloudflare": (0.0, 0.0),
}

# Neurônios por milhão de tokens, da tabela da Cloudflare. Serve para o painel
# mostrar quanto da franquia diária de 10.000 já foi consumida: sem isso a
# máquina só descobre que estourou quando as chamadas começam a falhar.
NEURONIOS = {
    "@cf/meta/llama-3.3-70b-instruct-fp8-fast": (26668, 204805),
    "@cf/meta/llama-3.1-8b-instruct": (25608, 75147),
    "@cf/meta/llama-4-scout-17b-16e-instruct": (24545, 77273),
    "@cf/mistralai/mistral-small-3.1-24b-instruct": (31876, 50488),
}


class ErroIA(Exception):
    pass


def disponivel() -> bool:
    if IA_PROVEDOR == "anthropic":
        return bool(ANTHROPIC_API_KEY)
    if IA_PROVEDOR == "gemini":
        return bool(GEMINI_API_KEY)
    if IA_PROVEDOR == "cloudflare":
        return bool(CF_ACCOUNT_ID and CF_API_TOKEN)
    if IA_PROVEDOR == "alibaba":
        return bool(ALIBABA_API_KEY and ALIBABA_BASE_URL)
    return False


def modelo_atual() -> str:
    if IA_PROVEDOR == "anthropic":
        return IA_MODELO
    if IA_PROVEDOR == "cloudflare":
        return CF_MODELO
    if IA_PROVEDOR == "alibaba":
        return ALIBABA_MODELO
    return GEMINI_MODELO


def neuronios(tokens_entrada: int, tokens_saida: int) -> int:
    """Consumo estimado em neurônios. Zero fora da Cloudflare."""
    if IA_PROVEDOR != "cloudflare":
        return 0
    entrada, saida = NEURONIOS.get(CF_MODELO, (26668, 204805))
    return round(tokens_entrada / 1e6 * entrada + tokens_saida / 1e6 * saida)


def custo_estimado(tokens_entrada: int, tokens_saida: int) -> float:
    if IA_PROVEDOR == "alibaba":
        entrada, saida = ALIBABA_PRECO_ENTRADA, ALIBABA_PRECO_SAIDA
    else:
        entrada, saida = PRECOS.get(
            modelo_atual() if IA_PROVEDOR == "anthropic" else "gemini", (0.0, 0.0)
        )
    return tokens_entrada / 1e6 * entrada + tokens_saida / 1e6 * saida


def gerar_json(sistema: str, prompt: str, esquema: dict, max_tokens: int = 2000) -> dict:
    """Devolve o objeto já validado contra o esquema, mais o consumo.

    A saída estruturada não é conforto: sem ela a resposta vem em prosa às
    vezes, com cerca de JSON outras, e o parser vira um campo minado.
    """
    if IA_PROVEDOR == "anthropic":
        return _anthropic(sistema, prompt, esquema, max_tokens)
    if IA_PROVEDOR == "gemini":
        return _gemini(sistema, prompt, esquema, max_tokens)
    if IA_PROVEDOR == "cloudflare":
        return _cloudflare(sistema, prompt, esquema, max_tokens)
    if IA_PROVEDOR == "alibaba":
        return _alibaba(sistema, prompt, esquema, max_tokens)
    raise ErroIA(f"provedor desconhecido: {IA_PROVEDOR}")


def _alibaba(sistema: str, prompt: str, esquema: dict, max_tokens: int) -> dict:
    """Alibaba Model Studio pelo modo compatível com a OpenAI.

    Duas armadilhas, as duas medidas no endpoint de verdade:

      1. Os modelos do catálogo raciocinam por padrão e gastam o orçamento de
         saída num campo `reasoning_content` ANTES de escrever a resposta. Com
         `max_tokens` apertado a chamada volta HTTP 200, `content` vazio e
         `finish_reason: length`: sucesso aparente com matéria em branco. Por
         isso `enable_thinking` vai desligado, e o corte por tamanho vira erro
         explícito em vez de JSON quebrado.
      2. O esquema vai embrulhado em `{name, strict, schema}`, diferente da
         Cloudflare, que recebe o esquema cru.
    """
    if not (ALIBABA_API_KEY and ALIBABA_BASE_URL):
        raise ErroIA("ALIBABA_API_KEY ou ALIBABA_BASE_URL ausente")

    corpo = {
        "model": ALIBABA_MODELO,
        "messages": [
            {"role": "system", "content": sistema},
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "noticia", "strict": True, "schema": esquema},
        },
        "enable_thinking": False,
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(
                f"{ALIBABA_BASE_URL}/chat/completions",
                json=corpo,
                headers={"Authorization": f"Bearer {ALIBABA_API_KEY}"},
            )
        dados = r.json()
    except Exception as exc:
        raise ErroIA(f"alibaba: {str(exc)[:300]}") from exc

    if "error" in dados:
        erro = dados["error"]
        mensagem = erro.get("message", "") if isinstance(erro, dict) else str(erro)
        raise ErroIA(f"alibaba: {mensagem[:300]}")
    if dados.get("code"):
        raise ErroIA(f"alibaba: {dados.get('code')} {str(dados.get('message'))[:250]}")

    try:
        escolha = dados["choices"][0]
        texto = escolha["message"]["content"]
    except (KeyError, IndexError):
        raise ErroIA(f"alibaba: resposta inesperada {str(dados)[:200]}")

    if not texto:
        raise ErroIA(
            f"alibaba: resposta vazia (finish_reason={escolha.get('finish_reason')}); "
            "se for 'length', o modelo gastou a saída raciocinando"
        )

    uso = dados.get("usage") or {}
    return {
        "dados": json.loads(texto),
        "tokens_entrada": uso.get("prompt_tokens", 0),
        "tokens_saida": uso.get("completion_tokens", 0),
    }


def _cloudflare(sistema: str, prompt: str, esquema: dict, max_tokens: int) -> dict:
    """Workers AI. O esquema vai DENTRO de `json_schema`, sem a camada
    `schema` que a Anthropic usa: mandar aninhado a mais devolve 400."""
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        raise ErroIA("CF_ACCOUNT_ID ou CF_API_TOKEN ausente")

    esquema_limpo = {k: v for k, v in esquema.items() if k != "additionalProperties"}
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/ai/run/{CF_MODELO}"
    )
    corpo = {
        "messages": [
            {"role": "system", "content": sistema},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_schema", "json_schema": esquema_limpo},
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(
                url, json=corpo, headers={"Authorization": f"Bearer {CF_API_TOKEN}"}
            )
        dados = r.json()
    except Exception as exc:
        raise ErroIA(f"cloudflare: {str(exc)[:300]}") from exc

    if not dados.get("success", False):
        erros = dados.get("errors") or [{"message": r.text[:200]}]
        raise ErroIA(f"cloudflare: {str(erros[0].get('message', ''))[:300]}")

    resultado = dados.get("result", {})
    saida = resultado.get("response")
    # Com JSON Mode a resposta às vezes já vem desserializada, às vezes vem
    # como string. Aceitar as duas evita quebrar quando o modelo muda.
    if isinstance(saida, str):
        saida = json.loads(saida)
    if not isinstance(saida, dict):
        raise ErroIA(f"cloudflare: resposta inesperada {str(resultado)[:200]}")

    uso = resultado.get("usage") or {}
    return {
        "dados": saida,
        "tokens_entrada": uso.get("prompt_tokens", 0),
        "tokens_saida": uso.get("completion_tokens", 0),
    }


def _anthropic(sistema: str, prompt: str, esquema: dict, max_tokens: int) -> dict:
    import anthropic

    if not ANTHROPIC_API_KEY:
        raise ErroIA("ANTHROPIC_API_KEY ausente")

    cliente = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=TIMEOUT)
    try:
        resposta = cliente.messages.create(
            model=IA_MODELO,
            max_tokens=max_tokens,
            system=sistema,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": esquema}},
        )
    except Exception as exc:
        raise ErroIA(f"anthropic: {str(exc)[:300]}") from exc

    texto = next((b.text for b in resposta.content if b.type == "text"), None)
    if not texto:
        raise ErroIA("resposta sem bloco de texto")
    return {
        "dados": json.loads(texto),
        "tokens_entrada": resposta.usage.input_tokens,
        "tokens_saida": resposta.usage.output_tokens,
    }


def _gemini(sistema: str, prompt: str, esquema: dict, max_tokens: int) -> dict:
    """A Gemini não aceita `additionalProperties` nem `$schema` no
    responseSchema; manda 400 se vierem. Por isso o esquema é podado aqui, e
    não escrito diferente lá em cima."""
    if not GEMINI_API_KEY:
        raise ErroIA("GEMINI_API_KEY ausente")

    def podar(no):
        if isinstance(no, dict):
            return {
                k: podar(v)
                for k, v in no.items()
                if k not in ("additionalProperties", "$schema", "title")
            }
        if isinstance(no, list):
            return [podar(v) for v in no]
        return no

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODELO}:generateContent"
    )
    corpo = {
        "systemInstruction": {"parts": [{"text": sistema}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": podar(esquema),
            "maxOutputTokens": max_tokens,
            "temperature": 0.4,
        },
    }
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.post(url, json=corpo, headers={"x-goog-api-key": GEMINI_API_KEY})
        dados = r.json()
    except Exception as exc:
        raise ErroIA(f"gemini: {str(exc)[:300]}") from exc

    if "error" in dados:
        raise ErroIA(f"gemini: {dados['error'].get('message', '')[:300]}")
    try:
        texto = dados["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise ErroIA(f"gemini: resposta inesperada {str(dados)[:200]}")

    uso = dados.get("usageMetadata", {})
    return {
        "dados": json.loads(texto),
        "tokens_entrada": uso.get("promptTokenCount", 0),
        "tokens_saida": uso.get("candidatesTokenCount", 0),
    }
