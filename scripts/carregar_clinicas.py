"""Carrega/atualiza clínicas e clientes a partir de um JSON (fica em dados_dir, fora do repo: tem PIX/CPF das clínicas).
Uso: .venv/bin/python scripts/carregar_clinicas.py /srv/prompt-admissoes-dados/clinicas-AAAA-MM-DD.json
Formato: {"clinicas":[{chave,nome,cidade,endereco,dias,inicio,fim,horario_texto,orientacoes,clientes,agendamento,obs_interna}],
          "clientes":[{nome, contrato_modo}]}   (cliente novo nasce como cópia da Aerovip)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from admissoes import config  # noqa: E402
from admissoes.banco import Banco  # noqa: E402

cfg = config.carregar()
b = Banco(str(Path(cfg["dados_dir"]) / "admissoes.db"))
d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
CAMPOS = ["nome", "cidade", "endereco", "dias", "inicio", "fim", "horario_texto", "orientacoes", "clientes", "agendamento", "obs_interna"]
for c in d.get("clinicas", []):
    vals = [c.get(k, "") if k != "agendamento" else int(bool(c.get(k))) for k in CAMPOS]
    if b.um("SELECT id FROM clinicas WHERE chave=?", c["chave"]):
        b.exec(f"UPDATE clinicas SET {', '.join(k + '=?' for k in CAMPOS)}, origem='documento' WHERE chave=?", *vals, c["chave"])
        print("atualizada:", c["nome"])
    else:
        b.exec(f"INSERT INTO clinicas ({', '.join(CAMPOS)}, origem, chave) VALUES ({', '.join('?' * len(CAMPOS))}, 'documento', ?)", *vals, c["chave"])
        print("nova:", c["nome"])
base = b.um("SELECT * FROM clientes WHERE nome='Aerovip'")
for cl in d.get("clientes", []):
    if not b.um("SELECT id FROM clientes WHERE nome=?", cl["nome"]):
        b.exec("INSERT INTO clientes (nome, documentos, documentos_filhos, infos, clinica_id, textos) VALUES (?,?,?,?,?,?)",
               cl["nome"], base["documentos"], base["documentos_filhos"], base["infos"], None, base["textos"])
        print("cliente novo:", cl["nome"])
    b.exec("UPDATE clientes SET contrato_modo=? WHERE nome=?", cl.get("contrato_modo", ""), cl["nome"])
