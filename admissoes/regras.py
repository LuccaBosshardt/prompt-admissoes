"""Regras de tempo: fuso SP, janela de envio, dias úteis, datas do exame. Relógio sempre injetado (agora=...)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DIAS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def fuso(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("fuso", "America/Sao_Paulo"))


def agora(cfg: dict) -> datetime:
    return datetime.now(fuso(cfg))


def _hm(txt: str) -> time:
    h, m = txt.split(":")
    return time(int(h), int(m))


def na_janela(cfg: dict, quando: datetime) -> bool:
    """Seg–sáb, inicio ≤ hora < fim, no fuso de SP."""
    j = cfg["janela_envio"]
    q = quando.astimezone(fuso(cfg))
    return q.weekday() in j["dias"] and _hm(j["inicio"]) <= q.time() < _hm(j["fim"])


def feriado(cfg: dict, d: date) -> bool:
    return d.isoformat() in set(cfg.get("feriados", []))


def dia_util(cfg: dict, d: date) -> bool:
    return d.weekday() < 5 and not feriado(cfg, d)


def dias_clinica(clinica: dict | None) -> set[int]:
    if not clinica or not clinica.get("dias"):
        return {0, 1, 2, 3, 4}
    return {int(x) for x in str(clinica["dias"]).split(",") if x.strip() != ""}


def opcoes_exame(cfg: dict, hoje: date, clinica: dict | None, n: int = 2) -> list[date]:
    """Próximos n dias úteis (a partir de amanhã) em que a clínica atende."""
    dias = dias_clinica(clinica)
    out, d = [], hoje
    while len(out) < n:
        d += timedelta(days=1)
        if dia_util(cfg, d) and d.weekday() in dias:
            out.append(d)
    return out


def data_exame_valida(cfg: dict, hoje: date, d: date, clinica: dict | None) -> bool:
    return d > hoje and dia_util(cfg, d) and d.weekday() in dias_clinica(clinica) and d <= hoje + timedelta(days=30)


def fmt_data(d: date) -> str:
    return f"{DIAS[d.weekday()]}, {d.strftime('%d/%m')}"


def ler_data(txt: str, hoje: date) -> date | None:
    """Aceita dd/mm/aaaa, dd/mm ou aaaa-mm-dd."""
    txt = (txt or "").strip()
    for f in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(txt, f).date()
        except ValueError:
            pass
    try:
        d = datetime.strptime(f"{txt}/{hoje.year}", "%d/%m/%Y").date()
        return d if d >= hoje else d.replace(year=hoje.year + 1)
    except ValueError:
        return None
