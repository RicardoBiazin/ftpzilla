"""Base dos backends de nuvem (Google Drive, OneDrive).

O que e comum aos dois e mais do que parece: sessao HTTP com repeticao
automatica, renovacao de token quando o servico responde 401, e a decisao de
NAO segmentar download.

Sobre nao segmentar: nos protocolos de arquivo o gargalo e a banda por
conexao, e mais conexoes ajudam. Em API de nuvem o gargalo e a cota de
REQUISICOES - o Drive e o Graph respondem 429 e mandam esperar. Paralelizar
ali nao acelera nada e ainda aproxima o bloqueio, entao segmentavel fica
False de proposito.

A repeticao fica na camada HTTP (urllib3 Retry) porque os dois servicos
respondem 429 com Retry-After, e respeitar esse cabecalho e a diferenca
entre ser desbloqueado em segundos ou em minutos.
"""
from __future__ import annotations

import time
from typing import Optional

from .. import log, oauth
from .base import (BLOCO, Cancelado, Entry, ErroAutenticacao, ErroPermanente,
                   ErroRemoto, ErroTransitorio, Remote)

logger = log.get()

TIMEOUT = 60
#: pedaco do envio em varias partes. Os dois servicos exigem multiplo de
#: 320 KiB no OneDrive; 8 MiB e multiplo e da um bom equilibrio.
PEDACO = 8 * 320 * 1024


def _requests():
    import requests
    return requests


def sessao_http():
    """requests.Session com repeticao para erro passageiro e 429."""
    requests = _requests()
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    retry = Retry(total=4, connect=4, read=2, status=3,
                  backoff_factor=0.6,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "PUT", "POST", "DELETE",
                                             "PATCH", "HEAD"]),
                  respect_retry_after_header=True)
    adaptador = HTTPAdapter(max_retries=retry, pool_maxsize=8)
    s.mount("https://", adaptador)
    s.mount("http://", adaptador)
    return s


def traduzir_http(status: int, texto: str) -> ErroRemoto:
    if status in (401, 403):
        if "quota" in (texto or "").lower() or status == 403:
            return ErroTransitorio("HTTP %d: %s" % (status, texto[:200]))
        return ErroAutenticacao("HTTP %d: %s" % (status, texto[:200]))
    if status == 404:
        return ErroPermanente("nao encontrado")
    if status in (409, 412, 413):
        return ErroPermanente("HTTP %d: %s" % (status, texto[:200]))
    if status == 429 or status >= 500:
        return ErroTransitorio("HTTP %d: %s" % (status, texto[:200]))
    return ErroRemoto("HTTP %d: %s" % (status, texto[:200]))


class CloudRemote(Remote):
    """Pedaco comum de Drive e OneDrive."""

    provedor = ""
    tem_dirs = True
    resume_download = True
    resume_upload = True
    segmentavel = False        # ver o comentario no topo do arquivo
    pode_chmod = False
    preserva_mtime = True
    case_sensitive = False
    max_conexoes = 4

    def __init__(self, site=None):
        super().__init__(site)
        self.sessao = None
        self.http = None
        self.suja = False
        self._cache_id = {}     # caminho -> id (some ao fechar a conexao)

    # --- conexao ----------------------------------------------------------
    def conectar(self) -> None:
        try:
            self.sessao = oauth.sessao_de(self.site, self.provedor)
            self.http = sessao_http()
            self.sessao.token()          # falha aqui se o acesso foi revogado
            self._conectado = True
            self.suja = False
        except ValueError as e:
            raise ErroAutenticacao(str(e)) from e
        except RuntimeError as e:
            raise ErroAutenticacao(str(e)) from e

    def fechar(self) -> None:
        try:
            if self.http is not None:
                self.http.close()
        except Exception:
            pass
        self.http = None
        self._cache_id.clear()
        self._conectado = False

    def viva(self) -> bool:
        return bool(self._conectado and self.sessao is not None)

    def clone(self):
        return type(self)(self.site)

    # --- HTTP -------------------------------------------------------------
    def pedir(self, metodo: str, url: str, *, esperar=(200, 201, 204),
              **kwargs):
        """Chamada autenticada. Renova o token uma vez em caso de 401.

        401 nao significa so 'token vencido': acontece tambem quando o
        usuario revoga o acesso do aplicativo. Por isso renova-se UMA vez e,
        se continuar 401, o erro sobe como autenticacao - que a fila trata
        pausando o site em vez de repetir para sempre.
        """
        cabecalhos = dict(kwargs.pop("headers", {}) or {})
        cabecalhos.update(self.sessao.cabecalho())
        kwargs.setdefault("timeout", TIMEOUT)
        resp = self.http.request(metodo, url, headers=cabecalhos, **kwargs)
        if resp.status_code == 401:
            cabecalhos.update(self.sessao.cabecalho(forcar=True))
            resp = self.http.request(metodo, url, headers=cabecalhos, **kwargs)
        if esperar and resp.status_code not in esperar:
            raise traduzir_http(resp.status_code, resp.text)
        return resp

    def json(self, metodo: str, url: str, **kwargs) -> dict:
        resp = self.pedir(metodo, url, **kwargs)
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # --- apoio ------------------------------------------------------------
    @staticmethod
    def _iso_para_epoch(texto: str) -> float:
        if not texto:
            return 0.0
        t = texto.replace("Z", "+00:00")
        try:
            from datetime import datetime
            return datetime.fromisoformat(t).timestamp()
        except ValueError:
            return 0.0

    @staticmethod
    def _epoch_para_iso(quando: float) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(quando, timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")

    def _escrever(self, destino_fobj, resp, cb, cancelar) -> int:
        escritos = 0
        for pedaco in resp.iter_content(chunk_size=BLOCO):
            if not pedaco:
                continue
            destino_fobj.write(pedaco)
            escritos += len(pedaco)
            self._passo(cb, len(pedaco), cancelar)
        return escritos

    # --- nao se aplica a nuvem --------------------------------------------
    def chmod(self, caminho: str, modo: int) -> None:
        raise ErroPermanente("Armazenamento em nuvem nao usa permissoes "
                             "no estilo Unix.")
