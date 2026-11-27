import hashlib
import hmac
import json
from pathlib import Path

from fastapi.testclient import TestClient

from admissoes import painel, ponte


def test_painel_404_sem_segredo_e_200_com_segredo():
    c = TestClient(painel.app)
    seg = painel.SEGREDO
    assert len(seg) >= 32
    assert c.get("/").status_code == 404
    assert c.get("/pessoas").status_code == 404
    assert c.get("/p/errado/pessoas").status_code == 404
    for rota in ("/", "/pessoas", "/aprovacoes", "/humano", "/vagas", "/estoque", "/clientes", "/saida"):
        r = c.get(f"/p/{seg}{rota}")
        assert r.status_code == 200, rota
        assert r.headers["x-robots-tag"].startswith("noindex")


def test_painel_cadastra_e_abre_ficha():
    c = TestClient(painel.app)
    seg = painel.SEGREDO
    cid = painel.BANCO.um("SELECT id FROM clientes WHERE nome='Aerovip'")["id"]
    r = c.post(f"/p/{seg}/pessoas", data={"nome": "Ana Teste", "telefone": "11 97777-6666", "cliente_id": cid},
               follow_redirects=False)
    assert r.status_code == 303
    r = c.get(r.headers["location"])
    assert r.status_code == 200 and "Ana Teste" in r.text and "Parabéns" in r.text
    r = c.post(f"/p/{seg}/vagas", data={"cliente_id": cid, "funcao": "Porteiro", "quantidade": 2}, follow_redirects=True)
    assert "Porteiro" in r.text


def _assinar(seg: str, corpo: bytes) -> str:
    return "sha256=" + hmac.new(seg.encode(), corpo, hashlib.sha256).hexdigest()


def test_webhook_hmac():
    c = TestClient(ponte.app)
    p = Path(ponte.CFG["dados_dir"]) / "webhook-segredo.txt"
    if p.exists():
        p.unlink()
    corpo = json.dumps({"event": "message.received", "data": {}}).encode()
    assert c.post("/webhook", content=corpo).status_code == 503
    p.write_text("segredo-de-teste-com-mais-de-32-caracteres")
    assert c.post("/webhook", content=corpo, headers={"X-Webhook-Signature": "sha256=errado"}).status_code == 401
    r = c.post("/webhook", content=corpo, headers={"X-Webhook-Signature": _assinar(p.read_text(), corpo)})
    assert r.status_code == 200
    assert c.get("/healthz").json()["ok"] is True


def test_webhook_mensagem_real_e_repetida():
    c = TestClient(ponte.app)
    p = Path(ponte.CFG["dados_dir"]) / "webhook-segredo.txt"
    p.write_text("segredo-de-teste-com-mais-de-32-caracteres")
    ev = {"event": "message.received", "data": {"key": {"id": "WAKEY1", "fromMe": False, "remoteJid": "5511955554444@s.whatsapp.net"},
                                                "from": "5511955554444@s.whatsapp.net", "pushName": "Zé", "content": "oi", "type": "TEXT",
                                                "chatType": "PERSONAL"}}
    corpo = json.dumps(ev).encode()
    h = {"X-Webhook-Signature": _assinar(p.read_text(), corpo)}
    assert c.post("/webhook", content=corpo, headers=h).json()["resultado"] == "sem cadastro"
    assert c.post("/webhook", content=corpo, headers=h).json()["resultado"] == "repetido"
    grupo = json.dumps({"event": "message.received", "data": {"isGroup": True, "key": {"id": "G1"}}}).encode()
    assert "ignorado" in c.post("/webhook", content=grupo, headers={"X-Webhook-Signature": _assinar(p.read_text(), grupo)}).json()["resultado"]
