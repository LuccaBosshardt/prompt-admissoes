"""Cliente do WA-AKG `wa-akg-prompt` para a aba "Agente e WhatsApp": sessão, QR, situação, webhook.
Nunca chama `logout` (regra do Lucca: logout derruba o pareamento). Cada chamada = 1 tentativa, timeout curto.
"""
from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from pathlib import Path

from .config import ler_chave


class GatewayErro(Exception):
    pass


def _api(cfg: dict, metodo: str, caminho: str, corpo: dict | None = None, timeout: int = 15) -> tuple[int, dict]:
    w = cfg["whatsapp"]
    chave = ler_chave(w["credenciais_arquivo"], "API_KEY")
    if not chave or chave.startswith("PREENCHER"):
        raise GatewayErro("sem API key do WA-AKG (CREDENCIAIS.txt)")
    req = urllib.request.Request(w["base_url"] + caminho, method=metodo,
                                 data=json.dumps(corpo).encode() if corpo is not None else None,
                                 headers={"Content-Type": "application/json", "x-api-key": chave})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError) as e:
        raise GatewayErro(f"WA-AKG fora do ar ({e})") from e


def segredo_webhook(cfg: dict) -> str:
    p = Path(cfg["dados_dir"]) / "webhook-segredo.txt"
    if not p.exists():
        p.write_text(secrets.token_hex(32))
        p.chmod(0o600)
    return p.read_text().strip()


def estado(cfg: dict) -> dict:
    """{'gateway': bool, 'sessao': bool, 'status': str, 'numero': str|None, 'webhook': bool, 'erro': str|None}"""
    sid = cfg["whatsapp"]["sessao"]
    out = {"gateway": False, "sessao": False, "status": "—", "numero": None, "webhook": False, "erro": None}
    try:
        cod, d = _api(cfg, "GET", f"/api/sessions/{sid}")
    except GatewayErro as e:
        out["erro"] = str(e)
        return out
    out["gateway"] = True
    if cod == 404:
        out["status"] = "sem sessão"
        return out
    dado = d.get("data") or {}
    out.update(sessao=True, status=str(dado.get("status") or "?"))
    eu = dado.get("me") or dado.get("user") or {}
    if isinstance(eu, dict):
        out["numero"] = (eu.get("id") or "").split(":")[0].split("@")[0] or None
    try:
        cod, w = _api(cfg, "GET", f"/api/webhooks/{sid}")
        lista = w.get("data") or w.get("webhooks") or []
        url = f"http://{cfg['ponte']['host']}:{cfg['ponte']['porta']}/webhook"
        out["webhook"] = any(isinstance(x, dict) and x.get("url") == url for x in lista)
    except GatewayErro:
        pass
    return out


def conectar(cfg: dict) -> list[str]:
    """Cria a sessão (se falta), desliga o bot embutido e liga o webhook para a ponte. Idempotente."""
    sid, feito = cfg["whatsapp"]["sessao"], []
    cod, d = _api(cfg, "GET", f"/api/sessions/{sid}")
    if cod == 404:
        cod, d = _api(cfg, "POST", "/api/sessions", {"name": "Prompt Admissões", "sessionId": sid}, timeout=30)
        if cod >= 300:
            raise GatewayErro(f"não criou a sessão: {d.get('message') or cod}")
        feito.append("sessão criada")
        status = "STOPPED"  # o WA-AKG cria a sessão parada
    else:
        status = str((d.get("data") or {}).get("status") or "")
    # bot embutido do WA-AKG (comandos #ping, figurinha, auto-resposta) DESLIGADO; sem leitura automática e sem "online"
    _api(cfg, "POST", f"/api/sessions/{sid}/bot-config",
         {"enabled": False, "autoRead": False, "alwaysOnline": False, "autoReplyMode": "ALL"})
    cod, bc = _api(cfg, "GET", f"/api/sessions/{sid}/bot-config")  # confere de verdade
    bcd = bc.get("data") or bc
    if cod < 300 and bcd.get("enabled") is False and not bcd.get("autoRead") and not bcd.get("alwaysOnline"):
        feito.append("bot embutido desligado (conferido)")
    else:
        feito.append("ATENÇÃO: o bot embutido do WA-AKG NÃO desligou; avise o suporte")
    if not estado(cfg)["webhook"]:
        url = f"http://{cfg['ponte']['host']}:{cfg['ponte']['porta']}/webhook"
        cod, d = _api(cfg, "POST", f"/api/webhooks/{sid}", {"name": "ponte prompt-admissoes", "url": url,
                                                            "secret": segredo_webhook(cfg),
                                                            "events": ["message.received", "message.sent"]})
        feito.append("webhook ligado à ponte" if cod < 300 else f"webhook respondeu {cod}: {d.get('message', '')}")
    if status not in ("CONNECTED", "SCAN_QR"):  # sessão parada: liga o socket para o QR aparecer (nunca logout)
        cod, d = _api(cfg, "POST", f"/api/sessions/{sid}/start", {}, timeout=30)
        feito.append("sessão iniciada" if cod < 300 else f"start respondeu {cod}: {d.get('message', '')}")
    return feito


def qr(cfg: dict) -> dict:
    """{'base64': str} | {'conectado': True} | {'aguarde': msg}"""
    sid = cfg["whatsapp"]["sessao"]
    cod, d = _api(cfg, "GET", f"/api/sessions/{sid}/qr")
    if cod == 200 and d.get("base64"):
        return {"base64": d["base64"]}
    if d.get("connected"):
        return {"conectado": True}
    return {"aguarde": d.get("error") or d.get("message") or f"QR ainda não disponível ({cod})"}


def reiniciar(cfg: dict) -> str:
    cod, d = _api(cfg, "POST", f"/api/sessions/{cfg['whatsapp']['sessao']}/restart", {}, timeout=30)
    return "sessão reiniciada" if cod < 300 else f"não reiniciou: {d.get('message') or cod}"


# ---------------- tela WhatsApp (conversas do número) ----------------
def conversas(cfg: dict) -> list[dict]:
    cod, d = _api(cfg, "GET", f"/api/chat/{cfg['whatsapp']['sessao']}")
    if cod >= 300:
        raise GatewayErro(d.get("message") or f"lista de conversas respondeu {cod}")
    out = []
    for c in d.get("data") or []:
        jid = c.get("jid") or ""
        if jid.endswith("@broadcast") or jid.endswith("@newsletter"):
            continue
        ult = c.get("lastMessage") or {}
        out.append({"jid": jid, "nome": c.get("name") or c.get("notify") or jid.split("@")[0],
                    "grupo": jid.endswith("@g.us"), "ultima": ult.get("content") or "", "tipo": ult.get("type") or "TEXT",
                    "quando": ult.get("timestamp") or ""})
    out.sort(key=lambda x: x["quando"], reverse=True)
    return out


def mensagens(cfg: dict, jid: str) -> list[dict]:
    import urllib.parse
    cod, d = _api(cfg, "GET", f"/api/chat/{cfg['whatsapp']['sessao']}/{urllib.parse.quote(jid, safe='')}")
    if cod >= 300:
        raise GatewayErro(d.get("message") or f"mensagens respondeu {cod}")
    out = []
    for m in d.get("data") or []:
        url = m.get("mediaUrl") or ""
        out.append({"id": m.get("keyId") or m.get("id"), "eu": bool(m.get("fromMe")), "tipo": m.get("type") or "TEXT",
                    "texto": m.get("content") or "", "midia": url.rsplit("/", 1)[-1] if url.startswith("/api/media/") else "",
                    "quando": m.get("timestamp") or "", "status": m.get("status") or "", "autor": m.get("pushName") or ""})
    out.sort(key=lambda x: x["quando"])
    return out


def midia(cfg: dict, nome: str) -> tuple[bytes, str]:
    if not nome or "/" in nome or ".." in nome or "\\" in nome:
        raise GatewayErro("nome inválido")
    w = cfg["whatsapp"]
    req = urllib.request.Request(f"{w['base_url']}/api/media/{nome}", headers={"x-api-key": ler_chave(w["credenciais_arquivo"], "API_KEY") or ""})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read(), r.headers.get("Content-Type") or "application/octet-stream"
    except (urllib.error.URLError, OSError) as e:
        raise GatewayErro(f"mídia indisponível ({e})") from e


def enviar_humano(cfg: dict, jid: str, texto: str) -> str:
    """Mensagem digitada por uma pessoa da equipe na tela WhatsApp. 1 tentativa, sem reenvio."""
    import urllib.parse
    cod, d = _api(cfg, "POST", f"/api/messages/{cfg['whatsapp']['sessao']}/{urllib.parse.quote(jid, safe='')}/send",
                  {"message": {"text": texto}}, timeout=30)
    if cod >= 300:
        raise GatewayErro(d.get("message") or f"envio respondeu {cod}")
    return ((d.get("data") or {}).get("key") or {}).get("id") or "enviado"
