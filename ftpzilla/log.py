"""Log em arquivo, com mascaramento de credenciais.

O log de um cliente FTP e o lugar mais facil do mundo para vazar senha: o
comando PASS vai no protocolo em texto puro, e um token OAuth aparece na URL.
Por isso o filtro de mascaramento nao e opcional - ele entra no logger antes
de qualquer handler.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler

from . import paths

NOME = "ftpzilla"

#: (padrao, substituicao). Aplicados na mensagem ja formatada.
_MASCARAS = [
    (re.compile(r"(?i)\b(PASS)\s+\S+"), r"\1 ***"),
    (re.compile(r"(?i)\b(senha|password|passphrase)\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"(?i)\b(access_token|refresh_token|client_secret|token)"
                r"\s*[=:]\s*[\"']?[\w\-.~+/]+"), r"\1=***"),
    (re.compile(r"(?i)(Authorization:\s*\w+\s+)\S+"), r"\1***"),
    # senha embutida em URL: ftp://usuario:senha@host
    (re.compile(r"(?i)(\w+://[^:/\s]+):([^@/\s]+)@"), r"\1:***@"),
]


class FiltroSegredos(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            texto = record.getMessage()
        except Exception:
            return True
        limpo = texto
        for padrao, troca in _MASCARAS:
            limpo = padrao.sub(troca, limpo)
        if limpo != texto:
            # substitui a mensagem ja interpolada e zera os args, senao o
            # handler refaria a interpolacao com o valor original
            record.msg = limpo
            record.args = ()
        return True


def mascarar(texto: str) -> str:
    """Versao avulsa do filtro, para textos que nao passam pelo logger."""
    for padrao, troca in _MASCARAS:
        texto = padrao.sub(troca, texto)
    return texto


def configurar(console: bool = True, nivel: int = logging.INFO) -> logging.Logger:
    log_dir = paths.dir_logs()
    logger = logging.getLogger(NOME)
    logger.setLevel(nivel)
    logger.handlers.clear()
    logger.filters.clear()
    logger.addFilter(FiltroSegredos())

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    dia = time.strftime("%Y-%m-%d")
    fh = RotatingFileHandler(os.path.join(log_dir, "ftpzilla-%s.log" % dia),
                             maxBytes=5 * 1024 * 1024, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)
    return logger


def limpar_antigos(dias: int = 30) -> None:
    if dias <= 0:
        return
    corte = time.time() - dias * 86400
    for f in glob.glob(os.path.join(paths.dir_logs(), "ftpzilla-*.log*")):
        try:
            if os.path.getmtime(f) < corte:
                os.remove(f)
        except OSError:
            pass


def get() -> logging.Logger:
    return logging.getLogger(NOME)
