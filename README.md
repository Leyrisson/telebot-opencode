# telebot-opencode

Controle o **opencode pelo Telegram**, do celular, de qualquer rede.

Você manda "cria um sistema flask e sobe ele" no app do Telegram; a tarefa roda no
seu PC com o opencode e a resposta volta no Telegram — e também vira notificação
no PC.

## Por que long-polling e não webhook

O PC **nunca abre porta**. Ele pergunta ao Telegram "tem mensagem nova?" a cada
30 s. Isso significa:

- funciona do 4G, de hotel, de qualquer lugar, **sem VPN e sem Tailscale Funnel**;
- não expõe nada na internet — não precisa abrir porta, ngrok ou proxy reverso;
- o `n8n` não serve nesse caminho, porque o container não alcança o host
  (`172.20.0.1:8788` dá timeout).

O único requisito de rede é *saída* HTTPS para `api.telegram.org`.

## Como funciona

```
Android (Telegram) → @BotFather → bot
        ↓ long-polling (o PC pergunta, não é perguntado)
telebot.py
        ↓ opencode run --auto --session <id>
tarefa executada no PC  →  resposta no Telegram + notificação no PC
```

O id da sessão fica num arquivo (`sessao.txt`), então **o bot lembra do contexto**
entre mensagens: "e aquele sistema em flask?" funciona.

Se a sessão sumiu (apagada no TUI), o bot tenta de novo sem `--session` em vez de
falhar, e avisa que perdeu o contexto.

## Comandos

| Comando | O que faz |
|---|---|
| `/start`, `/ajuda`, `/help` | menu com exemplos |
| `/status` | o que está sendo executado agora |
| `/novo` | zera a memória, começa do zero |
| qualquer outro texto | executa como tarefa |

## Instalação

```bash
cp credenciais.env.example credenciais.env
$EDITOR credenciais.env          # token do @BotFather + seu chat.id

mkdir -p ~/.config/omarchy-voice/plugins/telebot
cp telebot.py ~/.config/omarchy-voice/plugins/telebot/
mkdir -p ~/.local/state/omarchy-voice/telebot

cp telebot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telebot
```

O serviço é de **usuário** (`--user`), então liga junto com a sua sessão gráfica
e não pede sudo.

## O detalhe que faz funcionar: `--auto`

Sem `--auto`, o opencode **para para pedir permissão** a cada arquivo fora do
diretório de trabalho. Como o bot roda sem TUI, ninguém responde, a permissão é
recusada e a tarefa *termina sem fazer nada* — foi exatamente o bug que custard
uma hora antes de o `--auto` ser descoberto.

Com `--auto`, o opencode aprova sozinho o que normalmente pediria aprovação.
Regras `deny` continuam valendo (ex.: `.env` nunca é tocado).

> ⚠️ Isso significa que **quem mandar mensagem no chat autorizado roda comandos
> no seu PC com o seu usuário**. Mitigue assim:
> - token do bot e `chat.id` só no seu aparelho e no `credenciais.env` (600);
> - o `TELEGRAM_CHAT_ID` acts as allowlist — qualquer outro chat é ignorado em silêncio;
> - nunca versione o `credenciais.env`.

## Variáveis

| Variável | Padrão | Para quê |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | obrigatório, em `credenciais.env` |
| `TELEGRAM_CHAT_ID` | — | obrigatório, em `credenciais.env` |
| `OPENCODE_BIN` | `shutil.which("opencode")` | caminho do binário, se não estiver no `PATH` |
| `TIMEOUT` (código) | 45 min | teto por tarefa |
| `POLL` (código) | 30 s | intervalo de long-poll |

## Estado e diagnóstico

```bash
systemctl --user status telebot
tail -f ~/.local/state/omarchy-voice/telebot/telebot.log
```

O log é **anexado**, nunca sobrescrito — inclusive nos erros de boot.

## Requisitos

- Python 3 (só stdlib: `urllib`, `json`, `subprocess`, `shutil`)
- [opencode](https://opencode.ai) instalado
- Linux com systemd (unidade de usuário)
