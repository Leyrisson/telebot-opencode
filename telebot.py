#!/usr/bin/env python3
"""telebot — ponte Telegram <-> opencode, por LONG-POLLING.

Você manda uma mensagem no app do Telegram (celular, qualquer rede), ela cai
aqui, o opencode executa no PC, e a resposta volta pro Telegram — e também
aparece como notificação no PC.

Por que long-polling e não webhook: o PC **nunca abre porta**. Ele pergunta ao
Telegram "tem mensagem nova?" a cada 30 s. Funciona do 4G, de hotel, de
qualquer lugar, sem Tailscale Funnel, sem ngrok, sem proxy. (O n8n não serve
aqui porque o container não alcança o host: 172.20.0.1:8788 dá timeout.)

O estado da conversa (a sessão do opencode) fica num arquivo, então o bot
lembra do contexto entre mensagens — "e aquele sistema em flask?" funciona.

Rode direto:   telebot.py            (foreground, Ctrl+C sai)
Rode serviço:  systemctl --user start telebot
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home()
BASE = HOME / ".config/omarchy-voice/plugins/telebot"
ENV = BASE / "credenciais.env"
STATE = HOME / ".local/state/omarchy-voice/telebot"
LOG = STATE / "telebot.log"
SESSAO = STATE / "sessao.txt"
OFFSET = STATE / "offset.txt"     # ultima update confirmada (evita replay)
PEND = STATE / "pendente.txt"      # o que o opencode está fazendo agora
LOCK = STATE / "rodando.lock"

TELEGRAM = "https://api.telegram.org"
WORKDIR = HOME                     # onde o opencode trabalha


def monta_path():
    """Monta o PATH do seu PC — o que o serviço systemd NÃO tem.

    Bug de 30/09: o serviço herda o PATH do systemd (`/usr/local/bin:/usr/bin`),
    onde não existe o opencode (ele vive no mise). Resultado: shutil.which()
    devolvia None, o bot caía no literal "opencode" e TODA tarefa morria com
    FileNotFoundError. Aqui o PATH é reconstruído com os diretórios reais.
    """
    extras = [
        Path("/usr/share/omarchy/bin"),
        HOME / ".local/bin",
        HOME / ".local/share/mise/shims",
        HOME / ".bun/bin",
    ]
    mise = HOME / ".local/share/mise/installs"
    if mise.is_dir():
        extras += sorted(mise.glob("*/bin")) + sorted(mise.glob("*/*/bin"))
    base = ["/usr/local/bin", "/usr/bin", "/bin",
            "/usr/local/sbin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys([str(p) for p in extras if p.is_dir()] + base))


PATH = monta_path()


def acha_binario(nome):
    """Procura um executável no PATH montado (funciona fora do seu shell)."""
    achado = shutil.which(nome, path=PATH)
    if achado:
        return achado
    for p in PATH.split(":"):
        alvo = Path(p) / nome
        if alvo.is_file() and os.access(alvo, os.X_OK):
            return str(alvo)
    return None


def descobre_wayland():
    """WAYLAND_DISPLAY certo: o do serviço pode ser obsoleto após um reboot."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    atual = os.environ.get("WAYLAND_DISPLAY", "")
    if atual and (runtime / atual).exists():
        return atual
    for sock in sorted(runtime.glob("wayland-*")):
        if sock.name.endswith(".lock"):
            continue
        return sock.name
    return atual or "wayland-1"


OPENCODE = os.environ.get("OPENCODE_BIN") or acha_binario("opencode") or "opencode"
TIMEOUT = 45 * 60                  # 45 min por tarefa (sistema flask pede tempo)
# Bug de 30/09 (o 4º): 60s era pouco. O opencode pode ficar **inteiramente
# mudo** antes do primeiro evento — o boot dele sozinho leva ~18s com AGENTS.md
# grande, e o provedor pode demorar. Medido: 18.4s de silêncio num tarefa de 2
# comandos. Com 60s o bot se auto-curava antes da hora e jogava a tarefa fora.
PACIENCIA = 120                    # sem NENHUM evento por isso = sessão morta
# `step_finish` NÃO é o fim do turno: o opencode emite um a cada passo do
# agente. Uma tarefa de 2 comandos gera 2 step_finish. Bug de 30/09 (o 4º): o
# bot tratava o 1º step_finish como "terminou", armava um prazo de 8s e matava
# o processo — cortando a tarefa 0.7s antes da resposta, que o dono via como
# "travou". Agora step_finish só reinicia o relógio de silêncio; quem decide que
# acabou é o processo SAIR (stdout fecha) ou o silêncio longo demais.
FIM_PERFEITO = 6                   # após o último passo, espera o opencode fechar
CARENCIA = 25                      # silêncio depois do último evento = travou
MAX_ENVIO = 3800                   # Telegram corta em 4096; fica folga p/ moldura
POLL = 30                          # segundos de long-poll

# Só este chat conversa com o bot. Trava de segurança: mesmo com o token
# vazado, ninguem mais consegue mandar comando no seu PC.
# Vem do credenciais.env (TELEGRAM_CHAT_ID) — nunca versionado.
CHAT_ID = ""

_lock = threading.Lock()
_ultimo_log = 0.0


def log(msg):
    """Escreve no log. Sempre."""
    STATE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a") as f:
        f.write(f"{ts} {msg}\n")
    print(f"{ts} {msg}", flush=True)


def log_erro(msg):
    """Log de erro com freio: a mesma falha repetida (ex.: sem rede no boot)
    entra 1x por minuto, não 1x por tentativa."""
    global _ultimo_log
    agora = time.time()
    if agora - _ultimo_log < 60:
        return
    _ultimo_log = agora
    log(msg)


def load_env():
    data = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    data.setdefault("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    data.setdefault("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", ""))
    return data


def api(env, metodo, payload=None, params=None, timeout=70):
    """Chama a API do Telegram. Devolve o JSON (ou None em falha)."""
    url = f"{TELEGRAM}/bot{env['TELEGRAM_BOT_TOKEN']}/{metodo}"
    dados = None
    if payload is not None:
        dados = json.dumps(payload).encode()
    elif params is not None:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=dados, headers={
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        corpo = e.read().decode(errors="replace")[:300]
        log_erro(f"ERRO telegram {metodo}: HTTP {e.code} {corpo}")
    except Exception as e:
        log_erro(f"ERRO telegram {metodo}: {e}")
    return None


def manda(chat, texto, botao=None):
    """Envia mensagem. Texto longo e cortado em pedaços que caibam no limite."""
    texto = str(texto or "").strip() or "(vazio)"
    linhas, atual, tamanho = [], [], 0
    for ln in texto.splitlines() or [""]:
        add = len(ln) + 1
        if tamanho + add > MAX_ENVIO and atual:
            linhas.append("\n".join(atual))
            atual, tamanho = [], 0
        atual.append(ln)
        tamanho += add
    if atual:
        linhas.append("\n".join(atual))
    api_env = load_env()
    for i, parte in enumerate(linhas, 1):
        cab = f"[{i}/{len(linhas)}]\n" if len(linhas) > 1 else ""
        api(api_env, "sendMessage", {
            "chat_id": chat, "text": cab + parte, "disable_web_page_preview": True})
        time.sleep(0.4)


def avisa_pc(titulo, texto):
    """Notificação no PC — a outra metade do 'me responda no celular E no PC'."""
    subprocess.run(["notify-send", "-a", "opencode", "-t", "25000",
                    str(titulo)[:70], str(texto)[:220]],
                   check=False, capture_output=True)


def le_sessao():
    return SESSAO.read_text().strip() if SESSAO.exists() else ""


def guarda_sessao(sid):
    STATE.mkdir(parents=True, exist_ok=True)
    SESSAO.write_text(sid)


def le_offset():
    try:
        return int(OFFSET.read_text().strip())
    except Exception:
        return 0


def guarda_offset(valor):
    STATE.mkdir(parents=True, exist_ok=True)
    OFFSET.write_text(str(valor))


def _consome(fluxo, fila):
    """Joga as linhas do opencode numa fila (thread separada)."""
    try:
        for ln in fluxo:
            fila.put(ln)
    finally:
        fila.put(None)


def roda_opencode(pergunta):
    """Executa o opencode e devolve (texto, ok).

    Usa a MESMA sessão entre mensagens, então o bot tem memória.

    Quem decide que a tarefa acabou é o PROCESSO SAIR (o stdout fecha), e não um
    evento do stream. Dois bugs de 30/09 explicam por quê:

    1. Retomar sessão antiga deixava o processo travado para sempre depois de já
       ter respondido — o bot ficava 45 min parado e o dono achava que o pedido
       tinha falhado.
    2. `step_finish` NÃO é o fim do turno: sai um a cada passo do agente. Tratar
       o primeiro como "terminou" matava a tarefa no meio (prova: uma tarefa de
       2 comandos, resposta 0.7s depois do prazo de 8s).

    Então: `step_finish` só reinicia o relógio. Sai no fim de verdade — processo
    fechou, ou CARENCIA de silêncio depois do último evento (o processo continua
    vivo e mudo, aí mata), ou TIMEOUT.
    """
    env = os.environ.copy()
    env.update({
        "PATH": PATH,
        "HOME": str(HOME),
        "DISPLAY": env.get("DISPLAY", ":0"),
        "WAYLAND_DISPLAY": descobre_wayland(),
        "XDG_RUNTIME_DIR": env.get("XDG_RUNTIME_DIR",
                                   f"/run/user/{os.getuid()}"),
        "DBUS_SESSION_BUS_ADDRESS": env.get(
            "DBUS_SESSION_BUS_ADDRESS",
            f"unix:path=/run/user/{os.getuid()}/bus"),
        "XDG_SESSION_TYPE": "wayland",
    })
    sid = le_sessao()

    def executa(usar_sessao=True):
        # --auto: sem isso o opencode PARE para pedir permissao a cada arquivo
        # fora do diretorio de trabalho e o bot (sem TUI) recusa tudo — a tarefa
        # "cria um sistema flask" rodava e nao criava nada. O --auto so aprova o
        # que pediria aprovacao; regra "deny" continua valendo (ex.: .env).
        c = [OPENCODE, "run", "--format", "json", "--auto"]
        if usar_sessao and sid:
            c += ["--session", sid]
        # "--" garante que um texto comecando com "-" nao vire flag do yargs
        c += ["--", pergunta]
        proc = subprocess.Popen(c, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                stdin=subprocess.DEVNULL, cwd=str(WORKDIR), env=env)
        fila = queue.Queue()
        threading.Thread(target=_consome, args=(proc.stdout, fila),
                         daemon=True).start()
        texto, novo_sid, bruto, passos = [], "", [], 0
        fim = time.time() + TIMEOUT
        ultimo = time.time()  # quando saiu o último evento
        motivo = "processo encerrou"
        while time.time() < fim:
            try:
                ln = fila.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None:
                    # processo morreu: drena o que sobrou na fila
                    while not fila.empty():
                        ln = fila.get_nowait()
                        if ln is None:
                            continue
                        bruto.append(ln)
                    break
                calado = time.time() - ultimo
                if not bruto and calado > PACIENCIA:
                    # Nenhum evento nenhum desde o começo: o opencode nem
                    # começou (sessão estragada, ou boot lento demais).
                    motivo = f"nenhum evento em {PACIENCIA}s"
                    break
                if bruto and calado > CARENCIA:
                    # Teve evento, mas calou: o opencode travou sem fechar.
                    motivo = f"calou {CARENCIA}s depois do último evento"
                    break
                continue
            if ln is None:
                motivo = "stdout fechou"   # acabou de vez
                break
            bruto.append(ln)
            ultimo = time.time()
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("sessionID"):
                novo_sid = novo_sid or d["sessionID"]
            if d.get("type") == "text":
                t = (d.get("part") or {}).get("text")
                if t:
                    texto.append(t)
            if d.get("type") == "step_finish":
                # FIM DE PASSO, não de turno: só dá mais FIM_PERFEITO de prazo
                # pro opencode fechar sozinho depois do último passo.
                passos += 1
                ultimo = time.time() - FIM_PERFEITO
        else:
            motivo = f"passou de {TIMEOUT // 60} min"
        travado = proc.poll() is None
        if travado:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        if passos:
            log(f"opencode: {passos} passo(s), {motivo}")
        return (("\n".join(texto).strip(), novo_sid, proc.returncode,
                 "\n".join(bruto), motivo), travado)

    try:
        (saida, novo_sid, rc, bruto, motivo), travado = executa()
    except FileNotFoundError:
        return (f"❌ Não achei o opencode neste PC. "
                f"Procurei em: {OPENCODE}.", False)
    except Exception as e:
        return (f"Falhou ao rodar o opencode: {e}", False)

    if saida and novo_sid:
        # Só reaproveita a sessão se o opencode encerrou sozinho. Quando é
        # preciso matar, a sessão fica com estado pela metade e a PRÓXIMA tarefa
        # que tentar retomá-la trava — melhor perder a memória do que travar.
        guarda_sessao("" if travado else novo_sid)
        if travado:
            log("opencode nao encerrou sozinho; contexto descartado")

    # Não respondeu nada? A sessão salva deve estar estragada — joga fora e
    # tenta uma vez do zero, senão o bot fica travado em loop.
    if not saida:
        if sid:
            log(f"sem resposta ({motivo}); zerando a sessao e tentando de novo")
            guarda_sessao("")
            try:
                (saida, novo_sid, rc, bruto,
                 motivo), travado = executa(usar_sessao=False)
            except Exception as e:
                return (f"Falhou: {e}", False)
            if saida and novo_sid:
                guarda_sessao("" if travado else novo_sid)

    if saida:
        return (saida + ("\n\n_(opencode não fechou sozinho; encerrei)_"
                         if travado else ""), True)
    # A mensagem tem que dizer o que ACONTECEU, não o prazo teórico: o dono
    # lia "travou sem responder em 45 min" em tarefas que cortaram em 8s e achava
    # que o bot era lento, quando na verdade ele tinha matado a tarefa.
    if not travado:
        return (f"❌ O opencode encerrou com erro (rc={rc}):\n{bruto[-600:]}",
                False)
    return (f"⏱️ O opencode {motivo} e eu tive que encerrá-lo — "
            "não respondeu nada. Zerei o contexto; manda de novo que eu tento "
            "do zero.", False)


APPS = {
    "firefox": ["firefox"], "navegador": ["firefox"], "browser": ["firefox"],
    "chrome": ["google-chrome-stable", "chromium"], "chromium": ["chromium"],
    "vscode": ["code"], "codigo": ["code"], "code": ["code"],
    "terminal": ["alacritty"], "emulador": ["alacritty"], "kitty": ["kitty"],
    "ghostty": ["ghostty"], "arquivos": ["nautilus"], "explorer": ["nautilus"],
    "pastas": ["nautilus"], "spotify": ["spotify"], "discord": ["discord"],
    "telegram": ["telegram-desktop"], "calculadora": ["qalculate"],
    "loja": ["pamacor"], "okular": ["okular"],
    # qBittorrent: instalado em /usr/bin (pacman 5.2.3) mas faltava aqui. Sem
    # esta entrada o /abrir caía no `acha_binario`, que acha o binário — mas o
    # dono não tinha como pedir por nome e o caminho de erro não ajudava.
    "qbittorrent": ["qbittorrent"], "torrent": ["qbittorrent"],
    "qbitorrent": ["qbittorrent"], "qbit": ["qbittorrent"],
}

# Apps que aqui NÃO têm GUI possível, e o certo é abrir a interface web.
#
# qBittorrent: o `qbittorrent.service` do sistema roda `qbittorrent-nox
# --webui-port=8080` (headless). GUI e nox dividem o MESMO lock de instância
# única, então pedir "abre o qbittorrent" launching `qbittorrent` só acordava o
# daemon e não abria janela nenhuma — 2026-10-04, foi exatamente o "não abre" do
# dono. A WebUI é o mesmo cliente e responde em localhost:8080.
URLS_APP = {
    "qbittorrent": "http://localhost:8080",
    "qbit": "http://localhost:8080",
    "torrent": "http://localhost:8080",
    "qbitorrent": "http://localhost:8080",
}


def porta_ouvida(host, porta, timeout=2):
    import socket
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, porta)) == 0


# Endereço colado sem esquema: "localhost:5000", "127.0.0.1:8080",
# "exemplo.com/pagina". É o formato que a ajuda do /abrir sugere, então tem que
# funcionar — e continua sendo http, nunca javascript: ou file:.
ENDERECO = re.compile(
    r"^(localhost|127\.0\.0\.1|\[::1\]|[\w-]+(\.[\w-]+)+)(:\d+)?(/\S*)?$", re.I)


def _env_gui():
    """Ambiente com o PATH e as variáveis de sessão do dono.

    O serviço roda sem sessão gráfica; sem DISPLAY/WAYLAND_DISPLAY/DBUS o
   xdg-open não acha o navegador e o app abre em lugar nenhum.
    """
    env = os.environ.copy()
    env.update({"PATH": PATH, "DISPLAY": env.get("DISPLAY", ":0"),
                "WAYLAND_DISPLAY": descobre_wayland(),
                "XDG_RUNTIME_DIR": env.get("XDG_RUNTIME_DIR",
                                           f"/run/user/{os.getuid()}"),
                "DBUS_SESSION_BUS_ADDRESS": env.get(
                    "DBUS_SESSION_BUS_ADDRESS",
                    f"unix:path=/run/user/{os.getuid()}/bus")})
    return env


def _abre_url(url, alvo, nota="Abri"):
    """Abre uma URL no navegador padrão. Devolve (texto, deu_certo)."""
    exe = acha_binario("xdg-open")
    if not exe:
        return ("❌ Não achei 'xdg-open' no PATH deste serviço.", False)
    try:
        subprocess.Popen([exe, url], env=_env_gui(), cwd=str(WORKDIR),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return (f"❌ Falhou ao abrir '{alvo}': {e}", False)
    log(f"abriu {url} -> {exe}")
    return (f"✅ {nota}: *{url}*", True)


def abrir_alvo(alvo):
    """Abre um app/URL direto, sem passar pelo opencode.

    Caminho determinístico: garante que o pedido mais comum do dono
    ('abre o firefox') funcione de primeira, sem depender do opencode.
    """
    alvo = (alvo or "").strip().strip("<>\"'")
    if not alvo:
        return ("Diga o que abrir: /abrir firefox, /abrir vscode, "
                "/abrir localhost:5000", False)
    # App que só existe como interface web (qBittorrent com o daemon nox no ar):
    # abrir a GUI não adianta, então abre a URL.
    url_app = URLS_APP.get(alvo.lower())
    if url_app and porta_ouvida("localhost", 8080):
        return _abre_url(url_app, alvo,
                         "Abri a WebUI (o cliente roda como daemon, sem janela)")
    # Endereço sem esquema: o regex de esquema abaixo exige ":" e não pega.
    if ENDERECO.match(alvo):
        return _abre_url("http://" + alvo, alvo)
    # URL (http, https, about:, file:, mailto:) — abre no navegador padrão
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(//)?\S", alvo):
        if not re.match(r"^(https?|file|mailto):", alvo, re.I):
            return (f"Não abro '{alvo}' por segurança — só http(s), file e mailto.",
                    False)
        return _abre_url(alvo, alvo)
    else:
        cmd = APPS.get(alvo.lower())
        if not cmd:
            achado = acha_binario(alvo)
            if not achado:
                return (f"Não achei o aplicativo '{alvo}' neste PC.\n"
                        f"Apps rápidos: {', '.join(sorted(set(APPS))[:14])}…\n"
                        "Ou mande a frase completa que eu uso o opencode.", False)
            cmd = [alvo]
    env = _env_gui()
    exe = cmd[0] if Path(cmd[0]).is_absolute() else acha_binario(cmd[0])
    if not exe:
        return (f"❌ Não achei '{cmd[0]}' no PATH deste serviço.", False)
    try:
        subprocess.Popen([exe] + cmd[1:], env=env, cwd=str(WORKDIR),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return (f"❌ Falhou ao abrir '{alvo}': {e}", False)
    log(f"abriu {alvo} -> {exe}")
    return (f"✅ Abri: *{alvo}*", True)


def diagnostico():
    """Roda do celular e mostra se a máquina está pronta pra trabalhar."""
    linhas = [
        f"opencode: `{OPENCODE}`",
        f"existe no disco: {'sim' if Path(OPENCODE).exists() else 'NÃO ❌'}",
        f"PATH: `{PATH[:110]}…`",
        f"tela: DISPLAY={os.environ.get('DISPLAY')} "
        f"WAYLAND={descobre_wayland()}",
        f"firefox: {acha_binario('firefox') or 'NÃO achei ❌'}",
        f"wayland socket: "
        f"{'ok' if (Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user')) / descobre_wayland()).exists() else 'NÃO ❌'}",
        f"sessão opencode: {le_sessao() or '(sem memória)'}",
    ]
    return "\n".join(linhas)


def trata(mensagem):
    """Roda uma tarefa e responde no Telegram + no PC."""
    texto = (mensagem.get("text") or "").strip()
    chat = str(mensagem.get("chat", {}).get("id", ""))
    nome = (mensagem.get("from") or {}).get("first_name", "")
    if not texto or chat != CHAT_ID:
        return
    log(f"msg de {nome}: {texto[:200]}")

    partes = texto.split(maxsplit=1)
    cmd = partes[0].lower().lstrip("/")
    resto = partes[1] if len(partes) > 1 else ""
    if cmd in ("start", "ajuda", "help"):
        manda(chat,
              "Oi! Eu sou o opencode no seu PC.\n\n"
              "Mande o que quiser, em português, do celular mesmo:\n"
              "• abre o vscode e cria um sistema flask\n"
              "• abre localhost:5000 no navegador\n"
              "• le meu e-mail mais recente\n"
              "• quanto disk ta sobrando?\n\n"
              "Eu executo aqui e respondo aqui. Lembro do contexto entre "
              "as mensagens. Tempo limite de 45 min por tarefa.\n\n"
              "Comandos: /abrir firefox (abre direto, na hora) · /teste "
              "(diagnóstico) · /status (o que estou fazendo) · "
              "/novo (zera a memória)")
        return
    if cmd == "status":
        oq = PEND.read_text().strip() if PEND.exists() else "nada"
        manda(chat, f"Agora eu estou: {oq}" if oq != "nada" else "Tô parado, esperando tarefa.")
        return
    if cmd == "teste":
        manda(chat, "🩺 Diagnóstico:\n\n" + diagnostico())
        return
    if cmd == "abrir" and resto:
        resposta, ok = abrir_alvo(resto)
        marca = "✅" if ok else "⚠️"
        manda(chat, f"{marca} {resposta}")
        avisa_pc(f"{marca} {resto}", resposta)
        return
    if cmd == "novo":
        guarda_sessao("")
        manda(chat, "Contexto zerado. Próxima mensagem começa do zero.")
        return

    avisa_pc("opencode", f"{nome}: {texto[:80]}")
    manda(chat, f"Recebi — começando.\n\n{texto}")
    PEND.parent.mkdir(parents=True, exist_ok=True)
    PEND.write_text(f"{nome}: {texto[:200]}")
    inicio = time.time()
    try:
        resposta, ok = roda_opencode(texto)
    finally:
        PEND.unlink(missing_ok=True)
    dt = int(time.time() - inicio)
    marca = "✅" if ok else "⚠️"
    log(f"tarefa em {dt}s ok={ok}: {resposta[:300].replace(chr(10), ' ')}")
    manda(chat, f"{marca} *Concluído em {dt}s*\n\n{resposta}")
    avisa_pc(f"{marca} opencode ({dt}s)", resposta)


def main():
    global CHAT_ID
    env = load_env()
    CHAT_ID = str(env.get("TELEGRAM_CHAT_ID", "")).strip()
    if not env.get("TELEGRAM_BOT_TOKEN"):
        print("sem TELEGRAM_BOT_TOKEN — veja credenciais.env", file=sys.stderr)
        log_erro("ERRO de boot: sem TELEGRAM_BOT_TOKEN em credenciais.env")
        return 1
    if not CHAT_ID:
        print("sem TELEGRAM_CHAT_ID — veja credenciais.env", file=sys.stderr)
        log_erro("ERRO de boot: sem TELEGRAM_CHAT_ID em credenciais.env")
        return 1

    STATE.mkdir(parents=True, exist_ok=True)
    log(f"ligado | chat {CHAT_ID} | opencode {OPENCODE}")
    if not Path(OPENCODE).exists():
        log(f"AVISO: opencode NAO existe em {OPENCODE} — toda tarefa vai falhar")

    # Espera a rede: no boot a DNS ainda não resolve e o bot despejava erro.
    for tentativa in range(30):
        if api(env, "getMe"):
            break
        if tentativa == 0:
            log_erro("rede ainda nao respondeu; aguardando")
        time.sleep(10)

    # avisa no Telegram que subiu
    api(env, "sendMessage", {
        "chat_id": CHAT_ID,
        "text": "✅ opencode no PC <b>ligado</b>. Pode mandar tarefa.",
    })

    # Offset persistido: sem isso, cada reinício do serviço RECEBE de novo a
    # última mensagem e reexecuta a tarefa — inclusive "desliga o PC".
    offset = le_offset()
    log(f"retomando da update {offset}")
    r = None
    while True:
        if not _lock.locked():
            _lock.acquire()
            try:
                r = api(env, "getUpdates",
                        params={"timeout": POLL, "offset": offset,
                                "allowed_updates": '["message"]'})
            finally:
                _lock.release()
        if not r or not r.get("ok"):
            time.sleep(10)
            continue
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            guarda_offset(offset)
            msg = upd.get("message") or {}
            # só o SEU chat dispara tarefa; o resto é ignorado em silêncio
            if str(msg.get("chat", {}).get("id", "")) != CHAT_ID:
                continue
            try:
                trata(msg)
            except Exception as e:
                log_erro(f"ERRO tratando: {e}")
                manda(CHAT_ID, f"⚠️ Deu erro aqui: {e}")
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
