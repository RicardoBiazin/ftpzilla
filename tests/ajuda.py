"""Apoio para as suites de teste.

Nao usamos pytest: cada suite e um script que roda sozinho e imprime uma
linha por verificacao. O rodar_todos.py conta essas linhas. Isso mantem os
testes executaveis com um duplo clique, sem instalar nada.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

# O console do Windows abre em cp1252: imprimir um acento (ou o nome de um
# arquivo com emoji, que e caso de teste legitimo aqui) derrubaria a suite
# com UnicodeEncodeError e ninguem descobriria o que realmente falhou.
for _fluxo in (sys.stdout, sys.stderr):
    try:
        _fluxo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_contas = {"ok": 0, "falha": 0, "pulado": 0}


def ok(msg: str) -> None:
    _contas["ok"] += 1
    print("  OK    %s" % msg)


def falha(msg: str) -> None:
    _contas["falha"] += 1
    print("  FALHA %s" % msg)


def pulado(msg: str) -> None:
    _contas["pulado"] += 1
    print("  PULADO %s" % msg)


def checar(condicao, msg: str) -> bool:
    if condicao:
        ok(msg)
        return True
    falha(msg)
    return False


def igual(obtido, esperado, msg: str) -> bool:
    if obtido == esperado:
        ok(msg)
        return True
    falha("%s (obtido %r, esperado %r)" % (msg, obtido, esperado))
    return False


def levanta(excecao, funcao, msg: str) -> bool:
    try:
        funcao()
    except excecao:
        ok(msg)
        return True
    except Exception as e:      # noqa: BLE001
        falha("%s (levantou %s)" % (msg, type(e).__name__))
        return False
    falha("%s (nao levantou nada)" % msg)
    return False


def secao(titulo: str) -> None:
    print("\n-- %s" % titulo)


def rodar(testes, titulo: str) -> int:
    """Roda uma lista de funcoes e devolve o codigo de saida."""
    print("=" * 62)
    print(titulo)
    print("=" * 62)
    for func in testes:
        secao(func.__name__.replace("teste_", "").replace("_", " "))
        try:
            func()
        except _Pular as e:
            pulado(str(e))
        except Exception:       # noqa: BLE001
            falha("excecao inesperada em %s" % func.__name__)
            traceback.print_exc()
    print("\n%d ok, %d falha(s), %d pulado(s)"
          % (_contas["ok"], _contas["falha"], _contas["pulado"]))
    return 1 if _contas["falha"] else 0


class _Pular(Exception):
    pass


def pular(motivo: str):
    """Levanta a excecao que o rodar() traduz em PULADO."""
    raise _Pular(motivo)


class PastaTemp:
    """Pasta temporaria com FTPZILLA_HOME apontando para dentro dela, para
    nenhum teste sujar a configuracao real do usuario."""

    def __init__(self, isolar_home: bool = True):
        self.isolar_home = isolar_home
        self.caminho = ""
        self._home_antes = None

    def __enter__(self) -> str:
        self.caminho = tempfile.mkdtemp(prefix="ftpzilla_teste_")
        if self.isolar_home:
            self._home_antes = os.environ.get("FTPZILLA_HOME")
            os.environ["FTPZILLA_HOME"] = os.path.join(self.caminho, "_dados")
        return self.caminho

    def __exit__(self, *exc) -> None:
        if self.isolar_home:
            if self._home_antes is None:
                os.environ.pop("FTPZILLA_HOME", None)
            else:
                os.environ["FTPZILLA_HOME"] = self._home_antes
        shutil.rmtree(self.caminho, ignore_errors=True)


def escrever(caminho: str, dados: bytes = b"conteudo") -> str:
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "wb") as f:
        f.write(dados)
    return caminho


def tem_display() -> bool:
    """Da para criar uma janela Tk aqui?"""
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False
