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
| `/vagas` | lista as vagas travadas esperando resposta e pergunta no chat, uma por vez |
| `/pular` | pula a pergunta atual e mostra a próxima |
| `/descartar` | joga fora a sessão de respostas e começa de novo |
| `/novo` | zera a memória, começa do zero |
| `/start`, `/ajuda`, `/help` | menu com exemplos |

Apps que o `/abrir` reconhece: firefox, chrome/chromium, vscode, terminal
(alacritty/kitty/ghostty), arquivos, spotify, discord, telegram, calculadora,
loja, okular, qbittorrent.

Também aceita endereço: `/abrir localhost:8080`, `/abrir exemplo.com/pagina`.
Sem esquema vira `http://` automaticamente; `javascript:` e `file:` ficam
bloqueados (só `http`, `https`, `file` e `mailto` passam).

### qBittorrent: WebUI em vez de janela

Se o qBittorrent roda como daemon (`qbittorrent-nox`, comum em instalação que
sobe no boot), **`/abrir qbittorrent` não abre janela — e não deve**. GUI e
`qbittorrent-nox` dividem o mesmo lock de instância única, então pedir o GUI
apenas acorda o daemon e nada aparece na tela.

Nesses casos o `/abrir` detecta a porta 8080 respondendo e abre a **WebUI**
(`http://localhost:8080`), que é o mesmo cliente. Se o daemon não estiver no
ar, cai no caminho normal e tenta o GUI. Vale para os apelidos `qbit`,
`torrent` e `qbitorrent`.

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

## Os quatro detalhes que fazem funcionar

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

### 4. `step_finish` é fim de **passo**, não de turno

No stream JSON, `step_finish` sai **uma vez por passo do agente**. Uma tarefa de
dois comandos gera dois `step_finish` — o segundo é o fim do turno de verdade.

Tratar o primeiro como "acabou" (e usar isso para matar o processo) corta a
tarefa no meio. Medido, com uma tarefa de dois comandos:

```
18.40s step_start   19.16s tool_use   19.16s step_finish   <-- fim do PASSO 1
27.84s step_start   27.93s text        27.93s step_finish   <-- fim do TURNO
```

Com um prazo de 8s a partir do primeiro `step_finish`, o processo era morto às
~27.2s — 0.7s antes da resposta, que o dono recebia como "o opencode travou".

Aqui `step_finish` só reinicia um relógio. Quem decide que a tarefa acabou é o
**processo sair** (o stdout fechar). Se o processo continuar vivo e calado por
`CARENCIA`, aí sim ele é encerrado — é assim que o bot sobrevive ao outro
clássico do opencode, que é responder e não encerrar.

## Variáveis

| Variável | Padrão | Para quê |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | obrigatório, em `credenciais.env` |
| `TELEGRAM_CHAT_ID` | — | obrigatório, em `credenciais.env` (é a allowlist) |
| `OPENCODE_BIN` | procurando no PATH e em locais conhecidos | caminho do binário, se não for achado |
| `TIMEOUT` (código) | 45 min | teto por tarefa |
| `PACIENCIA` (código) | 120 s | sem **nenhum** evento nesse tempo = o opencode nem começou; descarta e tenta de novo |
| `FIM_PERFEITO` (código) | 6 s | carência dada ao opencode para fechar sozinho depois do último passo |
| `CARENCIA` (código) | 25 s | silêncio depois do último evento com o processo ainda vivo = travou; aí mata |
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
## Vagas: responder o questionário pelo chat

Vaga com formulário travava e a única forma de responder era abrir o browser.
Agora dá para responder do próprio Telegram, e **nada é candidato sem o seu
botão**:

```
vaga: Analista de TI

1/3  Aceita o salário de R$ 2.400,00? *
    (responda, ou /pular)
```

No fim aparece o resumo com **Enviar** e **Descartar**. O botão **Enviar**
grava a resposta (o que for objetivo e genérico vai para o `perfil.json` e
reaproveita nas próximas vagas) e só então enfileira a candidatura.

Duas armadilhas que já custaram uma candidatura errada:

- **`POST /vaga` não candidata.** Esse endpoint só lê a página e devolve texto
  e links. Quem preenche e envia é o watcher do sidecar, lendo o
  `fila-vagas.jsonl`. Chamar o endpoint errado dá "enviei" sem ter feito nada.
- **Uma mensagem por vaga.** A Catho escreve `*` nos campos obrigatórios e,
  se as perguntas viajarem num texto só, os `*` se casam entre perguntas
  diferentes e o formulário recebe lixo.

O caminho do projeto de vagas vem de `VAGAS_DIR`, com padrão relativo ao
`HOME` — o repositório não carrega o caminho de ninguém.
