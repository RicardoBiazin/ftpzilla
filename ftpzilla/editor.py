"""Editor remoto: abrir um arquivo do servidor e reenviar ao salvar.

E a funcao que mais economiza tempo de quem mexe em site: editar um .php ou
um .conf sem baixar, abrir, salvar, achar a pasta de novo e subir.

Por que POLLING de os.stat e nao um observador de sistema de arquivos: sao
tipicamente menos de vinte arquivos abertos, entao olhar a cada segundo e
meio custa nada; e, principalmente, editores de verdade nao salvam
escrevendo no arquivo. Notepad++, VS Code e companhia gravam um arquivo
temporario e fazem rename por cima - o que, para um observador, parece o
arquivo ser APAGADO e outro aparecer. O polling nao se importa com como o
salvamento foi feito, so com o resultado.

O arquivo so e considerado salvo depois de ficar ESTAVEL por dois ciclos.
Sem isso, um arquivo grande seria enviado pela metade, no meio da gravacao.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import log, paths
from .remotes.base import ErroRemoto, Remote

logger = log.get()

INTERVALO = 1.5        # segundos entre verificacoes
CICLOS_ESTAVEL = 2     # quantas leituras iguais antes de considerar salvo


@dataclass
class Aberto:
    """Um arquivo remoto aberto para edicao."""
    site_id: str
    site_nome: str
    caminho_remoto: str
    caminho_local: str
    assinatura: tuple = ()          # (tamanho, mtime, hash das pontas)
    estavel: int = 0
    enviado_em: float = 0.0
    modificado: bool = False
    #: assinatura vista no ciclo anterior, ainda nao confirmada como estavel
    pendente: tuple = None

    @property
    def nome(self) -> str:
        return os.path.basename(self.caminho_local)


def _assinatura(caminho: str) -> tuple:
    """Tamanho, data e hash das pontas do arquivo.

    So as pontas (64 KB de cada lado) porque o objetivo e detectar mudanca,
    e nao provar igualdade - e ler um arquivo de 200 MB inteiro a cada 1,5s
    seria pior que o problema.
    """
    try:
        info = os.stat(caminho)
    except OSError:
        return ()
    h = hashlib.sha1()
    try:
        with open(caminho, "rb") as f:
            h.update(f.read(65536))
            if info.st_size > 131072:
                f.seek(-65536, os.SEEK_END)
                h.update(f.read(65536))
    except OSError:
        return (info.st_size, info.st_mtime, "")
    return (info.st_size, info.st_mtime, h.hexdigest())


class EditorRemoto:
    """Abre arquivos remotos numa pasta temporaria e vigia as gravacoes."""

    def __init__(self, ao_modificar: Optional[Callable] = None,
                 ao_status: Optional[Callable] = None):
        self.ao_modificar = ao_modificar     # (Aberto) -> enfileirar o envio
        self.ao_status = ao_status
        self.abertos: Dict[str, Aberto] = {}   # caminho local -> Aberto
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    def abrir(self, remote: Remote, site, caminho_remoto: str,
              programa: str = "") -> str:
        """Baixa o arquivo e abre no editor. Devolve o caminho local."""
        nome = caminho_remoto.rsplit("/", 1)[-1] or "arquivo"
        # uma subpasta por caminho remoto: dois 'config.php' de pastas
        # diferentes nao podem se sobrescrever na pasta temporaria
        marca = hashlib.sha1(
            ("%s|%s" % (getattr(site, "id", ""), caminho_remoto)).encode("utf-8")
        ).hexdigest()[:10]
        pasta = os.path.join(paths.dir_temp_editor(), marca)
        os.makedirs(pasta, exist_ok=True)
        local = os.path.join(pasta, nome)

        with open(local, "wb") as f:
            remote.baixar(caminho_remoto, f)

        aberto = Aberto(site_id=getattr(site, "id", "") if site else "",
                        site_nome=getattr(site, "nome", "") if site else "",
                        caminho_remoto=caminho_remoto, caminho_local=local,
                        assinatura=_assinatura(local))
        with self._lock:
            self.abertos[local] = aberto
        self._garantir_vigia()
        self._abrir_no_editor(local, programa)
        logger.info("Editando %s (copia em %s)", caminho_remoto, local)
        return local

    @staticmethod
    def _abrir_no_editor(caminho: str, programa: str = "") -> None:
        try:
            if programa:
                subprocess.Popen([programa, caminho])
            elif sys.platform == "win32":
                os.startfile(caminho)       # noqa: S606 - abre com o padrao
            elif sys.platform == "darwin":
                subprocess.Popen(["open", caminho])
            else:
                subprocess.Popen(["xdg-open", caminho])
        except Exception as e:      # noqa: BLE001
            logger.error("Nao consegui abrir o editor para %s: %s", caminho, e)

    # ------------------------------------------------------------------
    def _garantir_vigia(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._parar.clear()
        self._thread = threading.Thread(target=self._vigiar, name="editor",
                                        daemon=True)
        self._thread.start()

    def _vigiar(self) -> None:
        while not self._parar.is_set():
            self._parar.wait(INTERVALO)
            if self._parar.is_set():
                return
            with self._lock:
                alvos = list(self.abertos.values())
            for aberto in alvos:
                self._conferir(aberto)

    def _conferir(self, aberto: Aberto) -> None:
        agora = _assinatura(aberto.caminho_local)
        if not agora:
            return          # sumiu (ou esta no meio de um rename do editor)
        if agora == aberto.assinatura:
            aberto.estavel = 0
            return
        # mudou: so envia depois de ficar igual por dois ciclos seguidos,
        # senao um arquivo grande subiria no meio da gravacao
        if aberto.pendente == agora:
            aberto.estavel += 1
        else:
            aberto.pendente = agora
            aberto.estavel = 1
        if aberto.estavel < CICLOS_ESTAVEL:
            return

        aberto.assinatura = agora
        aberto.estavel = 0
        aberto.pendente = None
        aberto.modificado = True
        aberto.enviado_em = time.time()
        logger.info("%s foi salvo; enviando de volta para o servidor.",
                    aberto.nome)
        if self.ao_status is not None:
            self.ao_status("%s salvo: enviando para o servidor." % aberto.nome)
        if self.ao_modificar is not None:
            self.ao_modificar(aberto)

    # ------------------------------------------------------------------
    def pendentes(self) -> List[Aberto]:
        """Arquivos que mudaram e cujo envio ainda nao foi confirmado."""
        with self._lock:
            return [a for a in self.abertos.values() if a.modificado]

    def confirmar_envio(self, caminho_local: str) -> None:
        with self._lock:
            aberto = self.abertos.get(caminho_local)
        if aberto is not None:
            aberto.modificado = False

    def descartar(self, caminho_local: str, apagar: bool = True) -> None:
        with self._lock:
            aberto = self.abertos.pop(caminho_local, None)
        if aberto is None:
            return
        if apagar:
            try:
                os.remove(caminho_local)
                os.rmdir(os.path.dirname(caminho_local))
            except OSError:
                pass

    def fechar(self) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def limpar_temporarios(self, dias: int = 7) -> None:
        """Apaga copias antigas que nao estao mais abertas."""
        raiz = paths.dir_temp_editor()
        corte = time.time() - dias * 86400
        with self._lock:
            em_uso = set(self.abertos)
        for pasta, _, arquivos in os.walk(raiz, topdown=False):
            for nome in arquivos:
                caminho = os.path.join(pasta, nome)
                if caminho in em_uso:
                    continue
                try:
                    if os.path.getmtime(caminho) < corte:
                        os.remove(caminho)
                except OSError:
                    pass
            try:
                if pasta != raiz and not os.listdir(pasta):
                    os.rmdir(pasta)
            except OSError:
                pass
