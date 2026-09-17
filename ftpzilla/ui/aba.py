"""Uma aba: painel local a esquerda, painel do servidor a direita.

Cada aba tem sua propria conexao de controle, dentro de um BrowserWorker. A
fila de transferencias, ao contrario, e uma so para o programa inteiro -
trocar de aba nao pode perder transferencia em andamento.

A aba tambem e onde moram as decisoes de confianca (senha recusada,
certificado desconhecido, chave de host nova): sao as unicas perguntas que
precisam de um humano no meio de uma conexao, e concentra-las aqui evita
que cada backend invente o proprio jeito de perguntar.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from .. import log, remotes
from ..browser import BrowserWorker, NavegadorLocal
from ..remotes.base import (ErroAutenticacao, ErroCertificado,
                            ErroChaveDesconhecida)
from ..remotes.local import LocalRemote
from ..sites import Site
from . import dialogos
from .painel import FilePane

logger = log.get()


class AbaLocal(ttk.Frame):
    """Aba sem servidor: dois paineis do disco local."""

    titulo = "Disco local"

    def __init__(self, master, janela):
        super().__init__(master)
        self.janela = janela
        self.site = None
        self.worker = None

        divisor = ttk.PanedWindow(self, orient="horizontal")
        divisor.pack(fill="both", expand=True)
        self.esquerda = FilePane(divisor, NavegadorLocal(LocalRemote()),
                                 titulo="Local",
                                 ao_transferir=janela.transferir,
                                 ao_status=janela.status_painel)
        self.direita = FilePane(divisor, NavegadorLocal(LocalRemote()),
                                titulo="Local",
                                ao_transferir=janela.transferir,
                                ao_status=janela.status_painel)
        divisor.add(self.esquerda, weight=1)
        divisor.add(self.direita, weight=1)

        inicio = self.esquerda.nav.remote.home()
        self.esquerda.ir_para(inicio)
        self.direita.ir_para(inicio)

    def fechar(self) -> None:
        pass

    def retema(self) -> None:
        self.esquerda.retema()
        self.direita.retema()


class AbaSite(ttk.Frame):
    """Aba de um servidor: disco local | servidor."""

    def __init__(self, master, janela, site: Site):
        super().__init__(master)
        self.janela = janela
        self.site = site
        self.titulo = site.nome
        self.worker: Optional[BrowserWorker] = None
        self._tentou_senha = False

        divisor = ttk.PanedWindow(self, orient="horizontal")
        divisor.pack(fill="both", expand=True)

        self.esquerda = FilePane(divisor, NavegadorLocal(LocalRemote()),
                                 titulo="Local",
                                 ao_transferir=janela.transferir,
                                 ao_status=janela.status_painel)
        divisor.add(self.esquerda, weight=1)

        self.quadro_remoto = ttk.Frame(divisor)
        divisor.add(self.quadro_remoto, weight=1)
        self.lb_espera = ttk.Label(self.quadro_remoto, style="Fraco.TLabel",
                                   text="Conectando...")
        self.lb_espera.pack(padx=12, pady=12, anchor="w")
        self.direita: Optional[FilePane] = None

        self.esquerda.ir_para(site.pasta_local
                              or self.esquerda.nav.remote.home())
        self.conectar()

    # ------------------------------------------------------------------
    def conectar(self) -> None:
        self.lb_espera.configure(text="Conectando em %s..." % self.site.host)
        try:
            remote = remotes.make_remote(self.site.revelado())
        except RuntimeError as e:      # dependencia faltando
            self._falhou(e)
            return
        self.worker = BrowserWorker(remote, self.after,
                                    nome="browser-%s" % self.site.nome)
        self.worker.conectar(ok=self._conectou, erro=self._falhou)

    def _conectou(self, home: str) -> None:
        self.lb_espera.pack_forget()
        if self.direita is None:
            self.direita = FilePane(self.quadro_remoto, self.worker,
                                    titulo=self.site.nome,
                                    ao_transferir=self.janela.transferir,
                                    ao_status=self.janela.status_painel)
            self.direita.pack(fill="both", expand=True)
        self.direita.ir_para(self.worker.remote.home())
        remote = self.worker.remote
        logger.info("Conectado em %s (%s). Retomada: %s. Data preservada: %s.",
                    self.site.host, remotes.rotulo(self.site.kind),
                    "sim" if remote.resume_download else "nao",
                    "sim" if remote.preserva_mtime else "nao")
        self.janela.status("Conectado em %s." % self.site.host)
        self.janela.atualizar_titulo_aba(self)

    def _falhou(self, exc: Exception) -> None:
        """Erro de conexao: alguns dao para resolver perguntando ao usuario."""
        if isinstance(exc, ErroCertificado):
            if dialogos.confirmar_certificado(self.janela, self.site.host,
                                              exc.fingerprint, str(exc)):
                self.site.cert_fingerprint = exc.fingerprint
                self.janela.salvar_sites()
                self._reconectar()
                return
        elif isinstance(exc, ErroChaveDesconhecida):
            escolha = dialogos.confirmar_chave_host(
                self.janela, exc.host, exc.fingerprint, exc.mudou)
            if escolha in ("salvar", "uma_vez"):
                self.site.opcoes["aceitar_chave"] = escolha
                if escolha == "salvar":
                    self.janela.salvar_sites()
                self._reconectar()
                return
        elif isinstance(exc, ErroAutenticacao) and not self._tentou_senha:
            # uma tentativa manual, nunca repeticao automatica: senha errada
            # repetida tranca conta em servidor com politica de bloqueio
            self._tentou_senha = True
            senha = dialogos.pedir_senha(
                self.janela, "Autenticacao",
                "O servidor %s recusou a credencial de %s.\nDigite a senha:"
                % (self.site.host, self.site.usuario or "anonimo"))
            if senha:
                self.site.senha = senha
                self._reconectar()
                return

        self.lb_espera.configure(text="Nao conectou: %s" % exc)
        self.lb_espera.pack(padx=12, pady=12, anchor="w")
        self.janela.status("Nao conectou em %s: %s" % (self.site.host, exc))
        logger.error("Falha ao conectar em %s: %s", self.site.host, exc)

    def _reconectar(self) -> None:
        if self.worker is not None:
            self.worker.fechar()
            self.worker = None
        self.conectar()

    # ------------------------------------------------------------------
    @property
    def conectado(self) -> bool:
        return (self.worker is not None
                and self.worker.remote is not None
                and self.worker.remote.conectado)

    def fechar(self) -> None:
        if self.worker is not None:
            self.worker.fechar()
            self.worker = None

    def retema(self) -> None:
        self.esquerda.retema()
        if self.direita is not None:
            self.direita.retema()
