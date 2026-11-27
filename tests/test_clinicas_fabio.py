"""Regras das clínicas do documento do Fabio (27/11): clínica por cliente/cidade, dado interno nunca vai ao candidato."""
import json

from admissoes import fluxo, motor
from conftest import em

QUI = "2026-11-26T10:00:00"


def _clinicas(b):
    def ins(chave, nome, cidade, clientes, agend=0, horario=""):
        return b.exec("INSERT INTO clinicas (nome, cidade, endereco, clientes, agendamento, obs_interna, horario_texto, chave) "
                      "VALUES (?,?,?,?,?,?,?,?)", nome, cidade, f"Rua de {nome}, 1", clientes, agend,
                      "PIX 123.456.789-00 valor R$ 50", horario, chave)
    return {"asc": ins("a", "ASCONCIPA", "São Paulo", "Búfalo, Cilasi, Spoleto", horario="das 8h às 12h"),
            "arte": ins("b", "Arte Médica", "Embu das Artes", "Customiza", agend=1, horario="no horário agendado pela equipe"),
            "d2": ins("c", "D2", "São Paulo", "Nexhub, Customiza", agend=1)}


def _cliente(b, nome, modo=""):
    base = b.um("SELECT * FROM clientes WHERE nome='Aerovip'")
    return b.exec("INSERT INTO clientes (nome, documentos, documentos_filhos, infos, textos, contrato_modo) VALUES (?,?,?,?,?,?)",
                  nome, base["documentos"], base["documentos_filhos"], base["infos"], base["textos"], modo)


def test_clinica_escolhida_pelo_cliente_e_pela_cidade(b, cfg):
    c = _clinicas(b)
    bufalo, customiza = _cliente(b, "Búfalo", "presencial"), _cliente(b, "Customiza", "remoto")
    assert motor.cadastrar(b, cfg, "A", "11911110001", bufalo, em(QUI))["clinica_id"] == c["asc"]
    assert motor.cadastrar(b, cfg, "B", "11911110002", customiza, em(QUI), cidade="São Paulo")["clinica_id"] == c["d2"]
    assert motor.cadastrar(b, cfg, "C", "11911110003", customiza, em(QUI), cidade="Embu das Artes")["clinica_id"] == c["arte"]
    aero = b.um("SELECT id FROM clientes WHERE nome='Aerovip'")["id"]
    assert "Rua Abaiara" in b.um("SELECT endereco FROM clinicas WHERE id=?",
                                 motor.cadastrar(b, cfg, "D", "11911110004", aero, em(QUI))["clinica_id"])["endereco"]


def test_dado_interno_da_clinica_nunca_vai_ao_candidato(b, cfg):
    _clinicas(b)
    p = motor.cadastrar(b, cfg, "Ana", "11922220000", _cliente(b, "Búfalo"), em(QUI))
    for i in b.itens(p["id"]):
        b.exec("UPDATE itens SET status='aprovado' WHERE id=?", i["id"])
    motor.checar_documentos(b, cfg, p["id"], em(QUI))
    aid = motor.criar_acao(b, p["id"], "exame", {"data": "2026-11-27"}, "x", em(QUI))
    motor.aprovar(b, cfg, aid, em(QUI))
    textos = " ".join(s["texto"] for s in b.todos("SELECT texto FROM saida"))
    assert "das 8h às 12h" in textos and "123.456" not in textos and "R$ 50" not in textos
    ev = fluxo.evento(b, cfg, b.pessoa(p["id"]), [], em(QUI))
    assert "123.456" not in json.dumps(ev, ensure_ascii=False)


def test_clinica_com_agendamento_avisa_a_equipe(b, cfg):
    _clinicas(b)
    p = motor.cadastrar(b, cfg, "Bia", "11933330000", _cliente(b, "Customiza", "remoto"), em(QUI), cidade="Embu das Artes")
    b.atualizar(p["id"], etapa="exame")
    b.exec("INSERT INTO mensagens (pessoa_id, quem, texto, processada) VALUES (?, 'pessoa', 'sexta', 0)", p["id"])
    novas = b.todos("SELECT * FROM mensagens WHERE quem='pessoa'")
    motor.aplicar_resultado(b, cfg, p["id"], {"mensagens": [], "propor_exame": {"data": "2026-11-27"}}, em(QUI), novas)
    assert "exige agendamento" in b.um("SELECT resumo FROM acoes WHERE tipo='exame'")["resumo"]


def test_contrato_padrao_do_cliente_aparece_na_aprovacao(b, cfg):
    _clinicas(b)
    p = motor.cadastrar(b, cfg, "Caio", "11944440000", _cliente(b, "Spoleto", "presencial"), em(QUI))
    b.exec("INSERT OR IGNORE INTO itens (pessoa_id, item, grupo, status) VALUES (?, ?, 'exame', 'recebido')", p["id"], motor.ITEM_GUIA)
    it = b.um("SELECT id FROM itens WHERE item=?", motor.ITEM_GUIA)
    b.atualizar(p["id"], etapa="guia")
    aid = motor.criar_acao(b, p["id"], "guia", {"item_id": it["id"]}, "guia", em(QUI))
    motor.aprovar(b, cfg, aid, em(QUI))
    assert "PRESENCIAL" in b.um("SELECT resumo FROM acoes WHERE tipo='enviar_contrato'")["resumo"]


def test_sem_funcao_nao_escreve_vaga_de_a_vaga(b, cfg):
    aero = b.um("SELECT id FROM clientes WHERE nome='Aerovip'")["id"]
    motor.cadastrar(b, cfg, "Zé", "11955550000", aero, em(QUI))
    t = b.um("SELECT texto FROM saida")["texto"]
    assert "vaga de a vaga" not in t and "aprovado(a) para a vaga." in t
