# grana numa VPS (usuário `grana`, Podman rootless, só no Tailscale)

Tudo roda da mesma imagem (`podman build -f deploy/Containerfile -t grana-web .`):
o chat web, o sync diário e os avisos. Nenhuma porta pública: o web escuta em
127.0.0.1 e o `tailscale serve` publica `https://<máquina>.<tailnet>.ts.net`.

Segredos como `podman secret` do usuário `grana` (arquivo 0600 no home dele):

    grana-claude-token   claude setup-token
    grana-pluggy-id      client_id da Pluggy
    grana-pluggy-secret  client_secret da Pluggy
    grana-vapid          chave privada VAPID (PEM) das notificações

Arquivos: `~grana/grana/config` (config.json, web_tailscale.json, push_public_key)
e `~grana/grana/data` (grana.db, salt). Copie estes Quadlets para
`~grana/.config/containers/systemd/`, troque PROXY_IP pelo endereço de onde o
`tailscale serve` conecta (veja docs/web.md) e rode `systemctl --user daemon-reload`.
