"""Lista de clínicas: planilha Google (via CLI `gws` da conta do Lucca), lida a cada 10 min.
Colunas (linha 1 = cabeçalho): nome | cidade | endereco | dias (seg,ter,qua,qui,sex,sab) | inicio | fim | orientacoes | clientes
planilha_id vazio ou erro → fica o que já está no banco (no mínimo a clínica padrão da Aerovip).
"""
from __future__ import annotations

import json
import subprocess

from .banco import Banco

DIA = {"seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "sáb": 5, "dom": 6}


def _dias(txt: str) -> str:
    out = [str(DIA[x.strip().lower()[:3]]) for x in (txt or "").replace(";", ",").split(",")
           if x.strip().lower()[:3] in DIA]
    return ",".join(out) or "0,1,2,3,4"


def ler_planilha(planilha_id: str, faixa: str) -> list[list[str]]:
    r = subprocess.run(["gws", "sheets", "spreadsheets", "values", "get", "--params",
                        json.dumps({"spreadsheetId": planilha_id, "range": faixa})],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "")[:300])
    return json.loads(r.stdout[r.stdout.find("{"):]).get("values", [])


def sincronizar(b: Banco, cfg: dict, ler=ler_planilha) -> str:
    c = cfg["clinicas"]
    if not c.get("planilha_id"):
        return "sem planilha: usando clínicas do banco (padrão Aerovip)"
    try:
        linhas = ler(c["planilha_id"], c["aba"])
    except Exception as e:  # noqa: BLE001
        b.log(None, "erro_clinicas", repr(e))
        return "erro na planilha: mantidas as clínicas do banco"
    n = 0
    for ln in linhas[1:]:
        ln = (ln + [""] * 8)[:8]
        nome, cidade, end, dias, ini, fim, ori, clis = [x.strip() for x in ln]
        if not nome or not end:
            continue
        chave = f"planilha:{nome.lower()}|{cidade.lower()}"
        dados = (nome, cidade, end, _dias(dias), ini or "08:00", fim or "11:30",
                 ori or "Leve seu RG (documento de identidade).", clis, "planilha")
        if b.um("SELECT id FROM clinicas WHERE chave=?", chave):
            b.exec("UPDATE clinicas SET nome=?, cidade=?, endereco=?, dias=?, inicio=?, fim=?, orientacoes=?, clientes=?, "
                   "origem=? WHERE chave=?", *dados, chave)
        else:
            b.exec("INSERT INTO clinicas (nome, cidade, endereco, dias, inicio, fim, orientacoes, clientes, origem, chave) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)", *dados, chave)
        n += 1
    b.log(None, "clinicas", f"{n} clínicas lidas da planilha")
    return f"{n} clínicas"
