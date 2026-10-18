# Plano: Paraibão como o maior jornal da Paraíba, com franquias

## Princípio central

**O jornal é a base de tudo.** A franquia não é um produto separado: ela é uma extensão do jornal. Marca, plataforma, audiência, distribuição e padrão editorial pertencem à matriz. O franqueado opera uma praça dentro dessa estrutura e nunca fora dela.

Consequência prática: primeiro o jornal precisa ser o maior e o mais confiável. A franquia só se vende bem depois que o jornal prova audiência, receita e método.

## 1. Modelo editorial: três camadas

- **Núcleo (matriz):** política estadual, economia, grandes investigações e a capa. A matriz controla tudo isso, e é isso que dá autoridade à marca.
- **Local (franqueado):** prefeitura, câmara, polícia, comércio, esporte e cultura da cidade. É o conteúdo que a matriz não consegue produzir sozinha, e o que faz o jornal ser o maior do estado.
- **Rede:** o que o franqueado produz e a matriz promove para a capa estadual. Isso dá ao franqueado um incentivo de qualidade, porque ele quer ser promovido.

## 2. Revisão: fluxo com papéis, não um gargalo

- O franqueado (repórter local) escreve e submete. Um editor da matriz aprova ou devolve.
- Para evitar fila, os franqueados que se provarem confiáveis ganham publicação direta para o local, com revisão por amostragem. Os novos começam com revisão de tudo.
- Os temas de risco (denúncia, crime, política, processo) passam sempre pela revisão, mesmo para os mais confiáveis. É onde estão os riscos jurídicos e de reputação.
- Manter um manual editorial curto e um registro de quem aprovou o quê.

## 3. Franquia: o que a matriz entrega

- Domínio e marca (`cidade.paraibao.com.br` ou seção da cidade), plataforma, painel, hospedagem e backup.
- Distribuição: capa estadual, redes sociais, newsletter e WhatsApp.
- Publicidade nacional e estadual vendida pela matriz. O franqueado vende o comércio local.
- Treinamento e o manual editorial.

## 4. Dinheiro e risco

- Taxa de adesão mais um percentual sobre a receita, e não sobre o conteúdo. Assim não se incentiva volume em vez de qualidade.
- O contrato deve prever suspensão e descredenciamento por violação editorial.
- A responsabilidade jurídica por difamação costuma recair sobre o veículo. O contrato deve exigir fontes, direito de resposta e indenização regressiva. Consultar um advogado de mídia antes de vender a primeira franquia.

## 5. Ordem de execução

1. Fortalecer o jornal-base: audiência, receita, processo editorial e estabilidade da plataforma.
2. Provar com 1 ou 2 cidades-piloto (próprias ou de parceiros próximos) antes de vender a franquia.
3. Só vender depois que o fluxo de revisão estiver rodando e medido (tempo de aprovação, taxa de devolução).
4. Escalar por regiões (Litoral, Brejo, Sertão), com um editor regional para cada grupo de franqueados.

## 6. Plataforma: o que provavelmente falta

O código atual já tem papéis (`dono`, `editor` e um terceiro mais restrito) e perfis de jornal (`pb.json`, `pe-prova.json`), então já foi pensado para mais de um jornal. Para a franquia, provavelmente falta:

- **Escopo por cidade ou franqueado:** o franqueado só vê e edita o conteúdo da sua praça.
- **Fluxo de submissão e aprovação:** rascunho do franqueado, fila do editor da matriz e publicação.
- **Promoção:** uma matéria local pode subir para a capa estadual.

Próximo passo: ler `contas.py` e o modelo de matérias e propor o desenho mínimo disso, sem alterar nada ainda.

## Pendências operacionais (da migração de 2026-10-18)

- Backup off-site ainda não existe para nenhuma das VMs.
- Origem Azure parada como rollback até cerca de 2026-10-25; depois pode ser apagada.
- O `deploy.sh` antigo dos devs não funciona mais; avisar quem usava.
- Redefinir a senha de acesso ao painel (conta de dono) por um caminho autorizado.
