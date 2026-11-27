"""Painel da equipe (127.0.0.1:4133). Sem login: tudo mora em /p/<segredo>/... (segredo ≥32 chars em dados_dir).
Qualquer outro caminho → 404. Não indexa (robots + X-Robots-Tag)."""
from __future__ import annotations

import hmac
import json
import shutil
import uuid
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import clinicas, config, motor, regras, whatsapp
from .banco import Banco

CFG = config.carregar()
BANCO = Banco(str(Path(CFG["dados_dir"]) / "admissoes.db"))
SEGREDO = config.segredo(CFG, "painel-segredo")
ARQ = Path(CFG["dados_dir"]) / "arquivos"
T = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
T.env.globals.update(NOME_ETAPA=motor.NOME_ETAPA, TIPOS_ACAO=motor.TIPOS_ACAO, ETAPAS=motor.ETAPAS)
T.env.filters["dt"] = lambda x: (x or "")[:16].replace("T", " ")
ROTULO = {"aguardando": {"pessoa": "do candidato", "equipe": "da equipe", "exame": "do dia do exame", "nada": "—"},
          "status": {"ativo": "com o robô", "humano": "precisa de humano", "admitida": "admitida", "desistiu": "desistiu"},
          "item": {"pendente": "falta", "recebido": "chegou (aprovar)", "aprovado": "ok", "recusado": "pedido de novo"}}
T.env.globals.update(ROTULO=ROTULO)
T.env.filters["json"] = lambda x: json.loads(x or "[]")
T.env.filters["dia"] = lambda x: regras.fmt_data(date.fromisoformat(x)) if x else ""

app = FastAPI(title="Prompt Admissões", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _cabecalhos(request: Request, call_next):
    r = await call_next(request)
    r.headers["X-Robots-Tag"] = "noindex, nofollow"
    r.headers["Referrer-Policy"] = "no-referrer"
    return r


def _ok(seg: str) -> None:
    if not hmac.compare_digest(seg.encode(), SEGREDO.encode()):
        raise HTTPException(404)


def _base(seg: str) -> str:
    return f"/p/{seg}"


def _ir(seg: str, caminho: str, msg: str = "") -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"{_base(seg)}{caminho}" + (f"{'&' if '?' in caminho else '?'}msg={quote(msg)}" if msg else ""), 303)


def _r(req: Request, seg: str, nome: str, **ctx):
    cont = {
        "aprov": BANCO.um("SELECT COUNT(*) n FROM acoes WHERE status='pendente'")["n"],
        "humano": BANCO.um("SELECT COUNT(*) n FROM pessoas WHERE status='humano' OR msg_nova=1")["n"],
    }
    return T.TemplateResponse(req, nome, {"b": _base(seg), "cont": cont, "msg": req.query_params.get("msg", ""),
                                          "envio_ligado": config.chaves(CFG).get("envio_ligado"), **ctx})


def _agora():
    return regras.agora(CFG)


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


# ---------------- início ----------------
@app.get("/p/{seg}/", response_class=HTMLResponse)
def inicio(req: Request, seg: str):
    _ok(seg)
    por_etapa = {r["etapa"]: r["n"] for r in BANCO.todos(
        "SELECT etapa, COUNT(*) n FROM pessoas WHERE status='ativo' GROUP BY etapa")}
    vagas = BANCO.um("SELECT COUNT(*) n FROM vagas WHERE status='aberta'")["n"]
    estoque = len(motor.estoque(BANCO))
    return _r(req, seg, "inicio.html", por_etapa=por_etapa, vagas=vagas, estoque=estoque)


# ---------------- pessoas ----------------
@app.get("/p/{seg}/pessoas", response_class=HTMLResponse)
def pessoas(req: Request, seg: str, etapa: str = "", status: str = "", q: str = ""):
    _ok(seg)
    sql, a = ("SELECT p.*, c.nome cliente, v.funcao vaga_funcao FROM pessoas p LEFT JOIN clientes c ON c.id=p.cliente_id "
              "LEFT JOIN vagas v ON v.id=p.vaga_id WHERE 1=1"), []
    if etapa:
        sql += " AND p.etapa=?"; a.append(etapa)
    if status:
        sql += " AND p.status=?"; a.append(status)
    if q:
        sql += " AND (p.nome LIKE ? OR p.telefone LIKE ?)"; a += [f"%{q}%", f"%{q}%"]
    lista = BANCO.todos(sql + " ORDER BY p.atualizado_em DESC LIMIT 500", *a)
    return _r(req, seg, "pessoas.html", lista=lista, clientes=BANCO.todos("SELECT * FROM clientes WHERE ativo=1 ORDER BY nome"),
              vagas=BANCO.todos("SELECT v.*, c.nome cliente FROM vagas v JOIN clientes c ON c.id=v.cliente_id "
                                "WHERE v.status='aberta' ORDER BY v.id DESC"), f={"etapa": etapa, "status": status, "q": q})


@app.post("/p/{seg}/pessoas")
def pessoas_nova(seg: str, nome: str = Form(...), telefone: str = Form(...), cliente_id: int = Form(...),
                 vaga_id: str = Form(""), funcao: str = Form(""), cidade: str = Form("")):
    _ok(seg)
    tel = motor.norm_tel(telefone)
    ja = motor.achar_por_tel(BANCO, tel)
    if ja and ja["status"] in ("ativo", "humano") and ja["cliente_id"]:
        return _ir(seg, f"/pessoas/{ja['id']}", "Este telefone já está em admissão.")
    try:
        if ja and not ja["cliente_id"]:  # contato que escreveu primeiro: some e vira cadastro de verdade
            BANCO.exec("UPDATE pessoas SET status='desistiu', motivo_humano='virou cadastro' WHERE id=?", ja["id"])
        p = motor.cadastrar(BANCO, CFG, nome, telefone, cliente_id, _agora(), funcao, cidade, int(vaga_id) if vaga_id else None)
    except ValueError as e:
        return _ir(seg, "/pessoas", f"Erro: {e}")
    return _ir(seg, f"/pessoas/{p['id']}", "Pessoa cadastrada. A mensagem inicial foi para a caixa de saída.")


@app.get("/p/{seg}/pessoas/{pid}", response_class=HTMLResponse)
def ficha(req: Request, seg: str, pid: int):
    _ok(seg)
    p = BANCO.pessoa(pid)
    if not p:
        raise HTTPException(404)
    return _r(req, seg, "ficha.html", p=p, itens=BANCO.itens(pid), cliente=BANCO.cliente(p["cliente_id"]) if p["cliente_id"] else None,
              clientes=BANCO.todos("SELECT * FROM clientes WHERE ativo=1 ORDER BY nome"),
              vaga=BANCO.um("SELECT * FROM vagas WHERE id=?", p["vaga_id"]) if p["vaga_id"] else None,
              vagas=BANCO.todos("SELECT v.*, c.nome cliente FROM vagas v JOIN clientes c ON c.id=v.cliente_id "
                                "WHERE v.status='aberta' OR v.id=? ORDER BY v.id DESC", p["vaga_id"] or 0),
              clinica=BANCO.um("SELECT * FROM clinicas WHERE id=?", p["clinica_id"]) if p["clinica_id"] else None,
              msgs=BANCO.todos("SELECT * FROM mensagens WHERE pessoa_id=? ORDER BY id", pid),
              acoes=BANCO.todos("SELECT * FROM acoes WHERE pessoa_id=? ORDER BY id DESC", pid),
              log=BANCO.todos("SELECT * FROM log WHERE pessoa_id=? ORDER BY id DESC LIMIT 40", pid))


@app.post("/p/{seg}/pessoas/{pid}/acao")
async def ficha_acao(seg: str, pid: int, op: str = Form(...), vaga_id: str = Form(""), texto: str = Form(""),
                     item_id: str = Form(""), arquivo: UploadFile | None = File(None)):
    _ok(seg)
    p, agora = BANCO.pessoa(pid), _agora()
    if not p:
        raise HTTPException(404)
    msg = "Feito."
    if op == "vaga":
        msg = motor.ligar_vaga(BANCO, pid, int(vaga_id) if vaga_id else None)
    elif op == "humano":
        motor.humano(BANCO, pid, texto or "a equipe assumiu pelo painel")
    elif op == "robo":
        if not p["cliente_id"]:
            msg = "Cadastre a pessoa com um cliente antes de devolver ao robô."
        else:
            motor.devolver_robo(BANCO, pid, agora)
            BANCO.atualizar(pid, msg_nova=0)
    elif op == "desistiu":
        BANCO.atualizar(pid, status="desistiu", motivo_humano=texto or "desistiu")
        if p["vaga_id"]:
            motor.checar_vaga(BANCO, p["vaga_id"])
    elif op == "lida":
        BANCO.atualizar(pid, msg_nova=0)
    elif op == "usar_anexo" and item_id and texto:
        msg = motor.usar_anexo(BANCO, CFG, pid, int(texto), int(item_id), agora)
        msg = "Anexo usado e item aprovado." if msg == "ok" else msg
    elif op == "mensagem" and texto.strip():
        BANCO.atualizar(pid, msg_nova=0)
        whatsapp.enviar(CFG, BANCO, p, texto.strip())
        BANCO.exec("UPDATE mensagens SET quem='humano' WHERE id=(SELECT MAX(id) FROM mensagens WHERE pessoa_id=?)", pid)
        msg = "Mensagem na caixa de saída." if not config.chaves(CFG).get("envio_ligado") else "Mensagem enviada."
    elif op in ("item_ok", "item_arquivo") and item_id:
        caminho = None
        if arquivo is not None and arquivo.filename:
            caminho = str(ARQ / f"{uuid.uuid4().hex}{Path(arquivo.filename).suffix.lower()[:6]}")
            with open(caminho, "wb") as f:
                shutil.copyfileobj(arquivo.file, f)
        it = BANCO.item(int(item_id))
        if not it or it["pessoa_id"] != pid:
            raise HTTPException(404)
        if it["grupo"] in ("exame", "contrato"):  # guia / contrato: avança a etapa pelo motor
            if caminho:
                motor.receber_arquivo(BANCO, it, caminho, None, agora)
            elif not it["arquivo"]:
                return _ir(seg, f"/pessoas/{pid}", "Suba o arquivo da guia ou do contrato para aprovar.")
            tipo = "guia" if it["grupo"] == "exame" else "contrato"
            aid = motor.criar_acao(BANCO, pid, tipo, {"item_id": it["id"]}, f"{it['item']} (aprovado na ficha)", agora)
            r = motor.aprovar(BANCO, CFG, aid, agora)
            return _ir(seg, f"/pessoas/{pid}", "Aprovado e etapa avançada." if r == "ok" else r)
        BANCO.exec("UPDATE itens SET status='aprovado', arquivo=COALESCE(?, arquivo), atualizado_em=? WHERE id=?",
                   caminho, agora.isoformat(), it["id"])
        BANCO.exec("UPDATE acoes SET status='aprovada', motivo='aprovado na ficha', decidido_em=? WHERE pessoa_id=? "
                   "AND status='pendente' AND payload=?", agora.isoformat(), pid, json.dumps({"item_id": it["id"]}))
        motor.checar_documentos(BANCO, CFG, pid, agora)
        msg = f"\"{motor.rotulo(it)}\" aprovado pela equipe."
    elif op == "contrato_presencial":
        caminho = None
        if arquivo is not None and arquivo.filename:
            caminho = str(ARQ / f"{uuid.uuid4().hex}{Path(arquivo.filename).suffix.lower()[:6]}")
            with open(caminho, "wb") as f:
                shutil.copyfileobj(arquivo.file, f)
        motor.contrato_presencial_assinado(BANCO, CFG, pid, caminho, agora)
        msg = "Contrato presencial registrado. O robô vai pedir a data de início."
    return _ir(seg, f"/pessoas/{pid}", msg)


@app.get("/p/{seg}/arquivo/extra/{oid}/{n}")
def arquivo_extra(seg: str, oid: int, n: int):
    _ok(seg)
    it = BANCO.item(oid)
    extra = json.loads((it or {}).get("arquivos_extra") or "[]")
    if not it or n >= len(extra):
        raise HTTPException(404)
    p = Path(extra[n]).resolve()
    if ARQ.resolve() not in p.parents or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/p/{seg}/arquivo/{tipo}/{oid}")
def arquivo(seg: str, tipo: str, oid: int):
    _ok(seg)
    if tipo == "extra":  # /arquivo/extra/<item_id>?n=0
        raise HTTPException(404)
    r = BANCO.um("SELECT arquivo FROM itens WHERE id=?", oid) if tipo == "item" else \
        BANCO.um("SELECT arquivo FROM mensagens WHERE id=?", oid) if tipo == "msg" else \
        BANCO.um("SELECT contrato_pdf arquivo FROM clientes WHERE id=?", oid) if tipo == "contrato" else None
    if not r or not r["arquivo"]:
        raise HTTPException(404)
    p = Path(r["arquivo"]).resolve()
    if ARQ.resolve() not in p.parents or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ---------------- aprovações e humano ----------------
@app.get("/p/{seg}/aprovacoes", response_class=HTMLResponse)
def aprovacoes(req: Request, seg: str):
    _ok(seg)
    lista = BANCO.todos("SELECT a.*, p.nome, p.etapa, c.nome cliente FROM acoes a JOIN pessoas p ON p.id=a.pessoa_id "
                        "LEFT JOIN clientes c ON c.id=p.cliente_id WHERE a.status='pendente' ORDER BY a.id")
    for a in lista:
        a["payload"] = json.loads(a["payload"] or "{}")
        a["item"] = BANCO.item(a["payload"]["item_id"]) if "item_id" in a["payload"] else None
    feitas = BANCO.todos("SELECT a.*, p.nome FROM acoes a JOIN pessoas p ON p.id=a.pessoa_id WHERE a.status!='pendente' "
                         "ORDER BY a.decidido_em DESC LIMIT 20")
    return _r(req, seg, "aprovacoes.html", lista=lista, feitas=feitas)


@app.post("/p/{seg}/aprovacoes/{aid}")
def decidir(seg: str, aid: int, decisao: str = Form(...), motivo: str = Form(""), modo: str = Form(""), volta: str = Form("")):
    _ok(seg)
    if decisao == "aprovar":
        r = motor.aprovar(BANCO, CFG, aid, _agora(), modo or None)
    else:
        r = motor.recusar(BANCO, CFG, aid, motivo, _agora())
    destino = volta if volta.startswith("/pessoas/") else "/aprovacoes"
    return _ir(seg, destino, "Aprovado." if r == "ok" and decisao == "aprovar" else "Recusado." if r == "ok" else r)


@app.get("/p/{seg}/humano", response_class=HTMLResponse)
def fila_humano(req: Request, seg: str):
    _ok(seg)
    lista = BANCO.todos("SELECT p.*, c.nome cliente FROM pessoas p LEFT JOIN clientes c ON c.id=p.cliente_id "
                        "WHERE p.status='humano' OR p.msg_nova=1 ORDER BY p.atualizado_em DESC")
    return _r(req, seg, "humano.html", lista=lista)


# ---------------- vagas e estoque ----------------
@app.get("/p/{seg}/vagas", response_class=HTMLResponse)
def vagas(req: Request, seg: str, status: str = "aberta"):
    _ok(seg)
    lista = BANCO.todos("SELECT v.*, c.nome cliente, (SELECT COUNT(*) FROM pessoas p WHERE p.vaga_id=v.id AND p.status!='desistiu') ocupadas "
                        "FROM vagas v JOIN clientes c ON c.id=v.cliente_id WHERE (?='' OR v.status=?) ORDER BY v.id DESC", status, status)
    for v in lista:
        v["pessoas"] = BANCO.todos("SELECT id, nome, etapa, status FROM pessoas WHERE vaga_id=? AND status!='desistiu'", v["id"])
    return _r(req, seg, "vagas.html", lista=lista, status=status,
              clientes=BANCO.todos("SELECT * FROM clientes WHERE ativo=1 ORDER BY nome"))


@app.post("/p/{seg}/vagas")
def vagas_nova(seg: str, cliente_id: int = Form(...), funcao: str = Form(...), cidade: str = Form(""),
               quantidade: int = Form(1), data_inicio: str = Form(""), parceiro: str = Form(""), obs: str = Form("")):
    _ok(seg)
    BANCO.exec("INSERT INTO vagas (cliente_id, funcao, cidade, quantidade, data_inicio, parceiro, obs, criado_em) VALUES (?,?,?,?,?,?,?,?)",
               cliente_id, funcao.strip(), cidade.strip(), max(1, quantidade), data_inicio or None, parceiro.strip(), obs.strip(),
               _agora().isoformat())
    return _ir(seg, "/vagas", "Vaga criada.")


@app.post("/p/{seg}/vagas/{vid}/status")
def vaga_status(seg: str, vid: int, status: str = Form(...)):
    _ok(seg)
    if status in ("aberta", "fechada", "cancelada"):
        BANCO.exec("UPDATE vagas SET status=? WHERE id=?", status, vid)
    return _ir(seg, "/vagas", "Vaga atualizada.")


@app.get("/p/{seg}/estoque", response_class=HTMLResponse)
def estoque(req: Request, seg: str, cidade: str = "", funcao: str = "", completo: str = ""):
    _ok(seg)
    lista = motor.estoque(BANCO, cidade, funcao, bool(completo))
    return _r(req, seg, "estoque.html", lista=lista, f={"cidade": cidade, "funcao": funcao, "completo": completo},
              vagas=BANCO.todos("SELECT v.*, c.nome cliente FROM vagas v JOIN clientes c ON c.id=v.cliente_id "
                                "WHERE v.status='aberta' ORDER BY v.id DESC"))


# ---------------- clientes e clínicas ----------------
@app.get("/p/{seg}/clientes", response_class=HTMLResponse)
def clientes(req: Request, seg: str, id: int | None = None):
    _ok(seg)
    lista = [BANCO.cliente(c["id"]) for c in BANCO.todos("SELECT id FROM clientes ORDER BY nome")]
    atual = BANCO.cliente(id) if id else (lista[0] if lista else None)
    return _r(req, seg, "clientes.html", lista=lista, atual=atual, clinicas=BANCO.todos("SELECT * FROM clinicas ORDER BY cidade, nome"),
              planilha=CFG["clinicas"].get("planilha_id"))


@app.post("/p/{seg}/clientes/{cid}")
async def cliente_salvar(seg: str, cid: int, nome: str = Form(...), documentos: str = Form(""), documentos_filhos: str = Form(""),
                         infos: str = Form(""), clinica_id: str = Form(""), texto_inicial: str = Form(""), contrato_modo: str = Form(""),
                         contrato: UploadFile | None = File(None)):
    _ok(seg)
    linhas = lambda t: [x.strip() for x in t.splitlines() if x.strip()]  # noqa: E731
    inf = []
    for ln in linhas(infos):
        chave, _, rot = ln.partition("|")
        if chave.strip():
            inf.append({"chave": chave.strip(), "rotulo": (rot or chave).strip()})
    try:
        motor.texto_inicial(CFG, {"nome": nome, "textos": {"inicial": texto_inicial}, "documentos": [], "documentos_filhos": [],
                                  "infos": inf})
    except ValueError as e:
        return _ir(seg, f"/clientes?id={cid}", f"Não salvei: {e}")
    if not any(i["chave"] == "filhos_menores_14" for i in inf):
        inf.append({"chave": "filhos_menores_14", "rotulo": "Quantos filhos menores de 14 anos (0 se não tem)"})
    BANCO.exec("UPDATE clientes SET nome=?, documentos=?, documentos_filhos=?, infos=?, clinica_id=?, textos=json_set(textos, '$.inicial', ?) WHERE id=?",
               nome.strip(), json.dumps(linhas(documentos), ensure_ascii=False), json.dumps(linhas(documentos_filhos), ensure_ascii=False),
               json.dumps(inf, ensure_ascii=False), int(clinica_id) if clinica_id else None, texto_inicial, cid)
    BANCO.exec("UPDATE clientes SET contrato_modo=? WHERE id=?", contrato_modo if contrato_modo in ("presencial", "remoto") else "", cid)
    if contrato is not None and contrato.filename:
        if not contrato.filename.lower().endswith(".pdf"):
            return _ir(seg, f"/clientes?id={cid}", "O contrato precisa ser PDF.")
        destino = ARQ / f"contrato-cliente-{cid}-{uuid.uuid4().hex[:8]}.pdf"
        with open(destino, "wb") as f:
            shutil.copyfileobj(contrato.file, f)
        BANCO.exec("UPDATE clientes SET contrato_pdf=? WHERE id=?", str(destino), cid)
    return _ir(seg, f"/clientes?id={cid}", "Cliente salvo. Vale para os próximos cadastros.")


@app.post("/p/{seg}/clientes")
def cliente_novo(seg: str, nome: str = Form(...)):
    _ok(seg)
    base = BANCO.um("SELECT * FROM clientes WHERE nome='Aerovip'") or BANCO.um("SELECT * FROM clientes ORDER BY id LIMIT 1")
    if BANCO.um("SELECT id FROM clientes WHERE nome=?", nome.strip()):
        return _ir(seg, "/clientes", "Já existe um cliente com esse nome.")
    cid = BANCO.exec("INSERT INTO clientes (nome, documentos, documentos_filhos, infos, clinica_id, textos) VALUES (?,?,?,?,?,?)",
                     nome.strip(), base["documentos"], base["documentos_filhos"], base["infos"], base["clinica_id"], base["textos"])
    return _ir(seg, f"/clientes?id={cid}", "Cliente criado como cópia da Aerovip. Ajuste as regras.")


@app.post("/p/{seg}/clinicas/sincronizar")
def clinicas_sync(seg: str):
    _ok(seg)
    return _ir(seg, "/clientes", clinicas.sincronizar(BANCO, CFG))


@app.post("/p/{seg}/clinicas")
def clinica_nova(seg: str, nome: str = Form(...), cidade: str = Form(""), endereco: str = Form(...), dias: str = Form("seg,ter,qua,qui,sex"),
                 inicio: str = Form("08:00"), fim: str = Form("11:30"), orientacoes: str = Form("Leve seu RG (documento de identidade).")):
    _ok(seg)
    BANCO.exec("INSERT INTO clinicas (nome, cidade, endereco, dias, inicio, fim, orientacoes, origem, chave) VALUES (?,?,?,?,?,?,?,?,?)",
               nome, cidade, endereco, clinicas._dias(dias), inicio, fim, orientacoes, "painel", f"painel:{uuid.uuid4().hex[:8]}")
    return _ir(seg, "/clientes", "Clínica criada.")


# ---------------- caixa de saída ----------------
@app.get("/p/{seg}/saida", response_class=HTMLResponse)
def saida(req: Request, seg: str):
    _ok(seg)
    lista = BANCO.todos("SELECT s.*, p.nome FROM saida s LEFT JOIN pessoas p ON p.id=s.pessoa_id ORDER BY s.id DESC LIMIT 200")
    return _r(req, seg, "saida.html", lista=lista)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=CFG["painel"]["host"], port=CFG["painel"]["porta"], log_level="warning", proxy_headers=True)


if __name__ == "__main__":
    main()
