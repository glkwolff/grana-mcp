# grana-mcp

> 🚧 Em desenvolvimento. Ainda não está pronto para uso.

Servidor MCP **open source, auto-hospedado e somente leitura** para finanças pessoais no Brasil. Com ele, você pergunta ao seu cliente MCP (Claude Code, Claude Desktop, Cursor etc.) coisas como *"onde gastamos mais esse mês?"*. As respostas usam os dados das suas próprias contas, obtidos via Open Finance pelo [Meu Pluggy](https://meu.pluggy.ai), que é gratuito para uso pessoal.

## Por que mais um MCP da Pluggy?

- **Configuração fácil:** um assistente na linha de comando guia o passo a passo da Pluggy e do Meu Pluggy.
- **Casal ou família:** vários perfis, cada um com as próprias credenciais, somados numa visão só.
- **Limpeza feita para o Brasil:** remove o pagamento de fatura (que contaria o gasto duas vezes) e as transferências entre as pessoas da casa, trata parcelas e estornos e categoriza estabelecimentos brasileiros.
- **Privacidade em primeiro lugar:** as transações ficam num SQLite no seu computador, e o LLM recebe só totais por padrão.
- **Outra fonte de dados:** dá para importar OFX/CSV sem usar a Pluggy.

## Software, não serviço

Este projeto é **só software**. Não existe versão hospedada. Cada pessoa roda o servidor na própria máquina, com a própria conta da Pluggy. Os mantenedores não operam nada e não recebem nenhum dado de ninguém.

O texto que o seu cliente MCP recebe vai para o provedor do LLM que você usa (Anthropic, OpenAI etc.). Por isso, o padrão é enviar só valores somados.

## Créditos

Este projeto aproveita aprendizados e trechos de código, sob licença MIT, de:
- [TrainedLoop/cashbook-mcp](https://github.com/TrainedLoop/cashbook-mcp)
- [brunopedrazza/pluggy-mcp-server](https://github.com/brunopedrazza/pluggy-mcp-server)

## Licença

[MIT](LICENSE)
