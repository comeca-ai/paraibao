"""Checagem do provedor alibaba: usa o SISTEMA e o ESQUEMA de verdade do
redator e passa a saída pelas travas de verdade. Roda dentro do container."""
import os
os.environ["IA_PROVEDOR"] = "alibaba"

from app import ia
from app.redator import SISTEMA, ESQUEMA, verificar

assert ia.disponivel(), "provedor nao disponivel: confira ALIBABA_API_KEY/BASE_URL"
print(f"modelo: {ia.modelo_atual()}")

ORIGINAL = (
    "A Prefeitura de Campina Grande entregou nesta sexta-feira 320 unidades "
    "habitacionais no bairro do Catole. O investimento foi de R$ 24 milhoes, "
    "com recursos do governo federal. As familias beneficiadas estavam em "
    "areas de risco, segundo a Secretaria de Habitacao. O prefeito afirmou "
    "que outras 500 casas serao entregues ate dezembro, em tres bairros da "
    "zona leste da cidade. A lista de contemplados saiu no Diario Oficial. "
    "Cada unidade tem 42 metros quadrados, com dois quartos, sala, cozinha e "
    "banheiro. A entrega faz parte do programa municipal de habitacao lancado "
    "em 2024, que preve 1.200 moradias ate o fim da gestao."
)
prompt = (
    "VEICULO QUE APUROU: Portal Correio\n"
    "Titulo: Prefeitura de Campina Grande entrega 320 casas populares no Catole\n"
    f"Texto: {ORIGINAL}"
)

r = ia.gerar_json(SISTEMA, prompt, ESQUEMA, max_tokens=2000)
d = r["dados"]
print(f"tokens: {r['tokens_entrada']} entrada / {r['tokens_saida']} saida")
print(f"custo estimado: US$ {ia.custo_estimado(r['tokens_entrada'], r['tokens_saida']):.6f}")
print(f"\nTITULO: {d['titulo']}\nOLHO:   {d['olho']}")
for p in d["paragrafos"]:
    print(f"  {p}")

assert set(d) >= {"suficiente", "motivo", "titulo", "olho", "paragrafos"}, d.keys()
assert d["suficiente"] is True, d.get("motivo")
assert isinstance(d["paragrafos"], list) and d["paragrafos"]

ok, motivo = verificar(ORIGINAL, d)
print(f"\ntravas: {'PASSOU' if ok else 'REPROVOU'} ({motivo})")
assert ok, f"reprovou nas travas: {motivo}"

# a trava de numero tem que morder de verdade, senao ela nao esta protegendo nada
ruim = dict(d, paragrafos=[p.replace("320", "9871") for p in d["paragrafos"]])
ruim_ok, ruim_motivo = verificar(ORIGINAL, ruim)
assert not ruim_ok, "trava de numero inventado nao pegou"
print(f"trava de numero: OK ({ruim_motivo})")
print("\nTUDO CERTO")
