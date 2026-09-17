"""Navegacao: quem fala com o servidor para a interface poder desenhar.

A interface nunca chama um Remote diretamente. Ela pede a um Navegador, que
devolve o resultado por callback. Existem duas implementacoes com a MESMA
assinatura:

  NavegadorLocal   executa na hora, na propria thread do Tkinter (disco local
                   e rapido o bastante e nao vale uma thread por painel).
  BrowserWorker    uma thread por aba, dona da conexao de controle; a
                   interface so entrega comando e recebe callback.

Por serem iguais por fora, o FilePane e o mesmo widget para disco e para
servidor - foi o que evitou duplicar toda a tela.

Sobre a thread do BrowserWorker: FTP e SFTP nao sao thread-safe, entao uma
unica thread dona da conexao e mais seguro (e mais simples) que um Lock. De
quebra, a fila permite descartar listagem obsoleta: se o usuario clicar em
tres pastas seguidas, as duas primeiras respostas sao jogadas fora pelo
numero de geracao em vez de piscarem na tela uma atras da outra.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable, List, Optional

from . import log
from .remotes.base import Cancelado, Entry, ErroRemoto, Remote

logger = log.get()

Ok = Optional[Callable]
Erro = Optional[Callable[[Exception], None]]


class Navegador:
    """Interface que o FilePane consome."""

    def __init__(self, remote: Remote):
        self.remote = remote
        self.geracao = 0

    # --- consultas --------------------------------------------------------
    def nova_geracao(self) -> int:
        """Invalida as respostas pendentes. Chamado a cada navegacao."""
        self.geracao += 1
        return self.geracao

    def listar(self, caminho: str, ok: Ok = None, erro: Erro = None,
               geracao: int = None) -> None:
        raise NotImplementedError

    def criar_pasta(self, caminho: str, ok: Ok = None, erro: Erro = None) -> None:
        raise NotImplementedError

    def renomear(self, de: str, para: str, ok: Ok = None, erro: Erro = None) -> None:
        raise NotImplementedError

    def apagar(self, caminhos: List[str], ok: Ok = None, erro: Erro = None,
               progresso: Ok = None) -> None:
        raise NotImplementedError

    def chmod(self, caminho: str, modo: int, ok: Ok = None, erro: Erro = None) -> None:
        raise NotImplementedError

    def fechar(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Disco local: sincrono
# ---------------------------------------------------------------------------
class NavegadorLocal(Navegador):

    def _rodar(self, funcao, ok: Ok, erro: Erro, geracao: int = None):
        try:
            resultado = funcao()
        except Cancelado:
            return
        except (ErroRemoto, OSError) as e:
            if erro is not None:
                erro(e)
            else:
                logger.error("%s", e)
            return
        if ok is None:
            return
        if geracao is not None:
            ok(resultado, geracao)
        else:
            ok(resultado)

    def listar(self, caminho, ok=None, erro=None, geracao=None):
        self._rodar(lambda: self.remote.listar(caminho), ok, erro, geracao)

    def criar_pasta(self, caminho, ok=None, erro=None):
        self._rodar(lambda: self.remote.criar_pasta(caminho), ok, erro)

    def renomear(self, de, para, ok=None, erro=None):
        self._rodar(lambda: self.remote.renomear(de, para), ok, erro)

    def apagar(self, caminhos, ok=None, erro=None, progresso=None):
        def tarefa():
            for c in caminhos:
                if progresso is not None:
                    progresso(c)
                e = self.remote.stat(c)
                if e is not None and e.is_dir and not e.is_link:
                    self.remote.apagar_arvore(c)
                else:
                    self.remote.apagar_arquivo(c)
        self._rodar(tarefa, ok, erro)

    def chmod(self, caminho, modo, ok=None, erro=None):
        self._rodar(lambda: self.remote.chmod(caminho, modo), ok, erro)


# ---------------------------------------------------------------------------
# Servidor: uma thread dona da conexao de controle
# ---------------------------------------------------------------------------
class _Comando:
    __slots__ = ("funcao", "ok", "erro", "geracao")

    def __init__(self, funcao, ok, erro, geracao):
        self.funcao = funcao
        self.ok = ok
        self.erro = erro
        self.geracao = geracao


class BrowserWorker(Navegador):
    """Thread unica que conversa com o servidor, alimentada por uma fila.

    'agendar' e a ponte de volta para a thread do Tkinter - na pratica,
    root.after. Sem ela nenhum callback poderia tocar em widget.
    """

    def __init__(self, remote: Remote, agendar: Callable, nome: str = "browser"):
        super().__init__(remote)
        self._fila: "queue.Queue[Optional[_Comando]]" = queue.Queue()
        self._agendar = agendar
        self._parar = threading.Event()
        self._thread = threading.Thread(target=self._laco, name=nome, daemon=True)
        self._thread.start()

    # --- ponte ------------------------------------------------------------
    def _devolver(self, cb, *args) -> None:
        if cb is None:
            return
        try:
            self._agendar(0, lambda: cb(*args))
        except RuntimeError:
            pass          # a janela ja foi fechada

    def _laco(self) -> None:
        while not self._parar.is_set():
            cmd = self._fila.get()
            if cmd is None:
                break
            try:
                resultado = cmd.funcao()
            except Cancelado:
                continue
            except Exception as e:           # noqa: BLE001 - vai inteiro para a GUI
                logger.warning("%s", e)
                self._devolver(cmd.erro, e)
                continue
            if cmd.geracao is not None:
                self._devolver(cmd.ok, resultado, cmd.geracao)
            else:
                self._devolver(cmd.ok, resultado)

    def _enviar(self, funcao, ok, erro, geracao=None) -> None:
        self._fila.put(_Comando(funcao, ok, erro, geracao))

    # --- comandos ---------------------------------------------------------
    def conectar(self, ok=None, erro=None) -> None:
        self._enviar(lambda: (self.remote.conectar(), self.remote.home())[1],
                     ok, erro)

    def listar(self, caminho, ok=None, erro=None, geracao=None):
        self._enviar(lambda: self.remote.listar(caminho), ok, erro, geracao)

    def criar_pasta(self, caminho, ok=None, erro=None):
        self._enviar(lambda: self.remote.criar_pasta(caminho), ok, erro)

    def renomear(self, de, para, ok=None, erro=None):
        self._enviar(lambda: self.remote.renomear(de, para), ok, erro)

    def apagar(self, caminhos, ok=None, erro=None, progresso=None):
        def tarefa():
            for c in caminhos:
                if progresso is not None:
                    self._devolver(progresso, c)
                e = self.remote.stat(c)
                if e is not None and e.is_dir and not e.is_link:
                    self.remote.apagar_arvore(c)
                else:
                    self.remote.apagar_arquivo(c)
        self._enviar(tarefa, ok, erro)

    def chmod(self, caminho, modo, ok=None, erro=None):
        self._enviar(lambda: self.remote.chmod(caminho, modo), ok, erro)

    def stat(self, caminho, ok=None, erro=None):
        self._enviar(lambda: self.remote.stat(caminho), ok, erro)

    def executar(self, funcao, ok=None, erro=None) -> None:
        """Escotilha para comandos que so um backend entende."""
        self._enviar(funcao, ok, erro)

    # --- fim --------------------------------------------------------------
    def fechar(self) -> None:
        self._parar.set()
        self._fila.put(None)
        try:
            self.remote.fechar()
        except Exception:
            pass


def entradas_ordenadas(itens: List[Entry], coluna: str = "nome",
                       invertido: bool = False) -> List[Entry]:
    """Ordena a listagem mantendo as pastas sempre antes dos arquivos.

    Misturar pasta com arquivo na ordenacao e tecnicamente correto e
    praticamente inutil: ninguem procura pasta no meio de mil arquivos.
    """
    from .util import ordenar_chave

    tipos = {"nome": "str", "tamanho": "int", "modificado": "float",
             "tipo": "str", "permissoes": "str"}
    campo = {"nome": lambda e: e.name, "tamanho": lambda e: e.size,
             "modificado": lambda e: e.mtime, "tipo": lambda e: e.ext,
             "permissoes": lambda e: e.perms}.get(coluna, lambda e: e.name)
    tipo = tipos.get(coluna, "str")
    chave = lambda e: ordenar_chave(campo(e), tipo)   # noqa: E731
    # ordenar os dois grupos separadamente, e nao com uma chave composta:
    # com chave composta, inverter a ordem jogaria os arquivos para cima
    pastas = sorted([e for e in itens if e.is_dir], key=chave, reverse=invertido)
    arquivos = sorted([e for e in itens if not e.is_dir], key=chave,
                      reverse=invertido)
    return pastas + arquivos
