# Mapeamento Pluggy → modelo interno

Resolve os pontos em aberto **U3** e **U4** do plano. Cada campo cita a fonte. O que não foi possível confirmar está marcado como **NÃO VERIFICADO**. Esses pontos precisam ser conferidos no primeiro sync real e, se for o caso, corrigidos aqui e no código.

Fontes (consultadas em 15/11/2026):

- **[TX]** Transaction: https://docs.pluggy.ai/en/docs/products/transactions.md
- **[OAS]** OpenAPI oficial: https://docs.pluggy.ai/openapi/pluggy-api.json (schemas `Transaction`, `CreditCardMetadata`, `PaymentData`, `PaymentDataParticipant`, `Document`, `Account`, `Item`, `CursorPageResponseTransactions`)
- **[BC]** Basic concepts (paginação): https://docs.pluggy.ai/en/docs/reference/basic-concepts.md
- **[AUTH]** Authentication: https://docs.pluggy.ai/en/docs/reference/authentication.md
- **[MP]** Meu Pluggy: https://docs.pluggy.ai/en/docs/guides/meu-pluggy-personal-use.md
- **[CYC]** Cartão e ciclo de fatura: https://docs.pluggy.ai/en/docs/products/credit-card-statement-cycle.md
- **[INST]** Parcelamento: https://docs.pluggy.ai/en/docs/products/credit-card-installments.md
- **[PD]** Cobertura de Payment Data: https://docs.pluggy.ai/en/docs/open-finance/payment-data.md
- **[CAT]** Categorização: https://docs.pluggy.ai/en/docs/products/transaction-categorization.md
- **[RL]** Rate limits: https://docs.pluggy.ai/en/docs/reference/rate-limits.md
- **[ID]** Identity: https://docs.pluggy.ai/en/docs/products/identity.md, `GET /identity?itemId=` (operationId `identity-find-by-item`) e schema `IdentityResponse` na [OAS]; cobertura em https://docs.pluggy.ai/en/docs/connections/connectors-coverage.md e https://docs.pluggy.ai/en/docs/connections/identity-coverage.md (consultados em 15/11/2026)
- **[BILL]** Credit Card Bills: https://docs.pluggy.ai/en/docs/products/credit-card-bills.md (consultado em 15/11/2026; schemas `Bill`, `BillPayment` e `BillFinanceCharge` e o endpoint `GET /bills` na [OAS])
- **[REAL]** Formato observado numa resposta real de `GET /bills` (só as chaves e o formato das datas/valores; nenhum valor real foi guardado no repositório)
- **[BP]** Fonte secundária, MIT: brunopedrazza/pluggy-mcp-server, `DESIGN.md`. São medições em 3 itens MeuPluggy reais.
- **[CB]** Fonte secundária, MIT: TrainedLoop/cashbook-mcp, `pluggy/normalize.py`, `classify.py` e `pluggy/client.py`.

## Endpoints usados (somente leitura)

| Chamada | Uso | Fonte |
|---|---|---|
| `POST /auth` `{clientId, clientSecret}` → `{apiKey}` | API key válida por 2 h, enviada no header `X-API-KEY`. `401 CLIENT_KEYS_UNAUTHORIZED` indica credenciais erradas. | [AUTH] |
| `GET /items/{id}` | `connector.name`, `status`, `consentExpiresAt`, `lastUpdatedAt` | [OAS] `Item`, [MP] |
| `GET /accounts?itemId=` | Contas do item. A resposta tem `{page, total, totalPages, results}` e o endpoint não documenta parâmetro de página. | [OAS] |
| `GET /v2/transactions?accountId=&dateFrom=` | Transações. Paginação por cursor, até 500 por página. | [BC], [TX] |
| `GET /bills?accountId=&pageSize=500&page=N` | Faturas **fechadas** do cartão. Paginação por página (`page`, `totalPages`); `pageSize` máximo 500. Só chamado para contas `credit_card`. | [OAS], [BILL] |
| `GET /identity?itemId=` | Documento do titular do item (`document`, `documentType`). Só o hash é gravado. Uma vez por item; `404 IDENTITY_NOT_FOUND` = sem identidade. | [OAS], [ID] |

- **v1 `/transactions`:** está deprecated e só funciona até 31/12/2026. Aplicações criadas a partir de 02/06/2026 já recebem `410 Gone` (`ENDPOINT_DEPRECATED`) [BC]. A [BP] mediu o 410 em itens MeuPluggy. Por isso usamos só a `/v2`.
- **Cursor:** a resposta traz `next`, uma *query string* pronta (`?accountId=…&after=…`) ou `null`. Ela é anexada ao path sem alterações, e o `after` nunca deve ser montado à mão [BC]. O cliente recusa um `next` que aponte para outro host e para se o cursor se repetir.
- **`dateFrom`/`dateTo`:** usam o formato `yyyy-mm-dd`. `dateFrom` não pode ser combinado com `createdAtFrom` [OAS]. O sync pede `dateFrom = hoje − 365 dias` e não manda `dateTo`, para incluir parcelas futuras e agendamentos `PENDING`.
- **Listar itens:** `GET /v2/items` não existe para contas Meu Pluggy, então o usuário informa os itemIds [MP].
- **Refresh:** itens MeuPluggy não aceitam atualização manual nem `PATCH` [MP]. O cliente não tem esse código.
- **Rate limit:** `429` vem com os headers `RateLimit-Reset` e `Retry-After`. O cliente espera o tempo indicado (no máximo 60 s) e tenta de novo até 3 vezes [RL].
- **API key expirada:** a key é renovada sozinha depois de 1h50. **NÃO VERIFICADO:** os docs não dizem qual status uma key expirada devolve. O `401` está documentado como "API key is wrong". Por isso o cliente refaz o `/auth` uma vez ao receber `401` ou `403` e repete a chamada [CB faz o mesmo para 401].

## U4 — Convenção de sinal

**Convenção interna:** `amount_cents` é negativo quando o dinheiro sai (gasto) e positivo quando entra.

| Conta | `amount` da Pluggy | `type` da Pluggy | Fonte |
|---|---|---|---|
| Cartão de crédito | **positivo = compra** (aumenta a fatura), **negativo = pagamento/crédito** | compra = `DEBIT`, pagamento = `CREDIT` | [TX] "Sign Convention for Credit Card Transactions"; [OAS] `type`: "Pluggy normalizes the value so purchases are always `DEBIT` and payments to the card statement are `CREDIT`" |
| Conta corrente | **negativo = saída**, positivo = entrada | saída = `DEBIT`, entrada = `CREDIT` | [OAS] exemplo de `/v2/transactions` (`amount: -212.45`, `type: DEBIT`); [TX] exemplo de saldo de fim do dia; [BP] medido: `BANK DEBIT -1338.00`, `CREDIT DEBIT +188.16` |

O sinal de `amount` da conta corrente só aparece documentado **em exemplos**, não em texto normativo. A [BP] confirmou esse sinal com dados reais.

Regra implementada em `providers/pluggy.py::signed_cents`:

1. **Valor absoluto:** usa `amountInAccountCurrency` quando existe e `amount` quando não existe. O `amount` vem na moeda da compra, por exemplo USD num cartão em BRL [OAS], [BP], [CB].
2. **Sinal:** vem do `type`. `DEBIT` vira negativo e `CREDIT` vira positivo. O `type` é o campo que a OpenAPI documenta como normalizado entre tipos de conta.
3. **Fallback**, quando `type` vem nulo (o campo não está em `required` na OpenAPI): no cartão, o sinal de `amount` é invertido; nas outras contas, é mantido.
4. **Conferência:** quando `type` e o sinal de `amount` discordam, a transação é contada como `sign_mismatch`, e o `grana-mcp sync` mostra o total. **No primeiro sync real, esse número deve ser 0.** Se não for, a regra precisa ser revista.

Os testes estão em `tests/test_pluggy_normalize.py`, e há testes de ponta a ponta em `tests/test_sync.py` e `tests/test_cli.py`.

### Centavos

O JSON é lido com `parse_float=Decimal`, então o valor nunca passa por `float`. A conversão para centavos usa `ROUND_HALF_UP`, porque a Pluggy pode mandar mais de 2 casas decimais (ex.: `2431.5285` [BP]).

### Data

`date` é ISO 8601 em UTC. Para ler no horário do Brasil, a doc manda converter para GMT-3 [TX]. A [BP] mediu que as datas são instantes reais (0 de 1.595 caíam à meia-noite UTC), e 4,3% delas mudam de dia quando convertidas. Por isso `posted_date` é o dia em `America/Sao_Paulo`. Exceção: um valor exatamente à meia-noite UTC é tratado como data de calendário e fica como está, para não cair no dia anterior. Isso segue o `plainDay` da [BP].

## U3 — Campos para identificar cada caso

### Pagamento de fatura de cartão

| Lado | Campo | Valor | Status |
|---|---|---|---|
| Cartão | `operationType` | `PAGAMENTO_FATURA`. Se a instituição não informa o pagamento, a Pluggy cria a transação "Pagamento recebido" com esse valor. | Documentado [TX], [OAS]. **NÃO VERIFICADO para MeuPluggy:** a OpenAPI diz que `operationType` só vem em conectores Open Finance, e o conector MeuPluggy aparece como *Direct* no Dashboard. |
| Cartão | `type` = `CREDIT` (`amount` negativo) | crédito na fatura | Documentado [TX], [OAS]. Estornos também são `CREDIT`, então esse campo sozinho não basta. |
| Conta corrente | `category` | `"Credit card payment"`, subcategoria de *Transfers* | A categoria existe [CAT]. A [BP] e a [CB] (mapa `CATEGORY_KINDS`) encontraram esse valor em dados reais do MeuPluggy. **NÃO VERIFICADO** em Nubank e Itaú especificamente. |
| Conta corrente | `operationType` | Algo como `BOLETO`, `CARTAO` ou `OUTROS` | **NÃO VERIFICADO.** Nenhuma fonte diz qual valor o débito do pagamento de fatura recebe na conta corrente. |
| Ambos | `creditCardMetadata.billId` + valor da fatura (`GET /bills`) | Casar o débito na conta com o total da fatura | `billId` só vem em Open Finance [TX]. A [CB] casa o valor do débito com `bills.totalAmount`. Fica para a Fase 3. O `/bills` é sincronizado desde a Fase 2 (ver "Faturas de cartão"). |

Fase 3 (implementado, ver "Limpeza (dedupe)" abaixo): débito em conta com `category == "Credit card payment"`, `operationType == PAGAMENTO_FATURA` ou descrição de pagamento de fatura só sai do gasto se o cartão pago parecer sincronizado (crédito de pagamento no cartão ou pagamento de fatura fechada com o mesmo valor ±1 centavo e até 3 dias, ou um cartão no mesmo item). O caso Itaú dos testes é sintético; validar com dados reais.

### Contraparte de Pix/TED (CPF/CNPJ)

- **Campos:** `paymentData.payer` e `paymentData.receiver` → `documentNumber` = `{type: "CPF"|"CNPJ", value: "<formatado>"}` [OAS] `Document`, [TX]. Também há `paymentData.paymentMethod` (`PIX`, `TED`, `DOC`, `BOLETO`), e `operationType` traz `PIX`, `TED`, `TRANSFERENCIA_MESMA_INSTITUICAO` e outros [TX].
- **Qual lado é a contraparte:** em saída, o `receiver`; em entrada, o `payer`. Segue a [CB] (`normalize.py`); a doc oficial não diz isso explicitamente.
- **Cobertura:** o Pix tem cobertura 🟢 (>90%) no Nubank (612) e no Itaú (601) via Open Finance [PD]. **NÃO VERIFICADO** se o conector MeuPluggy repassa `paymentData`. A [BP] viu `paymentData` em 595 transações MeuPluggy, o que é um bom sinal.
- **Armazenamento:** só os dígitos, e só se forem 11 (CPF) ou 14 (CNPJ). Valores mascarados (`***`) são descartados. O que fica gravado é `SHA-256(sal_local + dígitos)` em `counterpart_doc_hash`. O sal fica em `<data dir>/salt`, com permissão 0600.

### Parcelas

- **Campos:** `creditCardMetadata.installmentNumber` e `creditCardMetadata.totalInstallments` [TX], [OAS]. Também há `purchaseDate` e `paymentType` (`SINGLE` | `INSTALLMENT`, só Open Finance) [OAS]. `totalAmount` só vem em conectores diretos [TX].
- **Comportamento:** cada banco publica as parcelas de um jeito (todas de uma vez ou mês a mês). As parcelas futuras podem vir `PENDING` e com data futura [INST], [TX]. Para o U5 (parcela no mês em que é cobrada), usamos o `date` de cada parcela.

### Categoria da Pluggy

- **Campos:** `category` (texto em **inglês**, ex.: `Groceries`, `Credit card payment`, `Same person transfer`) e `categoryId` [OAS], [CAT]. A tradução PT fica em `GET /categories` → `descriptionTranslated` [CAT], mas não é usada na Fase 1.
- **Disponibilidade:** a doc diz que `category` "requires Pro subscription" [TX]. A [BP] encontrou categorias preenchidas em itens MeuPluggy. **NÃO VERIFICADO** na conta deste projeto.
- `merchant` (`name`, `cnpj`, `category`) também exige Pro [TX]. Gravamos `merchant.name` quando ele vem.

## Outros campos gravados

| Interno | Pluggy | Fonte |
|---|---|---|
| `items.consent_expires_at` | `Item.consentExpiresAt`. A autorização Meu Pluggy dura 1 ano. Quando expira, as leituras voltam vazias e o `status` do item pode continuar `UPDATED`. | [MP], [OAS] |
| `items.status` | `Item.status` | [OAS] |
| `accounts.kind` | `subtype` `CHECKING_ACCOUNT`→`checking`, `CREDIT_CARD`→`credit_card`, `SAVINGS_ACCOUNT`→`other` | [OAS] `Account` |
| `accounts.number_masked` | Últimos 4 caracteres de `number`. No cartão, `number` já vem só com os 4 finais. | [OAS] |
| `transactions.description_raw` | `descriptionRaw` se não for nulo; senão `description` | [OAS] |
| `transactions.status` | `POSTED` / `PENDING` | [OAS] |
| `transactions.operation_type` | `operationType` (só Open Finance) | [OAS] |
| `transactions.counterpart_doc_type` | `CPF` (11 dígitos) ou `CNPJ` (14) do documento da contraparte, derivado do tamanho. Não é sensível e serve para mascarar pessoas físicas no MCP. (Migração v2.) | derivado |

## Faturas de cartão (`GET /bills`)

Tabelas `bills` e `bill_payments` (migração v2). Upsert pelo `id` da fatura; os pagamentos de cada fatura são substituídos quando algo muda. Um sync repetido não duplica nada.

| Interno | Pluggy | Status |
|---|---|---|
| `bills.id` | `id` | Documentado [OAS], [BILL]; observado [REAL] |
| `bills.due_date` | `dueDate` (só a data) | Documentado e obrigatório [OAS], [BILL]; observado [REAL] |
| `bills.closing_date` | `billClosingDate` (só a data; pode ser `null`) | Está na [OAS] (`string \| null`) e foi observado [REAL], mas **não aparece na tabela da página [BILL]**. Tratado como opcional. |
| `bills.total_cents` | `totalAmount`, arredondado half-up para centavos | Documentado [OAS], [BILL]. Veio com até 4 casas decimais [REAL]. |
| `bills.currency` | `totalAmountCurrencyCode` | Documentado [OAS], [BILL] |
| `bills.minimum_payment_cents` | `minimumPaymentAmount` | Opcional [BILL] |
| `bills.allows_installments` | `allowsInstallments` | Opcional [BILL] |
| `bill_payments.payment_date` / `amount_cents` | `payments[].paymentDate` / `amount` | Documentado [OAS], [BILL]; observado [REAL] |
| `bill_payments.value_type` / `payment_mode` | `payments[].valueType` / `paymentMode` | Documentado [OAS], [BILL]. **NÃO VERIFICADO** se o MeuPluggy preenche (a [REAL] só confirmou `paymentDate` e `amount`). |
| (não gravado) | `financeCharges[]` | A [BILL] avisa que é informativo: linhas `OTHER` podem ser agregados e os encargos também aparecem como transações. Não usamos. |
| (não gravado) | `createdAt`, `updatedAt` | Vieram na [REAL]; **não estão** no schema `Bill` da [OAS]. |

- **Datas:** vêm como meia-noite UTC (`2026-11-01T00:00:00.000Z` [REAL]; `2023-07-20T00:00:00` nos exemplos da [OAS]). São datas de calendário, então guardamos os 10 primeiros caracteres, sem converter fuso.
- **Só faturas fechadas.** A fatura aberta não vem do `/bills`; ela é estimada localmente (`analytics/card_bill.py`).
- **Cobertura:** a [BILL] diz que o Bill é obrigatório em conexões Open Finance *Regulado* e, em conexões *Direct*, só vem para Inter PF e Itaú Cartões. **NÃO VERIFICADO** como o conector MeuPluggy se encaixa; na conta do primeiro usuário, o Nubank devolveu faturas [REAL]. Se o `/bills` responder 4xx (fora 401/403/429), o sync segue e mostra um aviso.

### Estimativa da fatura aberta

Implementada em `analytics/card_bill.py` (funções puras, testes em `tests/test_card_bill.py`). Regra validada pelo usuário contra o app do banco em 15/11/2026:

1. Fechamento usado = `billClosingDate` da última fatura fechada (≤ hoje).
2. Compras = débitos do cartão com `fechamento ≤ data ≤ hoje`. O dia do fechamento entra: parcelas datadas no dia do fechamento já são da fatura nova.
3. Créditos = créditos do cartão com `fechamento < data ≤ hoje`, **menos um**: o primeiro cujo valor é igual ao `totalAmount` da fatura anterior (±1 centavo), que é o pagamento dela. Os demais são antecipações (`operationType = PAGAMENTO_FATURA` ou categoria `Credit card payment`) ou outros créditos (estornos).
4. Estimativa = compras − antecipações − outros créditos. Débitos com data futura são parcelas já agendadas e saem agrupados por mês.
5. **Ressalva conhecida:** compras "NuPay" do Nubank podem não aparecer como transações do Open Finance (existe uma linha de crédito "Limite Nupay" separada no `creditData` da conta). Isso aparece em `caveats`, sem correção.

## IDs que mudam (importante para não contar em dobro)

Quando a fatura fecha, a Pluggy pode **apagar uma transação de cartão e criá-la de novo com outro `id`**, e nada liga o id antigo ao novo [CYC]. Os webhooks resolvem isso, mas não os usamos (o MCP é só local). Por isso o sync relê a janela inteira de cada conta e **apaga as linhas locais daquela conta que a Pluggy não devolveu mais** (`n_deleted` em `sync_runs`). Duas proteções evitam apagar dados por engano:

- Se a leitura de uma conta vier vazia, nada é apagado e aparece um aviso. É o que acontece quando a autorização expira [MP].
- Se `consentExpiresAt` já passou, nada é apagado.

Reconectar um banco cria um **item novo, com ids novos** [TX], [MP]. Para deduplicar entre itens, `providerId` é o id estável (só Open Finance) [CYC]. Isso fica para a Fase 3.


## Identidade do titular (`GET /identity`) — Fase 3

- **Endpoint:** `GET /identity?itemId=<uuid>` → `IdentityResponse`. Respostas documentadas: `200`, `400` (itemId inválido), `404` (`IDENTITY_NOT_FOUND`) e `500` [OAS].
- **Campos lidos:** só `document` ("Primary document that identifies the owner"; no exemplo vem formatado `123.456.789-00` e, no exemplo Open Finance, só dígitos) e `documentType` (`CPF`/`CNPJ`) [OAS], [ID]. O modelo pydantic do cliente declara só esses dois campos; nome, endereços, telefones, e-mails, `relations` etc. são ignorados na leitura e **nunca** gravados nem logados.
- **Gravação:** `document` → dígitos (11 ou 14; mascarado é descartado) → `SHA-256(sal + dígitos)` em `item_identities.doc_hash` e em `household_docs` (`source='identity'`). Se todas as contas do item tiverem o mesmo dono, o hash é ligado a essa pessoa.
- **Cobertura:** a tabela de conectores marca **Identity 🟢 para o MeuPluggy** [ID]. A página de cobertura por campo **não lista o MeuPluggy**, então **NÃO VERIFICADO** se `document` vem preenchido. Por isso o uso é "melhor esforço": `404` é silencioso, outros erros (exceto `401`) só geram aviso, e `grana-mcp sync --no-identity` desliga a chamada.
- **Frequência:** só é chamado para itens que ainda não têm documento aprendido.

## Limpeza (dedupe) — Fase 3

Implementada em `pipeline/dedupe.py`; roda no fim de todo `sync` e no `grana-mcp reprocess`. Usa estes campos já gravados:

| Uso | Campo | Valores | Fonte |
|---|---|---|---|
| Pagamento de fatura (cartão) | `operationType` | `PAGAMENTO_FATURA` (inclusive o "Pagamento recebido" criado pela Pluggy) | [TX], [OAS] |
| Pagamento de fatura (conta) | `category` / descrição | `Credit card payment` (subcategoria de *Transfers*) ou descrição tipo "Pagamento de fatura", "PAGTO FATURA" | [CAT]; descrição observada em dados reais (Nubank) |
| Estorno | `operationType` | `ESTORNO` (cartão) | [TX] |
| Transferência | `operationType` | `PIX`, `TED`, `DOC`, `TRANSFERENCIA_MESMA_INSTITUICAO` (conta) | [TX] |
| Transferência | `category` | árvore *Transfers*, *Same person transfer*, *Third-party transfers* | [CAT] |
| Investimento | `operationType` | `RESGATE_APLIC_FINANCEIRA`. **Não existe** `APLICACAO_*` no enum documentado da conta (`TED DOC PIX TRANSFERENCIA_MESMA_INSTITUICAO BOLETO CONVENIO_ARRECADACAO PACOTE_TARIFA_SERVICOS TARIFA_SERVICOS_AVULSOS FOLHA_PAGAMENTO DEPOSITO SAQUE CARTAO ENCARGOS_JUROS_CHEQUE_ESPECIAL RENDIMENTO_APLIC_FINANCEIRA PORTABILIDADE_SALARIO RESGATE_APLIC_FINANCEIRA OPERACAO_CREDITO OUTROS`). Nos dados reais do Nubank, a *aplicação* RDB também vem como `RESGATE_APLIC_FINANCEIRA`. `RENDIMENTO_APLIC_FINANCEIRA` é rendimento (crédito) e não é marcado. | [TX] |
| Investimento | `category` | `Investments` e as filhas `Automatic investment`, `Fixed income`, `Mutual funds`, `Variable income`, `Pension` (ficam de fora `Margin` e `Proceeds interests and dividends`) | [CAT] |
| Transferência interna | `paymentData` → `counterpart_doc_hash` | hash comparado com `household_docs` | [OAS], [PD] |

Colunas novas (migração v3): `transactions.excluded_pair_id` (a outra ponta do par), `transactions.review_flag` (`unmatched_bill_payment` | `unmatched_refund`), `accounts.owner_person_id`, tabelas `people` e `item_identities`; `household_docs` passou a ter `person_id`, `source` e `created_at`; `excluded_reason` aceita `investment`.
