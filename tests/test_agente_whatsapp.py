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
        if caminho.endswith("/bot-config"):
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
    monkeypatch.setattr(agente, "responder", lambda ev, cfg: {"mensagens": ["Quem fala de salário é a equipe."], "precisa_humano": True})
    antes = (painel.BANCO.um("SELECT COUNT(*) n FROM saida")["n"], painel.BANCO.um("SELECT COUNT(*) n FROM pessoas")["n"])
    r = c.post(base + "/agente/testar", data={"texto": "quanto vou ganhar?", "etapa": "documentos"})
    assert r.status_code == 200 and "Quem fala de salário é a equipe." in r.text
    assert (painel.BANCO.um("SELECT COUNT(*) n FROM saida")["n"], painel.BANCO.um("SELECT COUNT(*) n FROM pessoas")["n"]) == antes
