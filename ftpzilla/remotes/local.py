"""O disco local visto como um Remote.

O painel local e o painel remoto sao a mesma classe de interface, entao o
disco precisa falar a mesma lingua do FTP: caminho absoluto com '/'. A
traducao para o Windows acontece so aqui, em _nativo():

    "/"                 -> a lista de unidades (C:, D:, rede)
    "C:/DEV/FTPZilla"   -> "C:\\DEV\\FTPZilla"
    "//servidor/share"  -> "\\\\servidor\\share"

Fora do Windows, '/' e a raiz de verdade e nao ha nada a traduzir.
"""
from __future__ import annotations

import os
import posixpath
import shutil
import stat as statmod
import string
import sys
from typing import List, Optional

from . import Field, RemoteSpec, register
from .base import BLOCO, Cancelado, Entry, ErroPermanente, ErroRemoto, Remote

WINDOWS = sys.platform == "win32"

#: sufixo dos arquivos sendo escritos; some quando a escrita termina
PARCIAL = ".ftzpart"


def _traduzir(exc: Exception) -> ErroRemoto:
    """Erro do sistema de arquivos -> excecao do FTPZilla.

    Disco local praticamente nao tem erro transitorio: o que da errado
    (nao existe, sem permissao, disco cheio) nao melhora repetindo.
    """
    if isinstance(exc, ErroRemoto):
        return exc
    return ErroPermanente(str(exc))


class LocalRemote(Remote):
    kind = "local"
    rotulo = "Disco local"

    is_local = True
    resume_download = True
    resume_upload = True
    segmentavel = False          # ler o mesmo disco em paralelo so atrapalha
    pode_renomear = True
    pode_chmod = not WINDOWS
    preserva_mtime = True
    case_sensitive = not WINDOWS
    max_conexoes = 1

    def __init__(self, site=None):
        super().__init__(site)
        self._conectado = True   # o disco esta sempre "conectado"

    # --- ciclo de vida ----------------------------------------------------
    def conectar(self) -> None:
        self._conectado = True

    def fechar(self) -> None:
        pass                     # nao ha o que fechar

    def viva(self) -> bool:
        return True

    def clone(self) -> "LocalRemote":
        return LocalRemote(self.site)

    # --- caminhos ---------------------------------------------------------
    def normalizar(self, caminho: str) -> str:
        c = (caminho or "/").replace("\\", "/")
        if not WINDOWS:
            return super().normalizar(c)
        if c in ("", "/"):
            return "/"
        if c.startswith("//"):                       # UNC
            return "//" + posixpath.normpath(c[2:]).lstrip("/")
        if len(c) >= 2 and c[1] == ":":              # "C:" ou "C:/..."
            unidade = c[:2].upper()
            resto = posixpath.normpath(c[2:] or "/")
            return unidade + (resto if resto.startswith("/") else "/" + resto)
        # caminho sem unidade: resolve contra o diretorio atual
        return self.normalizar(os.path.abspath(c))

    def pai(self, caminho: str) -> str:
        c = self.normalizar(caminho)
        if c == "/":
            return "/"
        if WINDOWS and len(c) == 3 and c[1] == ":":  # "C:/" -> lista de unidades
            return "/"
        if WINDOWS and c.startswith("//") and c.count("/") <= 3:
            return "/"                               # "//servidor/share"
        pai = posixpath.dirname(c.rstrip("/")) or "/"
        if WINDOWS and len(pai) == 2 and pai[1] == ":":
            pai += "/"
        return pai

    def juntar(self, base: str, nome: str) -> str:
        base = self.normalizar(base)
        if base == "/" and WINDOWS:
            return self.normalizar(nome)             # "/" + "C:" -> "C:/"
        return self.normalizar(base.rstrip("/") + "/" + nome)

    def home(self) -> str:
        return self.normalizar(os.path.expanduser("~"))

    def nativo(self, caminho: str) -> str:
        """Caminho no formato do sistema operacional."""
        c = self.normalizar(caminho)
        if not WINDOWS:
            return c
        if c == "/":
            return ""
        return c.replace("/", os.sep)

    # --- navegacao --------------------------------------------------------
    def _unidades(self) -> List[Entry]:
        """As unidades disponiveis, para a raiz virtual do Windows."""
        out = []
        for letra in string.ascii_uppercase:
            raiz = "%s:\\" % letra
            if os.path.exists(raiz):
                out.append(Entry(name="%s:" % letra, is_dir=True))
        return out

    def listar(self, caminho: str) -> List[Entry]:
        caminho = self.normalizar(caminho)
        if WINDOWS and caminho == "/":
            return self._unidades()
        alvo = self.nativo(caminho)
        out: List[Entry] = []
        try:
            with os.scandir(alvo) as it:
                for de in it:
                    try:
                        out.append(self._entry(de))
                    except OSError:
                        # arquivo sumiu no meio da listagem, ou acesso negado
                        # em um item so: mostra o que da, ignora o resto
                        out.append(Entry(name=de.name, is_dir=False))
        except OSError as e:
            raise _traduzir(e) from e
        return out

    @staticmethod
    def _entry(de: "os.DirEntry") -> Entry:
        info = de.stat(follow_symlinks=False)
        eh_link = statmod.S_ISLNK(info.st_mode)
        eh_dir = de.is_dir(follow_symlinks=True)
        return Entry(
            name=de.name,
            is_dir=eh_dir,
            size=0 if eh_dir else info.st_size,
            mtime=float(info.st_mtime),
            perms=oct(statmod.S_IMODE(info.st_mode))[2:].rjust(3, "0"),
            is_link=eh_link,
        )

    def stat(self, caminho: str) -> Optional[Entry]:
        caminho = self.normalizar(caminho)
        if caminho == "/":
            return Entry(name="/", is_dir=True)
        alvo = self.nativo(caminho)
        try:
            info = os.stat(alvo)
        except OSError:
            return None
        eh_dir = statmod.S_ISDIR(info.st_mode)
        return Entry(
            name=self.nome(caminho) or caminho,
            is_dir=eh_dir,
            size=0 if eh_dir else info.st_size,
            mtime=float(info.st_mtime),
            perms=oct(statmod.S_IMODE(info.st_mode))[2:].rjust(3, "0"),
            is_link=os.path.islink(alvo),
        )

    # --- manipulacao ------------------------------------------------------
    def criar_pasta(self, caminho: str) -> None:
        try:
            os.mkdir(self.nativo(caminho))
        except OSError as e:
            raise _traduzir(e) from e

    def criar_pastas(self, caminho: str) -> None:
        try:
            os.makedirs(self.nativo(caminho), exist_ok=True)
        except OSError as e:
            raise _traduzir(e) from e

    def apagar_arquivo(self, caminho: str) -> None:
        try:
            os.remove(self.nativo(caminho))
        except OSError as e:
            raise _traduzir(e) from e

    def apagar_pasta(self, caminho: str) -> None:
        try:
            os.rmdir(self.nativo(caminho))
        except OSError as e:
            raise _traduzir(e) from e

    def apagar_arvore(self, caminho: str, cancelar=None) -> None:
        # shutil resolve de uma vez e e bem mais rapido que descer na mao,
        # mas so da para cancelar entre os itens do primeiro nivel
        alvo = self.nativo(caminho)
        if cancelar is not None and cancelar.is_set():
            raise Cancelado()
        try:
            shutil.rmtree(alvo)
        except OSError as e:
            raise _traduzir(e) from e

    def renomear(self, de: str, para: str) -> None:
        try:
            os.replace(self.nativo(de), self.nativo(para))
        except OSError as e:
            raise _traduzir(e) from e

    def chmod(self, caminho: str, modo: int) -> None:
        if WINDOWS:
            raise ErroPermanente("O Windows nao usa permissoes no estilo Unix.")
        try:
            os.chmod(self.nativo(caminho), modo)
        except OSError as e:
            raise _traduzir(e) from e

    def definir_mtime(self, caminho: str, mtime: float) -> None:
        if not mtime:
            return
        try:
            os.utime(self.nativo(caminho), (mtime, mtime))
        except OSError as e:
            raise _traduzir(e) from e

    # --- transferencia ----------------------------------------------------
    def baixar(self, caminho: str, destino_fobj, *, offset: int = 0,
               limite=None, cb=None, cancelar=None) -> int:
        alvo = self.nativo(caminho)
        restante = limite
        escritos = 0
        try:
            with open(alvo, "rb") as f:
                if offset:
                    f.seek(offset)
                while True:
                    n = BLOCO if restante is None else min(BLOCO, restante)
                    if n <= 0:
                        break
                    bloco = f.read(n)
                    if not bloco:
                        break
                    destino_fobj.write(bloco)
                    escritos += len(bloco)
                    if restante is not None:
                        restante -= len(bloco)
                    self._passo(cb, len(bloco), cancelar)
        except OSError as e:
            raise _traduzir(e) from e
        return escritos

    def enviar(self, origem_fobj, caminho: str, *, tamanho: int = -1,
               offset: int = 0, cb=None, cancelar=None) -> int:
        """Grava em <arquivo>.ftzpart e so entao renomeia.

        Escrita atomica: quem estiver olhando a pasta nunca ve um arquivo
        pela metade com o nome final. Com offset, retoma o proprio parcial.
        """
        alvo = self.nativo(caminho)
        parcial = alvo + PARCIAL
        enviados = 0
        try:
            os.makedirs(os.path.dirname(alvo) or ".", exist_ok=True)
            modo = "r+b" if (offset and os.path.exists(parcial)) else "wb"
            with open(parcial, modo) as f:
                if offset:
                    f.seek(offset)
                    f.truncate(offset)
                while True:
                    bloco = origem_fobj.read(BLOCO)
                    if not bloco:
                        break
                    f.write(bloco)
                    enviados += len(bloco)
                    self._passo(cb, len(bloco), cancelar)
                f.flush()
                os.fsync(f.fileno())
            os.replace(parcial, alvo)
        except Cancelado:
            raise                      # o parcial fica, e o resume usa depois
        except OSError as e:
            raise _traduzir(e) from e
        return enviados


register(RemoteSpec(
    kind="local",
    label="Disco local",
    factory=lambda site=None: LocalRemote(site),
    fields=[],
    icone="disco",
    note="Arquivos desta maquina e unidades de rede mapeadas.",
))
