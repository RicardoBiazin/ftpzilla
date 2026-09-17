"""A abstracao de acesso a arquivos: Entry, Remote e as excecoes.

Este arquivo define o contrato que FTP, SFTP, disco local e nuvem cumprem.
Vale a pena ler os contratos abaixo antes de escrever um backend novo, porque
tres deles sao faceis de violar sem quebrar nada de imediato - e ai o bug
aparece so em transferencia grande, que e o pior lugar para descobrir.

Contratos
---------
cb(n)       recebe o DELTA de bytes desde a chamada anterior, nunca o total
            acumulado. E chamado a cada bloco, entao precisa ser barato: so
            incrementa contador, nunca toca em widget nem aloca.
cancelar    threading.Event conferido a cada bloco. Ao disparar, o metodo
            levanta Cancelado e a conexao passa a ser considerada suja: quem
            a pegou emprestada do pool deve fecha-la, nao devolve-la, porque
            sobrou resposta do servidor no meio do caminho.
caminhos    sempre com '/', mesmo no Windows; LocalRemote traduz na fronteira.
mtime       float, epoch UTC. 0.0 significa desconhecido, nunca "1970".
erros       cada backend traduz o erro nativo para uma das quatro excecoes
            abaixo numa funcao _traduzir(). E o que permite ao transfer.py
            decidir entre repetir, pedir senha ou desistir sem importar
            ftplib nem paramiko.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Callable, List, Optional

#: tamanho de bloco padrao das transferencias (bytes)
BLOCO = 256 * 1024


# ---------------------------------------------------------------------------
# Dados
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    """Uma linha de listagem de diretorio."""
    name: str
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0          # epoch UTC; 0.0 = desconhecido
    perms: str = ""             # "rwxr-xr-x" ou "0644", como o servidor deu
    owner: str = ""
    group: str = ""
    is_link: bool = False
    link_target: str = ""
    etag: str = ""              # hash/versao publicada pelo servidor, se houver
    id: str = ""                # id opaco (Google Drive, OneDrive)

    @property
    def ext(self) -> str:
        if self.is_dir:
            return ""
        return posixpath.splitext(self.name)[1].lower().lstrip(".")


# ---------------------------------------------------------------------------
# Excecoes
# ---------------------------------------------------------------------------
class ErroRemoto(Exception):
    """Base de tudo que da errado no acesso remoto."""


class ErroAutenticacao(ErroRemoto):
    """Credencial recusada. NAO repetir automaticamente: repetir senha errada
    tranca conta em servidor com politica de bloqueio."""


class ErroTransitorio(ErroRemoto):
    """Timeout, conexao derrubada, servidor ocupado. Vale repetir com espera."""


class ErroPermanente(ErroRemoto):
    """Arquivo nao existe, sem permissao, disco cheio. Repetir nao adianta."""


class ErroChaveDesconhecida(ErroRemoto):
    """Chave de host SSH nova ou trocada - quem decide e o usuario."""

    def __init__(self, host: str, fingerprint: str, mudou: bool = False):
        self.host = host
        self.fingerprint = fingerprint
        self.mudou = mudou
        super().__init__("Chave de host %s para %s: %s"
                         % ("DIFERENTE" if mudou else "desconhecida",
                            host, fingerprint))


class ErroCertificado(ErroRemoto):
    """Certificado TLS que nao valida - quem decide e o usuario."""

    def __init__(self, host: str, fingerprint: str, detalhe: str = ""):
        self.host = host
        self.fingerprint = fingerprint
        self.detalhe = detalhe
        super().__init__("Certificado nao confiavel em %s: %s" % (host, detalhe))


class Cancelado(Exception):
    """O usuario mandou parar. Nao e erro; nao conta tentativa."""


# ---------------------------------------------------------------------------
# A classe base
# ---------------------------------------------------------------------------
class Remote:
    """Acesso a um sistema de arquivos (local ou remoto).

    As capacidades sao atributos de CLASSE com valor conservador, e cada
    backend as rebaixa ou eleva na INSTANCIA depois de conversar com o
    servidor (o FTP so sabe se tem REST e MFMT depois do FEAT). A GUI e o
    motor leem sempre da instancia.
    """

    # --- identidade -------------------------------------------------------
    kind = "base"
    rotulo = "Base"

    # --- capacidades ------------------------------------------------------
    is_local = False
    tem_dirs = True             # tem diretorios de verdade
    resume_download = False
    resume_upload = False
    segmentavel = False         # download por faixa em conexoes paralelas
    pode_renomear = True
    pode_chmod = False
    preserva_mtime = True       # consegue gravar o mtime no destino
    case_sensitive = True
    max_conexoes = 4
    sep = "/"

    def __init__(self, site=None):
        self.site = site
        self._conectado = False

    # --- ciclo de vida ----------------------------------------------------
    def conectar(self) -> None:
        self._conectado = True

    def fechar(self) -> None:
        self._conectado = False

    @property
    def conectado(self) -> bool:
        return self._conectado

    def viva(self) -> bool:
        """Keepalive barato (NOOP/stat). False = precisa reconectar."""
        return self._conectado

    def clone(self) -> "Remote":
        """Nova conexao para o mesmo destino, usada pelo pool.

        Precisa ser independente: duas conexoes FTP nao podem compartilhar
        o mesmo socket de controle.
        """
        raise NotImplementedError

    def __enter__(self) -> "Remote":
        if not self._conectado:
            self.conectar()
        return self

    def __exit__(self, *exc) -> None:
        self.fechar()

    # --- caminhos ---------------------------------------------------------
    def home(self) -> str:
        return "/"

    def normalizar(self, caminho: str) -> str:
        """Caminho absoluto, com '/', sem '..' nem barra final redundante."""
        c = (caminho or "/").replace("\\", "/")
        if not c.startswith("/"):
            c = "/" + c
        c = posixpath.normpath(c)
        return c or "/"

    def juntar(self, base: str, nome: str) -> str:
        return self.normalizar(posixpath.join(base or "/", nome))

    def pai(self, caminho: str) -> str:
        c = self.normalizar(caminho)
        return "/" if c == "/" else posixpath.dirname(c) or "/"

    def nome(self, caminho: str) -> str:
        return posixpath.basename(self.normalizar(caminho))

    # --- navegacao --------------------------------------------------------
    def listar(self, caminho: str) -> List[Entry]:
        raise NotImplementedError

    def stat(self, caminho: str) -> Optional[Entry]:
        """Entry do caminho, ou None se nao existe.

        Implementacao generica: lista o pai e procura. Backends com stat
        barato (SFTP, local) devem sobrescrever.
        """
        caminho = self.normalizar(caminho)
        if caminho == "/":
            return Entry(name="/", is_dir=True)
        alvo = self.nome(caminho)
        try:
            itens = self.listar(self.pai(caminho))
        except ErroPermanente:
            return None
        chave = alvo if self.case_sensitive else alvo.lower()
        for e in itens:
            nome = e.name if self.case_sensitive else e.name.lower()
            if nome == chave:
                return e
        return None

    def existe(self, caminho: str) -> bool:
        return self.stat(caminho) is not None

    # --- manipulacao ------------------------------------------------------
    def criar_pasta(self, caminho: str) -> None:
        raise NotImplementedError

    def criar_pastas(self, caminho: str) -> None:
        """mkdir -p. Generico: sobe ate achar uma que exista e desce criando."""
        caminho = self.normalizar(caminho)
        faltando = []
        atual = caminho
        while atual != "/" and not self.existe(atual):
            faltando.append(atual)
            atual = self.pai(atual)
        for c in reversed(faltando):
            try:
                self.criar_pasta(c)
            except ErroPermanente:
                # corrida com outra conexao criando a mesma pasta: tudo bem
                if not self.existe(c):
                    raise

    def apagar_arquivo(self, caminho: str) -> None:
        raise NotImplementedError

    def apagar_pasta(self, caminho: str) -> None:
        """Apaga uma pasta VAZIA."""
        raise NotImplementedError

    def apagar_arvore(self, caminho: str, cancelar=None) -> None:
        """Apaga recursivamente, em pos-ordem."""
        for e in self.listar(caminho):
            if cancelar is not None and cancelar.is_set():
                raise Cancelado()
            filho = self.juntar(caminho, e.name)
            if e.is_dir and not e.is_link:
                self.apagar_arvore(filho, cancelar)
            else:
                self.apagar_arquivo(filho)
        self.apagar_pasta(caminho)

    def renomear(self, de: str, para: str) -> None:
        raise NotImplementedError

    def chmod(self, caminho: str, modo: int) -> None:
        raise ErroPermanente("Este tipo de conexao nao permite mudar permissoes.")

    def definir_mtime(self, caminho: str, mtime: float) -> None:
        """Grava a data de modificacao. Backend que nao consiga deve rebaixar
        self.preserva_mtime na instancia em vez de levantar excecao toda vez -
        senao o motor compara por data e reenvia tudo para sempre."""
        raise ErroPermanente("Este tipo de conexao nao grava a data.")

    # --- transferencia ----------------------------------------------------
    def baixar(self, caminho: str, destino_fobj, *, offset: int = 0,
               limite: Optional[int] = None,
               cb: Optional[Callable[[int], None]] = None,
               cancelar=None) -> int:
        """Le 'caminho' e escreve em destino_fobj. Devolve bytes escritos.

        destino_fobj ja vem posicionado pelo chamador (seek) quando ha offset
        ou segmentacao. 'limite' so e usado quando self.segmentavel.
        """
        raise NotImplementedError

    def enviar(self, origem_fobj, caminho: str, *, tamanho: int = -1,
               offset: int = 0, cb: Optional[Callable[[int], None]] = None,
               cancelar=None) -> int:
        """Escreve origem_fobj em 'caminho'. Devolve bytes enviados."""
        raise NotImplementedError

    # --- auxiliares para os backends --------------------------------------
    @staticmethod
    def _passo(cb, n: int, cancelar) -> None:
        """Contabiliza um bloco e confere o cancelamento. Usado no laco de
        todos os backends, para o comportamento ser identico em todos."""
        if cb is not None and n:
            cb(n)
        if cancelar is not None and cancelar.is_set():
            raise Cancelado()

    def __repr__(self) -> str:
        return "<%s %s>" % (type(self).__name__, self.kind)
