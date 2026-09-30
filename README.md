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
- funciona atrás de NAT e firewall restritivo, sem configuração nenhuma.

O único requisito de rede é *saída* HTTPS para `api.telegram.org`.

## Como funciona

```
Android (Telegram) → @BotFather → bot
        ↓ long-polling (o PC pergunta, não é perguntado)
telebot.py
        ↓ opencode run --auto --session <id>   (JSON por linha no stdout)
tarefa executada no PC  →  resposta no Telegram + notificação no PC
```

Dois arquivos guardam o estado, ambos em
`~/.local/state/omarchy-voice/telebot/`:

- `sessao.txt` — o id da sessão do opencode. É por isso que **o bot lembra do
  contexto** entre mensagens: "e aquele sistema em flask?" funciona.
- `offset.txt` — a última update do Telegram já processada. Sem isso, qualquer
  reinício do serviço fazia o Telegram **reenviar todas as mensagens antigas**:
  o bot repetia o histórico e, pior, reaplicava a última tarefa.

Se a sessão sumiu (apagada no TUI), o bot tenta de novo sem `--session` em vez de
falhar, avisa que perdeu o contexto e **descarta a sessão morta**, para não ficar
repetindo o erro a cada mensagem.

## Comandos

| Comando | O que faz |
|---|---|
| qualquer texto | executa como tarefa no seu PC |
| `/abrir firefox` | abre o app na hora, **sem passar pelo opencode** (instantâneo e à prova de sessão morta) |
| `/teste` | diagnóstico: token, chat, binário do opencode, display, sessão |
| `/status` | o que está sendo executado agora |
| `/novo` | zera a memória, começa do zero |
| `/start`, `/ajuda`, `/help` | menu com exemplos |

Apps que o `/abrir` reconhece: firefox, chrome/chromium, vscode, terminal
(alacritty/kitty/ghostty), arquivos, spotify, discord, telegram, calculadora,
loja, okular.

## Instalação

```bash
cp credenciais.env.example credenciais.env
$EDITOR credenciais.env          # token do @BotFather + seu chat.id
chmod 600 credenciais.env

mkdir -p ~/.config/omarchy-voice/plugins/telebot
cp telebot.py ~/.config/omarchy-voice/plugins/telebot/
cp credenciais.env ~/.config/omarchy-voice/plugins/telebot/
mkdir -p ~/.local/state/omarchy-voice/telebot

cp telebot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telebot
```

O serviço é de **usuário** (`--user`), então liga junto com a sua sessão gráfica
e não pede sudo.

## Os três detalhes que fazem funcionar

### 1. `--auto`, senão a tarefa termina sem fazer nada

Sem `--auto`, o opencode **para para pedir permissão** a cada arquivo fora do
diretório de trabalho. Como o bot roda sem TUI, ninguém responde, a permissão é
recusada e a tarefa *termina sem fazer nada* — foi exatamente o bug que custou
uma hora antes de o `--auto` ser descoberto.

Com `--auto`, o opencode aprova sozinho o que normalmente pediria aprovação.
Regras `deny` continuam valindo (ex.: `.env` nunca é tocado).

### 2. `Environment=PATH=` na unidade systemd

O PATH padrão de uma unidade de usuário é só `/usr/local/bin:/usr/bin`. O
opencode vive em `~/.local/bin` (via mise), então **toda tarefa morria com
`FileNotFoundError: opencode`** — com o resto do bot funcionando, o que faz o
problema parecer outro. A linha está no `telebot.service` pronto para uso.

### 3. `offset.txt` no estado

Sem guardar a última update processada, todo reinício do serviço faz o Telegram
reenviar o histórico inteiro. O bot passa a repetir mensagens antigas e a
reaplicar a última tarefa.

## Variáveis

| Variável | Padrão | Para quê |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | obrigatório, em `credenciais.env` |
| `TELEGRAM_CHAT_ID` | — | obrigatório, em `credenciais.env` (é a allowlist) |
| `OPENCODE_BIN` | procurando no PATH e em locais conhecidos | caminho do binário, se não for achado |
| `TIMEOUT` (código) | 45 min | teto por tarefa |
| `PACIENCIA` (código) | 60 s | sem nenhum evento nesse tempo = sessão morta, descarta e tenta de novo |
| `POLL` (código) | 30 s | intervalo de long-poll |

## Estado e diagnóstico

```bash
systemctl --user status telebot
tail -f ~/.local/state/omarchy-voice/telebot/telebot.log
```

Arquivos em `~/.local/state/omarchy-voice/telebot/`:

| arquivo | para quê |
|---|---|
| `telebot.log` | log **anexado**, nunca sobrescrito — inclusive nos erros de boot |
| `sessao.txt` | id da sessão do opencode |
| `offset.txt` | última update processada |
| `pendente.txt` | o que está rodando agora (o `/status` lê isto) |
| `rodando.lock` | trava contra dois loops ao mesmo tempo |

Se o bot parece travado mas o serviço está ativo, quase sempre é DNS: ele
espera a rede resolver antes de começar a fazer poll.

## Segurança

> ⚠️ Isso significa que **quem mandar mensagem no chat autorizado roda comandos
> no seu PC com o seu usuário**. Mitigue assim:
> - token do bot e `chat.id` só no seu aparelho e no `credenciais.env` (600);
> - o `TELEGRAM_CHAT_ID` é a allowlist — qualquer outro chat é ignorado em silêncio;
> - nunca versione o `credenciais.env` (o `.gitignore` já cobre).

## Requisitos

- Python 3 (só stdlib: `urllib`, `json`, `subprocess`, `shutil`) — sem pip install
- [opencode](https://opencode.ai) instalado
- Linux com systemd (unidade de usuário) e Wayland ou X11