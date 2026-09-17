"""OneDrive, pela API do Microsoft Graph.

O Graph aceita enderecar por CAMINHO (/me/drive/root:/pasta/arquivo:), o que
simplifica muito: nao ha ids opacos para resolver a cada navegacao, como
acontece no Google Drive.

O envio grande usa "upload session": o Graph devolve uma URL e o arquivo
sobe em pedacos com Content-Range. Isso e o que permite retomada de verdade
no envio - e a URL da sessao e guardada na fila, entao fechar o programa no
meio nao obriga a recomecar.
"""
from __future__ import annotations

import os
from typing import List, Optional

from . import Field, RemoteSpec, register
from .. import log, oauth
from .base import (BLOCO, Cancelado, Entry, ErroPermanente, ErroRemoto, Remote)
from .cloud_base import PEDACO, CloudRemote, traduzir_http

logger = log.get()

GRAFO = "https://graph.microsoft.com/v1.0"
#: campos pedidos na listagem: menos dados na resposta, menos tempo de espera
CAMPOS = "id,name,size,folder,file,lastModifiedDateTime,eTag"


class OneDriveRemote(CloudRemote):
    kind = "onedrive"
    rotulo = "OneDrive"
    provedor = "microsoft"

    def _url(self, caminho: str, sufixo: str = "") -> str:
        """Monta a URL do Graph para um caminho.

        A raiz tem sintaxe propria: '/me/drive/root' e nao
        '/me/drive/root::', que o Graph recusa.
        """
        caminho = self.normalizar(caminho)
        from urllib.parse import quote
        if caminho == "/":
            base = "%s/me/drive/root" % GRAFO
            return base + (("/" + sufixo) if sufixo else "")
        base = "%s/me/drive/root:%s:" % (GRAFO, quote(caminho))
        return base + (("/" + sufixo) if sufixo else "")

    # --- navegacao --------------------------------------------------------
    def _entry(self, item: dict) -> Entry:
        eh_dir = "folder" in item
        return Entry(
            name=item.get("name", ""),
            is_dir=eh_dir,
            size=0 if eh_dir else int(item.get("size") or 0),
            mtime=self._iso_para_epoch(item.get("lastModifiedDateTime", "")),
            etag=(item.get("eTag") or "").strip('"'),
            id=item.get("id", ""),
        )

    def listar(self, caminho: str) -> List[Entry]:
        url = self._url(caminho, "children")
        params = {"$select": CAMPOS, "$top": "200"}
        saida = []
        while url:
            dados = self.json("GET", url, params=params)
            for item in dados.get("value", []):
                saida.append(self._entry(item))
            # a paginacao vem com a URL inteira montada; os params ja estao
            # dentro dela, e repetir levaria a um 400
            url = dados.get("@odata.nextLink", "")
            params = None
        return saida

    def stat(self, caminho: str) -> Optional[Entry]:
        caminho = self.normalizar(caminho)
        if caminho == "/":
            return Entry(name="/", is_dir=True)
        resp = self.pedir("GET", self._url(caminho),
                          params={"$select": CAMPOS}, esperar=None)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise traduzir_http(resp.status_code, resp.text)
        return self._entry(resp.json())

    # --- manipulacao ------------------------------------------------------
    def criar_pasta(self, caminho: str) -> None:
        pai = self.pai(caminho)
        corpo = {"name": self.nome(caminho), "folder": {},
                 "@microsoft.graph.conflictBehavior": "fail"}
        resp = self.pedir("POST", self._url(pai, "children"), json=corpo,
                          esperar=None)
        if resp.status_code == 409:
            raise ErroPermanente("Ja existe algo com esse nome.")
        if resp.status_code >= 400:
            raise traduzir_http(resp.status_code, resp.text)

    def apagar_arquivo(self, caminho: str) -> None:
        self.pedir("DELETE", self._url(caminho), esperar=(204, 200))

    def apagar_pasta(self, caminho: str) -> None:
        self.apagar_arquivo(caminho)

    def apagar_arvore(self, caminho: str, cancelar=None) -> None:
        # o Graph apaga a pasta com o conteudo numa chamada so
        self.apagar_arquivo(caminho)

    def renomear(self, de: str, para: str) -> None:
        corpo = {"name": self.nome(para)}
        pai_novo = self.pai(para)
        if pai_novo != self.pai(de):
            corpo["parentReference"] = {"path": "/drive/root:%s" % pai_novo}
        self.pedir("PATCH", self._url(de), json=corpo, esperar=(200, 201))

    def definir_mtime(self, caminho: str, mtime: float) -> None:
        if not mtime:
            return
        corpo = {"fileSystemInfo":
                 {"lastModifiedDateTime": self._epoch_para_iso(mtime)}}
        try:
            self.pedir("PATCH", self._url(caminho), json=corpo,
                       esperar=(200, 201))
        except ErroRemoto as e:
            logger.info("Nao deu para gravar a data em %s: %s", caminho, e)
            self.preserva_mtime = False

    # --- transferencia ----------------------------------------------------
    def baixar(self, caminho: str, destino_fobj, *, offset: int = 0,
               limite=None, cb=None, cancelar=None) -> int:
        cabecalhos = {}
        if offset or limite is not None:
            fim = "" if limite is None else str(offset + limite - 1)
            cabecalhos["Range"] = "bytes=%d-%s" % (offset, fim)
        resp = self.pedir("GET", self._url(caminho, "content"),
                          headers=cabecalhos, stream=True,
                          esperar=(200, 206))
        try:
            return self._escrever(destino_fobj, resp, cb, cancelar)
        finally:
            resp.close()

    def enviar(self, origem_fobj, caminho: str, *, tamanho: int = -1,
               offset: int = 0, cb=None, cancelar=None) -> int:
        if tamanho >= 0 and tamanho < 4 * 1024 * 1024 and not offset:
            return self._enviar_simples(origem_fobj, caminho, cb, cancelar)
        return self._enviar_em_pedacos(origem_fobj, caminho, tamanho, offset,
                                       cb, cancelar)

    def _enviar_simples(self, origem_fobj, caminho, cb, cancelar) -> int:
        """Arquivo pequeno: um PUT so. Abrir sessao para 100 KB e desperdicio
        de duas viagens ate o servidor."""
        dados = origem_fobj.read()
        self.pedir("PUT", self._url(caminho, "content"), data=dados,
                   esperar=(200, 201))
        self._passo(cb, len(dados), cancelar)
        return len(dados)

    def _enviar_em_pedacos(self, origem_fobj, caminho, tamanho, offset, cb,
                           cancelar) -> int:
        url = self.criar_sessao_envio(caminho)
        enviados = 0
        posicao = offset
        while True:
            pedaco = origem_fobj.read(PEDACO)
            if not pedaco:
                break
            fim = posicao + len(pedaco) - 1
            total = tamanho if tamanho >= 0 else fim + 1
            cabecalhos = {
                "Content-Length": str(len(pedaco)),
                "Content-Range": "bytes %d-%d/%d" % (posicao, fim, total),
            }
            # a URL da sessao ja carrega a autorizacao; mandar o Bearer de
            # novo faz o Graph recusar alguns pedidos
            resp = self.http.put(url, data=pedaco, headers=cabecalhos,
                                 timeout=120)
            if resp.status_code not in (200, 201, 202):
                raise traduzir_http(resp.status_code, resp.text)
            enviados += len(pedaco)
            posicao = fim + 1
            self._passo(cb, len(pedaco), cancelar)
        return enviados

    def criar_sessao_envio(self, caminho: str) -> str:
        corpo = {"item": {"@microsoft.graph.conflictBehavior": "replace"}}
        dados = self.json("POST", self._url(caminho, "createUploadSession"),
                          json=corpo, esperar=(200, 201))
        url = dados.get("uploadUrl", "")
        if not url:
            raise ErroRemoto("O OneDrive nao devolveu a URL de envio.")
        return url

    def proximo_offset(self, url_sessao: str) -> int:
        """Quanto o servidor ja recebeu numa sessao de envio retomada.

        E o que permite continuar um envio grande depois de fechar o
        programa: pergunta-se ao servidor onde ele parou, em vez de confiar
        num numero guardado aqui.
        """
        resp = self.http.get(url_sessao, timeout=60)
        if resp.status_code >= 400:
            return 0
        try:
            faixas = resp.json().get("nextExpectedRanges") or []
        except ValueError:
            return 0
        if not faixas:
            return 0
        return int(str(faixas[0]).split("-")[0])


register(RemoteSpec(
    kind="onedrive",
    label="OneDrive",
    factory=lambda site=None: OneDriveRemote(site),
    icone="servidor",
    requires=["requests"],
    fields=[
        Field("client_id", "ID do aplicativo", required=True,
              help="Registre um aplicativo no portal do Azure (Entra ID) "
                   "como cliente publico e cadastre o redirecionamento "
                   "http://localhost:53682/."),
        Field("tenant", "Organizacao (tenant)", default="common",
              help="'common' serve para conta pessoal e corporativa."),
        Field("refresh_token", "Conta", kind="oauth", provedor="microsoft",
              help="Clique para autorizar no navegador."),
        Field("pasta_remota", "Pasta inicial"),
    ],
    note="Nao ha credencial embutida no programa: ela seria publica no "
         "executavel, e a Microsoft nao permite. O cadastro leva um minuto "
         "e fica na sua conta.",
))
