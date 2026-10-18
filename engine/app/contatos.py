"""Recebe quem chega pelo site: anunciante e quem quer escrever no jornal.

A validação mora aqui, e não na rota, pelo mesmo motivo de `views.py`: a
regra tem que ser a mesma se amanhã entrar uma segunda porta (formulário
dentro da matéria, link de campanha, WhatsApp).

Duas decisões que valem explicação:

- **Telefone é guardado normalizado E como a pessoa digitou.** Normalizar é
  palpite: brasileiro escreve com DDI, sem DDI, com nono dígito, sem. O campo
  normalizado serve para o convite de WhatsApp que vem depois; o digitado
  fica como prova de quando o palpite erra.
- **Nada de CAPTCHA.** Armadilha invisível mais limite por IP resolve robô de
  formulário nesse volume, sem cobrar do leitor um teste que ele não pediu.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .models import Contato

log = logging.getLogger("paraibao.contatos")

TIPOS = {"anuncio", "colaborador"}

AREAS = [
    "Política", "Segurança", "Cidades", "Economia",
    "Esportes", "Educação", "Saúde", "Cultura", "Outra",
]

MAX_MENSAGEM = 4000
# Janela e teto do limite por IP. Formulário de contato de portal local não
# tem pico legítimo: quem manda 5 em uma hora do mesmo IP é robô ou teste.
JANELA_ABUSO = timedelta(hours=1)
TETO_POR_JANELA = 5


class ErroContato(Exception):
    """Mensagem já pronta para mostrar a quem preencheu."""


def hash_ip(ip: str) -> str:
    """Hash, não o IP. Serve para contar repetição sem guardar dado pessoal."""
    return hashlib.sha256((ip or "").encode()).hexdigest()[:32] if ip else ""


def normalizar_telefone(bruto: str) -> str:
    """Devolve só dígitos, com DDI 55, ou string vazia se não parecer telefone.

    Aceita o que o brasileiro escreve de verdade: (83) 99999-9999,
    83999999999, +55 83 99999-9999. Não tenta consertar número curto demais,
    porque inventar dígito é pior que recusar.
    """
    d = re.sub(r"\D", "", bruto or "")
    if d.startswith("55") and len(d) in (12, 13):
        return d
    if len(d) in (10, 11):          # DDD + número, sem DDI
        return "55" + d
    return ""


def _limpo(valor: str | None, limite: int) -> str:
    return re.sub(r"\s+", " ", (valor or "").strip())[:limite]


def validar(tipo: str, dados: dict) -> dict:
    """Valida e devolve os campos prontos para gravar, ou levanta ErroContato."""
    if tipo not in TIPOS:
        raise ErroContato("Formulário desconhecido.")

    nome = _limpo(dados.get("nome"), 160)
    email = _limpo(dados.get("email"), 200).lower()
    telefone_digitado = _limpo(dados.get("telefone"), 40)
    mensagem = _limpo(dados.get("mensagem"), MAX_MENSAGEM)

    if len(nome) < 3:
        raise ErroContato("Escreva seu nome completo.")
    # Validação de e-mail proposital mente frouxa: a regra estrita recusa
    # endereço válido de verdade, e quem erra o próprio e-mail já se pune
    # sozinho ao não receber resposta.
    if "@" not in email or "." not in email.split("@")[-1] or len(email) < 6:
        raise ErroContato("Confira o e-mail: parece incompleto.")

    telefone = normalizar_telefone(telefone_digitado)
    if not telefone:
        raise ErroContato("Confira o telefone: informe DDD e número, com WhatsApp.")

    if len(mensagem) < 10:
        raise ErroContato(
            "Conte em uma ou duas linhas o que você precisa."
            if tipo == "anuncio"
            else "Conte em uma ou duas linhas sobre o que você quer escrever."
        )

    limpo = {
        "tipo": tipo,
        "nome": nome,
        "email": email,
        "telefone": telefone,
        "telefone_digitado": telefone_digitado,
        "cidade": _limpo(dados.get("cidade"), 120),
        "mensagem": mensagem,
        "empresa": None,
        "area": None,
        "link": None,
    }

    if tipo == "anuncio":
        limpo["empresa"] = _limpo(dados.get("empresa"), 160) or None
        if not limpo["empresa"]:
            raise ErroContato("Informe o nome da empresa ou do negócio.")
    else:
        area = _limpo(dados.get("area"), 80)
        limpo["area"] = area if area in AREAS else "Outra"
        link = _limpo(dados.get("link"), 500)
        if link and not re.match(r"^https?://", link):
            link = "https://" + link
        limpo["link"] = link or None

    return limpo


def excedeu_limite(session, ip_hash: str) -> bool:
    if not ip_hash:
        return False
    desde = datetime.now(timezone.utc) - JANELA_ABUSO
    total = session.scalar(
        select(func.count(Contato.id)).where(
            Contato.ip_hash == ip_hash, Contato.created_at >= desde
        )
    ) or 0
    return total >= TETO_POR_JANELA


def duplicado(session, tipo: str, email: str) -> bool:
    """Mesmo e-mail, mesma porta, nas últimas 24h.

    Quase sempre é clique duplo ou reenvio por impaciência, não gente
    diferente. Tratar como sucesso evita fila com o mesmo lead três vezes.
    """
    desde = datetime.now(timezone.utc) - timedelta(hours=24)
    return bool(
        session.scalar(
            select(Contato.id)
            .where(
                Contato.tipo == tipo,
                Contato.email == email,
                Contato.created_at >= desde,
            )
            .limit(1)
        )
    )


def registrar(session, tipo: str, dados: dict, *, ip: str, user_agent: str,
              origem: str) -> Contato | None:
    """Grava. Devolve None quando é repetição recente (que não é erro)."""
    limpo = validar(tipo, dados)
    ip_h = hash_ip(ip)

    if excedeu_limite(session, ip_h):
        raise ErroContato(
            "Recebemos vários envios deste acesso agora há pouco. "
            "Tente de novo mais tarde ou escreva para contato@paraibao.com.br."
        )
    if duplicado(session, tipo, limpo["email"]):
        log.info("contato repetido ignorado: tipo=%s", tipo)
        return None

    contato = Contato(
        **limpo,
        origem=_limpo(origem, 200),
        ip_hash=ip_h,
        user_agent=_limpo(user_agent, 300),
    )
    session.add(contato)
    session.commit()
    log.info("contato novo: tipo=%s id=%s cidade=%r", tipo, contato.id, limpo["cidade"])
    return contato
