"""Google Drive.

O Drive nao tem caminhos: tem ids e uma relacao de "pais". Duas pastas
irmas podem ter exatamente o mesmo nome, e um arquivo pode ter mais de um
pai. Para o FTPZilla mostrar uma arvore normal, cada componente do caminho e
resolvido em id, da raiz para baixo, e o resultado fica num cache que morre
junto com a conexao - guardar id entre sessoes daria caminho apontando para
arquivo errado depois de alguem mover algo pelo navegador.

Quando ha nomes repetidos na mesma pasta, o primeiro (por data de criacao)
ganha, e isso e registrado no log. Nao ha resposta certa: e uma limitacao do
modelo do Drive, nao do programa.
"""
from __future__ import annotations

import json as jsonmod
from typing import List, Optional

from . import Field, RemoteSpec, register
from .. import log
from .base import (BLOCO, Entry, ErroPermanente, ErroRemoto, Remote)
from .cloud_base import PEDACO, CloudRemote, traduzir_http

logger = log.get()

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
PASTA = "application/vnd.google-apps.folder"
CAMPOS = "id,name,mimeType,size,modifiedTime,md5Checksum,trashed"


def _escapar(nome: str) -> str:
    return nome.replace("\\", "\\\\").replace("'", "\\'")


class GDriveRemote(CloudRemote):
    kind = "gdrive"
    rotulo = "Google Drive"
    provedor = "google"
    case_sensitive = True       # o Drive diferencia maiusculas

    # --- resolucao de caminho em id ---------------------------------------
    def id_de(self, caminho: str) -> str:
        caminho = self.normalizar(caminho)
        if caminho == "/":
            return "root"
        if caminho in self._cache_id:
            return self._cache_id[caminho]

        pai = self.id_de(self.pai(caminho))
        nome = self.nome(caminho)
        consulta = ("'%s' in parents and name = '%s' and trashed = false"
                    % (pai, _escapar(nome)))
        dados = self.json("GET", API + "/files",
                          params={"q": consulta, "fields": "files(%s)" % CAMPOS,
                                  "pageSize": "10",
                                  "orderBy": "createdTime",
                                  "supportsAllDrives": "true",
                                  "includeItemsFromAllDrives": "true"})
        arquivos = dados.get("files", [])
        if not arquivos:
            raise ErroPermanente("nao encontrado: %s" % caminho)
        if len(arquivos) > 1:
            logger.info("O Drive tem %d itens chamados '%s' na mesma pasta; "
                        "usando o mais antigo.", len(arquivos), nome)
        ident = arquivos[0]["id"]
        self._cache_id[caminho] = ident
        return ident

    def _entry(self, item: dict) -> Entry:
        eh_dir = item.get("mimeType") == PASTA
        return Entry(
            name=item.get("name", ""),
            is_dir=eh_dir,
            size=0 if eh_dir else int(item.get("size") or 0),
            mtime=self._iso_para_epoch(item.get("modifiedTime", "")),
            etag=item.get("md5Checksum", ""),
            id=item.get("id", ""),
        )

    # --- navegacao --------------------------------------------------------
    def listar(self, caminho: str) -> List[Entry]:
        pai = self.id_de(caminho)
        consulta = "'%s' in parents and trashed = false" % pai
        saida = []
        pagina = ""
        while True:
            params = {"q": consulta,
                      "fields": "nextPageToken, files(%s)" % CAMPOS,
                      "pageSize": "200",
                      "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true"}
            if pagina:
                params["pageToken"] = pagina
            dados = self.json("GET", API + "/files", params=params)
            for item in dados.get("files", []):
                e = self._entry(item)
                saida.append(e)
                self._cache_id[self.juntar(caminho, e.name)] = e.id
            pagina = dados.get("nextPageToken", "")
            if not pagina:
                break
        return saida

    def stat(self, caminho: str) -> Optional[Entry]:
        caminho = self.normalizar(caminho)
        if caminho == "/":
            return Entry(name="/", is_dir=True)
        try:
            ident = self.id_de(caminho)
        except ErroPermanente:
            return None
        dados = self.json("GET", "%s/files/%s" % (API, ident),
                          params={"fields": CAMPOS,
                                  "supportsAllDrives": "true"})
        if not dados:
            return None
        return self._entry(dados)

    # --- manipulacao ------------------------------------------------------
    def criar_pasta(self, caminho: str) -> None:
        pai = self.id_de(self.pai(caminho))
        corpo = {"name": self.nome(caminho), "mimeType": PASTA,
                 "parents": [pai]}
        dados = self.json("POST", API + "/files", json=corpo,
                          params={"fields": "id", "supportsAllDrives": "true"},
                          esperar=(200, 201))
        if dados.get("id"):
            self._cache_id[self.normalizar(caminho)] = dados["id"]

    def apagar_arquivo(self, caminho: str) -> None:
        ident = self.id_de(caminho)
        self.pedir("DELETE", "%s/files/%s" % (API, ident),
                   params={"supportsAllDrives": "true"},
                   esperar=(204, 200))
        self._cache_id.pop(self.normalizar(caminho), None)

    def apagar_pasta(self, caminho: str) -> None:
        self.apagar_arquivo(caminho)

    def apagar_arvore(self, caminho: str, cancelar=None) -> None:
        # apagar a pasta leva o conteudo junto, numa chamada so
        self.apagar_arquivo(caminho)

    def renomear(self, de: str, para: str) -> None:
        ident = self.id_de(de)
        params = {"fields": "id", "supportsAllDrives": "true"}
        corpo = {"name": self.nome(para)}
        pai_antigo, pai_novo = self.pai(de), self.pai(para)
        if pai_antigo != pai_novo:
            params["addParents"] = self.id_de(pai_novo)
            params["removeParents"] = self.id_de(pai_antigo)
        self.pedir("PATCH", "%s/files/%s" % (API, ident), json=corpo,
                   params=params, esperar=(200, 201))
        self._cache_id.pop(self.normalizar(de), None)

    def definir_mtime(self, caminho: str, mtime: float) -> None:
        if not mtime:
            return
        try:
            ident = self.id_de(caminho)
            self.pedir("PATCH", "%s/files/%s" % (API, ident),
                       json={"modifiedTime": self._epoch_para_iso(mtime)},
                       params={"fields": "id", "supportsAllDrives": "true"},
                       esperar=(200, 201))
        except ErroRemoto as e:
            logger.info("Nao deu para gravar a data em %s: %s", caminho, e)
            self.preserva_mtime = False

    def hash_remoto(self, caminho: str, algoritmo: str = "md5"):
        """O Drive publica o MD5 de arquivos comuns - da para conferir de
        graca o que foi baixado."""
        if algoritmo != "md5":
            return None
        e = self.stat(caminho)
        if e is not None and e.etag:
            return ("md5", e.etag)
        return None

    # --- transferencia ----------------------------------------------------
    def baixar(self, caminho: str, destino_fobj, *, offset: int = 0,
               limite=None, cb=None, cancelar=None) -> int:
        ident = self.id_de(caminho)
        cabecalhos = {}
        if offset or limite is not None:
            fim = "" if limite is None else str(offset + limite - 1)
            cabecalhos["Range"] = "bytes=%d-%s" % (offset, fim)
        resp = self.pedir("GET", "%s/files/%s" % (API, ident),
                          params={"alt": "media", "supportsAllDrives": "true"},
                          headers=cabecalhos, stream=True, esperar=(200, 206))
        try:
            return self._escrever(destino_fobj, resp, cb, cancelar)
        finally:
            resp.close()

    def enviar(self, origem_fobj, caminho: str, *, tamanho: int = -1,
               offset: int = 0, cb=None, cancelar=None) -> int:
        url = self.criar_sessao_envio(caminho, tamanho)
        enviados = 0
        posicao = offset
        while True:
            pedaco = origem_fobj.read(PEDACO)
            if not pedaco:
                break
            fim = posicao + len(pedaco) - 1
            total = str(tamanho) if tamanho >= 0 else "*"
            cabecalhos = {"Content-Length": str(len(pedaco)),
                          "Content-Range": "bytes %d-%d/%s"
                                           % (posicao, fim, total)}
            resp = self.http.put(url, data=pedaco, headers=cabecalhos,
                                 timeout=120)
            # 308 = "continue mandando"; 200/201 = terminou
            if resp.status_code not in (200, 201, 308):
                raise traduzir_http(resp.status_code, resp.text)
            enviados += len(pedaco)
            posicao = fim + 1
            self._passo(cb, len(pedaco), cancelar)
        self._cache_id.pop(self.normalizar(caminho), None)
        return enviados

    def criar_sessao_envio(self, caminho: str, tamanho: int = -1) -> str:
        """Envio retomavel. Usado sempre, e nao so em arquivo grande: manter
        um caminho so evita que um bug apareca apenas acima de 5 MB."""
        nome = self.nome(caminho)
        existente = None
        try:
            existente = self.id_de(caminho)
        except ErroPermanente:
            existente = None

        if existente:
            url = "%s/files/%s" % (UPLOAD, existente)
            metodo = "PATCH"
            corpo = {"name": nome}
        else:
            url = UPLOAD + "/files"
            metodo = "POST"
            corpo = {"name": nome, "parents": [self.id_de(self.pai(caminho))]}

        resp = self.pedir(metodo, url,
                          params={"uploadType": "resumable",
                                  "supportsAllDrives": "true"},
                          json=corpo, esperar=(200, 201))
        destino = resp.headers.get("Location") or resp.headers.get("location")
        if not destino:
            raise ErroRemoto("O Drive nao devolveu a URL de envio.")
        return destino


register(RemoteSpec(
    kind="gdrive",
    label="Google Drive",
    factory=lambda site=None: GDriveRemote(site),
    icone="servidor",
    requires=["requests"],
    fields=[
        Field("client_id", "ID do cliente", required=True,
              help="No Google Cloud Console, credencial OAuth do tipo "
                   "'Aplicativo para computador', com a API do Drive ativada."),
        Field("client_secret", "Segredo do cliente", kind="password",
              required=True,
              help="O Google exige o segredo mesmo em aplicativo de "
                   "computador."),
        Field("refresh_token", "Conta", kind="oauth", provedor="google",
              help="Clique para autorizar no navegador."),
        Field("pasta_remota", "Pasta inicial"),
    ],
    note="Nao ha credencial embutida no programa: ela seria publica no "
         "executavel. O cadastro fica na sua conta do Google.",
))
