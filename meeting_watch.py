#!/usr/bin/env python3
"""
Reunião → resumo. Grava o áudio do sistema (o que você ouve na call),
transcreve localmente com faster-whisper e pede ao agente um resumo, que chega
pelo mesmo canal do Majordomo (Telegram direto, ou intermediário pela ponte, no nó
"<node>-reuniao"). Nada de áudio sai da máquina.

    python -m pip install faster-whisper soundcard numpy
    # para gravar, nada mais; para gravar o áudio DO SISTEMA (loopback) o
    # soundcard já faz no Windows/Linux. Veja "Captura de áudio" no README.

Uso:
    python meeting_watch.py start [rótulo]   # começa a gravar; rótulo opcional
    python meeting_watch.py stop             # para, transcreve e resume
    python meeting_watch.py file <arquivo>   # transcreve/resume um áudio já gravado

Também responde a comandos do Telegram quando rodado com --serve:
    python meeting_watch.py --serve          # escuta /gravar, /parar, /resumo
      /gravar [rótulo]   começa a gravar
      /parar             para e gera o resumo
      /status_reuniao    diz se está gravando e há quanto tempo

Config (bloco "meeting" no config.json do Majordomo):
  "meeting": {
    "model": "medium",              // tiny|base|small|medium|large-v3 (pt: medium+ recomendado)
    "device": "auto",              // auto|cpu|cuda
    "language": "pt",
    "out_dir": "C:/majordomo/reunioes",
    "summarize": true,              // usa o agente para resumir (gasta token); false = só transcrição
    "notify_every_min": 0           // >0 envia um "ainda gravando (Xmin)" periódico
  }
"""
import datetime as dt
import json
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import channels as _channels

BASE = Path(__file__).resolve().parent
CFG = _channels.load_config(BASE / "config.json")
MT = CFG.get("meeting") or {}
OUT = Path(MT.get("out_dir", str(BASE / "reunioes")))
STATE = BASE / "meeting_state.json"
CH_STATE = BASE / "meeting_channel_state.json"  # offset/ids lidos do canal
SAMPLE_RATE = 16000  # Whisper trabalha em 16 kHz
CHUNK_SEC = 4


# ---------------------------------------------------------------- canal
ch_state = json.loads(CH_STATE.read_text(encoding="utf-8")) if CH_STATE.exists() else {}


def save_ch_state():
    CH_STATE.write_text(json.dumps(ch_state), encoding="utf-8")


# Mesmo canal do Majordomo (telegram direto ou intermediário, no nó "<node>-reuniao").
CHANNEL = _channels.open_channel(CFG, ch_state, save_ch_state, role="reuniao")


def send(text):
    CHANNEL.send(text)


# ---------------------------------------------------------------- gravação (loopback)
def record_loop(wav_path, stop_flag):
    """Grava o áudio que SAI do computador (loopback) até stop_flag['stop'] virar True."""
    try:
        import soundcard as sc
        import numpy as np
    except ImportError:
        send("❌ Faltam dependências da reunião. Rode: pip install faster-whisper soundcard numpy")
        raise

    spk = sc.default_speaker()
    mic = sc.get_microphone(id=str(spk.name), include_loopback=True)  # loopback do alto-falante
    OUT.mkdir(parents=True, exist_ok=True)
    wf = wave.open(str(wav_path), "wb")
    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SAMPLE_RATE)
    print("Gravando (loopback). Ctrl+C ou /parar para encerrar.")
    with mic.recorder(samplerate=SAMPLE_RATE, channels=1) as rec:
        while not stop_flag.get("stop"):
            data = rec.record(numframes=SAMPLE_RATE * CHUNK_SEC)  # float32 mono
            pcm = (np.clip(data[:, 0], -1, 1) * 32767).astype("<i2").tobytes()
            wf.writeframes(pcm)
    wf.close()


# ---------------------------------------------------------------- transcrição
def transcribe(wav_path):
    from faster_whisper import WhisperModel
    device = MT.get("device", "auto")
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = "float16" if device == "cuda" else "int8"
    model = WhisperModel(MT.get("model", "medium"), device=device, compute_type=compute)
    segments, info = model.transcribe(str(wav_path), language=MT.get("language", "pt"),
                                      vad_filter=True, beam_size=1)
    linhas = []
    for s in segments:
        ts = time.strftime("%H:%M:%S", time.gmtime(s.start))
        linhas.append(f"[{ts}] {s.text.strip()}")
    return "\n".join(linhas)


# ---------------------------------------------------------------- resumo pelo agente
def summarize(transcript, label):
    txt_path = OUT / f"{label}-transcricao.txt"
    txt_path.write_text(transcript, encoding="utf-8")
    if not MT.get("summarize", True):
        return f"📝 Transcrição salva em {txt_path}\n(resumo desligado em meeting.summarize)"
    cmd = __import__("shutil").which(CFG.get("claude_cmd", "claude")) or "claude"
    prompt = (
        "Esta é a transcrição de uma reunião (arquivo em " + str(txt_path) + "). "
        "Resuma em português, de forma objetiva, com: 1) assunto em 1 linha; "
        "2) decisões tomadas; 3) pendências e responsáveis (se citados); "
        "4) o que foi pedido/combinado para mim especificamente; 5) próximos passos com prazos, se houver. "
        "Não invente nomes nem dados que não estejam na transcrição."
    )
    try:
        p = subprocess.run([cmd, "-p", prompt, "--output-format", "json"],
                           cwd=CFG.get("project_dir", str(BASE)), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900, stdin=subprocess.DEVNULL)
        data = json.loads(p.stdout)
        res = data.get("result") or "(sem resumo)"
    except Exception as e:
        res = f"(não consegui resumir: {e})\nTranscrição salva em {txt_path}"
    (OUT / f"{label}-resumo.md").write_text(res, encoding="utf-8")
    return res


def process(wav_path, label):
    send(f"🎧 Transcrevendo {label}… (pode levar alguns minutos)")
    try:
        transcript = transcribe(wav_path)
    except Exception as e:
        send(f"❌ Falha na transcrição: {e}")
        return
    if not transcript.strip():
        send("⚠️ A gravação ficou sem fala reconhecível. Confira a captura de áudio (loopback).")
        return
    resumo = summarize(transcript, label)
    send(f"📋 Resumo — {label}\n\n{resumo}")


# ---------------------------------------------------------------- CLI e serve
def new_label(arg=""):
    base = (arg or "reuniao").strip().replace(" ", "-")[:30]
    return f"{base}-{dt.datetime.now():%Y%m%d-%H%M}"


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def cli_start(label):
    OUT.mkdir(parents=True, exist_ok=True)
    wav = OUT / f"{label}.wav"
    STATE.write_text(json.dumps({"label": label, "wav": str(wav), "started": time.time()}))
    send(f"🔴 Gravando: {label}")
    stop = {"stop": False}
    try:
        record_loop(wav, stop)
    except KeyboardInterrupt:
        pass
    # chegou aqui = parou
    process(wav, label)
    STATE.unlink(missing_ok=True)


def serve():
    """Escuta o canal (Telegram ou intermediário) e grava/para sob comando. Uma gravação por vez."""
    import threading
    send("🟢 Reuniões online. /gravar para começar.")
    rec_thread = None
    stop = {"stop": False}
    cur = {"label": None, "wav": None, "started": 0}

    while True:
        try:
            for t, meta in CHANNEL.poll():
                CHANNEL.ack(meta)  # antes de agir: um /parar não roda duas vezes
                cmd, _, arg = t.partition(" ")
                cmd = cmd.split("@")[0].lower()
                if cmd == "/gravar" and not rec_thread:
                    cur.update(label=new_label(arg), started=time.time())
                    cur["wav"] = str(OUT / f"{cur['label']}.wav")
                    stop["stop"] = False
                    rec_thread = threading.Thread(
                        target=lambda: record_loop(Path(cur["wav"]), stop), daemon=True)
                    OUT.mkdir(parents=True, exist_ok=True)
                    rec_thread.start()
                    send(f"🔴 Gravando: {cur['label']}")
                elif cmd == "/parar" and rec_thread:
                    stop["stop"] = True
                    rec_thread.join(timeout=20)
                    rec_thread = None
                    process(Path(cur["wav"]), cur["label"])
                elif cmd == "/status_reuniao":
                    if rec_thread:
                        mins = int((time.time() - cur["started"]) / 60)
                        send(f"🔴 Gravando {cur['label']} há {mins} min.")
                    else:
                        send("⚪ Nenhuma gravação em andamento.")
            # intermediários não têm long-poll: aguarda entre ciclos
            if not _channels.is_direct(CFG):
                time.sleep(CFG.get("poll_sec", 15))
        except Exception as e:
            print("serve:", e)
            time.sleep(5)


if __name__ == "__main__":
    if not MT:
        print('Falta o bloco "meeting" no config.json.'); sys.exit(1)
    args = sys.argv[1:]
    if not args:
        print(__doc__); sys.exit(0)
    if args[0] == "--serve":
        serve()
    elif args[0] == "start":
        cli_start(new_label(args[1] if len(args) > 1 else ""))
    elif args[0] == "file" and len(args) > 1:
        wav = Path(args[1])
        process(wav, wav.stem)
    else:
        print(__doc__)
