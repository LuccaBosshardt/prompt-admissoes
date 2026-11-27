"""Simula uma mensagem do WA-AKG para a ponte (webhook assinado). Uso:
  .venv/bin/python simular.py <telefone> "<texto>" [caminho_da_foto]
A foto é copiada para o media_dir do WA-AKG, como o gateway faria. Nada sai para o WhatsApp.
"""
import hashlib, hmac, json, shutil, sys, urllib.request, uuid
from pathlib import Path
from admissoes import config

cfg = config.carregar()
tel, texto = sys.argv[1], sys.argv[2]
foto = sys.argv[3] if len(sys.argv) > 3 else None
data = {"key": {"id": "SIM-" + uuid.uuid4().hex[:12], "fromMe": False, "remoteJid": f"{tel}@s.whatsapp.net"},
        "from": f"{tel}@s.whatsapp.net", "pushName": "Ensaio", "content": texto, "type": "TEXT", "chatType": "PERSONAL"}
if foto:
    md = Path(cfg["whatsapp"]["media_dir"]); md.mkdir(parents=True, exist_ok=True)
    nome = f"sim-{uuid.uuid4().hex[:8]}{Path(foto).suffix}"
    shutil.copy(foto, md / nome)
    data.update(type="IMAGE", caption=texto, fileUrl=f"/media/{nome}")
corpo = json.dumps({"event": "message.received", "data": data}).encode()
seg = (Path(cfg["dados_dir"]) / "webhook-segredo.txt").read_text().strip()
req = urllib.request.Request(f"http://{cfg['ponte']['host']}:{cfg['ponte']['porta']}/webhook", data=corpo,
                             headers={"Content-Type": "application/json",
                                      "X-Webhook-Signature": "sha256=" + hmac.new(seg.encode(), corpo, hashlib.sha256).hexdigest()})
print(urllib.request.urlopen(req, timeout=10).read().decode())
