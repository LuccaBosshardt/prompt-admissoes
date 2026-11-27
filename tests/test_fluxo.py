from datetime import timedelta
from pathlib import Path

import pytest

from admissoes import agente, clinicas, fluxo, motor, regras, whatsapp
from conftest import em

QUI = "2026-11-26T10:00:00"   # quinta-feira


def nova(b, cfg, aerovip, agora=QUI, **kw):
    return motor.cadastrar(b, cfg, "Maria da Silva", "11 98765-4321", aerovip, em(agora), funcao="Auxiliar de Lavanderia", **kw)


def chegar(b, cfg, p, texto="", arquivo=None, tipo="TEXT", kid=None, agora=QUI):
    return fluxo.receber(b, cfg, p["telefone"], p["jid"], "Maria", texto, tipo, arquivo, kid, em(agora))


def fake(res):
    return lambda ev, cfg: res


def foto(tmp, nome="rg.jpg"):
    p = Path(tmp) / "arquivos" / nome
    p.write_bytes(b"\xff\xd8 foto")
    return str(p)


def aprovar_tudo(b, cfg, p, agora=QUI):
    """Equipe aprova direto todos os itens de documentos/infos (atalho do teste)."""
    for i in b.itens(p["id"]):
        b.exec("UPDATE itens SET status='aprovado' WHERE id=?", i["id"])
    motor.checar_documentos(b, cfg, p["id"], em(agora))


# ---------- cadastro e checklist ----------
def test_cadastro_manda_mensagem_inicial_em_modo_seco(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    s = b.todos("SELECT * FROM saida")
    assert len(s) == 1 and s[0]["enviado"] == 0
    assert "Parabéns" in s[0]["texto"] and "Auxiliar de Lavanderia" in s[0]["texto"] and "Maria" in s[0]["texto"]
    assert "Departamento Pessoal – Prompt Serviços" in s[0]["texto"] and "Gabriela" not in s[0]["texto"]
    itens = {i["item"] for i in b.itens(p["id"])}
    assert {"CPF", "PIS", "pix", "vt_ida", "filhos_menores_14"} <= itens
    assert p["etapa"] == "documentos" and p["aguardando"] == "pessoa"


def test_checklist_segue_regras_do_cliente(b, cfg):
    import json
    cid = b.exec("INSERT INTO clientes (nome, documentos, infos) VALUES (?,?,?)", "Condomínio X",
                 json.dumps(["RG", "Certificado de vigilante"]), json.dumps([{"chave": "pix", "rotulo": "PIX"}]))
    p = motor.cadastrar(b, cfg, "João", "11912345678", cid, em(QUI))
    assert {i["item"] for i in b.itens(p["id"])} == {"RG", "Certificado de vigilante", "pix"}


def test_telefone_invalido(b, cfg, aerovip):
    with pytest.raises(ValueError):
        motor.cadastrar(b, cfg, "X", "1234", aerovip, em(QUI))


def test_telefone_com_e_sem_nove(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    assert motor.achar_por_tel(b, "551187654321")["id"] == p["id"]


# ---------- dados, filhos e VT ----------
def test_filhos_menores_criam_documentos_de_cada_filho(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    chegar(b, cfg, p, "tenho 2 filhos de 5 e 8 anos")
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["Anotado!"], "dados": {"filhos_menores_14": 2}}))
    filhos = [i["item"] for i in b.itens(p["id"]) if i["grupo"] == "filho_doc"]
    assert len(filhos) == 8 and "Filho 2 – Carteira de vacinação" in filhos


def test_vale_transporte_por_conducao(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    chegar(b, cfg, p, "ida onibus 5,30 e metro 5,40")
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["ok"], "dados": {
        "vt_ida": [{"transporte": "Ônibus", "valor": "5,30"}, {"transporte": "Metrô", "valor": "5,40"}],
        "chave_estranha": 1}}))
    it = {i["item"]: i for i in b.itens(p["id"])}
    assert it["vt_ida"]["status"] == "aprovado" and it["vt_ida"]["valor"] == "Ônibus R$ 5,30 + Metrô R$ 5,40"
    assert it["vt_volta"]["status"] == "pendente"


def test_chave_desconhecida_e_dado_fora_da_lista_sao_ignorados(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    chegar(b, cfg, p, "oi")
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["oi"], "dados": {"salario": "5000"}, "apagar_tudo": True}))
    assert b.um("SELECT * FROM log WHERE tipo='chave_ignorada'")
    assert "salario" not in {i["item"] for i in b.itens(p["id"])}


# ---------- documentos e aprovação ----------
def test_documento_recebido_vai_para_aprovacao_e_aprovado_avanca(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    chegar(b, cfg, p, "meu cpf", arquivo=foto(cfg["dados_dir"]), tipo="IMAGE")
    mid = b.um("SELECT id FROM mensagens WHERE quem='pessoa'")["id"]
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["Recebi o CPF!"], "documentos": [{"msg_id": mid, "item": "CPF"}]}))
    a = b.um("SELECT * FROM acoes WHERE status='pendente'")
    assert a["tipo"] == "documento"
    assert motor.aprovar(b, cfg, a["id"], em(QUI)) == "ok"
    assert {i["item"]: i["status"] for i in b.itens(p["id"])}["CPF"] == "aprovado"


def test_documento_sem_anexo_ou_item_inventado_e_ignorado(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    chegar(b, cfg, p, "mandei o rg")
    mid = b.um("SELECT id FROM mensagens WHERE quem='pessoa'")["id"]
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["?"], "documentos": [
        {"msg_id": mid, "item": "CPF"}, {"msg_id": 999, "item": "Passaporte"}]}))
    assert not b.um("SELECT * FROM acoes")


def test_recusar_documento_pede_de_novo_com_motivo(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    chegar(b, cfg, p, "", arquivo=foto(cfg["dados_dir"]), tipo="IMAGE")
    mid = b.um("SELECT id FROM mensagens WHERE quem='pessoa'")["id"]
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": [], "documentos": [{"msg_id": mid, "item": "PIS"}]}))
    a = b.um("SELECT * FROM acoes")
    motor.recusar(b, cfg, a["id"], "foto cortada", em(QUI))
    assert "foto cortada" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]
    assert {i["item"]: i["status"] for i in b.itens(p["id"])}["PIS"] == "recusado"


def test_fluxo_completo_ate_admitida_e_vaga_fecha(b, cfg, aerovip):
    import tempfile
    vid = b.exec("INSERT INTO vagas (cliente_id, funcao, cidade, quantidade, data_inicio) VALUES (?,?,?,?,?)",
                 aerovip, "Auxiliar de Lavanderia", "Guarulhos", 1, "2026-12-07")
    p = nova(b, cfg, aerovip, vaga_id=vid)
    assert b.um("SELECT status FROM vagas WHERE id=?", vid)["status"] == "fechada"
    assert p["id"] not in [x["id"] for x in motor.estoque(b)]
    aprovar_tudo(b, cfg, p)
    p = b.pessoa(p["id"])
    assert p["etapa"] == "exame"
    assert "sexta, 27/11" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]  # próximo dia útil
    # escolhe o dia → proposta → aprova
    chegar(b, cfg, p, "pode ser sexta")
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["Vou confirmar!"], "propor_exame": {"data": "2026-11-27"}}))
    a = b.um("SELECT * FROM acoes WHERE tipo='exame'")
    motor.aprovar(b, cfg, a["id"], em(QUI))
    p = b.pessoa(p["id"])
    assert p["etapa"] == "guia" and p["exame_data"] == "2026-11-27"
    assert "amanhã" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]
    assert "Rua Abaiara" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]
    # pós-exame às 14h do dia do exame
    motor.tick(b, cfg, em("2026-11-27T14:05:00"))
    assert "guia do exame" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]
    chegar(b, cfg, p, "fui sim", arquivo=foto(cfg["dados_dir"], "guia.jpg"), tipo="IMAGE", agora="2026-11-27T15:00:00")
    mid = b.um("SELECT id FROM mensagens WHERE quem='pessoa' ORDER BY id DESC")["id"]
    fluxo.processar(b, cfg, p["id"], em("2026-11-27T15:00:00"),
                    fake({"mensagens": ["Obrigado!"], "compareceu": True, "documentos": [{"msg_id": mid, "item": "Guia do exame"}]}))
    a = b.um("SELECT * FROM acoes WHERE tipo='guia'")
    motor.aprovar(b, cfg, a["id"], em("2026-11-27T15:10:00"))
    assert b.pessoa(p["id"])["etapa"] == "contrato"
    ec = b.um("SELECT * FROM acoes WHERE tipo='enviar_contrato' AND status='pendente'")
    pdf = Path(cfg["dados_dir"]) / "arquivos" / "contrato.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    b.exec("UPDATE clientes SET contrato_pdf=? WHERE id=?", str(pdf), aerovip)
    assert motor.aprovar(b, cfg, ec["id"], em("2026-11-27T15:12:00"), "remoto") == "ok"
    assert b.um("SELECT arquivo FROM saida ORDER BY id DESC")["arquivo"] == str(pdf)
    chegar(b, cfg, p, "assinado", arquivo=foto(cfg["dados_dir"], "ct.jpg"), tipo="IMAGE", agora="2026-11-27T16:00:00")
    mid = b.um("SELECT id FROM mensagens WHERE quem='pessoa' ORDER BY id DESC")["id"]
    fluxo.processar(b, cfg, p["id"], em("2026-11-27T16:00:00"),
                    fake({"mensagens": ["Recebi!"], "documentos": [{"msg_id": mid, "item": "Contrato assinado"}]}))
    motor.aprovar(b, cfg, b.um("SELECT id FROM acoes WHERE tipo='contrato'")["id"], em("2026-11-27T16:10:00"))
    p = b.pessoa(p["id"])
    assert p["etapa"] == "inicio" and "segunda, 07/12" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]
    chegar(b, cfg, p, "posso dia 07/12", agora="2026-11-27T16:20:00")
    fluxo.processar(b, cfg, p["id"], em("2026-11-27T16:20:00"), fake({"mensagens": ["Anotado"], "propor_inicio": {"data": "07/12/2026"}}))
    motor.aprovar(b, cfg, b.um("SELECT id FROM acoes WHERE tipo='inicio'")["id"], em("2026-11-27T16:30:00"))
    p = b.pessoa(p["id"])
    assert p["status"] == "admitida" and p["etapa"] == "concluida" and p["data_inicio"] == "2026-12-07"


def test_contrato_presencial(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    b.atualizar(p["id"], etapa="contrato", aguardando="equipe")
    aid = motor.criar_acao(b, p["id"], "enviar_contrato", {}, "x", em(QUI))
    assert motor.aprovar(b, cfg, aid, em(QUI), "presencial") == "ok"
    motor.contrato_presencial_assinado(b, cfg, p["id"], None, em(QUI))
    assert b.pessoa(p["id"])["etapa"] == "inicio"


# ---------- exame ----------
def test_proximo_dia_util_pula_fim_de_semana_e_feriado(cfg):
    from datetime import date
    assert regras.opcoes_exame(cfg, date(2026, 11, 27), None) == [date(2026, 11, 30), date(2026, 12, 1)]  # sex → seg, ter
    assert regras.opcoes_exame(cfg, date(2026, 12, 24), None)[0] == date(2026, 12, 28)  # 25/12 feriado
    assert not regras.data_exame_valida(cfg, date(2026, 11, 26), date(2026, 11, 28), None)  # sábado


def test_data_de_exame_invalida_nao_vira_acao(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    aprovar_tudo(b, cfg, p)
    chegar(b, cfg, p, "domingo")
    fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["?"], "propor_exame": {"data": "2026-11-29"}}))
    assert not b.um("SELECT * FROM acoes WHERE tipo='exame'")


def test_falta_no_exame_conta_tentativa_e_3_faltas_vao_para_humano(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    for n in range(1, 4):
        b.atualizar(p["id"], etapa="guia", exame_data="2026-11-27", aguardando="pessoa")
        chegar(b, cfg, p, "não consegui ir", kid=f"k{n}")
        fluxo.processar(b, cfg, p["id"], em(QUI), fake({"mensagens": ["Tudo bem"], "compareceu": False}))
        q = b.pessoa(p["id"])
        if n < 3:
            assert q["etapa"] == "exame" and q["faltas"] == n and q["cobrancas"] == n and q["status"] == "ativo"
            b.atualizar(p["id"], status="ativo")
    assert b.pessoa(p["id"])["status"] == "humano"


def test_vespera_manda_confirmacao(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    b.atualizar(p["id"], etapa="guia", exame_data="2026-12-01", aguardando="exame")
    motor.tick(b, cfg, em("2026-11-30T09:30:00"))
    assert "amanhã" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]
    n = b.um("SELECT COUNT(*) n FROM saida")["n"]
    motor.tick(b, cfg, em("2026-11-30T10:30:00"))
    assert b.um("SELECT COUNT(*) n FROM saida")["n"] == n  # não repete


# ---------- cobranças ----------
def test_cobrancas_24_48_72_e_depois_humano(b, cfg, aerovip):
    p = nova(b, cfg, aerovip, agora="2026-11-23T10:00:00")  # segunda
    assert motor.tick(b, cfg, em("2026-11-24T09:59:00")) == []
    assert motor.tick(b, cfg, em("2026-11-24T10:00:00")) == [f"cobranca {p['id']} #1"]
    assert "ainda falta" in b.um("SELECT texto FROM saida ORDER BY id DESC")["texto"]
    assert motor.tick(b, cfg, em("2026-11-25T10:00:00")) == [f"cobranca {p['id']} #2"]
    assert motor.tick(b, cfg, em("2026-11-26T10:00:00")) == [f"cobranca {p['id']} #3"]
    assert motor.tick(b, cfg, em("2026-11-26T23:00:00")) == []
    assert motor.tick(b, cfg, em("2026-11-27T10:00:00")) == [f"humano {p['id']}"]
    assert b.pessoa(p["id"])["status"] == "humano"


def test_janela_de_envio_sp(cfg):
    assert regras.na_janela(cfg, em("2026-11-27T20:59:00"))
    assert not regras.na_janela(cfg, em("2026-11-27T21:00:00"))
    assert not regras.na_janela(cfg, em("2026-11-27T07:59:00"))
    assert regras.na_janela(cfg, em("2026-11-27T08:00:00"))
    assert not regras.na_janela(cfg, em("2026-11-29T10:00:00"))  # domingo
    assert regras.na_janela(cfg, em("2026-11-28T10:00:00"))  # sábado


def test_cobranca_fora_da_janela_fica_adiada(b, cfg, aerovip):
    p = nova(b, cfg, aerovip, agora="2026-11-27T20:30:00")  # sexta 20h30
    assert motor.tick(b, cfg, em("2026-11-28T20:59:00")) == [f"cobranca {p['id']} #1"]
    p2 = nova_outra(b, cfg, aerovip, "2026-11-28T21:30:00")  # sábado 21h30
    assert motor.tick(b, cfg, em("2026-11-29T22:00:00")) == []          # domingo: nada
    assert f"cobranca {p2['id']} #1" in motor.tick(b, cfg, em("2026-11-30T08:00:00"))  # segunda 8h


def nova_outra(b, cfg, aerovip, agora):
    return motor.cadastrar(b, cfg, "José", "11 91111-2222", aerovip, em(agora))


def test_resposta_zera_relogio_mas_nao_o_contador(b, cfg, aerovip):
    p = nova(b, cfg, aerovip, agora="2026-11-23T10:00:00")
    motor.tick(b, cfg, em("2026-11-24T10:00:00"))
    chegar(b, cfg, p, "amanhã mando", agora="2026-11-24T12:00:00")
    assert motor.tick(b, cfg, em("2026-11-25T10:00:00")) == []
    assert motor.tick(b, cfg, em("2026-11-25T12:00:00")) == [f"cobranca {p['id']} #2"]


def test_nao_cobra_quando_a_vez_e_da_equipe(b, cfg, aerovip):
    p = nova(b, cfg, aerovip, agora="2026-11-23T10:00:00")
    b.atualizar(p["id"], aguardando="equipe")
    assert motor.tick(b, cfg, em("2026-11-25T10:00:00")) == []


# ---------- estoque ----------
def test_estoque_filtros_e_ligar_vaga(b, cfg, aerovip):
    p = nova(b, cfg, aerovip, cidade="Guarulhos")
    assert [x["id"] for x in motor.estoque(b, cidade="guaru")] == [p["id"]]
    assert motor.estoque(b, completo=True) == []
    vid = b.exec("INSERT INTO vagas (cliente_id, funcao, quantidade) VALUES (?,?,?)", aerovip, "Limpeza", 1)
    assert motor.ligar_vaga(b, p["id"], vid) == "ok"
    assert motor.estoque(b) == [] and b.um("SELECT status FROM vagas WHERE id=?", vid)["status"] == "fechada"
    motor.ligar_vaga(b, p["id"], None)
    assert len(motor.estoque(b)) == 1 and b.um("SELECT status FROM vagas WHERE id=?", vid)["status"] == "aberta"


# ---------- Hermes ----------
def test_hermes_json_invalido_duas_vezes_vai_para_humano(b, cfg, aerovip, monkeypatch):
    p = nova(b, cfg, aerovip)
    chegar(b, cfg, p, "oi")
    chamadas = []
    monkeypatch.setattr(agente, "perguntar", lambda pr, pf, t: chamadas.append(1) or "não sei")
    assert fluxo.processar(b, cfg, p["id"], em(QUI)) == "erro hermes"
    assert len(chamadas) == 2 and b.pessoa(p["id"])["status"] == "humano"
    assert b.um("SELECT COUNT(*) n FROM acoes")["n"] == 0


def test_numero_sem_cadastro_vai_para_humano(b, cfg):
    r = fluxo.receber(b, cfg, "5511900001111", "5511900001111@s.whatsapp.net", "Fulano", "oi, vi a vaga", "TEXT",
                      None, "k1", em(QUI))
    assert r == "sem cadastro"
    p = b.um("SELECT * FROM pessoas")
    assert p["status"] == "humano" and "equipe" in b.um("SELECT texto FROM saida")["texto"]


def test_mensagem_repetida_nao_entra_duas_vezes(b, cfg, aerovip):
    p = nova(b, cfg, aerovip)
    assert chegar(b, cfg, p, "oi", kid="ABC") == "ok"
    assert chegar(b, cfg, p, "oi", kid="ABC") == "repetido"


# ---------- envio ----------
def test_envio_ligado_uma_tentativa_e_erro_vai_para_humano(b, cfg, aerovip, monkeypatch):
    import json as _j
    p = nova(b, cfg, aerovip)
    (Path(cfg["dados_dir"]) / "chaves.json").write_text(_j.dumps({"envio_ligado": True}))
    chamadas = []

    def falha(*a):
        chamadas.append(a)
        raise OSError("caiu")
    monkeypatch.setattr(whatsapp, "_post", falha)
    s = whatsapp.enviar(cfg, b, b.pessoa(p["id"]), "teste")
    assert len(chamadas) == 1 and s["erro"] and s["enviado"] == 0
    assert b.pessoa(p["id"])["status"] == "humano"
    monkeypatch.setattr(whatsapp, "_post", lambda *a: "KEY1")
    s = whatsapp.enviar(cfg, b, b.pessoa(p["id"]), "teste 2")
    assert s["enviado"] == 1 and s["key_id"] == "KEY1"


def test_modo_seco_nao_chama_api(b, cfg, aerovip, monkeypatch):
    monkeypatch.setattr(whatsapp, "_post", lambda *a: pytest.fail("chamou a API em modo seco"))
    nova(b, cfg, aerovip)


# ---------- clínicas ----------
def test_clinicas_sem_planilha_fica_padrao(b, cfg):
    assert "sem planilha" in clinicas.sincronizar(b, cfg)
    assert b.um("SELECT * FROM clinicas")["endereco"].startswith("Rua Abaiara, 194")


def test_clinicas_da_planilha(b, cfg):
    cfg["clinicas"]["planilha_id"] = "X"
    linhas = [["nome", "cidade", "endereco"], ["Clínica B", "Bragança Paulista", "Rua 1, 10", "seg,qua", "07:00", "10:00"]]
    assert clinicas.sincronizar(b, cfg, ler=lambda i, a: linhas) == "1 clínicas"
    c = b.um("SELECT * FROM clinicas WHERE nome='Clínica B'")
    assert c["dias"] == "0,2" and c["inicio"] == "07:00"
    cfg["clinicas"]["planilha_id"] = "Y"
    assert "erro" in clinicas.sincronizar(b, cfg, ler=lambda i, a: (_ for _ in ()).throw(RuntimeError("x")))
