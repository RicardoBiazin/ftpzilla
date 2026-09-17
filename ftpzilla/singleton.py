"""Garante uma unica instancia do FTPZilla por vez na maquina.

Mutex nomeado do Windows: e liberado pelo proprio sistema quando o processo
morre, mesmo de forma abrupta, entao nao sobra 'lock' preso como aconteceria
com um arquivo de trava.
"""
from __future__ import annotations

ERROR_ALREADY_EXISTS = 183
_handle = None  # mantido vivo enquanto o processo existir


def already_running(name: str = "FTPZilla_UnicaInstancia") -> bool:
    """True se ja existe outra instancia rodando nesta maquina."""
    global _handle
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _handle = kernel32.CreateMutexW(None, False, name)
        return kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    except Exception:
        # outro SO, ou falha: nao bloqueia o uso
        return False
