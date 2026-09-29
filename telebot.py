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
PEND = STATE / "pendente.txt"      # o que o opencode está fazendo agora
LOCK = STATE / "rodando.lock"

TELEGRAM = "https://api.telegram.org"
OPENCODE = os.environ.get("OPENCODE_BIN") or shutil.which("opencode") or "opencode"
WORKDIR = HOME                     # onde o opencode trabalha
TIMEOUT = 45 * 60                  # 45 min por tarefa (sistema flask pede tempo)
MAX_ENVIO = 3800                   # Telegram corta em 4096; fica folga p/ moldura
POLL = 30                          # segundos de long-poll

# Só este chat conversa com o bot. Trava de segurança: mesmo com o token
# vazado, ninguem mais consegue mandar comando no seu PC.
# Vem do credenciais.env (TELEGRAM_CHAT_ID) — nunca versionado.
CHAT_ID = ""

_lock = threading.Lock()


def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a") as f:
        f.write(f"{ts} {msg}\n")
    print(f"{ts} {msg}", flush=True)


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
        log(f"ERRO telegram {metodo}: HTTP {e.code} {corpo}")
    except Exception as e:
        log(f"ERRO telegram {metodo}: {e}")
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


def roda_opencode(pergunta):
    """Executa o opencode e devolve (texto, ok).

    Usa a MESMA sessão entre mensagens, então o bot tem memória. Se a sessão
    sumiu (apagada no TUI, banco limpo), recomeça sem --session em vez de
    falhar — e avisa que perdeu o contexto.
    """
    env = os.environ.copy()
    env.update({
        "DISPLAY": env.get("DISPLAY", ":0"),
        "WAYLAND_DISPLAY": env.get("WAYLAND_DISPLAY", "wayland-1"),
        "XDG_RUNTIME_DIR": env.get("XDG_RUNTIME_DIR", "/run/user/1000"),
        "DBUS_SESSION_BUS_ADDRESS": env.get(
            "DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus"),
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
        return subprocess.run(c, capture_output=True, text=True,
                              timeout=TIMEOUT, cwd=str(WORKDIR), env=env)

    try:
        r = executa()
    except subprocess.TimeoutExpired:
        return (f"Estourei o tempo de {TIMEOUT // 60} min nessa tarefa. "
                "Tente de novo em partes menores.", False)
    except Exception as e:
        return (f"Falhou ao rodar o opencode: {e}", False)

    # Saiu com erro? Uma sessão morta é a causa mais comum — tenta de novo.
    if r.returncode != 0 and sid:
        log("sessao falhou; tentando sem --session")
        try:
            r2 = executa(usar_sessao=False)
        except Exception as e:
            return (f"Falhou: {e}", False)
        if r2.returncode == 0:
            r = r2
            guarda_sessao("")
        else:
            return (f"Erro do opencode: {(r2.stderr or r2.stdout)[-800:]}", False)

    if r.returncode != 0:
        return (f"Erro do opencode: {(r.stderr or r.stdout)[-800:]}", False)

    texto, novo_sid = [], ""
    for ln in r.stdout.splitlines():
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
    if novo_sid and novo_sid != le_sessao():
        guarda_sessao(novo_sid)
    saida = "\n".join(texto).strip()
    return (saida or "(opencode terminou sem texto)", True)


def trata(mensagem):
    """Roda uma tarefa e responde no Telegram + no PC."""
    texto = (mensagem.get("text") or "").strip()
    chat = str(mensagem.get("chat", {}).get("id", ""))
    nome = (mensagem.get("from") or {}).get("first_name", "")
    if not texto or chat != CHAT_ID:
        return

    cmd = texto.split()[0].lower().lstrip("/")
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
              "Comandos: /status (o que estou fazendo) · /novo (zera a memória) "
              "· /start")
        return
    if cmd == "status":
        oq = PEND.read_text().strip() if PEND.exists() else "nada"
        manda(chat, f"Agora eu estou: {oq}" if oq != "nada" else "Tô parado, esperando tarefa.")
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
    manda(chat, f"{marca} *Concluído em {dt}s*\n\n{resposta}")
    avisa_pc(f"{marca} opencode ({dt}s)", resposta)


def main():
    global CHAT_ID
    env = load_env()
    CHAT_ID = str(env.get("TELEGRAM_CHAT_ID", "")).strip()
    if not env.get("TELEGRAM_BOT_TOKEN"):
        print("sem TELEGRAM_BOT_TOKEN — veja credenciais.env", file=sys.stderr)
        log("ERRO de boot: sem TELEGRAM_BOT_TOKEN em credenciais.env")
        return 1
    if not CHAT_ID:
        print("sem TELEGRAM_CHAT_ID — veja credenciais.env", file=sys.stderr)
        log("ERRO de boot: sem TELEGRAM_CHAT_ID em credenciais.env")
        return 1

    STATE.mkdir(parents=True, exist_ok=True)
    log(f"ligado | chat {CHAT_ID} | opencode {OPENCODE}")
    # avisa no Telegram que subiu
    api(env, "sendMessage", {
        "chat_id": CHAT_ID,
        "text": "✅ opencode no PC <b>ligado</b>. Pode mandar tarefa.",
    })

    offset = 0
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
            msg = upd.get("message") or {}
            # só o SEU chat dispara tarefa; o resto é ignorado em silêncio
            if str(msg.get("chat", {}).get("id", "")) != CHAT_ID:
                continue
            try:
                trata(msg)
            except Exception as e:
                log(f"ERRO tratando: {e}")
                manda(CHAT_ID, f"⚠️ Deu erro aqui: {e}")
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
