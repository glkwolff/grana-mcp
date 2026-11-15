# grana-mcp

<!-- mcp-name: io.github.glkwolff/grana-mcp -->

Servidor MCP **open source, local e somente leitura** para finanças pessoais no Brasil. Você pergunta ao seu cliente MCP (Claude Code, Claude Desktop, Cursor etc.) coisas como *"onde gastamos mais esse mês?"* ou *"quanto está a fatura do cartão?"*, e as respostas vêm das suas próprias contas, via Open Finance, pelo [Meu Pluggy](https://meu.pluggy.ai), que é gratuito para uso pessoal.

## Por que mais um MCP da Pluggy?

- **Configuração guiada:** `grana-mcp init` leva você do zero ao primeiro sync.
- **Casal ou família:** contas de várias pessoas numa visão só, com o gasto separado por dono da conta.
- **Limpeza feita para o Brasil:** tira do gasto o pagamento de fatura (que contaria tudo duas vezes), as transferências entre as pessoas da casa, aplicações em caixinhas/CDB e compras estornadas; conta cada parcela no mês em que é cobrada; categoriza estabelecimentos brasileiros.
- **Privacidade primeiro:** as transações ficam num SQLite no seu computador; o LLM recebe totais por padrão, e nomes de pessoas de fora da casa são mascarados. Veja [docs/privacy.md](docs/privacy.md).
- **Sem Pluggy, se preferir:** importa OFX e CSV exportados do banco.

## Instalação

Precisa do [uv](https://docs.astral.sh/uv/) e de Python 3.12+.

```bash
uvx grana-mcp init
```

O assistente explica o Meu Pluggy, abre as páginas, pede o Client ID/Secret (o secret vai para o keyring do sistema), testa a conexão e roda o primeiro sync. O passo a passo detalhado está em [docs/setup-meu-pluggy.md](docs/setup-meu-pluggy.md).

Depois, adicione o servidor no seu cliente MCP. No Claude Code:

```bash
claude mcp add grana --scope user -- uvx grana-mcp serve
```

Em outros clientes, o comando do servidor stdio é `uvx grana-mcp serve`.

O servidor **só lê o SQLite local** e não acessa a rede. Para manter os dados em dia, agende o sync uma vez por dia (o Meu Pluggy atualiza a cada 24 h):

```bash
grana-mcp sync      # por exemplo, no cron: 0 7 * * * uvx grana-mcp sync
grana-mcp doctor    # confere credenciais, autorização, permissões e schema
```

## O que dá para perguntar

| Ferramenta | Para quê |
|---|---|
| `spending_summary` | Gasto do período por categoria, estabelecimento, pessoa, perfil ou conta |
| `compare_periods` | Um mês contra outro, por categoria |
| `top_merchants` | Onde mais se gastou, em valor e em frequência |
| `recurring_charges` | Assinaturas e cobranças mensais, com mudança de preço |
| `card_bills` | Faturas fechadas, estimativa da fatura aberta e parcelas futuras |
| `excluded_items` | O que ficou fora do gasto e por quê |
| `find_transactions` | Lançamentos individuais (no máximo 50, com filtro obrigatório) |
| `status` | Conexões, contas, donos, último sync e validade da autorização |
| `set_category_rule` | Corrige uma categoria (única ferramenta que grava, e só no banco local) |

Prompt pronto: `relatorio_mensal` ("faz o relatório de outubro").

## Casal ou família

Uma conta do Meu Pluggy pode trazer bancos de mais de uma pessoa. Diga quem é dono de cada conta:

```bash
grana-mcp person add "Ana"
grana-mcp person add "Bia"
grana-mcp account list
grana-mcp account assign 0001 ana        # id da conta ou final do número
```

As transferências entre vocês são reconhecidas pelo CPF dos titulares, que o `sync` lê da Pluggy e guarda **só como hash**. Se precisar, cadastre na mão: `grana-mcp household add-doc ana` (pede o CPF sem mostrar na tela).

## Categorias

Sem LLM, nesta ordem: suas regras → regras embutidas para estabelecimentos brasileiros ([categories_br.toml](src/grana_mcp/rules/categories_br.toml)) → categoria da Pluggy traduzida → "Outros". Para corrigir:

```bash
grana-mcp category add "padaria do bairro" Mercado   # palavras inteiras; "uber*" = prefixo
grana-mcp category list
grana-mcp category remove 3
```

Ou peça ao seu cliente MCP: "coloca a padaria do bairro em Mercado".

## Sem Pluggy: OFX e CSV

```bash
grana-mcp import-ofx extrato.ofx --profile ana --person ana
grana-mcp import-csv fatura.csv --profile ana --format nubank-cartao
```

Formatos de CSV: `nubank-cartao`, `nubank-conta` e `generic` (`date,description,amount`). Reimportar o mesmo arquivo não duplica. **Não importe uma conta que também é sincronizada pela Pluggy**, senão tudo conta duas vezes.

## Software, não serviço

O grana-mcp é **só software**. Não existe versão hospedada: cada pessoa roda na própria máquina, com a própria conta da Pluggy, e os mantenedores não operam nada nem recebem dado nenhum. O que o seu cliente MCP recebe vai para o provedor do LLM que você usa (Anthropic, OpenAI etc.); por isso o padrão é enviar só valores somados.

## Desenvolvimento

```bash
uv sync
uv run pytest
uv run ruff check && uv run mypy
uvx pre-commit install    # bloqueia commit de banco, extrato e CPF
```

Os testes usam só dados sintéticos. Nunca commite dados reais, nem anonimizados.

## Créditos

Aprendizados sobre a API da Pluggy e o Meu Pluggy vieram de projetos MIT:
[TrainedLoop/cashbook-mcp](https://github.com/TrainedLoop/cashbook-mcp) e
[brunopedrazza/pluggy-mcp-server](https://github.com/brunopedrazza/pluggy-mcp-server).

## Licença

[MIT](LICENSE)
