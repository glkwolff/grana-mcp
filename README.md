# grana-mcp

> 🚧 Em desenvolvimento. Ainda não está pronto para uso.

Servidor MCP **open source, auto-hospedado e somente leitura** para finanças pessoais no Brasil. Com ele, você pergunta ao seu cliente MCP (Claude Code, Claude Desktop, Cursor etc.) coisas como *"onde gastamos mais esse mês?"*. As respostas usam os dados das suas próprias contas, obtidos via Open Finance pelo [Meu Pluggy](https://meu.pluggy.ai), que é gratuito para uso pessoal.

## Por que mais um MCP da Pluggy?

- **Configuração fácil:** um assistente na linha de comando guia o passo a passo da Pluggy e do Meu Pluggy.
- **Casal ou família:** vários perfis, cada um com as próprias credenciais, somados numa visão só.
- **Limpeza feita para o Brasil:** remove o pagamento de fatura (que contaria o gasto duas vezes) e as transferências entre as pessoas da casa, trata parcelas e estornos e categoriza estabelecimentos brasileiros.
- **Privacidade em primeiro lugar:** as transações ficam num SQLite no seu computador, e o LLM recebe só totais por padrão.
- **Outra fonte de dados:** dá para importar OFX/CSV sem usar a Pluggy.

## Uso (em desenvolvimento)

Ainda não está no PyPI. A partir de um clone do repositório:

```bash
uv --directory /caminho/para/grana-mcp run grana-mcp profile add   # credenciais da Pluggy + itemIds
uv --directory /caminho/para/grana-mcp run grana-mcp sync          # baixa os dados para o SQLite local
```

Para adicionar o servidor MCP (stdio) no Claude Code:

```bash
claude mcp add grana -- uv --directory /caminho/para/grana-mcp run grana-mcp serve
```

Use `--scope user` para valer em todos os projetos. O servidor só lê o SQLite local e não acessa a rede; o `sync` continua sendo pela linha de comando (dá para agendar no cron).

Ferramentas: `status` (conexões, pessoas e dono de cada conta), `spending_summary` (por categoria, estabelecimento, pessoa, perfil ou conta), `compare_periods`, `top_merchants`, `find_transactions` (no máximo 50 linhas, com nomes de pessoas físicas mascarados), `excluded_items` (o que ficou fora do gasto e por quê) e `card_bills` (faturas fechadas e estimativa da fatura aberta). Prompt: `relatorio_mensal`.

### Casal ou família

Uma credencial pode trazer contas de mais de uma pessoa. Diga quem é dono de cada conta e quais CPFs são da casa (só o hash com sal local é guardado):

```bash
grana-mcp person add "Ana"
grana-mcp account list
grana-mcp account assign 0001 ana        # id da conta ou final do número
grana-mcp household add-doc ana          # pede o CPF/CNPJ sem mostrar na tela
grana-mcp reprocess                      # recalcula a limpeza, sem rede
```

A cada `sync` (e no `reprocess`), a limpeza tira do gasto: pagamento de fatura de cartão sincronizado, transferências entre contas/pessoas da casa, aplicações e resgates de investimento e pares compra/estorno. Sem pessoas cadastradas, cada perfil conta como uma pessoa. O `sync` também lê o CPF/CNPJ do titular de cada conexão (`GET /identity`) e guarda só o hash; use `--no-identity` para desligar.

## Software, não serviço

Este projeto é **só software**. Não existe versão hospedada. Cada pessoa roda o servidor na própria máquina, com a própria conta da Pluggy. Os mantenedores não operam nada e não recebem nenhum dado de ninguém.

O texto que o seu cliente MCP recebe vai para o provedor do LLM que você usa (Anthropic, OpenAI etc.). Por isso, o padrão é enviar só valores somados.

## Créditos

Este projeto aproveita aprendizados e trechos de código, sob licença MIT, de:
- [TrainedLoop/cashbook-mcp](https://github.com/TrainedLoop/cashbook-mcp)
- [brunopedrazza/pluggy-mcp-server](https://github.com/brunopedrazza/pluggy-mcp-server)

## Licença

[MIT](LICENSE)
