"""Pool de conexoes de transferencia.

A conexao de NAVEGACAO nao esta aqui: ela pertence ao BrowserWorker da aba e
nunca e usada para transferir. E essa separacao que faz a janela continuar
respondendo com a fila cheia - a queixa mais comum sobre o FileZilla.

Duas regras que parecem detalhe e nao sao:

1. Conexao que deu erro de rede, ou que foi cancelada no meio, NAO volta
   para o pool. Sobrou resposta do servidor no meio do caminho; a proxima
   transferencia receberia o fim do arquivo anterior. O Remote marca isso
   em .suja.
2. Conexao ociosa e testada com um NOOP antes de ser entregue. Servidor FTP
   derruba sessao parada por tempo (idle timeout) sem avisar ninguem, e
   descobrir isso no meio de um envio custa a transferencia inteira.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List, Optional

from . import log, remotes
from .remotes.base import ErroRemoto, Remote

logger = log.get()

#: conexao parada por mais tempo que isto e testada antes de ser reusada
OCIOSA = 30.0
#: conexao parada por mais tempo que isto e simplesmente fechada
VELHA = 300.0


class _Guardada:
    __slots__ = ("remote", "quando")

    def __init__(self, remote: Remote):
        self.remote = remote
        self.quando = time.time()


class PoolConexoes:
    """Conexoes reaproveitaveis para um unico site."""

    def __init__(self, site, maximo: int = 0,
                 fabrica: Optional[Callable] = None):
        self.site = site
        self.fabrica = fabrica or (lambda: remotes.make_remote(site.revelado()))
        self._lock = threading.Condition()
        self._livres: List[_Guardada] = []
        self._emprestadas = 0
        self._fechando = False
        pedido = int(maximo or getattr(site, "max_conexoes", 0) or 4)
        self.maximo = max(1, pedido)
        self._teto_original = self.maximo

    # ------------------------------------------------------------------
    def pegar(self, timeout: float = 60.0) -> Remote:
        """Devolve uma conexao pronta. Bloqueia ate ter vaga."""
        limite = time.time() + timeout
        while True:
            with self._lock:
                if self._fechando:
                    raise ErroRemoto("O pool foi fechado.")
                while self._livres:
                    guardada = self._livres.pop()
                    if self._utilizavel(guardada):
                        self._emprestadas += 1
                        return guardada.remote
                    self._descartar(guardada.remote)
                if self._emprestadas < self.maximo:
                    self._emprestadas += 1
                    break
                restante = limite - time.time()
                if restante <= 0:
                    raise ErroRemoto("Sem conexao livre para %s."
                                     % getattr(self.site, "host", ""))
                self._lock.wait(min(restante, 1.0))

        # cria fora do lock: conectar leva segundos e nao pode travar o pool
        try:
            remote = self.fabrica()
            remote.conectar()
            return remote
        except Exception:
            with self._lock:
                self._emprestadas -= 1
                self._lock.notify_all()
            raise

    @staticmethod
    def _utilizavel(guardada: _Guardada) -> bool:
        parada = time.time() - guardada.quando
        if parada > VELHA:
            return False
        if getattr(guardada.remote, "suja", False):
            return False
        if parada > OCIOSA:
            return bool(guardada.remote.viva())
        return guardada.remote.conectado

    def devolver(self, remote: Remote, suja: bool = False) -> None:
        if remote is None:
            return
        suja = suja or getattr(remote, "suja", False) or not remote.conectado
        with self._lock:
            self._emprestadas = max(0, self._emprestadas - 1)
            if suja or self._fechando:
                self._descartar(remote)
            else:
                self._livres.append(_Guardada(remote))
            self._lock.notify_all()

    @staticmethod
    def _descartar(remote: Remote) -> None:
        try:
            remote.fechar()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def reduzir_teto(self, motivo: str = "") -> int:
        """Servidor reclamou de excesso de sessoes: baixa o limite.

        Muito servidor compartilhado permite duas ou tres sessoes por IP e
        responde 421/530 na quarta. Insistir so gera erro; reduzir o teto e
        avisar resolve, e a transferencia continua mais devagar em vez de
        falhar.
        """
        with self._lock:
            if self.maximo > 1:
                self.maximo -= 1
                logger.warning("O servidor limitou as conexoes simultaneas a "
                               "%d%s.", self.maximo,
                               (" (%s)" % motivo) if motivo else "")
            return self.maximo

    def limitar(self, maximo: int) -> None:
        """Fixa o teto de conexoes (servidor que nao aceita simultaneas)."""
        with self._lock:
            self.maximo = max(1, int(maximo))
            self._lock.notify_all()

    def fechar_ociosas(self, idade: float = VELHA) -> None:
        with self._lock:
            manter = []
            for g in self._livres:
                if time.time() - g.quando > idade:
                    self._descartar(g.remote)
                else:
                    manter.append(g)
            self._livres = manter

    def fechar(self) -> None:
        with self._lock:
            self._fechando = True
            for g in self._livres:
                self._descartar(g.remote)
            self._livres = []
            self._lock.notify_all()

    # ------------------------------------------------------------------
    @property
    def livres(self) -> int:
        with self._lock:
            return max(0, self.maximo - self._emprestadas)

    @property
    def em_uso(self) -> int:
        with self._lock:
            return self._emprestadas

    def __repr__(self) -> str:
        return "<PoolConexoes %s %d/%d>" % (getattr(self.site, "host", "?"),
                                            self.em_uso, self.maximo)


class PoolLocal:
    """Mesma interface do pool, para o disco local.

    O disco nao tem conexao para reaproveitar, mas o codigo da transferencia
    nao precisa saber disso - ele pede e devolve do mesmo jeito nos dois
    lados. Sem isto, cada transferencia teria dois caminhos diferentes.
    """

    maximo = 1

    def __init__(self, site=None):
        from .remotes.local import LocalRemote
        self._classe = LocalRemote
        self.site = site

    def pegar(self, timeout: float = 0.0) -> Remote:
        return self._classe(self.site)

    def devolver(self, remote: Remote, suja: bool = False) -> None:
        pass

    def reduzir_teto(self, motivo: str = "") -> int:
        return 1

    def limitar(self, maximo: int) -> None:
        pass

    def fechar_ociosas(self, idade: float = 0.0) -> None:
        pass

    def fechar(self) -> None:
        pass

    @property
    def livres(self) -> int:
        return 1

    @property
    def em_uso(self) -> int:
        return 0
