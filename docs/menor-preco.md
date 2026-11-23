# Menor Preço (Nota Paraná): contrato não documentado

Este é o registro do que foi tirado do JavaScript do site oficial
(`https://menorpreco.notaparana.pr.gov.br/`) em nov/2026, não de documentação da Secretaria
da Fazenda. **Pode mudar sem aviso.** Não há limite numérico de consultas publicado.

A investigação foi estática: foram baixados apenas os bundles públicos do site e feita uma
consulta a `/mapa/search?regiao=Curitiba`. Não foi chamado `/produtos` nem `/categorias`.

## Endpoints

Base: `https://menorpreco.notaparana.pr.gov.br/api/v1`

| Caminho | Uso |
|---|---|
| `/produtos` | busca de ofertas |
| `/categorias` | categorias possíveis para um termo, com `qtd` |
| `/produtos/{id}` | detalhe de uma oferta |
| `/historico/{id}` | histórico de preços de uma oferta |
| `/mapa/search?regiao=Curitiba` | geohash de uma cidade (fora de `/api/v1`) |

## Parâmetros de `/produtos` e `/categorias`

| Parâmetro | Observação |
|---|---|
| `local` | geohash; o site usa 9 a 12 caracteres |
| `termo` | texto livre (produto, marca ou código de barras) |
| `categoria` | id numérico, opcional |
| `offset` | começa em `0` |
| `raio` | km; default do site `2` |
| `gtin` | código de barras |
| `data` | janela temporal, ver abaixo; default do site `-1` |
| `valor_min`, `valor_max` | default `[0, 999999]` |
| `ordem` | ver abaixo; default `0` |
| `tp_comb` | tipo de combinação |

`data`: `-1` últimos 2 meses · `0` último mês · `1` 15 dias · `2` semana · `3` 24 h ·
`4` 12 h · `5` 6 h · `6` 1 h.

`ordem`: `0` Menor Preço · `1` Distância · `2` Data da venda · `3` Maior Preço.

A resposta de `/produtos` traz `total` e `precos.min/max`. Cada item tem `desc`, `valor`,
`gtin`, `datahora`, `distkm`, `local` e `estabelecimento` (com `codigo`, `nm_fan`, `nm_emp`,
`tp_logr`, `nm_logr`, `nr_logr`, `bairro`).

## Regras do site

- Busca `/categorias` primeiro. Com **0** categorias, busca sem `categoria`; com **1**,
  seleciona automaticamente; com **2 ou mais**, pede para a pessoa escolher. Escolher a de
  maior `qtd` não é o comportamento do site e pode esconder ofertas do mesmo produto.
- Uma página de **50** resultados, `offset = página * 50`, usando `total` para parar.
- O geohash vem de `geohash.encode(latitude, longitude)` com precisão **12** (≈ 3 cm × 2 cm)
  quando a posição exata do navegador é usada; `/mapa/search` devolve geohash de 9 caracteres
  para uma cidade inteira.

## Como o grana consulta

O navegador do celular faz as mesmas consultas públicas, em ritmo humano (1 pedido a cada
3 s), apenas dos itens da semana que ainda não têm preço hoje. O servidor não consulta nada:
`GRANA_PRICES` fica desligado e recebe só os resultados. O cliente em `web-ui/src/phonePrices.js`
usa `data=2` (semana, compatível com a janela de 7 dias usada ao decidir se um preço ainda
vale), `ordem=0`, uma página, e `categoria` somente quando `/categorias` devolve exatamente uma.

Itens avulsos pedidos no chat (fralda, por exemplo) entram na mesma fila por
`add_price_item(item, brand?, gtin?, category?)`. Antes de anotar, o assistente deve perguntar
o detalhe que falta: "carne" sozinho é ambíguo (bovina, suína, frango, qual corte?). A resposta
da ferramenta mostra o `termo` exato que será enviado, para conferir. Se a pessoa souber o
código de barras, ele vai em `gtin` **no lugar de** `termo` — é uma busca exata e não gera a
rodada extra de `/categorias`. Uma `categoria` escolhida também evita `/categorias`; sem ela,
itens avulsos que casam com 2+ categorias são marcados como ambíguos e **não** consomem uma
consulta a `/produtos`. A lista de categorias volta em `price_status().last_phone_lookup.ambiguous`,
e `price_items()` mostra o que está na fila. No máximo 8 itens avulsos por vez,
deduplicados com os ingredientes do cardápio.

O geohash guardado no SQLite é gerado por `geo.geohash` com precisão **12** (≈ 3 cm × 2 cm),
a mesma do site oficial. Como a precisão era 6 até nov/2026, é preciso tocar "usar minha
localização" de novo no celular para substituir o valor antigo: os 6 caracteres que faltam
não podem ser deduzidos do geohash curto. Até isso acontecer, buscas com o geohash antigo
continuam funcionando.

## Anti-bot e termos de uso

Contra excesso de consultas, IPs de datacenter ou automação, o serviço responde com dados
embaralhados: nomes de mercado com letras trocadas, `desc` contendo só a palavra buscada,
preços falsos e `estabelecimento.codigo` com ~85 caracteres (os reais têm 20 a 45). O grana
detecta isso e **para sem gravar nada**; não tenta burlar o bloqueio.

Os termos de uso não publicam um número de consultas: falam em "número limitado de consultas"
sem login, proíbem automação, uso para `pricing` e uso abusivo, e prevêem bloqueio por IP,
ASN, CPF e contas Google/Facebook. A cláusula (g) proíbe "utilizar os serviços da Plataforma
em outro aplicativo ou site" — o que inclui o grana. Por isso a estratégia de consultar pelo
celular da casa, devagar, é a menor exposição, mas não elimina esse risco.

## Pendências de validação

Só fariam sentido com 1 ou 2 chamadas manuais, com consentimento e a partir do celular:

- se a página realmente vem com 50 itens e se `total` é confiável;
- se consultas por `gtin` funcionam bem como substituto de `termo` — o grana já aceita o
  código em `add_price_item`, mas ainda não o tirou automaticamente dos cupons NFC-e lidos;
- o limite real de consultas, que não está publicado;

Não gravar respostas embaralhadas como se fossem preços, nem testar limites aumentando o
volume de chamadas.
