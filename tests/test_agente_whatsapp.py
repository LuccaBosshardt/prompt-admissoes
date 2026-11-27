"""Aba 'Agente e WhatsApp': sessão/QR/webhook pelo WA-AKG (simulado), chaves, ajustes e teste do agente."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from admissoes import agente, config, gateway, motor, painel
from conftest import em


class FakeWA:
    def __init__(self, status="SCAN_QR"):
        self.sessao, self.status, self.webhooks, self.chamadas = False, status, [], []

    def __call__(self, cfg, metodo, caminho, corpo=None, timeout=15):
        self.chamadas.append((metodo, caminho, corpo))
        if caminho == "/api/sessions/prompt" and metodo == "GET":
            return (200, {"data": {"status": self.status, "me": {"id": "5511900000000:1@s.whatsapp.net"}}}) if self.sessao else (404, {})
        if caminho == "/api/sessions" and metodo == "POST":
            self.sessao = True
            return 200, {"status": True}
        if caminho.endswith("/bot-config") and metodo == "POST":
            self.bot = corpo
            return 200, {}
        if caminho.endswith("/bot-config"):
            return 200, {"data": {"enabled": (getattr(self, "bot", {}) or {}).get("enabled", True), "autoRead": False, "alwaysOnline": False}}
        if caminho.endswith("/start"):
            self.iniciada = True
            self.status = "SCAN_QR"
            return 200, {}
        if caminho == "/api/webhooks/prompt" and metodo == "GET":
            return 200, {"data": self.webhooks}
        if caminho == "/api/webhooks/prompt" and metodo == "POST":
            self.webhooks.append(corpo)
            return 201, {}
        if caminho.endswith("/qr"):
            return 200, {"base64": "data:image/png;base64,AAAA"}
        if caminho.endswith("/restart"):
            return 200, {}
        if "logout" in caminho:
            pytest.fail("chamou logout")
        return 404, {}


@pytest.fixture
def wa(monkeypatch):
    f = FakeWA()
    monkeypatch.setattr(gateway, "_api", f)
    return f


def test_conectar_cria_sessao_desliga_bot_e_liga_webhook_uma_vez(cfg, wa):
    feito = gateway.conectar(cfg)
    assert "sessão criada" in feito and "webhook ligado à ponte" in feito
    assert "bot embutido desligado (conferido)" in feito and "sessão iniciada" in feito and wa.iniciada
    bot = next(c for c in wa.chamadas if c[1].endswith("/bot-config"))[2]
    assert bot["enabled"] is False and bot["autoRead"] is False and bot["alwaysOnline"] is False
    wh = wa.webhooks[0]
    assert wh["url"] == "http://127.0.0.1:4134/webhook" and len(wh["secret"]) >= 32
    assert (Path(cfg["dados_dir"]) / "webhook-segredo.txt").read_text().strip() == wh["secret"]
    gateway.conectar(cfg)
    assert len(wa.webhooks) == 1  # idempotente
    e = gateway.estado(cfg)
    assert e["sessao"] and e["webhook"] and e["numero"] == "5511900000000"
    assert gateway.qr(cfg)["base64"].startswith("data:image/png")


def test_gateway_fora_do_ar(cfg, monkeypatch):
    monkeypatch.setattr(gateway, "_api", lambda *a, **k: (_ for _ in ()).throw(gateway.GatewayErro("WA-AKG fora do ar")))
    e = gateway.estado(cfg)
    assert e["gateway"] is False and "fora do ar" in e["erro"]


def _c():
    return TestClient(painel.app), f"/p/{painel.SEGREDO}"


def test_tela_e_qr_json(wa):
    c, base = _c()
    assert c.get(base + "/agente").status_code == 200
    r = c.post(base + "/agente/conectar", follow_redirects=True)
    assert r.status_code == 200 and "sessão criada" in r.text
    d = c.get(base + "/agente/qr.json").json()
    assert d["base64"].startswith("data:image") and d["rotulo"] == "esperando a leitura do QR"


def test_ligar_envio_exige_conexao_e_zera_cobrancas(wa):
    c, base = _c()
    painel.CFG["dados_dir"]  # mesmo diretório do painel
    config.salvar_json(painel.CFG, "chaves.json", {"envio_ligado": False, "cobranca_ligada": True})
    r = c.post(base + "/agente/chaves", data={"envio": "1"}, follow_redirects=True)
    assert "Conecte o WhatsApp" in r.text and config.chaves(painel.CFG)["envio_ligado"] is False
    wa.sessao, wa.status = True, "CONNECTED"
    cid = painel.BANCO.um("SELECT id FROM clientes WHERE nome='Aerovip'")["id"]
    p = motor.cadastrar(painel.BANCO, painel.CFG, "Seco Antigo", "11966665555", cid, em("2026-11-20T10:00:00"))
    painel.BANCO.atualizar(p["id"], cobrancas=2)
    r = c.post(base + "/agente/chaves", data={"envio": "1"}, follow_redirects=True)
    assert "Envio LIGADO" in r.text and config.chaves(painel.CFG)["envio_ligado"] is True
    q = painel.BANCO.pessoa(p["id"])
    assert q["cobrancas"] == 0 and q["pedido_em"] > "2026-11-25"
    c.post(base + "/agente/chaves", data={"envio": "0"})
    assert config.chaves(painel.CFG)["envio_ligado"] is False


def test_ajustes_do_agente_valem_no_texto_e_no_prompt(wa):
    c, base = _c()
    c.post(base + "/agente/ajustes", data={"assinatura": "DP Prompt   Serviços", "instrucoes_extra": "Diga que o uniforme sai no 1º dia."})
    aj = config.ajustes(painel.CFG)
    assert aj["assinatura"] == "DP Prompt Serviços"
    cl = painel.BANCO.cliente(painel.BANCO.um("SELECT id FROM clientes WHERE nome='Aerovip'")["id"])
    assert "DP Prompt Serviços" in motor.texto_inicial(painel.CFG, cl, "Ana", "Limpeza")
    pr = agente.montar_prompt({"x": 1}, ajustes=aj)
    assert "uniforme sai no 1º dia" in pr and pr.index("uniforme") < pr.index("<<<EVENTO")
    r = c.post(base + "/agente/ajustes", data={"assinatura": "  ", "instrucoes_extra": ""}, follow_redirects=True)
    assert "não pode ficar vazia" in r.text
    config.salvar_json(painel.CFG, "ajustes.json", {"assinatura": painel.CFG["assinatura"], "instrucoes_extra": ""})


def test_testar_agente_nao_grava_nem_envia(wa, monkeypatch):
    c, base = _c()
    monkeypatch.setattr(painel, "_ULTIMO_TESTE", 0.0)
    monkeypatch.setattr(agente, "responder", lambda ev, cfg: {"mensagens": ["Quem fala de salário é a equipe."], "precisa_humano": True})
    antes = (painel.BANCO.um("SELECT COUNT(*) n FROM saida")["n"], painel.BANCO.um("SELECT COUNT(*) n FROM pessoas")["n"])
    r = c.post(base + "/agente/testar", data={"texto": "quanto vou ganhar?", "etapa": "documentos"})
    assert r.status_code == 200 and "Quem fala de salário é a equipe." in r.text
    assert (painel.BANCO.um("SELECT COUNT(*) n FROM saida")["n"], painel.BANCO.um("SELECT COUNT(*) n FROM pessoas")["n"]) == antes


def test_bot_que_nao_desliga_vira_aviso(cfg, monkeypatch):
    f = FakeWA()
    def fake(cfg_, metodo, caminho, corpo=None, timeout=15):
        if caminho.endswith("/bot-config") and metodo == "GET":
            return 200, {"data": {"enabled": True}}
        return f(cfg_, metodo, caminho, corpo, timeout)
    monkeypatch.setattr(gateway, "_api", fake)
    assert any("NÃO desligou" in x for x in gateway.conectar(cfg))


def test_post_de_outra_origem_e_recusado(wa):
    c, base = _c()
    r = c.post(base + "/agente/chaves", data={"envio": "0"}, headers={"origin": "https://malicioso.exemplo"})
    assert r.status_code == 403


def test_botao_nao_apaga_outras_chaves(wa):
    c, base = _c()
    config.salvar_json(painel.CFG, "chaves.json", {"envio_ligado": False, "cobranca_ligada": True, "cobranca_em_seco": True})
    c.post(base + "/agente/chaves", data={"cobranca": "0"})
    d = json.loads((Path(painel.CFG["dados_dir"]) / "chaves.json").read_text())
    assert d["cobranca_em_seco"] is True and d["cobranca_ligada"] is False


def test_tela_whatsapp(monkeypatch):
    chamadas = []

    def fake(cfg, metodo, caminho, corpo=None, timeout=15):
        chamadas.append((metodo, caminho, corpo))
        if caminho == "/api/sessions/prompt":
            return 200, {"data": {"status": "CONNECTED"}}
        if caminho.startswith("/api/webhooks"):
            return 200, {"data": []}
        if caminho == "/api/chat/prompt":
            return 200, {"data": [{"jid": "5511955551111@s.whatsapp.net", "name": "Fulano", "lastMessage": {"content": "oi", "timestamp": "2026-11-27T10:00:00Z", "type": "TEXT"}},
                                  {"jid": "status@broadcast", "name": "x", "lastMessage": {}},
                                  {"jid": "1203@g.us", "name": "Grupo", "lastMessage": {"content": "", "timestamp": "2026-11-26T10:00:00Z", "type": "IMAGE"}}]}
        if caminho.startswith("/api/chat/prompt/"):
            return 200, {"data": [{"keyId": "B", "fromMe": True, "type": "TEXT", "content": "resposta", "timestamp": "2026-11-27T10:01:00Z", "status": "READ"},
                                  {"keyId": "A", "fromMe": False, "type": "IMAGE", "content": "", "mediaUrl": "/api/media/prompt-A.jpeg", "timestamp": "2026-11-27T10:00:00Z"}]}
        if caminho.endswith("/send"):
            return 200, {"data": {"key": {"id": "NOVO1"}}}
        return 404, {}
    monkeypatch.setattr(gateway, "_api", fake)
    c, base = _c()
    cid = painel.BANCO.um("SELECT id FROM clientes WHERE nome='Aerovip'")["id"]
    if not motor.achar_por_tel(painel.BANCO, "5511955551111"):
        motor.cadastrar(painel.BANCO, painel.CFG, "Fulano Tal", "11955551111", cid, em("2026-11-26T10:00:00"))
    assert c.get(base + "/whatsapp").status_code == 200
    conv = c.get(base + "/whatsapp/conversas.json").json()["conversas"]
    assert [x["nome"] for x in conv] == ["Fulano", "Grupo"] and conv[0]["pessoa"]["etapa"] and conv[1]["grupo"]
    msgs = c.get(base + "/whatsapp/mensagens.json", params={"jid": "5511955551111@s.whatsapp.net"}).json()["mensagens"]
    assert [m["id"] for m in msgs] == ["A", "B"] and msgs[0]["midia"] == "prompt-A.jpeg"
    assert c.get(base + "/whatsapp/midia/..%2Fsegredo").status_code == 404
    assert c.post(base + "/whatsapp/enviar", data={"jid": "x", "texto": "oi"}).status_code == 400
    assert c.post(base + "/whatsapp/enviar", data={"jid": "5511955551111@s.whatsapp.net", "texto": "  "}).status_code == 400
    r = c.post(base + "/whatsapp/enviar", data={"jid": "5511955551111@s.whatsapp.net", "texto": "Olá, aqui é a equipe"})
    assert r.json()["id"] == "NOVO1"
    assert [x for x in chamadas if x[1].endswith("/send")][0][2] == {"message": {"text": "Olá, aqui é a equipe"}}


def test_nomear_e_ligar_contato_lid(monkeypatch):
    def fake(cfg, metodo, caminho, corpo=None, timeout=15):
        if caminho == "/api/chat/prompt":
            return 200, {"data": [{"jid": "1999888777@lid", "name": None, "lastMessage": {"content": "oi", "timestamp": "2026-11-27T10:00:00Z"}}]}
        return 200, {"data": {"status": "CONNECTED"}}
    monkeypatch.setattr(gateway, "_api", fake)
    c, base = _c()
    conv = c.get(base + "/whatsapp/conversas.json").json()["conversas"][0]
    assert conv["sem_nome"] and conv["pessoa"] is None
    cid = painel.BANCO.um("SELECT id FROM clientes WHERE nome='Aerovip'")["id"]
    p = motor.cadastrar(painel.BANCO, painel.CFG, "Lid Pessoa", "11944443333", cid, em("2026-11-26T10:00:00"))
    r = c.post(base + "/whatsapp/contato", data={"jid": "1999888777@lid", "apelido": "", "pessoa_id": str(p["id"])}).json()
    assert r["apelido"] == "Lid Pessoa"
    conv = c.get(base + "/whatsapp/conversas.json").json()["conversas"][0]
    assert conv["nome"] == "Lid Pessoa" and conv["pessoa"]["id"] == p["id"] and not conv["sem_nome"]
