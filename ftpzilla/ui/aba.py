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
                                 ao_status=janela.status_painel,
                                 acoes_extras=janela.acoes_de_painel())
        self.direita = FilePane(divisor, NavegadorLocal(LocalRemote()),
                                titulo="Local",
                                ao_transferir=janela.transferir,
                                ao_status=janela.status_painel,
                                acoes_extras=janela.acoes_de_painel())
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
                                 ao_status=janela.status_painel,
                                 acoes_extras=janela.acoes_de_painel())
        divisor.add(self.esquerda, weight=1)

        self.quadro_remoto = ttk.Frame(divisor)
        divisor.add(self.quadro_remoto, weight=1)
        self.quadro_espera = ttk.Frame(self.quadro_remoto, padding=12)
        self.quadro_espera.pack(anchor="w", fill="x")
        self.lb_espera = ttk.Label(self.quadro_espera, style="Fraco.TLabel",
                                   text="Conectando...", wraplength=420,
                                   justify="left")
        self.lb_espera.pack(anchor="w")
        # botoes so aparecem quando a conexao falha: uma aba morta sem saida
        # obriga a fechar e recomecar do gerente de sites
        self.quadro_botoes = ttk.Frame(self.quadro_espera)
        ttk.Button(self.quadro_botoes, text="Tentar de novo",
                   command=self.reconectar).pack(side="left")
        ttk.Button(self.quadro_botoes, text="Fechar a aba",
                   command=lambda: janela.fechar_aba(self)).pack(side="left",
                                                                 padx=6)
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
        # a ponte da janela, e nao self.after: o BrowserWorker responde de
        # outra thread, e tocar em Tk de la e o jeito classico de a resposta
        # simplesmente nunca chegar
        self.worker = BrowserWorker(remote, self.janela.ponte,
                                    nome="browser-%s" % self.site.nome)
        self.worker.conectar(ok=self._conectou, erro=self._falhou)

    def _conectou(self, home: str) -> None:
        self.quadro_botoes.pack_forget()
        self.quadro_espera.pack_forget()
        if self.direita is None:
            self.direita = FilePane(self.quadro_remoto, self.worker,
                                    titulo=self.site.nome,
                                    ao_transferir=self.janela.transferir,
                                    ao_status=self.janela.status_painel,
                                    acoes_extras=self.janela.acoes_de_painel())
            self.direita.pack(fill="both", expand=True)
        self.direita.ir_para(self.worker.remote.home())
        self.janela.ligar_arrastar(self)
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

        self.lb_espera.configure(text="Nao conectou em %s:\n%s"
                                      % (self.site.host, exc),
                                 style="Erro.TLabel")
        self.quadro_espera.pack(anchor="w", fill="x")
        self.quadro_botoes.pack(anchor="w", pady=(10, 0))
        self.janela.status("Nao conectou em %s: %s" % (self.site.host, exc))
        logger.error("Falha ao conectar em %s: %s", self.site.host, exc)

    def reconectar(self) -> None:
        """Tenta de novo na mesma aba, sem perder o lugar nem a pasta local."""
        self.quadro_botoes.pack_forget()
        self.lb_espera.configure(style="Fraco.TLabel")
        if self.worker is not None:
            self.worker.fechar()
            self.worker = None
        if self.direita is not None:
            self.direita.destroy()
            self.direita = None
        self.quadro_espera.pack(anchor="w", fill="x")
        self.conectar()

    #: nome antigo, mantido para o fluxo interno de confianca
    _reconectar = reconectar

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
