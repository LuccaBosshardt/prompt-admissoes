"""Painel da equipe (127.0.0.1:4133). Sem login: tudo mora em /p/<segredo>/... (segredo ≥32 chars em dados_dir).
Qualquer outro caminho → 404. Não indexa (robots + X-Robots-Tag)."""
from __future__ import annotations

import hmac
import json
import time
import shutil
import uuid
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import agente, clinicas, config, gateway, motor, regras, whatsapp
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


def _ic(nome: str, tam: int = 18, cls: str = "") -> Markup:
    return Markup(f'<svg class="ic {cls}" width="{tam}" height="{tam}" aria-hidden="true" focusable="false">'
                  f'<use href="#i-{nome}"/></svg>')


T.env.globals["ic"] = _ic
ESTATICOS = {"logo-prompt.png", "logo-prompt-768.png", "favicon.png"}
STATIC = Path(__file__).parent / "static"
T.env.filters["json"] = lambda x: json.loads(x or "[]")
T.env.filters["dia"] = lambda x: regras.fmt_data(date.fromisoformat(x)) if x else ""

app = FastAPI(title="Prompt Admissões", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _cabecalhos(request: Request, call_next):
    if request.method == "POST":
        from urllib.parse import urlparse
        origem = request.headers.get("origin") or request.headers.get("referer") or ""
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if origem and urlparse(origem).netloc != host:
            return PlainTextResponse("origem recusada", 403)
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
    atual = req.url.path.split(f"/p/{seg}", 1)[-1].strip("/").split("/")[0] or "inicio"
    return T.TemplateResponse(req, nome, {"b": _base(seg), "cont": cont, "msg": req.query_params.get("msg", ""), "atual": atual,
                                          "envio_ligado": config.chaves(CFG).get("envio_ligado"), **ctx})


def _agora():
    return regras.agora(CFG)


@app.get("/p/{seg}/static/{nome}")
def estatico(seg: str, nome: str):
    _ok(seg)
    if nome not in ESTATICOS:
        raise HTTPException(404)
    return FileResponse(STATIC / nome, headers={"Cache-Control": "private, max-age=86400"})


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
                         infos: str = Form(""), clinica_id: str = Form(""), texto_inicial: str = Form(""), contrato_modo: str = Form(""), info_vaga: str = Form(""),
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
    BANCO.exec("UPDATE clientes SET contrato_modo=?, info_vaga=? WHERE id=?", contrato_modo if contrato_modo in ("presencial", "remoto") else "",
               info_vaga.replace("<<<EVENTO", "").replace("EVENTO>>>", "").strip()[:2500], cid)
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


# ---------------- agente e WhatsApp ----------------
SITUACAO = {"CONNECTED": ("conectado", "ok"), "SCAN_QR": ("esperando a leitura do QR", "al"),
            "DISCONNECTED": ("desconectado", "er"), "STOPPED": ("parado", "er"), "LOGGED_OUT": ("número desligado do aparelho", "er"),
            "FAILED": ("falhou", "er"), "sem sessão": ("ainda não conectado", "al")}


def _perfil_hermes() -> dict:
    out = {}
    try:
        for ln in (config.PERFIL_DIR / "config.yaml").read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            for k in ("default", "provider", "base_url", "fallback_providers"):
                if ln.startswith(k + ":"):
                    out.setdefault(k, ln.split(":", 1)[1].strip())
    except OSError:
        out["erro"] = "perfil promptadmissoes não encontrado"
    return out


@app.get("/p/{seg}/agente", response_class=HTMLResponse)
def agente_tela(req: Request, seg: str, teste: str = ""):
    _ok(seg)
    est = gateway.estado(CFG)
    return _r(req, seg, "agente.html", est=est, sit=SITUACAO.get(est["status"], (est["status"], "")),
              chaves=config.chaves(CFG), aj=config.ajustes(CFG), perfil=_perfil_hermes(),
              logs=BANCO.todos("SELECT l.*, p.nome FROM log l LEFT JOIN pessoas p ON p.id=l.pessoa_id WHERE l.tipo IN "
                               "('hermes','erro_hermes','erro_turno','erro_envio','hermes_descartado') ORDER BY l.id DESC LIMIT 12"),
              teste=None)


@app.get("/p/{seg}/agente/qr.json")
def agente_qr(seg: str):
    _ok(seg)
    est = gateway.estado(CFG)
    out = {"status": est["status"], "rotulo": SITUACAO.get(est["status"], (est["status"], ""))[0], "numero": est["numero"],
           "erro": est["erro"]}
    if est["sessao"] and est["status"] != "CONNECTED":
        try:
            out.update(gateway.qr(CFG))
        except gateway.GatewayErro as e:
            out["erro"] = str(e)
    return out


@app.post("/p/{seg}/agente/conectar")
def agente_conectar(seg: str):
    _ok(seg)
    try:
        feito = gateway.conectar(CFG)
    except gateway.GatewayErro as e:
        return _ir(seg, "/agente", f"Não consegui: {e}")
    return _ir(seg, "/agente", "Pronto: " + ", ".join(feito) + ". Leia o QR com o celular do robô.")


@app.post("/p/{seg}/agente/reiniciar")
def agente_reiniciar(seg: str):
    _ok(seg)
    try:
        return _ir(seg, "/agente", gateway.reiniciar(CFG))
    except gateway.GatewayErro as e:
        return _ir(seg, "/agente", f"Não consegui: {e}")


@app.post("/p/{seg}/agente/chaves")
def agente_chaves(seg: str, envio: str = Form(""), cobranca: str = Form("")):
    _ok(seg)
    ch = config.chaves(CFG)
    msg = "Ajuste salvo."
    if envio in ("1", "0"):
        ligar = envio == "1"
        if ligar and not ch.get("envio_ligado"):
            if gateway.estado(CFG)["status"] != "CONNECTED":
                return _ir(seg, "/agente", "Conecte o WhatsApp antes de ligar o envio.")
            # quem foi cadastrado em modo seco não recebe cobrança atrasada em massa
            BANCO.exec("UPDATE pessoas SET pedido_em=?, ultima_cobranca=NULL, cobrancas=0 WHERE status='ativo'", _agora().isoformat())
            msg = "Envio LIGADO: o robô passa a mandar mensagens de verdade. O relógio das cobranças foi zerado."
        elif not ligar:
            msg = "Envio DESLIGADO: modo seco, nada sai no WhatsApp."
        ch["envio_ligado"] = ligar
    if cobranca in ("1", "0"):
        ch["cobranca_ligada"] = cobranca == "1"
        msg = "Cobranças automáticas " + ("ligadas." if ch["cobranca_ligada"] else "desligadas.")
    atual = {}
    try:
        atual = json.loads((Path(CFG["dados_dir"]) / "chaves.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    atual.update({k: ch[k] for k in ("envio_ligado", "cobranca_ligada")})
    config.salvar_json(CFG, "chaves.json", atual)
    BANCO.log(None, "chaves", json.dumps(config.chaves(CFG)))
    return _ir(seg, "/agente", msg)


@app.post("/p/{seg}/agente/ajustes")
def agente_ajustes(seg: str, assinatura: str = Form(""), instrucoes_extra: str = Form("")):
    _ok(seg)
    assinatura = " ".join(assinatura.split())[:80]
    if not assinatura:
        return _ir(seg, "/agente", "A assinatura não pode ficar vazia.")
    extra = instrucoes_extra.replace("<<<EVENTO", "").replace("EVENTO>>>", "").strip()[:1500]
    config.salvar_json(CFG, "ajustes.json", {"assinatura": assinatura, "instrucoes_extra": extra})
    BANCO.log(None, "ajustes_agente", f"assinatura={assinatura}; orientacoes={extra[:300]}")
    return _ir(seg, "/agente", "Ajustes do agente salvos. Valem na próxima mensagem.")


_ULTIMO_TESTE = 0.0


@app.post("/p/{seg}/agente/testar", response_class=HTMLResponse)
def agente_testar(req: Request, seg: str, texto: str = Form(...), etapa: str = Form("documentos")):
    """Conversa de teste com o Hermes real: candidato fictício, nada é gravado nem enviado."""
    _ok(seg)
    global _ULTIMO_TESTE
    if time.time() - _ULTIMO_TESTE < 10:
        return _ir(seg, "/agente", "Espere 10 segundos entre um teste e outro.")
    _ULTIMO_TESTE = time.time()
    primeiro = BANCO.um("SELECT id FROM clientes ORDER BY id LIMIT 1")
    if not primeiro:
        return _ir(seg, "/agente", "Cadastre um cliente antes de testar.")
    cl = BANCO.cliente(primeiro["id"])
    agora = _agora()
    etapa = etapa if etapa in motor.ETAPAS else "documentos"
    ev = {"agora": f"{regras.DIAS[agora.weekday()]}, {agora:%d/%m/%Y %H:%M}", "pessoa": {"nome": "Candidato Teste", "funcao": "Auxiliar", "cidade": ""},
          "cliente": cl["nome"], "vaga": None, "etapa": etapa,
          "itens": [{"item": d, "tipo": "doc", "descricao": d, "status": "pendente", "motivo_recusa": None} for d in cl["documentos"]]
          + [{"item": i["chave"], "tipo": "info", "descricao": i["rotulo"], "status": "pendente", "motivo_recusa": None} for i in cl["infos"]],
          "infos_aceitas": [i["chave"] for i in cl["infos"]],
          "opcoes_exame": [d.isoformat() for d in regras.opcoes_exame(CFG, agora.date(), None)] if etapa == "exame" else [],
          "clinica_do_exame": None, "exame": None, "aguardando_equipe": [], "historico": [],
          "novas_mensagens": [{"msg_id": 1, "tipo": "TEXT", "texto": texto[:1000], "anexo": False}]}
    try:
        res = agente.responder(ev, CFG)
        teste = {"pergunta": texto, "etapa": etapa, "mensagens": motor._msgs_limpas(res.get("mensagens")),
                 "json": json.dumps(res, ensure_ascii=False, indent=1)}
    except agente.HermesFalhou as e:
        teste = {"pergunta": texto, "etapa": etapa, "erro": str(e)}
    est = gateway.estado(CFG)
    return _r(req, seg, "agente.html", est=est, sit=SITUACAO.get(est["status"], (est["status"], "")),
              chaves=config.chaves(CFG), aj=config.ajustes(CFG), perfil=_perfil_hermes(),
              logs=BANCO.todos("SELECT l.*, p.nome FROM log l LEFT JOIN pessoas p ON p.id=l.pessoa_id WHERE l.tipo IN "
                               "('hermes','erro_hermes','erro_turno','erro_envio','hermes_descartado') ORDER BY l.id DESC LIMIT 12"),
              teste=teste)


# ---------------- tela WhatsApp ----------------
@app.get("/p/{seg}/whatsapp", response_class=HTMLResponse)
def whatsapp_tela(req: Request, seg: str):
    _ok(seg)
    est = gateway.estado(CFG)
    return _r(req, seg, "whatsapp.html", est=est)


def _pessoas_por_tel() -> dict:
    return {motor.chave_tel(p["telefone"]): p for p in BANCO.todos(
        "SELECT p.id, p.nome, p.telefone, p.etapa, p.status, c.nome cliente FROM pessoas p LEFT JOIN clientes c ON c.id=p.cliente_id "
        "WHERE p.status!='desistiu'")}


@app.get("/p/{seg}/whatsapp/conversas.json")
def whatsapp_conversas(seg: str):
    _ok(seg)
    try:
        lista = gateway.conversas(CFG)
    except gateway.GatewayErro as e:
        return JSONResponse({"erro": str(e)}, 503)
    idx = _pessoas_por_tel()
    ligados = {r["jid"]: r for r in BANCO.todos("SELECT * FROM wa_contatos")}
    for c in lista:
        lg = ligados.get(c["jid"])
        if lg and lg["apelido"]:
            c["nome"] = lg["apelido"]
        c["sem_nome"] = c["nome"] == c["jid"].split("@")[0] and not (lg and lg["apelido"])
        p = idx.get(motor.chave_tel(c["jid"].split("@")[0])) if c["jid"].endswith("@s.whatsapp.net") else None
        if lg and lg["pessoa_id"]:
            q = BANCO.um("SELECT p.id, p.etapa, c.nome cliente FROM pessoas p LEFT JOIN clientes c ON c.id=p.cliente_id WHERE p.id=?", lg["pessoa_id"])
            p = q or p
        c["pessoa"] = {"id": p["id"], "etapa": motor.NOME_ETAPA.get(p["etapa"], p["etapa"]), "cliente": p["cliente"]} if p else None
    return {"conversas": lista}


@app.get("/p/{seg}/whatsapp/pessoas.json")
def whatsapp_pessoas(seg: str):
    _ok(seg)
    return {"pessoas": BANCO.todos("SELECT p.id, p.nome, c.nome cliente FROM pessoas p LEFT JOIN clientes c ON c.id=p.cliente_id "
                                   "WHERE p.status!='desistiu' AND p.cliente_id IS NOT NULL ORDER BY p.nome")}


@app.post("/p/{seg}/whatsapp/contato")
def whatsapp_contato(seg: str, jid: str = Form(...), apelido: str = Form(""), pessoa_id: str = Form("")):
    _ok(seg)
    apelido = " ".join(apelido.split())[:60]
    pid = int(pessoa_id) if pessoa_id.isdigit() and BANCO.um("SELECT id FROM pessoas WHERE id=?", int(pessoa_id)) else None
    if pid and not apelido:
        apelido = BANCO.um("SELECT nome FROM pessoas WHERE id=?", pid)["nome"]
    BANCO.exec("INSERT INTO wa_contatos (jid, apelido, pessoa_id, em) VALUES (?,?,?,?) ON CONFLICT(jid) DO UPDATE SET "
               "apelido=excluded.apelido, pessoa_id=excluded.pessoa_id, em=excluded.em", jid[:120], apelido, pid, _agora().isoformat())
    return {"ok": True, "apelido": apelido, "pessoa_id": pid}


@app.get("/p/{seg}/whatsapp/mensagens.json")
def whatsapp_mensagens(seg: str, jid: str):
    _ok(seg)
    try:
        return {"mensagens": gateway.mensagens(CFG, jid)}
    except gateway.GatewayErro as e:
        return JSONResponse({"erro": str(e)}, 503)


@app.get("/p/{seg}/whatsapp/midia/{nome}")
def whatsapp_midia(seg: str, nome: str):
    _ok(seg)
    try:
        corpo, tipo = gateway.midia(CFG, nome)
    except gateway.GatewayErro:
        raise HTTPException(404)
    return Response(corpo, media_type=tipo, headers={"Cache-Control": "private, max-age=3600"})


@app.post("/p/{seg}/whatsapp/enviar")
def whatsapp_enviar(seg: str, jid: str = Form(...), texto: str = Form(...)):
    _ok(seg)
    texto = texto.strip()
    if not texto or len(texto) > 4000:
        return JSONResponse({"erro": "mensagem vazia ou longa demais"}, 400)
    if not (jid.endswith("@s.whatsapp.net") or jid.endswith("@g.us") or jid.endswith("@lid")):
        return JSONResponse({"erro": "conversa inválida"}, 400)
    try:
        kid = gateway.enviar_humano(CFG, jid, texto)
    except gateway.GatewayErro as e:
        return JSONResponse({"erro": str(e)}, 503)
    BANCO.log(None, "whatsapp_humano", f"{jid.split('@')[0][-4:]}: {texto[:120]}")
    return {"ok": True, "id": kid}


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
