"""Linha de comando. Sem argumento, abre a janela.

O mesmo binario serve para uso interativo e para script: 'FTPZilla.exe' abre
a interface, 'FTPZilla.exe --enviar ...' roda sem janela. Isso e o que
permite agendar uma subida no Agendador de Tarefas do Windows sem instalar
nada alem do que ja esta ali.
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import __version__, log, paths, singleton


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ftpzilla",
        description="FTPZilla - cliente de transferencia de arquivos.")
    p.add_argument("--versao", action="version", version="FTPZilla " + __version__)
    p.add_argument("--tema", default="", help="tema inicial da janela")
    p.add_argument("--verboso", action="store_true", help="log em nivel DEBUG")
    p.add_argument("--pasta-dados", default="",
                   help="onde guardar config, fila e logs (modo portatil)")
    p.add_argument("--varias-instancias", action="store_true",
                   help="nao bloquear uma segunda janela")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)

    if args.pasta_dados:
        import os
        os.environ["FTPZILLA_HOME"] = args.pasta_dados

    console = not getattr(sys, "frozen", False)
    log.configurar(console=console,
                   nivel=logging.DEBUG if args.verboso else logging.INFO)
    log.limpar_antigos(30)
    logger = log.get()
    logger.debug("Dados em %s", paths.dir_config())

    if not args.varias_instancias and singleton.already_running():
        logger.warning("O FTPZilla ja esta aberto nesta maquina.")
        try:
            import tkinter.messagebox as mb
            import tkinter as tk
            r = tk.Tk()
            r.withdraw()
            mb.showinfo("FTPZilla", "O FTPZilla ja esta aberto.")
            r.destroy()
        except Exception:
            pass
        return 0

    from .ui import janela, tema
    janela.abrir(args.tema or tema.PADRAO)
    return 0
