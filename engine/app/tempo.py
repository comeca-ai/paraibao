"""Previsão do tempo da Paraíba, no lugar do ticker da home.

Fonte: Open-Meteo. Escolhida por três motivos, nessa ordem: não pede chave
nem cadastro, responde as seis cidades numa requisição só, e devolve código
WMO, que é padrão e não invenção do fornecedor.

Quem busca é o **worker**, não a API: assim nenhum leitor espera requisição
externa para a home abrir. O resultado fica no Redis e a API só lê. Se o
Redis estiver vazio ou a Open-Meteo cair, a faixa simplesmente não aparece,
em vez de mostrar temperatura velha sem avisar.
"""

from __future__ import annotations

import json
import logging

import httpx
from redis import Redis

from . import jornal
from .config import REDIS_URL

log = logging.getLogger("paraibao.tempo")

CHAVE = jornal.CHAVE_TEMPO
# Vale mais que o intervalo de atualização de propósito: uma coleta que falha
# não deve apagar a faixa da home. Some só se ficar mais de duas horas sem
# conseguir atualizar, quando o dado realmente já não vale nada.
TTL = 2 * 60 * 60

# Seis cidades cobrindo as quatro mesorregiões, para a faixa não virar
# "previsão de João Pessoa" num portal que cobre 223 municípios.
# Do perfil do jornal: em outra praça são outras seis cidades, e nenhuma
# linha de código muda. Ver app/jornal.py.
CIDADES = jornal.cidades_previsao()

# Código WMO → como se fala no Nordeste. Agrupado: o leitor não precisa da
# diferença entre "chuvisco leve" e "chuvisco moderado".
WMO = {
    0: ("Céu limpo", "sol"),
    1: ("Poucas nuvens", "sol"),
    2: ("Parcialmente nublado", "sol-nuvem"),
    3: ("Nublado", "nuvem"),
    45: ("Névoa", "nevoa"), 48: ("Névoa", "nevoa"),
    51: ("Chuvisco", "chuva"), 53: ("Chuvisco", "chuva"), 55: ("Chuvisco", "chuva"),
    56: ("Chuvisco", "chuva"), 57: ("Chuvisco", "chuva"),
    61: ("Chuva fraca", "chuva"), 63: ("Chuva", "chuva"), 65: ("Chuva forte", "chuva"),
    66: ("Chuva", "chuva"), 67: ("Chuva forte", "chuva"),
    71: ("Neve", "nuvem"), 73: ("Neve", "nuvem"), 75: ("Neve", "nuvem"), 77: ("Neve", "nuvem"),
    80: ("Pancadas", "chuva"), 81: ("Pancadas", "chuva"), 82: ("Pancadas fortes", "chuva"),
    85: ("Neve", "nuvem"), 86: ("Neve", "nuvem"),
    95: ("Tempestade", "tempestade"),
    96: ("Tempestade", "tempestade"), 99: ("Tempestade", "tempestade"),
}

URL = "https://api.open-meteo.com/v1/forecast"


def _descrever(codigo: int) -> tuple[str, str]:
    return WMO.get(int(codigo), ("Tempo instável", "nuvem"))


def buscar() -> list[dict]:
    """Uma requisição para as seis cidades. Levanta em caso de falha."""
    r = httpx.get(
        URL,
        params={
            "latitude": ",".join(str(c["lat"]) for c in CIDADES),
            "longitude": ",".join(str(c["lon"]) for c in CIDADES),
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "America/Fortaleza",
            "forecast_days": 1,
        },
        timeout=20,
        headers={"user-agent": jornal.JORNAL.dominio},
    )
    r.raise_for_status()
    bruto = r.json()
    # Com uma coordenada só a Open-Meteo devolve objeto; com várias, lista.
    if isinstance(bruto, dict):
        bruto = [bruto]

    saida = []
    for cidade, dados in zip(CIDADES, bruto):
        agora = dados.get("current") or {}
        dia = dados.get("daily") or {}
        if "temperature_2m" not in agora:
            continue
        texto, icone = _descrever(agora.get("weather_code", -1))
        saida.append({
            "nome": cidade["nome"],
            "slug": cidade["slug"],
            "temp": round(agora["temperature_2m"]),
            "texto": texto,
            "icone": icone,
            "max": round((dia.get("temperature_2m_max") or [None])[0] or 0),
            "min": round((dia.get("temperature_2m_min") or [None])[0] or 0),
            "chuva": int((dia.get("precipitation_probability_max") or [0])[0] or 0),
        })
    return saida


def atualizar(redis: Redis | None = None) -> int:
    """Chamado pelo worker. Devolve quantas cidades gravou."""
    cli = redis or Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        dados = buscar()
    except Exception:
        # Não apaga o que está no Redis: dado de meia hora atrás é melhor que
        # faixa vazia, e o TTL de 2h cuida de sumir se a falha persistir.
        log.warning("previsão do tempo indisponível agora; mantendo a anterior")
        return 0
    if not dados:
        return 0
    cli.setex(CHAVE, TTL, json.dumps(dados, ensure_ascii=False))
    log.info("previsão do tempo atualizada: %s cidades", len(dados))
    return len(dados)


def atual(redis: Redis) -> list[dict]:
    """Chamado pela API. Nunca levanta: sem dado, a faixa não renderiza."""
    try:
        cru = redis.get(CHAVE)
        return json.loads(cru) if cru else []
    except Exception:
        log.exception("falha lendo previsão do tempo do Redis")
        return []
