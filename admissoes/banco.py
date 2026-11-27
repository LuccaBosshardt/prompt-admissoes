"""SQLite do Prompt Admissões. Uma conexão por chamada (painel e ponte são processos separados; WAL)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS clinicas (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL, cidade TEXT, endereco TEXT,
  dias TEXT DEFAULT '0,1,2,3,4', inicio TEXT DEFAULT '08:00', fim TEXT DEFAULT '11:30',
  orientacoes TEXT DEFAULT 'Leve seu RG (documento de identidade).', clientes TEXT DEFAULT '',
  origem TEXT DEFAULT 'padrao', chave TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS clientes (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL UNIQUE,
  documentos TEXT NOT NULL DEFAULT '[]', documentos_filhos TEXT NOT NULL DEFAULT '[]',
  infos TEXT NOT NULL DEFAULT '[]', clinica_id INTEGER, textos TEXT NOT NULL DEFAULT '{}',
  contrato_pdf TEXT, ativo INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS vagas (
  id INTEGER PRIMARY KEY, cliente_id INTEGER NOT NULL, funcao TEXT NOT NULL, cidade TEXT,
  quantidade INTEGER NOT NULL DEFAULT 1, data_inicio TEXT, parceiro TEXT, obs TEXT,
  status TEXT NOT NULL DEFAULT 'aberta', criado_em TEXT);
CREATE TABLE IF NOT EXISTS pessoas (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL, telefone TEXT NOT NULL, jid TEXT,
  cliente_id INTEGER, vaga_id INTEGER, funcao TEXT, cidade TEXT,
  etapa TEXT NOT NULL DEFAULT 'documentos', status TEXT NOT NULL DEFAULT 'ativo',
  aguardando TEXT NOT NULL DEFAULT 'pessoa', pedido_em TEXT, ultima_msg_pessoa TEXT,
  cobrancas INTEGER NOT NULL DEFAULT 0, ultima_cobranca TEXT, faltas INTEGER NOT NULL DEFAULT 0,
  exame_data TEXT, clinica_id INTEGER, contrato_modo TEXT, data_inicio TEXT,
  marcas TEXT NOT NULL DEFAULT '{}', pausa_ate TEXT, motivo_humano TEXT,
  criado_em TEXT, atualizado_em TEXT);
CREATE TABLE IF NOT EXISTS itens (
  id INTEGER PRIMARY KEY, pessoa_id INTEGER NOT NULL, item TEXT NOT NULL, grupo TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pendente', valor TEXT, arquivo TEXT, msg_id INTEGER, obs TEXT,
  atualizado_em TEXT, UNIQUE(pessoa_id, item));
CREATE TABLE IF NOT EXISTS mensagens (
  id INTEGER PRIMARY KEY, pessoa_id INTEGER, telefone TEXT, quem TEXT NOT NULL, texto TEXT,
  tipo TEXT DEFAULT 'TEXT', arquivo TEXT, key_id TEXT UNIQUE, processada INTEGER DEFAULT 0, em TEXT);
CREATE TABLE IF NOT EXISTS acoes (
  id INTEGER PRIMARY KEY, pessoa_id INTEGER NOT NULL, tipo TEXT NOT NULL, payload TEXT DEFAULT '{}',
  resumo TEXT, status TEXT NOT NULL DEFAULT 'pendente', motivo TEXT, criado_em TEXT, decidido_em TEXT);
CREATE TABLE IF NOT EXISTS saida (
  id INTEGER PRIMARY KEY, pessoa_id INTEGER, jid TEXT, texto TEXT, arquivo TEXT,
  enviado INTEGER NOT NULL DEFAULT 0, key_id TEXT, erro TEXT, em TEXT);
CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY, pessoa_id INTEGER, tipo TEXT, detalhe TEXT, em TEXT);
"""

# colunas acrescentadas depois da V1 (ALTER idempotente)
MIGRACOES = [
    "ALTER TABLE clinicas ADD COLUMN agendamento INTEGER DEFAULT 0",
    "ALTER TABLE clinicas ADD COLUMN obs_interna TEXT DEFAULT ''",
    "ALTER TABLE clinicas ADD COLUMN horario_texto TEXT DEFAULT ''",
    "ALTER TABLE clientes ADD COLUMN contrato_modo TEXT DEFAULT ''",
    "ALTER TABLE itens ADD COLUMN arquivos_extra TEXT DEFAULT '[]'",
    "ALTER TABLE pessoas ADD COLUMN msg_nova INTEGER DEFAULT 0",
]

DOCS_AEROVIP = ["RG ou CNH", "CPF", "PIS", "Carteira de Trabalho (frente e verso ou digital)",
                "Comprovante de residência"]
DOCS_FILHOS_AEROVIP = ["Certidão de nascimento ou RG", "CPF", "Carteira de vacinação",
                       "Declaração escolar (se já estuda)"]
INFOS_AEROVIP = [
    {"chave": "conta_bancaria", "rotulo": "Dados da conta bancária (banco, agência, conta, corrente ou poupança)"},
    {"chave": "pix", "rotulo": "Chave PIX"},
    {"chave": "vt_ida", "rotulo": "Condução da IDA (cada transporte e valor)"},
    {"chave": "vt_volta", "rotulo": "Condução da VOLTA (cada transporte e valor)"},
    {"chave": "cor_raca", "rotulo": "Cor/raça"},
    {"chave": "email", "rotulo": "E-mail"},
    {"chave": "camiseta", "rotulo": "Tamanho da camiseta"},
    {"chave": "calca", "rotulo": "Tamanho da calça"},
    {"chave": "bota", "rotulo": "Tamanho da bota"},
    {"chave": "filhos_menores_14", "rotulo": "Quantos filhos menores de 14 anos (0 se não tem)"},
]
TEXTO_INICIAL_AEROVIP = """Olá, {nome}, tudo bem?
Parabéns! Você foi aprovado(a) para a vaga de {funcao}. Aqui é o {assinatura} e vou dar continuidade ao seu processo de admissão.
Para darmos andamento, preciso que você me envie por aqui os seguintes documentos e informações:

DOCUMENTOS PARA ADMISSÃO:
{documentos}

PARA FILHOS MENORES DE 14 ANOS (de cada filho):
{documentos_filhos}

INFORMAÇÕES NECESSÁRIAS:
{infos}

Sobre a condução: informe o valor de cada condução da ida e da volta, cada transporte separado.
Exemplo: Ida: Ônibus R$ 5,30 + Metrô R$ 5,40. Volta: Metrô R$ 5,40 + Ônibus R$ 5,30.

Também vou agendar o seu exame médico admissional e, em breve, envio as orientações.
Fico no aguardo dos documentos e das informações. Qualquer dúvida, estou à disposição!"""


def agora_txt() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Banco:
    def __init__(self, caminho: str):
        self.caminho = caminho
        Path(caminho).parent.mkdir(parents=True, exist_ok=True)
        with self.con() as c:
            c.executescript(ESQUEMA)
            for m in MIGRACOES:
                try:
                    c.execute(m)
                except sqlite3.OperationalError:
                    pass  # coluna já existe
        self.semear()

    @contextmanager
    def con(self):
        c = sqlite3.connect(self.caminho, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---------- utilitários ----------
    def um(self, sql: str, *a) -> dict | None:
        with self.con() as c:
            r = c.execute(sql, a).fetchone()
            return dict(r) if r else None

    def todos(self, sql: str, *a) -> list[dict]:
        with self.con() as c:
            return [dict(r) for r in c.execute(sql, a).fetchall()]

    def exec(self, sql: str, *a) -> int:
        with self.con() as c:
            cur = c.execute(sql, a)
            return cur.lastrowid

    def log(self, pessoa_id, tipo: str, detalhe: str = "") -> None:
        self.exec("INSERT INTO log (pessoa_id, tipo, detalhe, em) VALUES (?,?,?,?)",
                  pessoa_id, tipo, str(detalhe)[:4000], agora_txt())

    # ---------- semente ----------
    def semear(self) -> None:
        if not self.um("SELECT id FROM clinicas WHERE chave='padrao-aerovip'"):
            self.exec("INSERT INTO clinicas (nome, cidade, endereco, dias, inicio, fim, orientacoes, clientes, origem, chave)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?)",
                      "PRESET (Guarulhos)", "Guarulhos",
                      "Rua Abaiara, 194 – Cidade Jardim Cumbica – Guarulhos/SP", "0,1,2,3,4", "08:00", "12:00",
                      "Não esqueça de levar seu RG (documento de identidade). Ao chegar ao local, nos avise por aqui.",
                      "Aerovip", "padrao", "padrao-aerovip")
        if not self.um("SELECT id FROM clientes WHERE nome='Aerovip'"):
            cl = self.um("SELECT id FROM clinicas WHERE chave='padrao-aerovip'")
            self.exec("INSERT INTO clientes (nome, documentos, documentos_filhos, infos, clinica_id, textos) VALUES (?,?,?,?,?,?)",
                      "Aerovip", json.dumps(DOCS_AEROVIP, ensure_ascii=False),
                      json.dumps(DOCS_FILHOS_AEROVIP, ensure_ascii=False),
                      json.dumps(INFOS_AEROVIP, ensure_ascii=False), cl["id"],
                      json.dumps({"inicial": TEXTO_INICIAL_AEROVIP}, ensure_ascii=False))

    # ---------- leitura com JSON ----------
    def cliente(self, cid) -> dict | None:
        c = self.um("SELECT * FROM clientes WHERE id=?", cid)
        if c:
            for k in ("documentos", "documentos_filhos", "infos", "textos"):
                c[k] = json.loads(c[k] or ("{}" if k == "textos" else "[]"))
        return c

    def pessoa(self, pid) -> dict | None:
        p = self.um("SELECT * FROM pessoas WHERE id=?", pid)
        if p:
            p["marcas"] = json.loads(p["marcas"] or "{}")
        return p

    def marcar(self, pid, **kv) -> None:
        p = self.pessoa(pid)
        m = p["marcas"]
        m.update(kv)
        self.atualizar(pid, marcas=json.dumps(m, ensure_ascii=False))

    def atualizar(self, pid, **campos) -> None:
        if not campos:
            return
        campos["atualizado_em"] = agora_txt()
        sets = ", ".join(f"{k}=?" for k in campos)
        self.exec(f"UPDATE pessoas SET {sets} WHERE id=?", *campos.values(), pid)

    def itens(self, pid) -> list[dict]:
        return self.todos("SELECT * FROM itens WHERE pessoa_id=? ORDER BY id", pid)

    def item(self, iid) -> dict | None:
        return self.um("SELECT * FROM itens WHERE id=?", iid)

    def acao(self, aid) -> dict | None:
        a = self.um("SELECT * FROM acoes WHERE id=?", aid)
        if a:
            a["payload"] = json.loads(a["payload"] or "{}")
        return a
