from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Municipality(Base):
    """Os 223 municípios da Paraíba. Seed pela API do IBGE, ver municipios_pb.py."""

    __tablename__ = "municipalities"

    id: Mapped[int] = mapped_column(primary_key=True)
    ibge_code: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    micro_region: Mapped[str] = mapped_column(String(120), default="")
    meso_region: Mapped[str] = mapped_column(String(120), default="", index=True)
    immediate_region: Mapped[str] = mapped_column(String(120), default="")
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    # Nome que colide com palavra comum ou sobrenome (Areia, Sousa, Boa Vista).
    # Só casa se houver marcador geográfico por perto. Ver territory.py.
    ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    feed_url: Mapped[str] = mapped_column(String(500), unique=True)
    site_url: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(2), default="PB")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    # O que pode ser publicado a partir dessa fonte. Amarra a camada de conteúdo
    # à fonte no banco, e não à disciplina de quem escreveu o código.
    #   link_out = manchete + resumo próprio + link que sai (portal comercial)
    #   full     = pode publicar texto integral (dado público, licença, ou nosso)
    #   official = comunicado institucional identificado
    content_policy: Mapped[str] = mapped_column(String(20), default="link_out", index=True)
    license_basis: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default="rss")
    trust_level: Mapped[int] = mapped_column(Integer, default=50)
    default_municipality_id: Mapped[int | None] = mapped_column(
        ForeignKey("municipalities.id"), index=True
    )
    slug: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)
    # Data da matéria mais recente já coletada. É o que torna a coleta
    # incremental: a próxima rodada pede só o que saiu depois disso.
    cursor_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    articles: Mapped[list["Article"]] = relationship(back_populates="source")


class MunicipalityTag(Base):
    """Mapa município -> tag no CMS da fonte.

    É o que permite BUSCAR notícia de um município em vez de esperar ela
    aparecer no feed. Medido em 2026-08-02: 15 de 16 municípios da amostra
    têm tag própria no ClickPB, incluindo Zabelê (7 matérias) e Coxixola (9).

    Matéria coletada por aqui tem o município conhecido de antemão, então o
    vínculo entra com confiança 1.0 e não passa pelo classificador.
    """

    __tablename__ = "municipality_tags"
    __table_args__ = (UniqueConstraint("municipality_id", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    municipality_id: Mapped[int] = mapped_column(ForeignKey("municipalities.id"), index=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    # None = já procurei e a fonte não tem tag para esse município.
    tag_id: Mapped[int | None] = mapped_column(Integer)
    tag_name: Mapped[str | None] = mapped_column(String(200))
    post_count: Mapped[int] = mapped_column(Integer, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cursor por município+fonte: a coleta de cada cidade anda sozinha.
    cursor_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("canonical_url"), UniqueConstraint("fingerprint"))

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    title: Mapped[str] = mapped_column(String(700))
    canonical_url: Mapped[str] = mapped_column(String(1000))
    summary: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String(250))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    # Assinatura do título para pegar a mesma matéria republicada
    # com outra URL. Ver chave_titulo() em collector.py.
    title_key: Mapped[str | None] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(30), default="collected", index=True)

    # Decide o template, se existe página interna e o que entra no sitemap.
    #   aggregate = card linka direto pra fonte, sem página nossa
    #   own       = matéria nossa a partir de dado público, tem página interna
    #   official  = comunicado de órgão verificado, página interna identificada
    layer: Mapped[str] = mapped_column(String(20), default="aggregate", index=True)
    slug: Mapped[str | None] = mapped_column(String(240), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(40), default="geral", index=True)
    category_confidence: Mapped[float | None] = mapped_column(Float)
    # Texto do veículo de origem. Nunca é sobrescrito: é a prova do que foi
    # reescrito e a base das verificações em redator.py.
    body: Mapped[str | None] = mapped_column(Text)

    # Texto próprio, escrito a partir do material acima. Quando existe, a
    # matéria vira layer='own' e é ele que aparece no site.
    own_title: Mapped[str | None] = mapped_column(String(700))
    own_summary: Mapped[str | None] = mapped_column(Text)
    own_body: Mapped[str | None] = mapped_column(Text)
    rewritten_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    rewrite_model: Mapped[str | None] = mapped_column(String(60))
    rewrite_error: Mapped[str | None] = mapped_column(Text)
    rewrite_tokens_in: Mapped[int | None] = mapped_column(Integer)
    rewrite_tokens_out: Mapped[int | None] = mapped_column(Integer)

    source: Mapped[Source] = relationship(back_populates="articles")


class ArticleMunicipality(Base):
    __tablename__ = "article_municipalities"
    __table_args__ = (UniqueConstraint("article_id", "municipality_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    municipality_id: Mapped[int] = mapped_column(ForeignKey("municipalities.id"), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # source_default | name | alias | llm
    detection_method: Mapped[str] = mapped_column(String(30), default="name")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)


class SocialPost(Base):
    """Uma publicação de uma matéria numa rede.

    A chave única (article_id, network) é o que garante que a mesma notícia
    nunca vá ao ar duas vezes na mesma rede, mesmo que o worker reinicie no
    meio da rodada ou que a chamada à Meta caia depois de já ter publicado.
    """

    __tablename__ = "social_posts"
    __table_args__ = (UniqueConstraint("article_id", "network"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    network: Mapped[str] = mapped_column(String(20), index=True)  # instagram | facebook
    # queued = escolhida e pronta | published | failed | dry (simulada)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    external_id: Mapped[str | None] = mapped_column(String(80))
    permalink: Mapped[str | None] = mapped_column(String(500))
    caption: Mapped[str | None] = mapped_column(Text)
    layout: Mapped[str | None] = mapped_column(String(30))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class Contato(Base):
    """Quem chega pelo site: anunciante ou quem quer escrever no jornal.

    Uma tabela só para as duas portas porque as duas viram a mesma fila de
    atendimento humano e, mais para frente, o mesmo convite de WhatsApp. O
    que muda entre elas é o `tipo` e dois campos opcionais.

    Nada aqui é apagado pelo site: a triagem acontece pelo `status`.
    """

    __tablename__ = "contatos"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 'anuncio' = quer anunciar · 'colaborador' = quer escrever para o jornal
    tipo: Mapped[str] = mapped_column(String(20), index=True)

    nome: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(200), index=True)
    # Guardado só com dígitos e com DDI, para o convite do WhatsApp mais tarde
    # não precisar adivinhar formato. O que a pessoa digitou fica em
    # `telefone_digitado`, porque normalização é palpite e pode errar.
    telefone: Mapped[str] = mapped_column(String(20), index=True)
    telefone_digitado: Mapped[str] = mapped_column(String(40), default="")
    cidade: Mapped[str] = mapped_column(String(120), default="")

    # Só anunciante preenche
    empresa: Mapped[str | None] = mapped_column(String(160))
    # Só colaborador preenche: área de interesse e link de portfólio
    area: Mapped[str | None] = mapped_column(String(80))
    link: Mapped[str | None] = mapped_column(String(500))

    mensagem: Mapped[str] = mapped_column(Text, default="")

    # De onde veio, para saber qual página converte
    origem: Mapped[str] = mapped_column(String(200), default="")
    # Hash do IP, não o IP: serve para limitar abuso sem guardar dado pessoal
    ip_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    user_agent: Mapped[str] = mapped_column(String(300), default="")

    status: Mapped[str] = mapped_column(String(20), default="novo", index=True)
    nota: Mapped[str | None] = mapped_column(Text)

    # Ponto de encaixe da Evolution: fica nulo até o convite de grupo existir.
    # A coluna nasce junto para ligar o WhatsApp depois não pedir migração.
    whatsapp_convidado_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )


class Usuario(Base):
    """Quem entra no jornal: dono, editor e, mais para frente, assinante.

    Uma tabela só para os três porque autenticação é a mesma coisa nos três
    casos; o que muda é o `papel`. Separar em `admins` e `assinantes` obrigaria
    a duplicar senha, sessão e recuperação de acesso, e é justamente onde esse
    tipo de sistema costuma vazar.

    As colunas de assinatura já nascem aqui, nulas. Não é adivinhação de
    requisito: é para que ligar cobrança depois não exija migração numa tabela
    que já vai estar guardando login de gente de verdade.
    """

    __tablename__ = "usuarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    # scrypt$n$r$p$sal$hash — ver contas.py. Nunca guarda a senha.
    senha_hash: Mapped[str] = mapped_column(String(255))
    papel: Mapped[str] = mapped_column(String(20), default="assinante", index=True)

    # Desativar é reversível e preserva o histórico; apagar não. Quem sai do
    # jornal vira `ativo=False`, e as sessões dele caem na hora.
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    precisa_trocar_senha: Mapped[bool] = mapped_column(Boolean, default=False)

    # Encaixe da assinatura. Vazio = não paga nada.
    plano: Mapped[str] = mapped_column(String(30), default="")
    assinatura_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    criado_em: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )
    senha_alterada_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ultimo_acesso_em: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Hash, não o IP: serve para notar acesso de lugar novo sem guardar dado
    # pessoal de quem trabalha aqui.
    ultimo_ip_hash: Mapped[str] = mapped_column(String(64), default="")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="running")
    sources_ok: Mapped[int] = mapped_column(Integer, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, default=0)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
