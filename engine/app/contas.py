"""Contas, sessões e papéis: quem entra no jornal e o que pode fazer.

Substitui a senha básica do navegador (`HTTPBasic`), que tinha três problemas
que só apareceram quando o painel virou coisa séria:

1. **Senha única e compartilhada.** Não existe "quem fez isso": todo mundo é
   `admin`. Num jornal com repórter e editor, isso não se sustenta.
2. **Não dá para revogar.** Tirar o acesso de alguém exigia trocar a senha de
   todos, e foi por aí que ela vazou.
3. **Não tem interface.** O popup do navegador não avisa nada, não explica
   nada e não tem como sair a não ser fechando o navegador.

Duas escolhas aqui merecem explicação, porque parecem estranhas de fora:

**A senha é derivada com `hashlib.scrypt`, da biblioteca padrão.** Não é
teimosia de evitar `argon2` ou `bcrypt`: os dois exigem extensão compilada, e
este projeto sobe em Docker num deploy que atravessa dois saltos de SSH.
Dependência que compila é onde deploy quebra às 23h. `scrypt` é KDF de
memória dura, está no OpenSSL desde sempre e resolve o problema real, que é
tornar o teste de senha caro para quem roubar a tabela.

**A sessão vive no Redis, não num cookie assinado.** Cookie assinado é mais
simples, mas não dá para revogar antes de expirar. Aqui, tirar acesso é
apagar uma chave, e "encerrar todas as sessões" é uma operação de verdade e
não uma promessa. O preço é que reiniciar o Redis desloga todo mundo. Para um
painel de jornal isso é irrelevante; quando existir assinante pagante vale
reavaliar, e o ponto de troca é só este arquivo.

O cookie é `SameSite=Strict` de propósito: com ele, requisição vinda de outro
site simplesmente não carrega a sessão, o que fecha CSRF em TODAS as rotas de
ação de uma vez, inclusive as que o painel chama por JavaScript. O custo é
que abrir o painel por link de fora mostra a tela de entrada na primeira vez.
Para uma tela que se acessa por favorito, é troca boa.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timezone

from redis import Redis
from sqlalchemy import func, select

from .config import REDIS_URL
from .jornal import SLUG
from .models import Usuario

log = logging.getLogger("paraibao.contas")

redis = Redis.from_url(REDIS_URL, decode_responses=True)

# ------------------------------------------------------------------ papéis
# Hierarquia simples e proposital: três papéis, não um sistema de permissões.
# Papel a mais é papel que ninguém usa e que dá brecha de configuração.
DONO = "dono"            # tudo, inclusive gerir a equipe
EDITOR = "editor"        # painel inteiro, menos a equipe
ASSINANTE = "assinante"  # ainda não tem painel; é o encaixe da assinatura
PAPEIS = {
    DONO: "Dono",
    EDITOR: "Editor",
    ASSINANTE: "Assinante",
}
# Quem pode abrir o painel. Assinante fica de fora por construção: quando a
# assinatura existir, ele entra pela área do leitor, que é outra superfície.
PAPEIS_DE_PAINEL = (DONO, EDITOR)

# ------------------------------------------------------------------- senha
# 128 * N * r bytes = 16 MiB por verificação. Cabe no limite padrão do
# OpenSSL e leva ~60ms, que é caro para quem tenta força bruta e imperceptível
# para quem está entrando.
_N, _R, _P = 2**14, 8, 1
_MAXMEM = 64 * 1024 * 1024
MIN_SENHA = 10

# ----------------------------------------------------------------- sessões
COOKIE = f"{SLUG}_sessao"
VALIDADE = 14 * 24 * 3600      # 14 dias
RENOVAR_APOS = 24 * 3600       # desliza a validade no máximo uma vez por dia

# ------------------------------------------------------- freio de tentativa
# Dois freios, porque eles pegam ataques diferentes: por conta pega quem
# martela uma caixa de entrada conhecida; por IP pega quem varre várias.
JANELA_FREIO = 15 * 60
TETO_POR_CONTA = 5
TETO_POR_IP = 12


class ErroConta(Exception):
    """Mensagem já pronta para mostrar na tela, mais um código estável.

    O código existe porque a tela de equipe recarrega por redirecionamento
    depois de cada ação, e mandar a mensagem na query string deixaria qualquer
    um montar um link que mostra o recado que quiser para quem está logado.
    Vai o código; o texto sai de uma lista fechada no `main.py`.
    """

    def __init__(self, mensagem: str, codigo: str = "falhou"):
        super().__init__(mensagem)
        self.codigo = codigo


# --------------------------------------------------------------------------
# senha


def hash_senha(senha: str) -> str:
    sal = secrets.token_bytes(16)
    bruto = hashlib.scrypt(
        senha.encode("utf-8"), salt=sal, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32
    )
    return f"scrypt${_N}${_R}${_P}${sal.hex()}${bruto.hex()}"


def conferir_senha(senha: str, guardado: str) -> bool:
    """Compara em tempo constante. Formato desconhecido é falso, não exceção:
    hash corrompido no banco não pode virar 500 na tela de entrada."""
    try:
        algo, n, r, p, sal_hex, esperado_hex = guardado.split("$")
        if algo != "scrypt":
            return False
        bruto = hashlib.scrypt(
            senha.encode("utf-8"),
            salt=bytes.fromhex(sal_hex),
            n=int(n), r=int(r), p=int(p),
            maxmem=_MAXMEM, dklen=len(bytes.fromhex(esperado_hex)),
        )
        return secrets.compare_digest(bruto.hex(), esperado_hex)
    except (ValueError, AttributeError, TypeError):
        return False


def validar_senha(senha: str, repetida: str | None = None) -> None:
    senha = senha or ""
    if len(senha) < MIN_SENHA:
        raise ErroConta(f"A senha precisa de pelo menos {MIN_SENHA} caracteres.", "senha_curta")
    if repetida is not None and senha != repetida:
        raise ErroConta("As duas senhas não são iguais.", "senha_diferente")
    if senha.lower() in {"12345678910", "senha123456", "paraibao123"}:
        raise ErroConta("Essa senha é fácil demais.", "senha_fraca")


def normalizar_email(bruto: str) -> str:
    email = (bruto or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        raise ErroConta("E-mail inválido.", "email")
    return email


def hash_ip(ip: str) -> str:
    """Mesma decisão de contatos.py: guarda-se o hash, nunca o IP."""
    return hashlib.sha256((ip or "").encode()).hexdigest()[:32] if ip else ""


# --------------------------------------------------------------------------
# usuários


def criar(
    session,
    *,
    nome: str,
    email: str,
    senha: str,
    papel: str = EDITOR,
    precisa_trocar: bool = False,
) -> Usuario:
    email = normalizar_email(email)
    if papel not in PAPEIS:
        raise ErroConta("Papel inválido.", "papel")
    validar_senha(senha)
    if session.scalar(select(Usuario).where(Usuario.email == email)):
        raise ErroConta("Já existe uma conta com esse e-mail.", "existe")
    usuario = Usuario(
        nome=(nome or "").strip()[:160] or email.split("@")[0],
        email=email,
        senha_hash=hash_senha(senha),
        papel=papel,
        precisa_trocar_senha=precisa_trocar,
        senha_alterada_em=datetime.now(timezone.utc),
    )
    session.add(usuario)
    session.commit()
    log.info("conta criada: %s (%s)", email, papel)
    return usuario


def trocar_senha(session, usuario: Usuario, nova: str, repetida: str) -> None:
    validar_senha(nova, repetida)
    usuario.senha_hash = hash_senha(nova)
    usuario.precisa_trocar_senha = False
    usuario.senha_alterada_em = datetime.now(timezone.utc)
    session.commit()
    # Trocar senha derruba as outras sessões. É o que a pessoa espera quando
    # troca a senha justamente porque desconfia que alguém entrou.
    encerrar_todas(usuario.id)
    log.info("senha trocada: %s", usuario.email)


def autenticar(session, email: str, senha: str) -> Usuario:
    """Devolve o usuário ou levanta ErroConta com mensagem para a tela.

    A mensagem é a mesma para e-mail inexistente e senha errada, de propósito:
    dizer "esse e-mail não existe" entrega quem tem conta no jornal.
    """
    generico = ErroConta("E-mail ou senha incorretos.", "credencial")
    try:
        email = normalizar_email(email)
    except ErroConta:
        raise generico
    usuario = session.scalar(select(Usuario).where(Usuario.email == email))
    if not usuario or not usuario.ativo:
        # Gasta o mesmo tempo de uma verificação real, para não dar para
        # descobrir quem tem conta cronometrando a resposta.
        hash_senha(senha or "x")
        raise generico
    if not conferir_senha(senha or "", usuario.senha_hash):
        raise generico
    return usuario


def total(session) -> int:
    return session.scalar(select(func.count(Usuario.id))) or 0


# --------------------------------------------------------------------------
# freio de tentativas


def _chave_freio(tipo: str, valor: str) -> str:
    return f"{SLUG}:login:{tipo}:{valor}"


def bloqueado(email: str, ip_hash: str) -> int:
    """Segundos que faltam para poder tentar de novo. Zero = liberado."""
    for chave, teto in (
        (_chave_freio("conta", (email or "").strip().lower()), TETO_POR_CONTA),
        (_chave_freio("ip", ip_hash), TETO_POR_IP),
    ):
        try:
            if int(redis.get(chave) or 0) >= teto:
                return max(redis.ttl(chave), 1)
        except Exception:  # Redis fora não pode impedir o dono de entrar
            log.warning("freio de login indisponível", exc_info=True)
            return 0
    return 0


def registrar_falha(email: str, ip_hash: str) -> None:
    try:
        tubo = redis.pipeline()
        for chave in (
            _chave_freio("conta", (email or "").strip().lower()),
            _chave_freio("ip", ip_hash),
        ):
            tubo.incr(chave)
            tubo.expire(chave, JANELA_FREIO)
        tubo.execute()
    except Exception:
        log.warning("não foi possível registrar falha de login", exc_info=True)


def limpar_falhas(email: str, ip_hash: str) -> None:
    try:
        redis.delete(
            _chave_freio("conta", (email or "").strip().lower()),
            _chave_freio("ip", ip_hash),
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# sessões


def _chave_sessao(token: str) -> str:
    # Guarda-se o hash do token, não o token. Assim um dump do Redis não
    # entrega sessão viva de ninguém.
    return f"{SLUG}:sessao:{hashlib.sha256(token.encode()).hexdigest()}"


def _chave_do_usuario(usuario_id: int) -> str:
    return f"{SLUG}:sessoes:{usuario_id}"


def abrir_sessao(usuario: Usuario, ip_hash: str = "") -> str:
    token = secrets.token_urlsafe(32)
    dados = {
        "usuario_id": usuario.id,
        "nome": usuario.nome,
        "email": usuario.email,
        "papel": usuario.papel,
        "precisa_trocar_senha": bool(usuario.precisa_trocar_senha),
        "aberta_em": datetime.now(timezone.utc).isoformat(),
        "ip_hash": ip_hash,
    }
    tubo = redis.pipeline()
    tubo.setex(_chave_sessao(token), VALIDADE, json.dumps(dados))
    tubo.sadd(_chave_do_usuario(usuario.id), _chave_sessao(token))
    tubo.expire(_chave_do_usuario(usuario.id), VALIDADE)
    tubo.execute()
    return token


def ler_sessao(token: str | None) -> dict | None:
    if not token:
        return None
    chave = _chave_sessao(token)
    try:
        cru = redis.get(chave)
    except Exception:
        log.error("Redis indisponível ao ler sessão", exc_info=True)
        return None
    if not cru:
        return None
    try:
        dados = json.loads(cru)
    except ValueError:
        redis.delete(chave)
        return None
    # Desliza a validade, mas no máximo uma vez por dia: renovar a cada
    # requisição seria uma escrita no Redis por página carregada.
    try:
        if redis.ttl(chave) < VALIDADE - RENOVAR_APOS:
            redis.expire(chave, VALIDADE)
    except Exception:
        pass
    return dados


def fechar_sessao(token: str | None) -> None:
    if not token:
        return
    chave = _chave_sessao(token)
    dados = ler_sessao(token)
    tubo = redis.pipeline()
    tubo.delete(chave)
    if dados:
        tubo.srem(_chave_do_usuario(dados["usuario_id"]), chave)
    tubo.execute()


def encerrar_todas(usuario_id: int) -> int:
    """Derruba todas as sessões de um usuário. É o que faz 'revogar acesso'
    ser verdade, e não só desmarcar uma coluna no banco."""
    indice = _chave_do_usuario(usuario_id)
    try:
        chaves = list(redis.smembers(indice))
        if chaves:
            redis.delete(*chaves)
        redis.delete(indice)
        return len(chaves)
    except Exception:
        log.warning("não foi possível encerrar sessões de %s", usuario_id, exc_info=True)
        return 0


def sessoes_ativas(usuario_id: int) -> int:
    try:
        chaves = list(redis.smembers(_chave_do_usuario(usuario_id)))
        if not chaves:
            return 0
        # O índice é um conjunto que pode conter chave já expirada; contar por
        # `exists` evita mostrar sessão fantasma na tela da equipe.
        return sum(1 for c in chaves if redis.exists(c))
    except Exception:
        return 0
