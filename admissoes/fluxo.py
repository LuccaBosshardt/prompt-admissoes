"""Um turno de conversa: junta as mensagens novas da pessoa, monta o EVENTO, chama o Hermes, aplica o resultado."""
from __future__ import annotations

from datetime import datetime

from . import agente, motor, regras
from .banco import Banco

RESPOSTA_SEM_CADASTRO = ("Olá! Aqui é o {assinatura}. Recebi sua mensagem e vou passar para a nossa equipe, "
                         "que retorna por aqui em breve.")


def evento(b: Banco, cfg: dict, p: dict, novas: list[dict], agora: datetime) -> dict:
    ctx = motor._ctx(b, cfg, p)
    cl, clin = ctx["cliente"], ctx["clinica"]
    v = b.um("SELECT * FROM vagas WHERE id=?", p["vaga_id"]) if p["vaga_id"] else None
    hist = b.todos("SELECT id, quem, texto, tipo, arquivo IS NOT NULL tem_arquivo FROM mensagens "
                   "WHERE pessoa_id=? ORDER BY id DESC LIMIT 30", p["id"])[::-1]
    itens = b.itens(p["id"])
    return {
        "agora": f"{regras.DIAS[agora.weekday()]}, {agora.strftime('%d/%m/%Y %H:%M')}",
        "pessoa": {"nome": p["nome"], "funcao": p["funcao"], "cidade": p["cidade"]},
        "cliente": cl["nome"] if cl else None,
        "vaga": {"funcao": v["funcao"], "cidade": v["cidade"], "data_inicio": v["data_inicio"]} if v else None,
        "etapa": p["etapa"],
        "itens": [{"item": i["item"], "tipo": i["grupo"], "descricao": motor.rotulo(i), "status": i["status"],
                   "motivo_recusa": i["obs"] if i["status"] == "recusado" and i["grupo"] != "info" else None}
                  for i in itens],
        "infos_aceitas": [i["chave"] for i in (cl or {}).get("infos", [])],
        "opcoes_exame": p["marcas"].get("opcoes_exame", []) if p["etapa"] == "exame" else [],
        "exame": {"data": p["exame_data"], "clinica": clin["nome"] if clin else None,
                  "endereco": clin["endereco"] if clin else None,
                  "janela": f"{clin['inicio']}–{clin['fim']}" if clin else None} if p["exame_data"] else None,
        "aguardando_equipe": [a["resumo"] for a in b.todos(
            "SELECT resumo FROM acoes WHERE pessoa_id=? AND status='pendente'", p["id"])],
        "historico": [{"id": m["id"], "quem": m["quem"], "texto": (m["texto"] or "")[:600],
                       "anexo": bool(m["tem_arquivo"])} for m in hist if m["id"] not in {n["id"] for n in novas}],
        "novas_mensagens": [{"msg_id": m["id"], "tipo": m["tipo"], "texto": m["texto"] or "",
                             "anexo": bool(m["arquivo"])} for m in novas],
    }


def processar(b: Banco, cfg: dict, pid: int, agora: datetime, responder=agente.responder) -> str:
    p = b.pessoa(pid)
    novas = b.todos("SELECT * FROM mensagens WHERE pessoa_id=? AND quem='pessoa' AND processada=0 ORDER BY id", pid)
    if not novas:
        return "nada"
    b.exec(f"UPDATE mensagens SET processada=1 WHERE id IN ({','.join('?' * len(novas))})", *[m["id"] for m in novas])
    if p["status"] != "ativo" or not p["cliente_id"]:
        return "guardado (pessoa não está com o robô)"
    if p["pausa_ate"] and datetime.fromisoformat(p["pausa_ate"]) > agora:
        return "guardado (humano assumiu)"
    ev = evento(b, cfg, p, novas, agora)
    try:
        res = responder(ev, cfg)
    except agente.HermesFalhou as e:
        b.log(pid, "erro_hermes", str(e))
        motor.humano(b, pid, f"erro técnico no agente: {str(e)[:120]}")
        return "erro hermes"
    b.log(pid, "hermes", f"resumo={res.get('resumo', '')}")
    feito = motor.aplicar_resultado(b, cfg, pid, res, agora, novas)
    for t in motor._msgs_limpas(res.get("mensagens")):
        motor.enviar(b, cfg, b.pessoa(pid), t, agora, pedido=b.pessoa(pid)["aguardando"] == "pessoa")
    b.log(pid, "turno", "; ".join(feito) or "só conversa")
    return "ok"


def receber(b: Banco, cfg: dict, tel: str, jid: str, nome_wa: str, texto: str, tipo: str, arquivo: str | None,
            key_id: str | None, agora: datetime) -> str:
    """Guarda a mensagem do candidato. Número sem cadastro: cria contato na fila 'Precisa de humano'."""
    p = motor.achar_por_tel(b, tel)
    if key_id and b.um("SELECT id FROM mensagens WHERE key_id=?", key_id):
        return "repetido"
    if not p:
        pid = b.exec("INSERT INTO pessoas (nome, telefone, jid, status, aguardando, motivo_humano, etapa, criado_em, "
                     "atualizado_em) VALUES (?,?,?,?,?,?,?,?,?)", nome_wa or "Sem nome", motor.norm_tel(tel), jid,
                     "humano", "equipe", "contato sem cadastro (escreveu primeiro)", "documentos",
                     agora.isoformat(), agora.isoformat())
        p = b.pessoa(pid)
        b.exec("INSERT INTO mensagens (pessoa_id, telefone, quem, texto, tipo, arquivo, key_id, processada, em) "
               "VALUES (?,?,?,?,?,?,?,1,?)", pid, tel, "pessoa", texto, tipo, arquivo, key_id, agora.isoformat())
        motor.enviar(b, cfg, p, RESPOSTA_SEM_CADASTRO.format(assinatura=cfg["assinatura"]), agora, pedido=False)
        return "sem cadastro"
    if jid and p["jid"] != jid:
        b.atualizar(p["id"], jid=jid)  # o WhatsApp manda o JID real (com ou sem o 9)
    b.exec("INSERT INTO mensagens (pessoa_id, telefone, quem, texto, tipo, arquivo, key_id, processada, em) "
           "VALUES (?,?,?,?,?,?,?,?,?)", p["id"], tel, "pessoa", texto, tipo, arquivo, key_id,
           0 if p["status"] == "ativo" else 1, agora.isoformat())
    b.atualizar(p["id"], ultima_msg_pessoa=agora.isoformat())
    return "ok"
