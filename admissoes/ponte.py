"""Ponte WA-AKG → Hermes (127.0.0.1:4134).
- POST /webhook: evento do WA-AKG, assinatura HMAC (X-Webhook-Signature: sha256=...) com o segredo de dados_dir.
  Sem segredo → 503; assinatura errada → 401. Mesmo keyId não entra 2 vezes.
- Só conversa privada. Mensagem do chip que a ponte não mandou = humano respondeu pelo celular → robô pausa.
- Vigia (thread): roda o turno da pessoa depois de `espera_juntar_mensagens_s` sem mensagem nova; relógio
  (cobranças, véspera, pós-exame) a cada 60 s; planilha de clínicas a cada 10 min.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import clinicas, config, fluxo, motor, regras
from .banco import Banco

CFG = config.carregar()
BANCO = Banco(str(Path(CFG["dados_dir"]) / "admissoes.db"))
TRAVAS: dict[int, threading.Lock] = {}
ULTIMA: dict[int, float] = {}
POOL = ThreadPoolExecutor(max_workers=3)


@asynccontextmanager
async def _vida(_app):
    threading.Thread(target=vigia, daemon=True).start()
    yield


app = FastAPI(title="Prompt Admissões — ponte", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_vida)


def _tel(jid: str) -> str:
    return jid.split("@")[0].split(":")[0]


def _arquivo(data: dict) -> str | None:
    url = data.get("fileUrl") or ""
    if not url:
        return None
    orig = Path(CFG["whatsapp"]["media_dir"]) / url.rsplit("/", 1)[-1]
    if not orig.exists():
        return None
    dest = Path(CFG["dados_dir"]) / "arquivos" / orig.name
    shutil.copy2(orig, dest)
    return str(dest)


def receber(ev: dict, cfg=None, banco=None) -> str:
    cfg, banco = cfg or CFG, banco or BANCO
    tipo, data = ev.get("event"), ev.get("data") or {}
    if data.get("isGroup") or data.get("chatType") not in (None, "PERSONAL"):
        return "ignorado: não é conversa privada"
    key = data.get("key") or {}
    if key.get("fromMe"):  # message.sent: `from` é o próprio chip; o contato está em receiver/remoteJid
        jid = data.get("receiver") or key.get("remoteJid") or ""
    else:
        jid = data.get("from") or key.get("remoteJid") or ""
    if not jid.endswith("@s.whatsapp.net"):
        return "ignorado: não é contato"
    agora = regras.agora(cfg)
    if tipo == "message.received" and not key.get("fromMe"):
        r = fluxo.receber(banco, cfg, _tel(jid), jid, data.get("pushName") or "",
                          data.get("caption") or data.get("content") or "", data.get("type") or "TEXT",
                          _arquivo(data), key.get("id"), agora)
        p = motor.achar_por_tel(banco, _tel(jid))
        if p:
            ULTIMA[p["id"]] = time.time()
        return r
    if key.get("fromMe") and key.get("id"):
        kid = key["id"]
        threading.Timer(15, _talvez_humano, args=(kid, _tel(jid), data.get("content") or "", cfg, banco)).start()
        return "ok"
    return "ignorado"


def _talvez_humano(kid: str, tel: str, texto: str, cfg, banco) -> None:
    if banco.um("SELECT id FROM saida WHERE key_id=?", kid) or banco.um("SELECT id FROM mensagens WHERE key_id=?", kid):
        return
    p = motor.achar_por_tel(banco, tel)
    if not p:
        return
    agora = regras.agora(cfg)
    banco.exec("INSERT INTO mensagens (pessoa_id, telefone, quem, texto, key_id, processada, em) VALUES (?,?,?,?,?,1,?)",
               p["id"], tel, "humano", texto, kid, agora.isoformat())
    ate = agora + timedelta(hours=cfg["whatsapp"]["pausa_humano_horas"])
    banco.atualizar(p["id"], pausa_ate=ate.isoformat())
    banco.log(p["id"], "humano_assumiu", f"robô pausado até {ate:%d/%m %H:%M}")


def _turno(pid: int) -> None:
    trava = TRAVAS.setdefault(pid, threading.Lock())
    if not trava.acquire(blocking=False):
        return
    try:
        fluxo.processar(BANCO, CFG, pid, regras.agora(CFG))
    except Exception as e:  # noqa: BLE001 — erro técnico: a pessoa não pode ficar sem resposta
        BANCO.log(pid, "erro_turno", repr(e))
        motor.humano(BANCO, pid, f"erro técnico no turno: {repr(e)[:120]}")
    finally:
        trava.release()


def vigia() -> None:
    ult_tick = ult_clin = 0.0
    while True:
        try:
            config.chaves(CFG)  # chaves.json pode mudar sem reiniciar
            if time.time() - ult_tick >= CFG["cobranca"]["checagem_s"]:
                ult_tick = time.time()
                motor.tick(BANCO, CFG, regras.agora(CFG))
            if time.time() - ult_clin >= CFG["clinicas"]["intervalo_s"]:
                ult_clin = time.time()
                clinicas.sincronizar(BANCO, CFG)
            espera = CFG["whatsapp"]["espera_juntar_mensagens_s"]
            for r in BANCO.todos("SELECT DISTINCT pessoa_id FROM mensagens WHERE quem='pessoa' AND processada=0"):
                pid = r["pessoa_id"]
                if pid and time.time() - ULTIMA.get(pid, 0) >= espera:
                    POOL.submit(_turno, pid)
        except Exception as e:  # noqa: BLE001
            BANCO.log(None, "erro_vigia", repr(e))
        time.sleep(2)


def assinatura_ok(segredo: str, corpo: bytes, cab: str) -> bool:
    esperado = "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, cab or "")


def segredo_webhook(cfg) -> str:
    p = Path(cfg["dados_dir"]) / "webhook-segredo.txt"
    return p.read_text().strip() if p.exists() else ""


@app.get("/healthz")
def healthz():
    return {"ok": True, "envio_ligado": CFG["chaves"].get("envio_ligado"),
            "agora": regras.agora(CFG).isoformat(timespec="seconds")}


@app.post("/webhook")
async def webhook(req: Request):
    corpo = await req.body()
    seg = segredo_webhook(CFG)
    if not seg:
        return JSONResponse({"erro": "sem segredo do webhook"}, 503)
    if not assinatura_ok(seg, corpo, req.headers.get("x-webhook-signature", "")):
        return JSONResponse({"erro": "assinatura inválida"}, 401)
    try:
        r = receber(json.loads(corpo))
    except Exception as e:  # noqa: BLE001
        BANCO.log(None, "erro_webhook", repr(e))
        r = "erro"
    return {"resultado": r}


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=CFG["ponte"]["host"], port=CFG["ponte"]["porta"], log_level="warning")


if __name__ == "__main__":
    main()
