import logging
import time

from redis import Redis
from sqlalchemy import select

from .collector import crawl_all
from .enrich import enriquecer_pendentes
from . import publicador, redator, tempo
from .config import CRAWL_INTERVAL_SECONDS, REDIS_URL
from . import migrate
from .db import SessionLocal
from .models import Source
from .jornal import FILA_COLETA, fontes as FONTES_DO_PERFIL


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("paraibao.worker")


def seed_sources():
    """Cria as fontes novas e sincroniza o degrau de extração das que existem."""
    with SessionLocal() as session:
        for item in FONTES_DO_PERFIL():
            existente = session.scalar(select(Source).where(Source.feed_url == item["feed_url"]))
            if existente is None:
                session.add(Source(**item, state="PB"))
                continue
            # Mudar o `kind` no código tem que valer em produção. Sem isto, a
            # fonte fica presa no adaptador com que foi criada.
            if existente.kind != item["kind"]:
                log.info(
                    "fonte %s: %s -> %s", existente.name, existente.kind, item["kind"]
                )
                existente.kind = item["kind"]
            existente.site_url = item["site_url"]
        session.commit()


def main():
    migrate.run()
    seed_sources()
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    next_scheduled = 0.0
    log.info("worker iniciado; intervalo=%ss", CRAWL_INTERVAL_SECONDS)
    while True:
        now = time.time()
        requested = redis.lpop(FILA_COLETA)
        if requested or now >= next_scheduled:
            with SessionLocal() as session:
                run = crawl_all(session)
                enriquecidos = enriquecer_pendentes(session)
                log.info(
                    "coleta #%s: status=%s novas=%s vistas=%s enriquecidas=%s",
                    run.id,
                    run.status,
                    run.items_new,
                    run.items_seen,
                    enriquecidos,
                )
                # A reescrita vem antes da publicação: assim o card e a
                # legenda saem com o título próprio, e não com o do outro
                # veículo. Falha aqui não pode derrubar a coleta.
                try:
                    proprias = redator.rodar(session)
                    if proprias:
                        log.info("redator: %s matérias viraram texto próprio", proprias)
                except Exception:
                    log.exception("redator falhou")

            # Previsão do tempo: fora da sessão do banco porque não usa banco,
            # e aqui no worker porque nenhum leitor deve esperar requisição
            # externa para a home abrir. Falha só mantém a previsão anterior.
            try:
                tempo.atualizar(redis)
            except Exception:
                log.exception("previsão do tempo falhou")

            with SessionLocal() as session:
                # A publicação vem depois do enriquecimento: o card usa o
                # resumo e a editoria, que só existem quando a matéria já
                # passou pelo enricher.
                try:
                    publicados = publicador.rodar(session)
                    if publicados:
                        log.info("rede social: %s publicações", publicados)
                except Exception:
                    # Falha em rede social não pode derrubar a coleta.
                    log.exception("publicador falhou")
            next_scheduled = time.time() + CRAWL_INTERVAL_SECONDS
        time.sleep(2)


if __name__ == "__main__":
    main()

