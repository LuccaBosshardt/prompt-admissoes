"""Envio pelo WA-AKG `wa-akg-prompt`. Toda mensagem entra na tabela `saida`.
chaves.envio_ligado = false → modo seco: grava e NÃO chama a API.
1 tentativa só, nunca reenvia (regra 25/11: reenvio automático no WA-AKG duplicou mensagens). Erro → log + humano.
API: POST /api/messages/<sessao>/<jid>/send {"message":{"text":...}} ; mídia: /media (multipart). Header x-api-key.
"""
from __future__ import annotations

import json
import mimetypes
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .banco import agora_txt
from .config import chaves, ler_chave


def _chave(cfg: dict) -> str:
    w = cfg["whatsapp"]
    k = ler_chave(w["credenciais_arquivo"], "API_KEY")
    if not k or k.startswith("PREENCHER"):
        raise RuntimeError("sem API_KEY do WA-AKG em CREDENCIAIS.txt")
    return k


def _post(cfg: dict, jid: str, texto: str, arquivo: str | None) -> str:
    w = cfg["whatsapp"]
    base = f"{w['base_url']}/api/messages/{w['sessao']}/{urllib.parse.quote(jid, safe='')}"
    if not arquivo:
        req = urllib.request.Request(base + "/send", data=json.dumps({"message": {"text": texto}}).encode(),
                                     headers={"Content-Type": "application/json", "x-api-key": _chave(cfg)})
    else:
        fronteira = uuid.uuid4().hex
        nome = Path(arquivo).name
        mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
        tipo = "image" if mime.startswith("image/") else "document"
        partes = []
        for k, v in (("type", tipo), ("caption", texto), ("fileName", nome)):
            partes.append(f'--{fronteira}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        partes.append(f'--{fronteira}\r\nContent-Disposition: form-data; name="file"; filename="{nome}"\r\n'
                      f"Content-Type: {mime}\r\n\r\n".encode() + Path(arquivo).read_bytes() + b"\r\n")
        partes.append(f"--{fronteira}--\r\n".encode())
        req = urllib.request.Request(base + "/media", data=b"".join(partes),
                                     headers={"Content-Type": f"multipart/form-data; boundary={fronteira}",
                                              "x-api-key": _chave(cfg)})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read() or b"{}")
    return ((d.get("data") or {}).get("key") or {}).get("id") or d.get("id") or "enviado"


def enviar(cfg: dict, banco, pessoa: dict, texto: str, arquivo: str | None = None) -> dict:
    """Grava em `saida` e, se o envio está ligado, faz 1 tentativa. Devolve a linha da saída."""
    jid = pessoa.get("jid") or f"{pessoa['telefone']}@s.whatsapp.net"
    sid = banco.exec("INSERT INTO saida (pessoa_id, jid, texto, arquivo, enviado, em) VALUES (?,?,?,?,0,?)",
                     pessoa["id"], jid, texto, arquivo, agora_txt())
    banco.exec("INSERT INTO mensagens (pessoa_id, telefone, quem, texto, tipo, arquivo, processada, em) "
               "VALUES (?,?,?,?,?,?,1,?)", pessoa["id"], pessoa["telefone"], "sistema", texto,
               "DOCUMENT" if arquivo else "TEXT", arquivo, agora_txt())
    if not chaves(cfg).get("envio_ligado"):
        banco.log(pessoa["id"], "seco", f"saida {sid}: {texto[:200]}")
        return banco.um("SELECT * FROM saida WHERE id=?", sid)
    try:
        kid = _post(cfg, jid, texto, arquivo)
        banco.exec("UPDATE saida SET enviado=1, key_id=? WHERE id=?", kid, sid)
    except Exception as e:  # noqa: BLE001 — 1 tentativa; erro vai para a equipe
        banco.exec("UPDATE saida SET erro=? WHERE id=?", repr(e)[:500], sid)
        banco.log(pessoa["id"], "erro_envio", repr(e))
        banco.atualizar(pessoa["id"], status="humano", motivo_humano=f"erro técnico no envio: {repr(e)[:150]}")
    return banco.um("SELECT * FROM saida WHERE id=?", sid)
