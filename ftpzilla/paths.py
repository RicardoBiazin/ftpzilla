"""Onde ficam os arquivos do FTPZilla.

Um lugar so para resolver caminhos, porque tres coisas precisam concordar:
o app instalado, o app congelado pelo PyInstaller e os testes. A variavel
de ambiente FTPZILLA_HOME sobrepoe tudo - eh o que permite modo portatil
(pasta ao lado do .exe) e um diretorio descartavel por teste.
"""
from __future__ import annotations

import os
import sys

APP = "FTPZilla"


def _base() -> str:
    forcado = os.environ.get("FTPZILLA_HOME", "").strip()
    if forcado:
        return os.path.abspath(forcado)
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return os.path.join(appdata, APP)
    # fora do Windows (ou sem APPDATA): ~/.ftpzilla
    return os.path.join(os.path.expanduser("~"), "." + APP.lower())


def dir_config() -> str:
    d = _base()
    os.makedirs(d, exist_ok=True)
    return d


def dir_logs() -> str:
    d = os.path.join(_base(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def dir_temp_editor() -> str:
    d = os.path.join(_base(), "editor")
    os.makedirs(d, exist_ok=True)
    return d


def arquivo_sites() -> str:
    return os.path.join(dir_config(), "sites.json")


def arquivo_prefs() -> str:
    return os.path.join(dir_config(), "preferencias.json")


def arquivo_fila() -> str:
    return os.path.join(dir_config(), "fila.db")


def arquivo_known_hosts() -> str:
    return os.path.join(dir_config(), "known_hosts")


def known_hosts_do_usuario() -> str:
    """O ~/.ssh/known_hosts do OpenSSH: lido, nunca escrito."""
    return os.path.join(os.path.expanduser("~"), ".ssh", "known_hosts")


def dir_recursos() -> str:
    """Pasta de recursos empacotados (icones). Muda quando congelado."""
    if getattr(sys, "frozen", False):
        return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)),
                            "recursos")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "recursos")
