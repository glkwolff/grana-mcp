# Painel e chat web pessoal (opcional)

Uma página que você abre no celular, fora de casa, com três abas: **Painel** (gasto do mês, por pessoa, orçamentos por categoria, objetivos de economia e avisos), **Análises** (gasto mês a mês nos últimos 12 meses, categorias comparadas com o período anterior, onde mais gastamos, assinaturas e faturas abertas; tocar numa categoria abre os lançamentos dela, sem passar pela IA) e **Conversa** (chat para perguntar sobre os gastos e pedir mudanças no painel, como "coloca um orçamento de R$ 600 para Delivery" ou "guardamos mais 200 na reserva"). Quem responde é o Claude Code rodando **no seu PC**, trancado para usar só as ferramentas do grana. O acesso é por uma VPN WireGuard entre o celular e o PC.

```
celular ──WireGuard──▶ PC: container grana-web (porta 8765, só no IP da VPN)
                          ├─ página de chat (token por pessoa)
                          └─ claude -p --tools "" (só mcp__grana__*) ─▶ grana-mcp serve ─▶ SQLite
PC (host): grana-mcp sync 1x/dia (credenciais da Pluggy no keyring, fora do container)
```

## Como o Claude edita o painel

O Claude não escreve HTML nem código na página. O painel é feito de **dados** (orçamentos, objetivos e avisos) guardados no SQLite local, e o Claude só os altera pelas ferramentas `set_budget`, `set_goal`, `delete_goal`, `post_notice` e `delete_notice`. A página mostra esses dados como texto. Cada edição registra quem pediu.

## Travas de segurança

- **O agente não tem ferramentas além do grana:** `--tools ""` remove Bash, leitura/escrita de arquivos e internet; `--strict-mcp-config` carrega só o MCP grana; `--setting-sources project` numa pasta vazia ignora hooks e plugins do seu usuário.
- **Container rootless**, sem capabilities, sistema de arquivos só leitura. Ele só enxerga o banco do grana, o `config.json` (itemIds, sem segredos) e os hashes dos tokens. **Nenhuma credencial da Pluggy entra no container.**
- **Rede:** a porta é publicada só no IP do WireGuard. `grana-mcp web serve` recusa `0.0.0.0` fora de container.
- **Login pelo Tailscale (recomendado):** `grana-mcp web tailscale add ana@github ana` libera uma conta do Tailscale. O servidor pergunta ao `tailscaled` local quem é o dono do IP 100.x que conectou (`tailscale whois`), então ninguém digita token. Cada pacote no tailnet é assinado pela chave do aparelho, e não dá para se passar por outro.
- **Login por token (alternativa):** um token por pessoa (`grana-mcp web token add`), guardado só como hash. Depois de 10 tokens errados em 15 min, o IP fica bloqueado por 15 min.
- **Limite de perguntas:** 30 por hora e 150 por dia por pessoa (`--max-per-hour`, `--max-per-day`), para um bug ou token vazado não esgotar a cota do Claude. Uma pergunta só começa quando a anterior terminou. Os contadores ficam em memória e zeram quando o serviço reinicia.
- **Conta do Claude:** as perguntas usam a conta de quem gerou o `claude setup-token`, inclusive as da outra pessoa da casa.

## Passo a passo

### 1. Imagem

```bash
uv build
podman build -f deploy/Containerfile -t grana-web .
```

### 2. Tokens de acesso

```bash
grana-mcp web token add gabriel     # mostra o token uma vez
grana-mcp web token add gabi
```

### 3. Login do Claude Code para o container

```bash
claude setup-token                                      # gera um token de longa duração
printf '%s' "<token>" | podman secret create grana-claude-token -
```

### 4. Serviço

```bash
mkdir -p ~/.config/containers/systemd
sed "s/LISTEN_IP/$(tailscale ip -4)/" deploy/grana-web.container > ~/.config/containers/systemd/grana-web.container
systemctl --user daemon-reload
systemctl --user start grana-web
loginctl enable-linger "$USER"     # sobe mesmo sem você logado
```

### 5. Sync diário no host

```bash
cp deploy/grana-sync.service deploy/grana-sync.timer ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now grana-sync.timer
```

### 6. WireGuard

O PC é o servidor da VPN (ex.: `10.8.0.1/24`), e cada celular é um peer (`10.8.0.2`, `10.8.0.3`). Para alcançar o PC fora de casa, o roteador precisa repassar a porta **UDP 51820** para o PC, e a operadora não pode usar CGNAT. No celular, use o app oficial do WireGuard e importe a configuração por QR code (`qrencode -t ansiutf8 < peer.conf`).

Depois, no celular: abra o link que o `web token add` mostra (`http://10.8.0.1:8765/#token=...`), ou um QR code dele, e pronto. A parte `#token=` nunca chega ao servidor e some da barra de endereço depois do login. Dá para "adicionar à tela inicial".

### 7. HTTPS e notificações no celular

O iPhone só mostra notificações de um site **instalado na Tela de Início** e aberto por **HTTPS**. O `tailscale serve` cuida do certificado sem expor nada fora da VPN:

1. No painel do Tailscale, em **DNS**, ligue **MagicDNS** e **HTTPS Certificates**. O nome da máquina (ex.: `pc.tailnet.ts.net`) passa a constar em logs públicos de certificados. O conteúdo não.
2. No PC:
   ```bash
   sudo tailscale set --operator=$USER                      # uma vez
   tailscale serve --bg --https=443 http://$(tailscale ip -4):8765
   grana-mcp notify setup                                   # chave privada no keyring
   cp deploy/grana-notify.service deploy/grana-notify.timer ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now grana-notify.timer
   ```
3. No celular, abra `https://<pc>.<tailnet>.ts.net`, toque em **Compartilhar → Adicionar à Tela de Início**, abra pelo ícone e toque em **Ativar notificações** no fim do Painel. Teste com `grana-mcp notify test`.

Avisos (cada um chega uma vez só):
- orçamento passou de 80% e de 100%;
- fatura vence em até 3 dias (no dia, mesmo de madrugada) e fatura fecha em até 3 dias, com a estimativa;
- gasto a partir de R$ 500 (`--big-purchase`), cobrança recorrente nova ou que mudou de preço;
- resumo da semana no domingo à noite, aviso novo no painel, sync parado ou com erro e autorização do banco perto de vencer.

Entre 22h e 7h só os urgentes saem; o resto espera a manhã. No máximo 5 por vez. Na primeira execução, o que já estava pendente é marcado como visto sem enviar.

O timer `grana-notify` roda **no host** a cada 15 min: a chave VAPID fica no keyring do host e o container só conhece a chave pública. Cada notificação é criptografada para o aparelho (RFC 8291). O serviço de push da Apple ou do Google só vê o endereço, o tamanho e a hora. Os celulares inscritos ficam em `push_subscriptions`. Liste com `grana-mcp notify list`.

Pelo HTTPS, quem você é vem do cabeçalho `Tailscale-User-Login` que o `tailscale serve` coloca, aceito só em conexões vindas do IP do próprio PC (`GRANA_WEB_TRUSTED_PROXY` no Quadlet).

## Retrato da casa, memória e consumo de tokens

- **Retrato da casa:** no começo de cada conversa, o servidor monta um resumo de ~400 tokens a partir do SQLite e o anexa ao prompt do sistema, num bloco `<retrato_da_casa>` marcado como dado. Ele traz o mês atual contra a média, o gasto típico por categoria, orçamentos, objetivos, recorrentes, faturas, alguns hábitos tirados dos dados (dia em que cai a maior entrada, frequência de delivery) e a memória da família. O texto fica igual durante a conversa inteira, para o cache de prompt do Claude reaproveitá-lo. Também existe como ferramenta MCP (`household_context`), para outros clientes.
- **Memória da família:** notas curtas (`remember`, `forget`, `recall`) que alguém da casa contou no chat, ou anotou no cartão "O que o grana sabe sobre vocês" do Painel, onde qualquer um pode apagar. A busca usa FTS5 do SQLite (sem acento, sem embeddings, nada sai do PC). As ferramentas instruem o modelo a nunca guardar texto vindo de transações. Limite de 100 notas.
- **Ferramentas compactas:** `overview` (um período inteiro numa chamada) e `explain_category` respondem em texto curto. No chat web, `status`, `compare_periods`, `top_merchants`, `excluded_items` e `household_context` ficam ocultas (`--disallowedTools`), porque o retrato e o `overview` cobrem o mesmo.
- **Medição:** cada resposta mostra quantas consultas fez e quantos tokens leu. `grana-mcp web usage` mostra as médias por dia e pessoa (só contagens, nunca o texto).

## Cozinha (dieta e compras da casa)

A aba **Cozinha** e as ferramentas `kitchen_status`, `set_diet_profile`, `set_kitchen_rules`, `log_weight`, `log_day`, `set_meal_plan`, `meal_plan`, `shopping_list`, `find_prices`, `markets` e `set_market` juntam a dieta com o financeiro:

- **Cardápio compartilhado:** a casa come o mesmo prato, escrito para uma porção de referência de 2000 kcal. Cada pessoa come a sua porção (`meta diária / 2000`). As metas vêm de fórmula (Mifflin-St Jeor, fator de atividade, −500 kcal para perder, com piso), não do modelo. São estimativas, não orientação médica.
- **Definido com vocês:** o chat pergunta o que falta (altura, ano de nascimento, rotina, restrições, gostos, meta, teto semanal) e confirma antes de salvar.
- **Registro leve:** peso uma vez por semana e um "seguimos o plano hoje?" (sim/não). O resto vem do extrato: gasto de Mercado, Delivery e Restaurantes na semana contra o teto, e pedidos de delivery contra o combinado.
- **Mercados e preços:** os mercados perto de casa vêm do **OpenStreetMap** (supermercado, atacado, hortifrúti, açougue, feira, peixaria; padarias e mercearias só as mais próximas), com endereço e distância; a lista é reaproveitada por 30 dias. Os preços vêm de duas fontes e **só valem no dia**: o **Menor Preço** (Nota Paraná), consultado pelo **celular** de vocês (botão "buscar preços de hoje" ou ferramenta `request_prices` no chat), e os preços **anotados** no chat (`note_price`). A lista de compras soma o cardápio para todos (menos o que já tem em casa), escolhe um mercado, ou dois quando o segundo economiza pelo menos R$ 15 e 5% da compra, e compara o custo com o teto semanal. Sem preço de hoje, o item fica sem preço.
- **Por que o Menor Preço é consultado pelo celular:** o serviço não tem API documentada e, contra excesso de consultas ou IPs de datacenter, responde com dados embaralhados (nomes de mercado com letras trocadas, descrição só com a palavra buscada, preços falsos). Por isso a VPS não consulta nada (`GRANA_PRICES` desligado): o navegador do celular faz as mesmas consultas públicas que o site oficial faz (a API libera CORS), pela internet da casa, em ritmo de gente (um pedido a cada 3 s, só os itens da semana sem preço de hoje, categoria guardada para poupar pedidos), e para na primeira resposta embaralhada, sem gravar nada. O grana não tenta contornar esse limite.
- **Mercados:** a localização vem do navegador ("usar minha localização") e é guardada como uma área de ~1 km (geohash de 6 letras), nunca o ponto exato. Marque com ★ onde vocês compram.
- **Avisos:** domingo à tarde (montar o cardápio), segunda de manhã (pesar), à noite (seguiram o plano?) e quando o delivery passa do combinado. Só depois que existe um perfil de dieta.

## Mexer na página

O código da página fica em `web-ui/` (Vite + Preact). O build vai direto para `src/grana_mcp/web/static/` e é commitado, então quem instala o pacote não precisa de Node.

```bash
cd web-ui && npm install && npm run build   # ou: npm run dev (proxy para a API em 127.0.0.1:8765)
```

## Testar sem WireGuard

```bash
grana-mcp web serve            # http://127.0.0.1:8765, só no próprio PC
```
