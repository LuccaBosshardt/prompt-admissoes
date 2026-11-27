import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="admissoes-teste-")
os.environ["ADMISSOES_DADOS"] = _TMP  # antes de importar painel/ponte (eles carregam a config no import)

from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from admissoes import config  # noqa: E402
from admissoes.banco import Banco  # noqa: E402

SP = ZoneInfo("America/Sao_Paulo")


def em(txt: str) -> datetime:
    return datetime.fromisoformat(txt).replace(tzinfo=SP)


@pytest.fixture
def cfg(tmp_path):
    os.environ["ADMISSOES_DADOS"] = str(tmp_path)
    c = config.carregar()
    c["chaves"]["envio_ligado"] = False
    return c


@pytest.fixture
def b(cfg):
    return Banco(str(Path(cfg["dados_dir"]) / "t.db"))


@pytest.fixture
def aerovip(b):
    return b.um("SELECT id FROM clientes WHERE nome='Aerovip'")["id"]
