# Privacidade: o que fica no seu computador e o que vai para o LLM

## O grana-mcp é software, não serviço

Não existe servidor do projeto. Você roda tudo na sua máquina, com a sua conta da Pluggy. Os mantenedores não operam nada e não recebem nenhum dado.

## O que fica só no seu computador

| Dado | Onde | Proteção |
|---|---|---|
| Transações, contas, faturas | SQLite em `~/.local/share/grana-mcp/grana.db` (Linux) | Arquivo `0600`, pasta `0700` |
| Client ID / Client Secret da Pluggy | Keyring do sistema | Nunca em arquivo de configuração nem em log |
| CPF/CNPJ (titulares e contrapartes de Pix) | Banco local | **Só como hash** SHA-256 com sal local por instalação |
| Logs | Saída do terminal | Só contagens e ids; nunca valores, descrições, documentos ou tokens |

O `grana-mcp sync` é o único comando que acessa a rede, e só a API da Pluggy, só para **ler** (`POST /auth` e `GET`). Não existe código de pagamento, Pix ou transferência.

## O que vai para o provedor do LLM

O servidor MCP roda por stdio e **não abre porta de rede**. Mas tudo o que uma ferramenta devolve ao seu cliente MCP (Claude, Cursor etc.) é enviado ao provedor do modelo (Anthropic, OpenAI etc.), sob a política de privacidade dele. Por isso:

- **Por padrão, só agregados:** totais por categoria, estabelecimento, pessoa e mês.
- **Itens individuais** só pelo `find_transactions` (no máximo 50 linhas, exige um filtro) e pelo `excluded_items` (no máximo 50).
- **Pessoas de fora da casa** são mascaradas como `PIX → pessoa física`. Isso vale para nomes em Pix, TED e para MEIs com nome de pessoa.
- **Documentos** (CPF/CNPJ) nunca saem, nem em hash.
- **Recorrências** não listam Pix para pessoa física.
- O **nome e o final do número das suas contas** aparecem (ex.: `Nubank ****90-5`), para você saber de qual conta é cada valor.

## Texto de terceiros é dado, não instrução

A descrição de uma transação (por exemplo, a mensagem de um Pix) é escrita por outra pessoa e pode tentar dar ordens ao modelo. O grana-mcp:

- corta esses textos em 80 caracteres e remove caracteres de controle;
- nunca coloca esse texto dentro das instruções das ferramentas;
- avisa o modelo, na descrição de cada ferramenta, que `description`/`merchant` são dados não confiáveis.

## A única ferramenta que grava

`set_category_rule` grava uma regra de categoria **no banco local** e recategoriza o histórico. Ela não acessa a rede.

## Apagar tudo

```bash
rm -rf ~/.local/share/grana-mcp ~/.config/grana-mcp   # caminhos no Linux
```

Remova também a entrada `grana-mcp` do keyring do sistema e revogue o consentimento no Meu Pluggy.
