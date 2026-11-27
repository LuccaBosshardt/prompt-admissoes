"""Correções da tentativa 2 (achados do crítico)."""
import json
from pathlib import Path

import pytest

from admissoes import config, fluxo, motor, ponte, whatsapp
from conftest import em

QUI = "2026-11-26T10:00:00"


def fake(res):
    return lambda ev, cfg: res


def nova(b, cfg, aero, agora=QUI, **kw):
    return motor.cadastrar(b, cfg, "Maria Souza", "11 98888-7777", aero, em(agora), funcao="Limpeza", **kw)


def foto(cfg, nome):
    p = Path(cfg["dados_dir"]) / "arquivos" / nome
    p.write_bytes(b"x")
    return str(p)


def test_chave_de_envio_relida_na_hora(b, cfg, aerovip, monkeypatch):
    p = nova(b, cfg, aerovip)
    envios = []
    monkeypatch.setattr(whatsapp, "_post", lambda *a: envios.append(1) or "K")
    cp = Path(cfg["dados_dir"]) / "chaves.json"
    cp.write_text(json.dumps({"envio_ligado": True}))
    whatsapp.enviar(cfg, b, b.pessoa(p["id"]), "a")
    cp.write_text(json.dumps({"envio_ligado": False}))
    whatsapp.enviar(cfg, b, b.pessoa(p["id"]), "b")
    assert len(envios) == 1


def test_humano_pelo_celular_pausa_o_robo(b, cfg, aerovip, monkeypatch):
    p = nova(b, cfg, aerovip)
    monkeypatch.setattr(ponte.threading, "Timer", lambda s, f, args: type("T", (), {"start": lambda self: f(*args)})())
    ev = {"event": "message.sent", "data": {"key": {"id": "HUM1", "fromMe": True, "remoteJid": "5511988887777@s.whatsapp.net"},
          "from": "5511900000000@s.whatsapp.net", "receiver": "5511988887777@s.whatsapp.net", "content": "oi, aqui é a Gabi",
          "chatType": "PERSONAL"}}
    ponte.receber(ev, cfg, b)
    assert b.pessoa(p["id"])["pausa_ate"]
    assert b.um("SELECT quem FROM mensagens WHERE key_id='HUM1'")["quem"] == "humano"


def test_vespera_de_exame_na_segunda_sai_no_sabado(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    b.atualizar(p["id"], etapa="guia", exame_data="2026-11-30", aguardando="exame")  # segunda
    assert motor.tick(b, cfg, em("2026-11-27T10:00:00")) == []              # sexta: ainda há sábado
    assert motor.tick(b, cfg, em("2026-11-28T09:30:00")) == [f"vespera {p['id']}"]  # sábado
    assert "segunda, 30/11" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]


def test_aprovar_exame_com_data_vencida_oferece_novas_datas(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    b.atualizar(p["id"], etapa="exame")
    aid = motor.criar_acao(b, p["id"], "exame", {"data": "2026-11-26"}, "x", em(QUI))
    r = motor.aprovar(b, cfg, aid, em("2026-11-26T15:00:00"))
    assert "já passou" in r and b.acao(aid)["status"] == "recusada"
    assert b.pessoa(p["id"])["etapa"] == "exame" and "não deu certo" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]


def test_pos_exame_nao_pede_guia_que_ja_chegou(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    b.atualizar(p["id"], etapa="guia", exame_data="2026-11-27", aguardando="exame")
    b.marcar(p["id"], vespera=True)
    b.exec("INSERT INTO itens (pessoa_id, item, grupo, status) VALUES (?,?,?,?)", p["id"], motor.ITEM_GUIA, "exame", "recebido")
    n = b.um("SELECT COUNT(*) n FROM saida")["n"]
    motor.tick(b, cfg, em("2026-11-27T14:30:00"))
    assert b.um("SELECT COUNT(*) n FROM saida")["n"] == n


def test_resposta_do_hermes_descartada_quando_etapa_muda(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    for i in b.itens(p["id"]):
        if i["item"] != "pix":
            b.exec("UPDATE itens SET status='aprovado' WHERE id=?", i["id"])
    fluxo.receber(b, cfg, p["telefone"], p["jid"], "", "meu pix é 11988887777", "TEXT", None, "k", em(QUI))
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["Ainda falta o PIX!"], "dados": {"pix": "11988887777"}}))
    textos = [s["texto"] for s in b.todos("SELECT texto FROM saida")]
    assert b.pessoa(p["id"])["etapa"] == "exame"
    assert "Ainda falta o PIX!" not in textos and "exame" in textos[-1]


def test_texto_inicial_invalido_nao_grava_pessoa(b, cfg, aerovip):
    b.exec("UPDATE clientes SET textos=? WHERE id=?", json.dumps({"inicial": "Oi {nom}"}), aerovip)
    with pytest.raises(ValueError, match="campo inválido"):
        nova(b, cfg, aerovip)
    assert not b.um("SELECT id FROM pessoas")


def test_pessoa_admitida_que_escreve_vai_para_equipe(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    b.atualizar(p["id"], status="admitida", etapa="concluida")
    fluxo.receber(b, cfg, p["telefone"], p["jid"], "", "qual o horário?", "TEXT", None, "k9", em(QUI))
    assert b.pessoa(p["id"])["msg_nova"] == 1
    assert fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["x"]})) == "nada"


def test_frente_e_verso_guardam_as_duas_fotos(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    for n, nome in enumerate(["frente.jpg", "verso.jpg"]):
        fluxo.receber(b, cfg, p["telefone"], p["jid"], "", "", "IMAGE", foto(cfg, nome), f"f{n}", em(QUI))
    novas = b.todos("SELECT * FROM mensagens WHERE quem='pessoa'")
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["ok"], "documentos": [
        {"msg_id": novas[0]["id"], "item": "RG ou CNH"}, {"msg_id": novas[1]["id"], "item": "RG ou CNH"}]}))
    it = next(i for i in b.itens(p["id"]) if i["item"] == "RG ou CNH")
    assert it["arquivo"].endswith("verso.jpg") and json.loads(it["arquivos_extra"])[0].endswith("frente.jpg")
    assert b.um("SELECT COUNT(*) n FROM acoes")["n"] == 1


def test_equipe_usa_anexo_da_conversa_como_item(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    fluxo.receber(b, cfg, p["telefone"], p["jid"], "", "", "IMAGE", foto(cfg, "solta.jpg"), "s1", em(QUI))
    mid = b.um("SELECT id FROM mensagens WHERE quem='pessoa'")["id"]
    cpf = next(i for i in b.itens(p["id"]) if i["item"] == "CPF")
    assert motor.usar_anexo(b, cfg, p["id"], mid, cpf["id"], em(QUI)) == "ok"
    assert b.item(cpf["id"])["status"] == "aprovado" and b.item(cpf["id"])["arquivo"].endswith("solta.jpg")


def test_declaracao_escolar_e_opcional_e_filhos_a_menos_somem(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    fluxo.receber(b, cfg, p["telefone"], p["jid"], "", "2 filhos", "TEXT", None, "a", em(QUI))
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["ok"], "dados": {"filhos_menores_14": 2}}))
    fluxo.receber(b, cfg, p["telefone"], p["jid"], "", "ops, é 1 só", "TEXT", None, "b", em(QUI))
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["ok"], "dados": {"filhos_menores_14": 1}}))
    filhos = [i for i in b.itens(p["id"]) if i["grupo"] == "filho_doc"]
    assert len(filhos) == 4 and all(i["item"].startswith("Filho 1") for i in filhos)
    for i in b.itens(p["id"]):
        if not motor.opcional(i):
            b.exec("UPDATE itens SET status='aprovado' WHERE id=?", i["id"])
    motor.checar_documentos(b, cfg, p["id"], em(QUI))
    assert b.pessoa(p["id"])["etapa"] == "exame"


def test_vaga_fechada_nao_aceita_cadastro(b, cfg, aerovip):
    vid = b.exec("INSERT INTO vagas (cliente_id, funcao, quantidade, status) VALUES (?,?,?,?)", aerovip, "X", 1, "fechada")
    with pytest.raises(ValueError, match="não está aberta"):
        nova(b, cfg, aerovip, vaga_id=vid)


def test_audio_chega_ao_hermes_como_aviso(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    fluxo.receber(b, cfg, p["telefone"], p["jid"], "", "", "AUDIO", None, "au", em(QUI))
    novas = b.todos("SELECT * FROM mensagens WHERE quem='pessoa'")
    ev = fluxo.evento(b, cfg, b.pessoa(p["id"]), novas, em(QUI))
    assert ev["novas_mensagens"][0]["texto"].startswith("[ÁUDIO")


def test_erro_no_turno_vai_para_humano(b, cfg, aerovip, monkeypatch):
    p = nova(b, cfg, aerovip)
    monkeypatch.setattr(ponte, "BANCO", b)
    monkeypatch.setattr(ponte.fluxo, "processar", lambda *a: (_ for _ in ()).throw(RuntimeError("quebrou")))
    ponte._turno(p["id"])
    assert b.pessoa(p["id"])["status"] == "humano"


def test_tentativa3_cobranca_nao_conta_em_modo_seco(b, cfg, aerovip):
    (Path(cfg["dados_dir"]) / "chaves.json").write_text(json.dumps({"envio_ligado": False}))
    p = nova(b, cfg, aerovip, agora="2026-11-23T10:00:00")
    assert motor.tick(b, cfg, em("2026-11-25T10:00:00")) == [] and b.pessoa(p["id"])["cobrancas"] == 0


def test_tentativa3_proposta_nova_substitui_e_acao_de_outra_etapa_nao_aprova(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    b.atualizar(p["id"], etapa="exame")
    a1 = motor.criar_acao(b, p["id"], "exame", {"data": "2026-11-27"}, "1", em(QUI))
    a2 = motor.criar_acao(b, p["id"], "exame", {"data": "2026-11-30"}, "2", em(QUI))
    assert b.acao(a1)["status"] == "substituida"
    assert motor.aprovar(b, cfg, a2, em(QUI)) == "ok" and b.pessoa(p["id"])["etapa"] == "guia"
    velha = b.exec("INSERT INTO acoes (pessoa_id, tipo, payload, status) VALUES (?, 'inicio', '{\"data\": \"2026-12-07\"}', 'pendente')", p["id"])
    assert "outra etapa" in motor.aprovar(b, cfg, velha, em(QUI)) and b.pessoa(p["id"])["status"] == "ativo"


def test_tentativa3_aprovar_guia_pela_ficha_avanca(b, cfg, aerovip):
    from fastapi.testclient import TestClient
    from admissoes import painel
    cid = painel.BANCO.um("SELECT id FROM clientes WHERE nome='Aerovip'")["id"]
    p = motor.cadastrar(painel.BANCO, painel.CFG, "Guia Teste", "11977770000", cid, em(QUI))
    painel.BANCO.atualizar(p["id"], etapa="guia", exame_data="2026-11-27")
    painel.BANCO.exec("INSERT INTO itens (pessoa_id, item, grupo) VALUES (?,?,?)", p["id"], motor.ITEM_GUIA, "exame")
    it = painel.BANCO.um("SELECT id FROM itens WHERE pessoa_id=? AND item=?", p["id"], motor.ITEM_GUIA)
    c = TestClient(painel.app)
    r = c.post(f"/p/{painel.SEGREDO}/pessoas/{p['id']}/acao", data={"op": "item_ok", "item_id": it["id"]},
               files={"arquivo": ("guia.jpg", b"x", "image/jpeg")}, follow_redirects=False)
    assert r.status_code == 303 and painel.BANCO.pessoa(p["id"])["etapa"] == "contrato"


def test_tentativa3_documento_atrasado_aprova_em_qualquer_etapa(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    b.atualizar(p["id"], etapa="guia")
    it = next(i for i in b.itens(p["id"]) if i["item"] == "RG ou CNH")
    b.exec("UPDATE itens SET status='recebido' WHERE id=?", it["id"])
    aid = motor.criar_acao(b, p["id"], "documento", {"item_id": it["id"]}, "2ª foto do RG", em(QUI))
    assert motor.aprovar(b, cfg, aid, em(QUI)) == "ok" and b.item(it["id"])["status"] == "aprovado"
    assert b.pessoa(p["id"])["etapa"] == "guia"


def test_contrato_digital_por_link_e_assinei(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    b.atualizar(p["id"], etapa="contrato", aguardando="equipe")
    aid = motor.criar_acao(b, p["id"], "enviar_contrato", {}, "x", em(QUI))
    assert motor.aprovar(b, cfg, aid, em(QUI), "remoto") == "ok"
    assert "link" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]
    fluxo.receber(b, cfg, p["telefone"], p["jid"], "", "assinei já", "TEXT", None, "ct", em(QUI))
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["Obrigado!"], "contrato_assinado": True}))
    a = b.um("SELECT * FROM acoes WHERE tipo='contrato' AND status='pendente'")
    assert a and "contrato digital" in a["resumo"]
    assert motor.aprovar(b, cfg, a["id"], em(QUI)) == "ok" and b.pessoa(p["id"])["etapa"] == "inicio"


def test_duvida_para_equipe_nao_para_o_robo(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    fluxo.receber(b, cfg, p["telefone"], p["jid"], "", "quanto é o salário?", "TEXT", None, "sal", em(QUI))
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["Vou confirmar com a equipe."], "duvida_para_equipe": "salário"}))
    q = b.pessoa(p["id"])
    assert q["status"] == "ativo" and q["msg_nova"] == 1


def test_evento_traz_info_da_vaga_e_horario_da_equipe(b, cfg, aerovip):
    from admissoes import regras
    b.exec("UPDATE clientes SET info_vaga=? WHERE id=?", "Local: Av. X, 300. Levar marmita.", aerovip)
    p = nova(b, cfg, aerovip)
    ev = fluxo.evento(b, cfg, b.pessoa(p["id"]), [], em(QUI))
    assert ev["info_da_vaga"].startswith("Local: Av. X") and ev["equipe_em_horario"] is True
    assert regras.equipe_em_horario(cfg, em("2026-11-27T16:10:00")) is False   # sexta depois das 16h
    assert regras.equipe_em_horario(cfg, em("2026-11-26T16:10:00")) is True    # quinta antes das 16h30
