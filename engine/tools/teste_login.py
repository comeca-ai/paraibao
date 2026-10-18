"""Teste de ponta a ponta do login do painel, sem Docker.

Existe porque errar em autenticação não dá aviso: ou tranca o dono do lado de
fora, ou deixa a porta aberta, e nos dois casos só se descobre em produção.

Roda contra um Postgres local descartável e um Redis falso, então não toca em
nada de verdade. O `.env` de produção não é lido.

    createdb -p 5433 -U postgres paraibao_teste
    ./.venv-previa/bin/python tools/teste_login.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

# Precisa vir ANTES de importar app.db, que monta a engine na importação.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://postgres@127.0.0.1:5433/paraibao_teste",
)
os.environ["JORNAL_PERFIL"] = "pb"
# Fora do container não existe /var/cache/paraibao-img, e imagens.py cria o
# diretório na importação.
os.environ.setdefault("IMAGE_CACHE_DIR", "/tmp/paraibao-teste-img")
os.environ["PANEL_PASSWORD"] = "senha-inicial-do-env"
os.environ["DONO_EMAIL"] = "dono@paraibao.com.br"

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import contas, main  # noqa: E402

FALSO = fakeredis.FakeRedis(decode_responses=True)
contas.redis = FALSO
main.redis = FALSO

PAINEL = "http://painel.paraibao.com.br"
JORNAL = "http://paraibao.com.br"

falhas: list[str] = []
feitos = 0


def conferir(condicao: bool, descricao: str, detalhe: str = "") -> None:
    global feitos
    feitos += 1
    if condicao:
        print(f"  ok   {descricao}")
    else:
        print(f"  FALHA {descricao}" + (f"  [{detalhe}]" if detalhe else ""))
        falhas.append(descricao)


def main_teste() -> int:
    with TestClient(main.app, base_url=PAINEL, follow_redirects=False) as c:
        print("\n— porta de entrada")
        r = c.get("/entrar")
        conferir(r.status_code == 200 and "Entrar" in r.text, "GET /entrar abre a tela", r.status_code)

        r = c.get("/painel/maquina")
        conferir(
            r.status_code == 303 and "/entrar" in r.headers.get("location", ""),
            "painel sem sessão manda para /entrar", r.headers.get("location", ""),
        )

        r = c.get("/api/metricas")
        conferir(r.status_code == 401, "API sem sessão devolve 401", r.status_code)

        print("\n— senha errada")
        r = c.post("/entrar", data={"email": "dono@paraibao.com.br", "senha": "chute"})
        conferir(
            r.status_code == 200 and "incorretos" in r.text,
            "senha errada volta com erro na tela", r.status_code,
        )
        conferir(
            contas.COOKIE not in r.cookies, "senha errada não grava cookie",
        )

        print("\n— entrada correta")
        r = c.post(
            "/entrar",
            data={"email": "dono@paraibao.com.br", "senha": "senha-inicial-do-env"},
        )
        conferir(r.status_code == 303, "entrada correta redireciona", r.status_code)
        conferir(
            r.headers.get("location") == "/senha",
            "primeira entrada é obrigada a trocar a senha", r.headers.get("location"),
        )
        biscoito = r.cookies.get(contas.COOKIE)
        conferir(bool(biscoito), "cookie de sessão foi gravado")
        cabecalho = r.headers.get("set-cookie", "")
        conferir("HttpOnly" in cabecalho, "cookie é HttpOnly", cabecalho[:90])
        conferir("samesite=strict" in cabecalho.lower(), "cookie é SameSite=Strict", cabecalho)

        c.cookies.set(contas.COOKIE, biscoito)

        r = c.get("/painel/maquina")
        conferir(
            r.status_code == 303 and r.headers.get("location") == "/senha",
            "com senha pendente o painel continua bloqueado", r.status_code,
        )

        print("\n— troca de senha obrigatória")
        r = c.post("/senha", data={"nova": "curta", "repetida": "curta"})
        conferir("pelo menos" in r.text, "senha curta é recusada", r.status_code)

        r = c.post("/senha", data={"nova": "senha-nova-boa-1", "repetida": "outra-coisa"})
        conferir("não são iguais" in r.text, "senhas diferentes são recusadas", r.status_code)

        r = c.post("/senha", data={"nova": "senha-nova-boa-1", "repetida": "senha-nova-boa-1"})
        conferir(
            r.status_code == 303 and r.headers.get("location") == "/painel/maquina",
            "troca válida leva ao painel", f"{r.status_code} {r.headers.get('location')}",
        )
        if r.cookies.get(contas.COOKIE):
            c.cookies.set(contas.COOKIE, r.cookies.get(contas.COOKIE))

        print("\n— dentro do painel")
        r = c.get("/painel/maquina")
        conferir(r.status_code == 200, "tela da máquina abre", r.status_code)
        conferir("sair" in r.text.lower(), "tela da máquina mostra quem está logado")

        r = c.get("/api/metricas")
        conferir(r.status_code == 200, "API responde com sessão", r.status_code)

        r = c.get("/painel/equipe")
        conferir(r.status_code == 200 and "Equipe" in r.text, "dono abre a tela de equipe", r.status_code)

        print("\n— senha antiga não vale mais")
        r = c.post(
            "/entrar",
            data={"email": "dono@paraibao.com.br", "senha": "senha-inicial-do-env"},
        )
        conferir("incorretos" in r.text, "senha antiga foi invalidada", r.status_code)
        contas.limpar_falhas("dono@paraibao.com.br", "")

        print("\n— criar editor")
        r = c.post(
            "/painel/equipe/criar",
            data={"nome": "Repórter", "email": "reporter@paraibao.com.br", "papel": "editor"},
        )
        conferir(r.status_code == 303, "criação redireciona de volta", r.status_code)
        r = c.get("/painel/equipe")
        conferir("reporter@paraibao.com.br" in r.text, "editor aparece na lista")
        conferir("não aparece de novo" in r.text, "senha temporária é exibida uma vez")
        r = c.get("/painel/equipe")
        conferir(
            "senha1x" not in r.text or "não aparece de novo" not in r.text,
            "senha temporária some no segundo carregamento",
        )

        r = c.post(
            "/painel/equipe/criar",
            data={"nome": "Outro", "email": "reporter@paraibao.com.br", "papel": "editor"},
        )
        # A mensagem vem no destino do 303, não numa visita solta à tela.
        r = c.get(r.headers["location"])
        conferir("Já existe uma conta" in r.text, "e-mail repetido é recusado")
        r = c.get("/painel/equipe?erro=<script>alert(1)</script>")
        conferir(
            "<script>alert(1)" not in r.text,
            "recado inventado na URL não aparece na tela",
        )

    print("\n— editor não é dono")
    with TestClient(main.app, base_url=PAINEL, follow_redirects=False) as c2:
        # A senha temporária não é recuperável de propósito; para o teste,
        # define-se uma conhecida direto no banco, que é o que o dono faria
        # pela tela "Nova senha".
        from app.db import SessionLocal
        from app.models import Usuario
        from sqlalchemy import select

        with SessionLocal() as s:
            u = s.scalar(select(Usuario).where(Usuario.email == "reporter@paraibao.com.br"))
            u.senha_hash = contas.hash_senha("senha-do-reporter")
            u.precisa_trocar_senha = False
            s.commit()

        r = c2.post(
            "/entrar",
            data={"email": "reporter@paraibao.com.br", "senha": "senha-do-reporter"},
        )
        conferir(
            r.status_code == 303 and r.headers.get("location") == "/painel/maquina",
            "editor entra direto no painel", r.headers.get("location"),
        )
        c2.cookies.set(contas.COOKIE, r.cookies.get(contas.COOKIE))
        conferir(c2.get("/painel/maquina").status_code == 200, "editor abre a máquina")
        conferir(c2.get("/painel/equipe").status_code == 404, "editor NÃO abre a equipe")
        conferir(
            c2.post("/painel/equipe/criar", data={"nome": "x", "email": "x@y.com"}).status_code == 404,
            "editor NÃO cria conta",
        )

        print("\n— sair")
        r = c2.get("/sair")
        conferir(r.status_code == 303, "sair redireciona", r.status_code)
        r = c2.get("/painel/maquina")
        conferir(r.status_code == 303, "depois de sair o painel bloqueia de novo", r.status_code)

    print("\n— freio de tentativas")
    with TestClient(main.app, base_url=PAINEL, follow_redirects=False) as c3:
        bloqueou = False
        for _ in range(contas.TETO_POR_CONTA + 2):
            r = c3.post("/entrar", data={"email": "dono@paraibao.com.br", "senha": "errada"})
            if "Tentativas demais" in r.text:
                bloqueou = True
                break
        conferir(bloqueou, "força bruta é freada depois de poucas tentativas")

    print("\n— redirecionamento aberto")
    contas.limpar_falhas("dono@paraibao.com.br", "")
    with TestClient(main.app, base_url=PAINEL, follow_redirects=False) as c4:
        r = c4.post(
            "/entrar",
            data={
                "email": "dono@paraibao.com.br",
                "senha": "senha-nova-boa-1",
                "de": "https://site-do-golpe.example/",
            },
        )
        conferir(
            r.headers.get("location") == "/painel/maquina",
            "destino externo é ignorado", r.headers.get("location"),
        )

    print("\n— separação de domínio")
    with TestClient(main.app, base_url=JORNAL, follow_redirects=False) as c5:
        conferir(c5.get("/entrar").status_code == 404, "login não existe no domínio do jornal")
        conferir(c5.get("/painel/equipe").status_code == 404, "equipe não existe no domínio do jornal")
        conferir(c5.get("/").status_code == 200, "o jornal continua abrindo normalmente")

    print(f"\n{feitos - len(falhas)}/{feitos} verificações passaram")
    if falhas:
        print("\nFALHOU:")
        for f in falhas:
            print(f"  · {f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_teste())
