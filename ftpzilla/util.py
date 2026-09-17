"""Formatacao e pequenas funcoes usadas em todo lugar."""
from __future__ import annotations

import time

_UNIDADES = ("B", "KB", "MB", "GB", "TB", "PB")


def fmt_bytes(n: float, casas: int = 1) -> str:
    """1536 -> '1,5 KB'. Usa virgula decimal, como o resto da interface."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    neg = n < 0
    n = abs(n)
    i = 0
    while n >= 1024 and i < len(_UNIDADES) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        texto = "%d B" % int(n)
    else:
        texto = ("%.*f %s" % (casas, n, _UNIDADES[i])).replace(".", ",")
    return ("-" + texto) if neg else texto


def fmt_velocidade(bps: float) -> str:
    if not bps or bps <= 0:
        return "-"
    return fmt_bytes(bps) + "/s"


def fmt_tempo(seg: float) -> str:
    """Duracao curta: '45s', '3m12s', '2h05m'."""
    if seg is None or seg < 0 or seg != seg or seg == float("inf"):
        return "-"
    seg = int(seg)
    if seg < 60:
        return "%ds" % seg
    if seg < 3600:
        return "%dm%02ds" % (seg // 60, seg % 60)
    if seg < 86400:
        return "%dh%02dm" % (seg // 3600, (seg % 3600) // 60)
    return "%dd%02dh" % (seg // 86400, (seg % 86400) // 3600)


def fmt_data(epoch: float) -> str:
    """Data local legivel. 0 (desconhecido) vira '-', nunca 1970."""
    if not epoch:
        return "-"
    try:
        return time.strftime("%d/%m/%Y %H:%M", time.localtime(epoch))
    except (ValueError, OSError):
        return "-"


def fmt_perms(perms: str) -> str:
    """Normaliza a permissao para exibicao: aceita '0644' ou 'rwxr-xr-x'."""
    if not perms:
        return ""
    p = perms.strip()
    if p.isdigit():
        return p.lstrip("0").rjust(3, "0") if len(p) > 3 else p.rjust(3, "0")
    return p


def perms_para_int(perms: str) -> int:
    """'rwxr-xr-x' ou '755' -> 0o755. 0 se nao der para entender."""
    p = (perms or "").strip()
    if not p:
        return 0
    if p.isdigit():
        try:
            return int(p, 8)
        except ValueError:
            return 0
    if len(p) >= 9:
        p = p[-9:]
        modo = 0
        for i, bit in enumerate(p):
            if bit not in ("-", ""):
                modo |= 1 << (8 - i)
        return modo
    return 0


def perms_para_texto(modo: int) -> str:
    """0o755 -> 'rwxr-xr-x'."""
    letras = "rwx" * 3
    return "".join(letras[i] if modo & (1 << (8 - i)) else "-" for i in range(9))


def elidir(texto: str, limite: int = 60) -> str:
    """Corta pelo meio, preservando inicio e fim - bom para caminho longo."""
    if not texto or len(texto) <= limite:
        return texto or ""
    meio = (limite - 3) // 2
    return texto[:meio] + "..." + texto[-(limite - 3 - meio):]


def ordenar_chave(valor, tipo: str):
    """Chave de ordenacao por tipo de coluna. Nome ordena sem diferenciar
    maiusculas; tamanho e data ordenam como numero, nunca como texto."""
    if tipo in ("int", "float"):
        try:
            return float(valor)
        except (TypeError, ValueError):
            return -1.0
    return str(valor or "").casefold()
