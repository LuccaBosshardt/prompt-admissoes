"""Config: config.json (repo, sem segredo) + dados_dir/chaves.json (fora do repo: envio_ligado etc.)."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("ADMISSOES_CONFIG", RAIZ / "config.json"))
PERFIL_DIR = Path(os.environ.get("ADMISSOES_PERFIL_DIR", "/root/.hermes/profiles/promptadmissoes"))

CHAVES_PADRAO = {"envio_ligado": False, "cobranca_ligada": True}


def carregar() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if os.environ.get("ADMISSOES_DADOS"):
        cfg["dados_dir"] = os.environ["ADMISSOES_DADOS"]
    d = Path(cfg["dados_dir"])
    (d / "arquivos").mkdir(parents=True, exist_ok=True)
    chaves = dict(CHAVES_PADRAO)
    cp = d / "chaves.json"
    if cp.exists():
        chaves.update(json.loads(cp.read_text(encoding="utf-8")))
    else:
        cp.write_text(json.dumps(chaves, indent=1), encoding="utf-8")
    cfg["chaves"] = chaves
    return cfg


def chaves(cfg: dict) -> dict:
    """Relê dados_dir/chaves.json (liga/desliga vale na hora, no painel e na ponte)."""
    out = dict(CHAVES_PADRAO)
    cp = Path(cfg["dados_dir"]) / "chaves.json"
    try:
        out.update(json.loads(cp.read_text(encoding="utf-8")))  # o arquivo é a fonte da verdade
    except (OSError, ValueError):
        out.update(cfg.get("chaves") or {})  # arquivo ilegível: fica o último valor lido
    cfg["chaves"] = out
    return out


def segredo(cfg: dict, nome: str) -> str:
    """Lê (ou cria, ≥32 chars aleatórios, chmod 600) um segredo em dados_dir. Nunca imprime."""
    p = Path(cfg["dados_dir"]) / f"{nome}.txt"
    if not p.exists():
        old = os.umask(0o077)
        try:
            p.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        finally:
            os.umask(old)
    return p.read_text(encoding="utf-8").strip()


def ler_chave(arquivo: str, nome: str) -> str | None:
    """Lê `NOME=valor` de um arquivo de credenciais. Nunca imprime."""
    try:
        for linha in Path(arquivo).read_text(encoding="utf-8").splitlines():
            k, _, v = linha.partition("=")
            if v and k.strip().lower() == nome.lower():
                return v.strip().strip('"').strip("'")
    except OSError:
        return None
    return None
