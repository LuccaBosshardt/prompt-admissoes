"""Motor de etapas: documentos → exame → guia → contrato → inicio → concluida.

Quem decide é a equipe (fila de Aprovações). O Hermes só propõe. As mensagens de transição são modelos fixos;
o Hermes conversa no meio. Regras de tempo em regras.py; todas as funções recebem `agora` (relógio injetável).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from . import config, regras, whatsapp
from .banco import Banco

ETAPAS = ["documentos", "exame", "guia", "contrato", "inicio", "concluida"]
NOME_ETAPA = {"documentos": "Documentos", "exame": "Agendar exame", "guia": "Exame / guia",
              "contrato": "Contrato", "inicio": "Data de início", "concluida": "Admitida"}
ITEM_GUIA = "Guia do exame"
ITEM_CONTRATO = "Contrato assinado"
TIPOS_ACAO = {"documento": "Documento recebido", "exame": "Data do exame", "guia": "Guia do exame",
              "enviar_contrato": "Liberar contrato", "contrato": "Contrato assinado", "inicio": "Data de início"}


# ---------------- telefone ----------------
def norm_tel(t: str) -> str:
    d = re.sub(r"\D", "", t or "")
    if len(d) in (10, 11):
        d = "55" + d
    return d


def chave_tel(t: str) -> str:
    """DDD + 8 últimos dígitos: casa número com e sem o 9 extra."""
    d = norm_tel(t)
    return d[2:4] + d[-8:] if len(d) >= 12 else d


def achar_por_tel(b: Banco, tel: str) -> dict | None:
    k = chave_tel(tel)
    for p in b.todos("SELECT id, telefone FROM pessoas WHERE status != 'desistiu' ORDER BY id DESC"):
        if chave_tel(p["telefone"]) == k:
            return b.pessoa(p["id"])
    return None


# ---------------- envio ----------------
def _norm(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", (t or "").lower()) if unicodedata.category(c) != "Mn").strip()


def escolher_clinica(b: Banco, cliente: dict, cidade: str = "") -> int | None:
    """Clínicas que listam o cliente; com mais de uma, a da cidade da pessoa; senão a clínica fixa do cliente."""
    nome = _norm(cliente["nome"])
    cands = [c for c in b.todos("SELECT * FROM clinicas ORDER BY id")
             if nome in [_norm(x) for x in re.split(r"[,/;]", c.get("clientes") or "")]]
    if cidade and len(cands) > 1:
        mesma = [c for c in cands if _norm(cidade) in _norm(c["cidade"]) or _norm(c["cidade"]) in _norm(cidade)]
        if mesma:
            return mesma[0]["id"]
    if cliente.get("clinica_id") and any(c["id"] == cliente["clinica_id"] for c in cands):
        return cliente["clinica_id"]
    return cands[0]["id"] if cands else cliente.get("clinica_id")


def _ctx(b: Banco, cfg: dict, p: dict) -> dict:
    cl = b.cliente(p["cliente_id"]) if p.get("cliente_id") else None
    clin = b.um("SELECT * FROM clinicas WHERE id=?", p.get("clinica_id") or (cl or {}).get("clinica_id"))
    return {"cliente": cl, "clinica": clin}


def enviar(b: Banco, cfg: dict, p: dict, texto: str, agora: datetime, pedido: bool = True, arquivo=None) -> None:
    whatsapp.enviar(cfg, b, p, texto, arquivo)
    if pedido:
        b.atualizar(p["id"], pedido_em=agora.isoformat())


def assinar(cfg: dict, texto: str) -> str:
    return texto


# ---------------- cadastro ----------------
def _lista(xs) -> str:
    return "\n".join(f"* {x}" for x in xs)


def texto_inicial(cfg: dict, cl: dict, nome: str = "Maria", funcao: str = "") -> str:
    """Monta a 1ª mensagem. Campo errado no texto do cliente ({nom}, chave solta) → ValueError claro."""
    modelo = cl["textos"].get("inicial") or "Olá, {nome}! Aqui é o {assinatura}. Vamos começar sua admissão."
    if not funcao:
        modelo = modelo.replace(" de {funcao}", "")
    try:
        return modelo.format(nome=nome.split()[0].title(), funcao=funcao or "a vaga",
                             assinatura=config.ajustes(cfg)["assinatura"] or cfg["assinatura"],
                             documentos=_lista(cl["documentos"]), documentos_filhos=_lista(cl["documentos_filhos"]),
                             infos=_lista(i["rotulo"] for i in cl["infos"] if i["chave"] != "filhos_menores_14")
                             + "\n* Você tem filhos menores de 14 anos? Quantos?")
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"o texto inicial do cliente {cl['nome']} tem um campo inválido ({e}). "
                         "Use só {nome}, {funcao}, {assinatura}, {documentos}, {documentos_filhos}, {infos}.") from e


def cadastrar(b: Banco, cfg: dict, nome: str, telefone: str, cliente_id: int, agora: datetime,
              funcao: str = "", cidade: str = "", vaga_id: int | None = None) -> dict:
    tel = norm_tel(telefone)
    if len(tel) < 12:
        raise ValueError("telefone inválido (use DDD + número)")
    cl = b.cliente(cliente_id)
    if not cl:
        raise ValueError("cliente não existe")
    if vaga_id:
        v = b.um("SELECT * FROM vagas WHERE id=?", vaga_id)
        if not v or v["status"] != "aberta":
            raise ValueError("a vaga escolhida não está aberta")
        funcao = funcao or v["funcao"]
        cidade = cidade or (v["cidade"] or "")
    texto = texto_inicial(cfg, cl, nome, funcao)
    pid = b.exec("INSERT INTO pessoas (nome, telefone, jid, cliente_id, vaga_id, funcao, cidade, etapa, status, "
                 "aguardando, clinica_id, criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 nome.strip(), tel, f"{tel}@s.whatsapp.net", cliente_id, vaga_id, funcao, cidade, "documentos",
                 "ativo", "pessoa", escolher_clinica(b, cl, cidade), agora.isoformat(), agora.isoformat())
    for d in cl["documentos"]:
        b.exec("INSERT INTO itens (pessoa_id, item, grupo) VALUES (?,?,?)", pid, d, "doc")
    for i in cl["infos"]:
        b.exec("INSERT INTO itens (pessoa_id, item, grupo, obs) VALUES (?,?,?,?)", pid, i["chave"], "info", i["rotulo"])
    if vaga_id:
        checar_vaga(b, vaga_id)
    p = b.pessoa(pid)
    enviar(b, cfg, p, texto, agora)
    b.log(pid, "cadastro", f"{nome} / {cl['nome']}")
    return b.pessoa(pid)


# ---------------- pendências ----------------
def opcional(i: dict) -> bool:
    """Item com "(se ...)" no nome (ex.: Declaração escolar (se já estuda)) não trava a etapa."""
    return "(se " in i["item"].lower()


def pendentes(b: Banco, pid) -> list[dict]:
    return [i for i in b.itens(pid) if i["status"] in ("pendente", "recusado") and not opcional(i)]


def rotulo(i: dict) -> str:
    return i["obs"] if i["grupo"] == "info" and i["obs"] else i["item"]


def _abrir_etapa(b: Banco, cfg: dict, pid, etapa: str, agora: datetime, aguardando: str = "pessoa") -> None:
    b.atualizar(pid, etapa=etapa, cobrancas=0, ultima_cobranca=None, aguardando=aguardando,
                pedido_em=agora.isoformat())
    b.log(pid, "etapa", etapa)


def checar_documentos(b: Banco, cfg: dict, pid, agora: datetime) -> None:
    """Na etapa documentos: tudo aprovado → abre exame; nada a pedir → vez da equipe."""
    p = b.pessoa(pid)
    if p["etapa"] != "documentos":
        return
    its = [i for i in b.itens(pid) if i["grupo"] in ("doc", "info", "filho_doc") and not opcional(i)]
    if its and all(i["status"] == "aprovado" for i in its):
        _abrir_etapa(b, cfg, pid, "exame", agora)
        oferecer_exame(b, cfg, b.pessoa(pid), agora)
        return
    falta_pessoa = any(i["status"] in ("pendente", "recusado") for i in its)
    b.atualizar(pid, aguardando="pessoa" if falta_pessoa else "equipe")


def janela(clin: dict) -> str:
    return clin.get("horario_texto") or f"das {clin['inicio']} às {clin['fim']}"


def oferecer_exame(b: Banco, cfg: dict, p: dict, agora: datetime, prefixo: str = "") -> None:
    clin = _ctx(b, cfg, p)["clinica"]
    ops = regras.opcoes_exame(cfg, agora.date(), clin)
    b.marcar(p["id"], opcoes_exame=[d.isoformat() for d in ops])
    local = f" na {clin['nome']} ({clin['endereco']}), {janela(clin)}" if clin else ""
    txt = (prefixo + f"Seus documentos foram conferidos. Agora vamos agendar o seu exame médico admissional{local}. "
           f"Você pode ir {regras.fmt_data(ops[0])} ou {regras.fmt_data(ops[1])}? Qual dia fica melhor?")
    enviar(b, cfg, p, txt, agora)


# ---------------- ações (fila de aprovação) ----------------
def receber_arquivo(b: Banco, it: dict, arquivo: str, msg_id, agora: datetime) -> None:
    """Frente e verso: a 2ª foto do mesmo item não apaga a 1ª (vai para arquivos_extra)."""
    extra = json.loads(it.get("arquivos_extra") or "[]")
    if it.get("arquivo") and it["status"] == "recebido" and it["arquivo"] != arquivo and it["arquivo"] not in extra:
        extra.append(it["arquivo"])
    elif it["status"] != "recebido":
        extra = []
    b.exec("UPDATE itens SET status='recebido', arquivo=?, msg_id=?, arquivos_extra=?, atualizado_em=? WHERE id=?",
           arquivo, msg_id, json.dumps(extra), agora.isoformat(), it["id"])


def usar_anexo(b: Banco, cfg: dict, pid, msg_id: int, item_id: int, agora: datetime) -> str:
    """Equipe diz no painel: 'este anexo da conversa é o item X' → item aprovado."""
    m = b.um("SELECT * FROM mensagens WHERE id=? AND pessoa_id=?", msg_id, pid)
    it = b.item(item_id)
    if not m or not m["arquivo"] or not it or it["pessoa_id"] != pid:
        return "anexo ou item não encontrado"
    receber_arquivo(b, it, m["arquivo"], m["id"], agora)
    tipo = {"exame": "guia", "contrato": "contrato"}.get(it["grupo"], "documento")
    aid = criar_acao(b, pid, tipo, {"item_id": it["id"]}, f"{it['item']} (marcado pela equipe)", agora)
    return aprovar(b, cfg, aid, agora)


def criar_acao(b: Banco, pid, tipo: str, payload: dict, resumo: str, agora: datetime) -> int:
    ja = b.um("SELECT id FROM acoes WHERE pessoa_id=? AND tipo=? AND status='pendente' AND payload=?",
              pid, tipo, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if ja:
        return ja["id"]
    if tipo in ("exame", "inicio", "enviar_contrato"):
        b.exec("UPDATE acoes SET status='substituida', decidido_em=? WHERE pessoa_id=? AND tipo=? AND status='pendente'",
               agora.isoformat(), pid, tipo)
    aid = b.exec("INSERT INTO acoes (pessoa_id, tipo, payload, resumo, criado_em) VALUES (?,?,?,?,?)",
                 pid, tipo, json.dumps(payload, ensure_ascii=False, sort_keys=True), resumo, agora.isoformat())
    b.log(pid, "acao_criada", f"{tipo} {payload}")
    return aid


def _fechar_acao(b: Banco, aid, status: str, agora: datetime, motivo: str = "") -> None:
    b.exec("UPDATE acoes SET status=?, motivo=?, decidido_em=? WHERE id=?", status, motivo, agora.isoformat(), aid)


def aprovar(b: Banco, cfg: dict, aid: int, agora: datetime, modo: str | None = None) -> str:
    a = b.acao(aid)
    if not a or a["status"] != "pendente":
        return "ação já decidida"
    p = b.pessoa(a["pessoa_id"])
    pl, pid = a["payload"], p["id"]
    etapa_certa = {"exame": "exame", "guia": "guia", "enviar_contrato": "contrato",
                   "contrato": "contrato", "inicio": "inicio"}.get(a["tipo"])
    if etapa_certa and p["etapa"] != etapa_certa:
        _fechar_acao(b, aid, "substituida", agora, f"a pessoa já está na etapa {NOME_ETAPA.get(p['etapa'], p['etapa'])}")
        return f"esta ação é de outra etapa (a pessoa está em {NOME_ETAPA.get(p['etapa'], p['etapa'])}); fechei sem mandar nada"
    if a["tipo"] == "documento":
        b.exec("UPDATE itens SET status='aprovado', atualizado_em=? WHERE id=?", agora.isoformat(), pl["item_id"])
        _fechar_acao(b, aid, "aprovada", agora)
        checar_documentos(b, cfg, pid, agora)
    elif a["tipo"] == "exame":
        d = date.fromisoformat(pl["data"])
        if d <= agora.date():
            recusar(b, cfg, aid, "a data já passou", agora)
            return "a data do exame já passou: mandei novas opções à pessoa"
        clin = _ctx(b, cfg, p)["clinica"]
        _fechar_acao(b, aid, "aprovada", agora)
        b.atualizar(pid, exame_data=d.isoformat(), clinica_id=clin["id"] if clin else None)
        _abrir_etapa(b, cfg, pid, "guia", agora, aguardando="exame")
        b.exec("INSERT OR IGNORE INTO itens (pessoa_id, item, grupo) VALUES (?,?,?)", pid, ITEM_GUIA, "exame")
        b.exec("UPDATE itens SET status='pendente', arquivo=NULL WHERE pessoa_id=? AND item=?", pid, ITEM_GUIA)
        amanha = d == agora.date() + timedelta(days=1)
        b.marcar(pid, vespera=amanha or ultimo_dia_de_aviso(cfg, agora.date(), d), pos_exame=False)
        quando = "amanhã" if amanha else regras.fmt_data(d)
        txt = (f"Seu exame admissional está agendado para {quando}, {janela(clin)}.\n"
               f"📌 Local do exame: {clin['endereco']}.\n⚠️ Importante: {clin['orientacoes']}") if clin else \
              f"Seu exame admissional está agendado para {quando}. A equipe vai te passar o local."
        enviar(b, cfg, b.pessoa(pid), txt, agora, pedido=False)
    elif a["tipo"] in ("guia", "contrato"):
        b.exec("UPDATE itens SET status='aprovado', atualizado_em=? WHERE id=?", agora.isoformat(), pl["item_id"])
        _fechar_acao(b, aid, "aprovada", agora)
        if a["tipo"] == "guia":
            _abrir_etapa(b, cfg, pid, "contrato", agora, aguardando="equipe")
            cl = _ctx(b, cfg, p)["cliente"] or {}
            padrao = {"presencial": " Padrão deste cliente: PRESENCIAL.", "remoto": " Padrão deste cliente: ONLINE (remoto)."}.get(cl.get("contrato_modo") or "", "")
            criar_acao(b, pid, "enviar_contrato", {}, "Escolha: contrato remoto (envia o PDF) ou presencial." + padrao, agora)
            enviar(b, cfg, b.pessoa(pid), "Recebi a guia do exame, obrigado! Agora vamos para o contrato. "
                   "Em breve te passo as orientações.", agora, pedido=False)
        else:
            abrir_inicio(b, cfg, pid, agora)
    elif a["tipo"] == "enviar_contrato":
        if modo not in ("remoto", "presencial"):
            return "escolha remoto ou presencial"
        cl = _ctx(b, cfg, p)["cliente"]
        if modo == "remoto" and not (cl and cl.get("contrato_pdf")):
            return "o cliente não tem modelo de contrato (PDF). Suba o PDF em Clientes."
        _fechar_acao(b, aid, "aprovada", agora, modo)
        b.atualizar(pid, contrato_modo=modo)
        b.exec("INSERT OR IGNORE INTO itens (pessoa_id, item, grupo) VALUES (?,?,?)", pid, ITEM_CONTRATO, "contrato")
        if modo == "remoto":
            b.atualizar(pid, aguardando="pessoa", cobrancas=0)
            enviar(b, cfg, b.pessoa(pid), "Segue o seu contrato. Leia, assine e me mande aqui a foto ou o PDF "
                   "de todas as páginas assinadas.", agora, arquivo=cl["contrato_pdf"])
        else:
            b.atualizar(pid, aguardando="equipe")
            enviar(b, cfg, b.pessoa(pid), "A assinatura do seu contrato vai ser presencial, na unidade da Prompt "
                   "Serviços. A equipe vai combinar o dia e o horário com você.", agora, pedido=False)
    elif a["tipo"] == "inicio":
        _fechar_acao(b, aid, "aprovada", agora)
        d = date.fromisoformat(pl["data"])
        b.atualizar(pid, data_inicio=d.isoformat(), status="admitida")
        _abrir_etapa(b, cfg, pid, "concluida", agora, aguardando="nada")
        enviar(b, cfg, b.pessoa(pid), f"Tudo certo! Seu início na vaga está confirmado para {regras.fmt_data(d)}. "
               "Bem-vindo(a) à Prompt Serviços! Qualquer dúvida, é só chamar aqui.", agora, pedido=False)
    else:
        return "tipo desconhecido"
    return "ok"


def abrir_inicio(b: Banco, cfg: dict, pid, agora: datetime) -> None:
    p = b.pessoa(pid)
    v = b.um("SELECT * FROM vagas WHERE id=?", p["vaga_id"]) if p["vaga_id"] else None
    dica = f" A previsão da vaga é {regras.fmt_data(date.fromisoformat(v['data_inicio']))}." if v and v.get("data_inicio") else ""
    _abrir_etapa(b, cfg, pid, "inicio", agora)
    enviar(b, cfg, b.pessoa(pid), f"Contrato conferido!{dica} Qual a data em que você pode começar na vaga?", agora)


def contrato_presencial_assinado(b: Banco, cfg: dict, pid, arquivo: str | None, agora: datetime) -> None:
    b.exec("INSERT OR IGNORE INTO itens (pessoa_id, item, grupo) VALUES (?,?,?)", pid, ITEM_CONTRATO, "contrato")
    b.exec("UPDATE itens SET status='aprovado', arquivo=?, atualizado_em=? WHERE pessoa_id=? AND item=?",
           arquivo, agora.isoformat(), pid, ITEM_CONTRATO)
    abrir_inicio(b, cfg, pid, agora)


def recusar(b: Banco, cfg: dict, aid: int, motivo: str, agora: datetime) -> str:
    a = b.acao(aid)
    if not a or a["status"] != "pendente":
        return "ação já decidida"
    p, pl = b.pessoa(a["pessoa_id"]), a["payload"]
    motivo = (motivo or "").strip() or "não ficou legível"
    _fechar_acao(b, aid, "recusada", agora, motivo)
    if a["tipo"] in ("documento", "guia", "contrato"):
        it = b.item(pl["item_id"])
        if not it:
            return "ok"  # item apagado (ex.: filho a menos): nada a pedir
        b.exec("UPDATE itens SET status='recusado', obs=CASE WHEN grupo='info' THEN obs ELSE ? END WHERE id=?",
               motivo, it["id"])
        b.atualizar(p["id"], aguardando="pessoa")
        enviar(b, cfg, b.pessoa(p["id"]), f"O documento \"{rotulo(it)}\" precisa ser enviado de novo: {motivo}. "
               "Pode me mandar outra foto ou PDF?", agora)
    elif a["tipo"] == "exame":
        b.atualizar(p["id"], aguardando="pessoa")
        oferecer_exame(b, cfg, b.pessoa(p["id"]), agora, prefixo=f"Essa data não deu certo ({motivo}). ")
    elif a["tipo"] == "inicio":
        b.atualizar(p["id"], aguardando="pessoa")
        enviar(b, cfg, b.pessoa(p["id"]), f"Essa data de início não deu certo ({motivo}). "
               "Qual outra data você pode começar?", agora)
    return "ok"


# ---------------- resultado do Hermes ----------------
CHAVES_RESULTADO = {"mensagens", "dados", "documentos", "propor_exame", "compareceu", "propor_inicio",
                    "precisa_humano", "motivo_humano", "resumo"}


def _msgs_limpas(xs) -> list[str]:
    out = []
    for t in (xs or [])[:3]:
        if not isinstance(t, str):
            continue
        t = re.sub(r"\s*[—]\s*", ", ", t).strip()
        if t and "http" not in t.lower() and len(t) <= 900:
            out.append(t)
    return out


def _vt(v) -> str:
    if isinstance(v, list):
        return " + ".join(f"{x.get('transporte', '?')} R$ {x.get('valor', '?')}" if isinstance(x, dict) else str(x) for x in v)
    return str(v)


def aplicar_resultado(b: Banco, cfg: dict, pid, res: dict, agora: datetime, novas: list[dict]) -> list[str]:
    """Aplica o JSON do Hermes com lista fechada de chaves. Devolve o que foi feito (para o log)."""
    feito = []
    desconhecidas = set(res) - CHAVES_RESULTADO
    if desconhecidas:
        b.log(pid, "chave_ignorada", ",".join(sorted(desconhecidas)))
    p = b.pessoa(pid)
    ids_validos = {m["id"]: m for m in novas}
    itens = {i["item"]: i for i in b.itens(pid)}

    # dados (infos): gravam direto, sem aprovação
    for k, v in (res.get("dados") or {}).items():
        it = itens.get(k)
        if not it or it["grupo"] != "info" or v in (None, "", []):
            continue
        valor = _vt(v) if k.startswith("vt_") else str(v)
        if k == "filhos_menores_14":
            try:
                n = max(0, min(10, int(v)))
            except (TypeError, ValueError):
                continue
            valor = str(n)
            cl = b.cliente(p["cliente_id"])
            for x in b.todos("SELECT id, item FROM itens WHERE pessoa_id=? AND grupo='filho_doc' AND status!='aprovado'", pid):
                m_ = re.match(r"Filho (\d+) –", x["item"])
                if m_ and int(m_.group(1)) > n:
                    b.exec("DELETE FROM itens WHERE id=?", x["id"])
            for f in range(1, n + 1):
                for d in cl["documentos_filhos"]:
                    b.exec("INSERT OR IGNORE INTO itens (pessoa_id, item, grupo) VALUES (?,?,?)",
                           pid, f"Filho {f} – {d}", "filho_doc")
        b.exec("UPDATE itens SET status='aprovado', valor=?, atualizado_em=? WHERE id=?", valor, agora.isoformat(), it["id"])
        feito.append(f"dado {k}")
    itens = {i["item"]: i for i in b.itens(pid)}

    # documentos recebidos → fila de aprovação
    for d in res.get("documentos") or []:
        if not isinstance(d, dict):
            continue
        m = ids_validos.get(d.get("msg_id"))
        it = itens.get(d.get("item"))
        it = b.item(it["id"]) if it else None  # estado atual (2ª foto do mesmo item no mesmo turno)
        if not m or not m.get("arquivo") or not it or it["grupo"] == "info" or it["status"] == "aprovado":
            b.log(pid, "documento_ignorado", json.dumps(d, ensure_ascii=False))
            continue
        receber_arquivo(b, it, m["arquivo"], m["id"], agora)
        tipo = {"exame": "guia", "contrato": "contrato"}.get(it["grupo"], "documento")
        criar_acao(b, pid, tipo, {"item_id": it["id"]}, f"{it['item']} (mensagem {m['id']})", agora)
        feito.append(f"documento {it['item']}")

    p = b.pessoa(pid)
    hoje = agora.date()
    clin = _ctx(b, cfg, p)["clinica"]
    if res.get("propor_exame") and p["etapa"] == "exame":
        d = regras.ler_data(str((res["propor_exame"] or {}).get("data", "")), hoje)
        if d and regras.data_exame_valida(cfg, hoje, d, clin):
            aviso = " ⚠️ Esta clínica exige agendamento: agende com a clínica antes de aprovar." if clin and clin.get("agendamento") else ""
            criar_acao(b, pid, "exame", {"data": d.isoformat()}, f"Exame {regras.fmt_data(d)} em {clin['nome'] if clin else '?'}.{aviso}", agora)
            b.atualizar(pid, aguardando="equipe")
            feito.append(f"exame proposto {d}")
        else:
            b.log(pid, "exame_invalido", str(res["propor_exame"]))
    if res.get("compareceu") is False and p["etapa"] == "guia":
        faltas = (p["faltas"] or 0) + 1
        b.atualizar(pid, faltas=faltas, exame_data=None)
        if faltas >= cfg["exame"]["max_faltas"]:
            humano(b, pid, f"faltou ao exame {faltas} vezes")
        else:
            _abrir_etapa(b, cfg, pid, "exame", agora)
            b.atualizar(pid, cobrancas=faltas)  # falta conta como tentativa
            oferecer_exame(b, cfg, b.pessoa(pid), agora, prefixo="Sem problema, vamos remarcar. ")
        feito.append("faltou ao exame")
    if res.get("propor_inicio") and p["etapa"] == "inicio":
        d = regras.ler_data(str((res["propor_inicio"] or {}).get("data", "")), hoje)
        if d and hoje <= d <= hoje + timedelta(days=90):
            criar_acao(b, pid, "inicio", {"data": d.isoformat()}, f"Início {regras.fmt_data(d)}", agora)
            b.atualizar(pid, aguardando="equipe")
            feito.append(f"início proposto {d}")
    if res.get("precisa_humano"):
        humano(b, pid, res.get("motivo_humano") or "o agente pediu uma pessoa")
        feito.append("precisa humano")

    checar_documentos(b, cfg, pid, agora)
    p = b.pessoa(pid)
    if p["etapa"] in ("guia",) and any(i["item"] == ITEM_GUIA and i["status"] in ("pendente", "recusado") for i in b.itens(pid)) \
            and p["marcas"].get("pos_exame"):
        b.atualizar(pid, aguardando="pessoa")
    if p["etapa"] in ("guia", "contrato") and not pendentes(b, pid) and b.um(
            "SELECT id FROM acoes WHERE pessoa_id=? AND status='pendente'", pid):
        b.atualizar(pid, aguardando="equipe")
    return feito


def humano(b: Banco, pid, motivo: str) -> None:
    b.atualizar(pid, status="humano", motivo_humano=motivo[:300])
    b.log(pid, "humano", motivo)


def devolver_robo(b: Banco, pid, agora: datetime) -> None:
    b.atualizar(pid, status="ativo", motivo_humano=None, cobrancas=0, pausa_ate=None, pedido_em=agora.isoformat())
    b.log(pid, "devolvido_robo", "")


# ---------------- vagas e estoque ----------------
def checar_vaga(b: Banco, vid) -> None:
    v = b.um("SELECT * FROM vagas WHERE id=?", vid)
    if not v:
        return
    n = b.um("SELECT COUNT(*) n FROM pessoas WHERE vaga_id=? AND status != 'desistiu'", vid)["n"]
    novo = "fechada" if n >= v["quantidade"] else "aberta"
    if novo != v["status"]:
        b.exec("UPDATE vagas SET status=? WHERE id=?", novo, vid)


def ligar_vaga(b: Banco, pid, vid: int | None) -> str:
    p = b.pessoa(pid)
    antiga = p["vaga_id"]
    if vid:
        v = b.um("SELECT * FROM vagas WHERE id=?", vid)
        if not v:
            return "vaga não existe"
        if v["status"] == "fechada" and antiga != vid:
            return "vaga já está completa"
        b.atualizar(pid, vaga_id=vid, funcao=p["funcao"] or v["funcao"])
        checar_vaga(b, vid)
    else:
        b.atualizar(pid, vaga_id=None)
    if antiga and antiga != vid:
        checar_vaga(b, antiga)
    b.log(pid, "vaga", f"{antiga} → {vid}")
    return "ok"


def estoque(b: Banco, cidade: str = "", funcao: str = "", completo: bool = False) -> list[dict]:
    ps = b.todos("SELECT p.*, c.nome cliente FROM pessoas p LEFT JOIN clientes c ON c.id=p.cliente_id "
                 "WHERE p.vaga_id IS NULL AND p.status != 'desistiu' AND p.cliente_id IS NOT NULL ORDER BY p.nome")
    out = []
    for p in ps:
        if cidade and cidade.lower() not in (p["cidade"] or "").lower():
            continue
        if funcao and funcao.lower() not in (p["funcao"] or "").lower():
            continue
        its = [i for i in b.itens(p["id"]) if i["grupo"] in ("doc", "filho_doc") and not opcional(i)]
        p["docs_ok"] = bool(its) and all(i["status"] == "aprovado" for i in its)
        if completo and not p["docs_ok"]:
            continue
        out.append(p)
    return out


# ---------------- relógio: cobranças, véspera, pós-exame ----------------
def _ts(x) -> datetime | None:
    return datetime.fromisoformat(x) if x else None


def ultimo_dia_de_aviso(cfg: dict, hoje: date, exame: date) -> bool:
    """Hoje é o último dia com janela de envio antes do exame (sexta/sábado para exame na segunda)."""
    if hoje >= exame:
        return False
    d = hoje + timedelta(days=1)
    while d < exame:
        if d.weekday() in cfg["janela_envio"]["dias"]:
            return False
        d += timedelta(days=1)
    return True


def texto_cobranca(b: Banco, p: dict, n: int) -> str:
    nome = p["nome"].split()[0].title()
    if p["etapa"] == "documentos":
        falta = [rotulo(i) for i in pendentes(b, p["id"])]
        lista = "\n".join(f"* {x}" for x in falta[:12])
        return f"Oi, {nome}! Para seguir com a sua admissão, ainda falta:\n{lista}\nPode me mandar por aqui?"
    if p["etapa"] == "exame":
        ops = [date.fromisoformat(x) for x in p["marcas"].get("opcoes_exame", [])]
        o = " ou ".join(regras.fmt_data(d) for d in ops) if ops else "nos próximos dias"
        return f"Oi, {nome}! Ainda preciso agendar o seu exame admissional. Você pode ir {o}?"
    if p["etapa"] == "guia":
        return f"Oi, {nome}! Conseguiu fazer o exame? Me manda a foto da guia do exame, por favor."
    if p["etapa"] == "contrato":
        return f"Oi, {nome}! Ainda falta me mandar o contrato assinado (foto ou PDF de todas as páginas)."
    if p["etapa"] == "inicio":
        return f"Oi, {nome}! Qual a data em que você pode começar na vaga?"
    return f"Oi, {nome}! Ainda preciso da sua resposta para seguir com a admissão."


def tick(b: Banco, cfg: dict, agora: datetime) -> list[str]:
    feito = []
    if not regras.na_janela(cfg, agora):
        return feito  # fora da janela: tudo fica adiado para a próxima abertura
    h = timedelta(hours=cfg["cobranca"]["intervalo_h"])
    for p in b.todos("SELECT id FROM pessoas WHERE status='ativo'"):
        p = b.pessoa(p["id"])
        pid = p["id"]
        if p["pausa_ate"] and _ts(p["pausa_ate"]) > agora:
            continue
        # véspera e pós-exame
        if p["etapa"] == "guia" and p["exame_data"] and not p["marcas"].get("pos_exame"):
            d = date.fromisoformat(p["exame_data"])
            hv = regras._hm(cfg["exame"]["hora_vespera"])
            hp = regras._hm(cfg["exame"]["hora_pos_exame"])
            clin = _ctx(b, cfg, p)["clinica"]
            if not p["marcas"].get("vespera") and ultimo_dia_de_aviso(cfg, agora.date(), d) and agora.time() >= hv:
                b.marcar(pid, vespera=True)
                quando = "amanhã" if agora.date() == d - timedelta(days=1) else regras.fmt_data(d)
                txt = (f"Olá! Passando para confirmar seu exame admissional {quando}, {janela(clin)}.\n"
                       f"📌 Local do exame: {clin['endereco']}.\n⚠️ Importante: {clin['orientacoes']}") if clin else \
                      f"Olá! Passando para confirmar seu exame admissional {quando}."
                enviar(b, cfg, p, txt, agora, pedido=False)
                feito.append(f"vespera {pid}")
            elif not p["marcas"].get("pos_exame") and agora.date() >= d and agora.time() >= hp:
                b.marcar(pid, pos_exame=True)
                guia = next((i for i in b.itens(pid) if i["item"] == ITEM_GUIA), None)
                if guia and guia["status"] in ("recebido", "aprovado"):
                    continue  # a guia já chegou: não pede de novo
                b.atualizar(pid, aguardando="pessoa", cobrancas=0, ultima_cobranca=None)
                enviar(b, cfg, b.pessoa(pid), "Oi! Conseguiu fazer o exame hoje? Se sim, me manda a foto da guia do exame. "
                       "Se não conseguiu ir, me avisa que a gente remarca.", agora)
                feito.append(f"pos_exame {pid}")
            continue  # antes do pós-exame não há cobrança
        ch = config.chaves(cfg)
        if p["aguardando"] != "pessoa" or not ch.get("cobranca_ligada", True):
            continue
        if not ch.get("envio_ligado") and not ch.get("cobranca_em_seco"):
            continue  # modo seco: a pessoa nunca recebeu nada; cobrança não conta
        base = max(x for x in (_ts(p["pedido_em"]), _ts(p["ultima_msg_pessoa"]), _ts(p["ultima_cobranca"]),
                               _ts(p["criado_em"])) if x)
        if agora < base + h:
            continue
        if p["cobrancas"] >= cfg["cobranca"]["maximo"]:
            humano(b, pid, f"{p['cobrancas']} cobranças sem resposta na etapa {NOME_ETAPA[p['etapa']]}")
            feito.append(f"humano {pid}")
            continue
        n = p["cobrancas"] + 1
        b.atualizar(pid, cobrancas=n, ultima_cobranca=agora.isoformat())
        enviar(b, cfg, b.pessoa(pid), texto_cobranca(b, p, n), agora, pedido=False)
        b.log(pid, "cobranca", str(n))
        feito.append(f"cobranca {pid} #{n}")
    return feito
