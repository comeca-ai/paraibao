import os

from .jornal import JORNAL


DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://paraibao:paraibao@db:5432/paraibao"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
# Sobraram da senha básica do navegador, que saiu de cena quando o painel
# ganhou contas de verdade (ver contas.py). Hoje `PANEL_PASSWORD` serve a duas
# coisas e nenhuma delas é autenticar:
#   1. semente da PRIMEIRA conta, uma única vez, com troca obrigatória no
#      primeiro acesso (ver migrate.seed_dono);
#   2. valor de reserva do IMAGE_SECRET, abaixo.
# Por causa do item 2, mudar esta variável invalida toda URL assinada de
# imagem. Defina IMAGE_SECRET explicitamente antes de mexer nela.
PANEL_USER = os.getenv("PANEL_USER", "admin")
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "change-me")
CRAWL_INTERVAL_SECONDS = int(os.getenv("CRAWL_INTERVAL_SECONDS", "600"))
MAX_ITEMS_PER_SOURCE = int(os.getenv("MAX_ITEMS_PER_SOURCE", "30"))
# Assina a URL do proxy de imagem. Sem isso o endpoint vira proxy aberto.
IMAGE_SECRET = os.getenv("IMAGE_SECRET", os.getenv("PANEL_PASSWORD", "troque-isto"))
# Vêm do perfil do jornal (app/perfis/<slug>.json); a variável de ambiente
# ainda vence, para o mesmo perfil poder rodar em homologação.
BASE_URL = os.getenv("BASE_URL", JORNAL.base_url).rstrip("/")
USER_AGENT = JORNAL.user_agent

# ----------------------------------------------------------------------- IA
# Quem reescreve a notícia para virar texto próprio. Ver redator.py.
#   gemini    = plano gratuito do Google AI Studio (padrão)
#   anthropic = pago por token, melhor português, sem uso do texto para treino
#   off       = não reescreve nada
#   cloudflare= Workers AI, franquia de 10.000 neurônios por DIA, renovável,
#               e a Cloudflare não treina com o conteúdo enviado
#   ollama    = servidor próprio do Wander (ollama.decisao.ai, atrás de
#               Cloudflare Access). Custo zero e o texto não sai de casa, mas
#               é o mais lento: ~120s por matéria. Ver ia.py.
#   alibaba   = Alibaba Model Studio (Qwen, Kimi, DeepSeek, GLM) pelo modo
#               compatível com a OpenAI. Pago por token e a ~2s por matéria
#               desde Singapura. Ver ia.py: raciocínio vai DESLIGADO.
IA_PROVEDOR = os.getenv("IA_PROVEDOR", "gemini").strip().lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODELO = os.getenv("GEMINI_MODELO", "gemini-2.5-flash")
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "")
# Precisa ser um modelo da lista que suporta JSON Mode; fora dela a saída
# volta em prosa e o parser quebra. O 70B é o melhor em português entre eles.
CF_MODELO = os.getenv("CF_MODELO", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
# Alibaba Model Studio (Qwen, Kimi, DeepSeek, GLM) pelo modo compatível com a
# OpenAI. O endpoint nativo DashScope do workspace devolve "url error", só o
# compatible-mode responde. A base já inclui /compatible-mode/v1.
ALIBABA_API_KEY = os.getenv("ALIBABA_API_KEY", "")
ALIBABA_BASE_URL = os.getenv("ALIBABA_BASE_URL", "").rstrip("/")
ALIBABA_MODELO = os.getenv("ALIBABA_MODELO", "qwen3.8-flash")
# US$ por milhão de tokens do modelo escolhido. Fica em env porque o workspace
# serve 165 modelos com preços diferentes; sem isso o painel mostraria a
# reescrita como gratuita, que é justamente o erro que a coluna existe para evitar.
ALIBABA_PRECO_ENTRADA = float(os.getenv("ALIBABA_PRECO_ENTRADA", "0"))
ALIBABA_PRECO_SAIDA = float(os.getenv("ALIBABA_PRECO_SAIDA", "0"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
IA_MODELO = os.getenv("IA_MODELO", "claude-haiku-4-5")
# Teto de reescritas por dia. É a trava que impede a conta de surpreender, e
# vale igual no plano gratuito, onde o limite é de requisição e não de dinheiro.
IA_TETO_DIA = int(os.getenv("IA_TETO_DIA", "60"))
# Quantas por rodada do worker. Espalha o consumo em vez de estourar o limite
# por minuto do provedor logo na primeira execução.
IA_POR_RODADA = int(os.getenv("IA_POR_RODADA", "4"))

# ------------------------------------------------------------------- social
# off  = não faz nada
# dry  = escolhe a matéria, gera o card e monta a legenda, mas NÃO chama a
#        Meta. É o modo em que dá para ver exatamente o que iria ao ar.
# on   = publica de verdade
SOCIAL_MODE = os.getenv("SOCIAL_MODE", "dry").strip().lower()
# Versão da Graph API. Cada versão vive ~2 anos; deixar em env evita ter que
# fazer deploy de código só para trocar o número quando a atual for aposentada.
META_API_VERSION = os.getenv("META_API_VERSION", "v23.0")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
IG_USER_ID = os.getenv("IG_USER_ID", "")
FB_PAGE_ID = os.getenv("FB_PAGE_ID", "")
# Publicar tudo mata o alcance: portal que despeja 200 posts por dia é tratado
# como spam pelo ranqueamento. O teto é editorial, não técnico.
SOCIAL_POSTS_POR_DIA = int(os.getenv("SOCIAL_POSTS_POR_DIA", "8"))
# Espaçamento mínimo entre publicações, em minutos.
SOCIAL_INTERVALO_MIN = int(os.getenv("SOCIAL_INTERVALO_MIN", "75"))
# Não publica notícia velha: se ficou parada mais que isso, perdeu a hora.
SOCIAL_JANELA_HORAS = int(os.getenv("SOCIAL_JANELA_HORAS", "20"))
SOCIAL_LAYOUT = os.getenv("SOCIAL_LAYOUT", "tipografico")
# Horário de Brasília em que a máquina pode publicar.
SOCIAL_HORA_INICIO = int(os.getenv("SOCIAL_HORA_INICIO", "6"))
SOCIAL_HORA_FIM = int(os.getenv("SOCIAL_HORA_FIM", "22"))
