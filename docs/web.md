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

## Mexer na página

O código da página fica em `web-ui/` (Vite + Preact). O build vai direto para `src/grana_mcp/web/static/` e é commitado, então quem instala o pacote não precisa de Node.

```bash
cd web-ui && npm install && npm run build   # ou: npm run dev (proxy para a API em 127.0.0.1:8765)
```

## Testar sem WireGuard

```bash
grana-mcp web serve            # http://127.0.0.1:8765, só no próprio PC
```
