# Passo a passo: Meu Pluggy + Pluggy Dashboard

O grana-mcp lê suas contas pelo **Meu Pluggy**, que é gratuito para uso pessoal (até 5 conexões, sem uso comercial). Você precisa de duas coisas, as duas com o **mesmo e-mail**:

| Onde | Para quê |
|---|---|
| [meu.pluggy.ai](https://meu.pluggy.ai) | Conectar seus bancos via Open Finance |
| [dashboard.pluggy.ai](https://dashboard.pluggy.ai) | Pegar o Client ID, o Client Secret e os `itemId` |

O `grana-mcp init` mostra estes passos e abre as páginas para você.

## 1. Conectar os bancos no Meu Pluggy

1. Crie a conta em meu.pluggy.ai.
2. Em **Conexões → Nova conexão**, escolha o banco. O app do banco abre para você autorizar o compartilhamento. Escolha o **prazo máximo (12 meses)** e marque contas, cartões e transações.
3. Repita para cada banco. Espere as conexões ficarem ativas; a primeira carga pode levar alguns minutos.

> Cada banco é autorizado no app do próprio banco, pelo titular da conta. Para um casal, o mais simples é cada pessoa ter o próprio Meu Pluggy. Também funciona conectar os bancos das duas pessoas num único Meu Pluggy (cada titular autoriza o seu banco), mas a documentação da Pluggy fala em "ler as suas próprias contas", então isso é zona cinzenta.

## 2. Criar a aplicação no Pluggy Dashboard

1. Entre em dashboard.pluggy.ai com o **mesmo e-mail** do Meu Pluggy.
2. Em **Aplicações → Novo**, crie uma aplicação. Ela será do tipo **desenvolvimento**, e isso basta: **não** clique em "Ir para produção" (é o caminho comercial, pago, com contrato).
3. Copie o **Client ID** e o **Client Secret** para um gerenciador de senhas. O secret só aparece uma vez; se perder, gere outro no ícone de chave.

> O painel mostra "X dias restantes no seu trial". O trial é dos recursos comerciais; a documentação da Pluggy diz que o Meu Pluggy **não expira junto com o trial**.

## 3. Gerar os `itemId`

1. Na linha da sua aplicação, clique no **▶ (Demo)**.
2. No widget, procure o conector **MeuPluggy**, faça login com a sua conta do Meu Pluggy e autorize.
3. O Demo mostra o `itemId` de cada conexão (formato UUID). **É um `itemId` por banco.**

Os `itemId` pertencem à aplicação que os criou: use o Client ID **da mesma aplicação** em que você rodou o Demo.

## 4. Rodar o grana-mcp

```bash
uvx grana-mcp init        # ou: grana-mcp profile add && grana-mcp sync
grana-mcp doctor          # confere credenciais, autorização e permissões
```

O Client Secret é digitado sem aparecer na tela e fica no **keyring do sistema**, nunca em arquivo. Não cole o secret num chat com IA.

## Bom saber

- O Meu Pluggy atualiza os dados **a cada 24 horas**. Não existe atualização sob demanda pela API (a Pluggy recusa com "MeuPluggy item cant be updated"). Agende `grana-mcp sync` uma vez por dia.
- A autorização dura **1 ano**. O `status` e o `doctor` avisam quando faltam 30 dias ou menos. Para renovar, refaça a conexão no Meu Pluggy e o passo 3.
- O Open Finance traz no máximo **365 dias** de histórico, e alguns bancos trazem menos.
- Compras feitas com **NuPay** podem não aparecer como transação do cartão no Open Finance.
- Se a sua conta Pluggy pedir pagamento um dia, a alternativa é importar o extrato do banco: `grana-mcp import-ofx` ou `grana-mcp import-csv`.
