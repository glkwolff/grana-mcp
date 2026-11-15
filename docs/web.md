# Painel e chat web pessoal (opcional)

Uma página que você abre no celular, fora de casa, com duas abas: **Painel** (gasto do mês, por pessoa, orçamentos por categoria, objetivos de economia e avisos) e **Conversa** (chat para perguntar sobre os gastos e pedir mudanças no painel, como "coloca um orçamento de R$ 600 para Delivery" ou "guardamos mais 200 na reserva"). Quem responde é o Claude Code rodando **no seu PC**, trancado para usar só as ferramentas do grana. O acesso é por uma VPN WireGuard entre o celular e o PC.

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
- **Login:** um token por pessoa (`grana-mcp web token add`), guardado só como hash. Depois de 10 tokens errados em 15 min, o IP fica bloqueado por 15 min.
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
sed "s/WG_IP/10.8.0.1/" deploy/grana-web.container > ~/.config/containers/systemd/grana-web.container
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

## Mexer na página

O código da página fica em `web-ui/` (Vite + Preact). O build vai direto para `src/grana_mcp/web/static/` e é commitado, então quem instala o pacote não precisa de Node.

```bash
cd web-ui && npm install && npm run build   # ou: npm run dev (proxy para a API em 127.0.0.1:8765)
```

## Testar sem WireGuard

```bash
grana-mcp web serve            # http://127.0.0.1:8765, só no próprio PC
```
