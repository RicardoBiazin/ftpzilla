"""Google Drive e OneDrive, contra APIs falsas que rodam no proprio teste."""
from __future__ import annotations

import hashlib
import io
import os
import time

import ajuda
import apis_falsas
from ajuda import PastaTemp, checar, igual, levanta, pular
from apis_falsas import Estado, servidor_drive, servidor_graph

from ftpzilla import oauth
from ftpzilla.remotes.base import ErroPermanente
from ftpzilla.sites import Site


def _precisa():
    try:
        import requests  # noqa: F401
    except ImportError:
        pular("requests nao instalado (pip install -r requirements.txt)")


def _site(kind, **extra) -> Site:
    dados = dict(nome="nuvem", kind=kind)
    dados.update(extra)
    site = Site(**dados)
    site.opcoes["client_id"] = "cliente-de-teste"
    site.opcoes["client_secret"] = "segredo-de-teste"
    site.opcoes["refresh_token"] = apis_falsas.REFRESH
    site.opcoes["tenant"] = "common"
    return site


class _Apontar:
    """Aponta as constantes de URL para o servidor falso e desfaz no fim."""

    def __init__(self, modulo_base: str, base: str, provedor: str):
        self.base = base
        self.provedor = provedor
        self.modulo_base = modulo_base
        self.antigos = {}

    def __enter__(self):
        from ftpzilla.remotes import gdrive, onedrive
        prov = oauth.PROVEDORES[self.provedor]
        self.antigos["token_url"] = prov.token_url
        prov.token_url = self.base + "/token"
        if self.modulo_base == "graph":
            self.antigos["GRAFO"] = onedrive.GRAFO
            onedrive.GRAFO = self.base
        else:
            self.antigos["API"] = gdrive.API
            self.antigos["UPLOAD"] = gdrive.UPLOAD
            gdrive.API = self.base + "/drive/v3"
            gdrive.UPLOAD = self.base + "/upload/drive/v3"
        return self

    def __exit__(self, *exc):
        from ftpzilla.remotes import gdrive, onedrive
        oauth.PROVEDORES[self.provedor].token_url = self.antigos["token_url"]
        if self.modulo_base == "graph":
            onedrive.GRAFO = self.antigos["GRAFO"]
        else:
            gdrive.API = self.antigos["API"]
            gdrive.UPLOAD = self.antigos["UPLOAD"]


def _arvore():
    return {"/leiame.txt": b"conteudo do leiame",
            "/dados.bin": bytes(range(256)) * 40,
            "/pasta/dentro.txt": b"estou dentro",
            "/pasta/outro.txt": b"outro",
            "/pasta/terceiro.txt": b"terceiro"}


# ---------------------------------------------------------------------------
# OneDrive
# ---------------------------------------------------------------------------
def _onedrive(base):
    from ftpzilla.remotes.onedrive import OneDriveRemote
    r = OneDriveRemote(_site("onedrive"))
    r.conectar()
    return r


def teste_onedrive_navegacao():
    _precisa()
    estado = Estado()
    estado.povoar(_arvore())
    with servidor_graph(estado) as base, _Apontar("graph", base, "microsoft"):
        r = _onedrive(base)
        try:
            nomes = sorted(e.name for e in r.listar("/"))
            igual(nomes, ["dados.bin", "leiame.txt", "pasta"], "lista a raiz")
            itens = {e.name: e for e in r.listar("/")}
            checar(itens["pasta"].is_dir, "reconhece a pasta")
            igual(itens["leiame.txt"].size, 18, "tamanho do arquivo")
            checar(itens["leiame.txt"].mtime > 0, "data preenchida")

            filhos = sorted(e.name for e in r.listar("/pasta"))
            igual(filhos, ["dentro.txt", "outro.txt", "terceiro.txt"],
                  "a paginacao do Graph foi seguida ate o fim")

            e = r.stat("/leiame.txt")
            checar(e is not None and e.size == 18, "stat do arquivo")
            igual(r.stat("/nao_existe"), None, "stat de inexistente e None")
        finally:
            r.fechar()


def teste_onedrive_manipulacao():
    _precisa()
    estado = Estado()
    estado.povoar(_arvore())
    with servidor_graph(estado) as base, _Apontar("graph", base, "microsoft"):
        r = _onedrive(base)
        try:
            r.criar_pasta("/nova")
            checar(r.existe("/nova"), "criar_pasta")
            levanta(ErroPermanente, lambda: r.criar_pasta("/nova"),
                    "criar pasta repetida vira ErroPermanente")
            r.criar_pastas("/nova/a/b")
            checar(r.existe("/nova/a/b"), "criar_pastas cria a arvore")
            r.renomear("/leiame.txt", "/renomeado.txt")
            checar(r.existe("/renomeado.txt"), "renomear")
            r.apagar_arquivo("/renomeado.txt")
            checar(not r.existe("/renomeado.txt"), "apagar_arquivo")
            r.apagar_arvore("/nova")
            checar(not r.existe("/nova"), "apagar_arvore")

            quando = time.time() - 86400
            r.definir_mtime("/dados.bin", quando)
            obtido = r.stat("/dados.bin").mtime
            checar(abs(obtido - quando) < 2.0,
                   "definir_mtime grava a data em UTC (erro de %.0fs)"
                   % (obtido - quando))
        finally:
            r.fechar()


def teste_onedrive_transferencia():
    _precisa()
    estado = Estado()
    estado.povoar(_arvore())
    with servidor_graph(estado) as base, _Apontar("graph", base, "microsoft"):
        r = _onedrive(base)
        try:
            buf = io.BytesIO()
            pedacos = []
            n = r.baixar("/dados.bin", buf, cb=pedacos.append)
            igual(n, 10240, "baixou o arquivo inteiro")
            igual(sum(pedacos), 10240, "a soma dos deltas bate")

            buf = io.BytesIO()
            r.baixar("/dados.bin", buf, offset=10000)
            igual(len(buf.getvalue()), 240,
                  "o cabecalho Range faz a retomada do download")

            r.enviar(io.BytesIO(b"pequeno"), "/pequeno.txt", tamanho=7)
            igual(r.stat("/pequeno.txt").size, 7,
                  "arquivo pequeno sobe num PUT so")

            grande = bytes(range(256)) * 12000      # ~3 MB
            r.enviar(io.BytesIO(grande), "/grande.bin", tamanho=len(grande))
            igual(r.stat("/grande.bin").size, len(grande),
                  "arquivo grande sobe em pedacos e chega inteiro")
            no = estado.por_caminho("/grande.bin")
            igual(hashlib.md5(no.dados).hexdigest(),
                  hashlib.md5(grande).hexdigest(),
                  "e o conteudo bate byte a byte")
        finally:
            r.fechar()


def teste_onedrive_retoma_envio():
    """A sessao de envio guarda onde parou: perguntar ao servidor e mais
    confiavel que confiar num numero guardado aqui."""
    _precisa()
    estado = Estado()
    estado.povoar(_arvore())
    with servidor_graph(estado) as base, _Apontar("graph", base, "microsoft"):
        r = _onedrive(base)
        try:
            url = r.criar_sessao_envio("/retomado.bin")
            igual(r.proximo_offset(url), 0, "sessao nova comeca do zero")
            metade = b"A" * (320 * 1024)
            resp = r.http.put(url, data=metade,
                              headers={"Content-Range":
                                       "bytes 0-%d/%d" % (len(metade) - 1,
                                                          len(metade) * 2)})
            igual(resp.status_code, 202, "o servidor aceitou o primeiro pedaco")
            igual(r.proximo_offset(url), len(metade),
                  "e informa exatamente de onde continuar")
        finally:
            r.fechar()


def teste_onedrive_renova_token_no_401():
    """Token vencido (ou revogado) devolve 401. O cliente tem que renovar e
    repetir sozinho - e esse caminho so aparece em producao, horas depois,
    se estiver quebrado."""
    _precisa()
    estado = Estado()
    estado.povoar(_arvore())
    with servidor_graph(estado) as base, _Apontar("graph", base, "microsoft"):
        r = _onedrive(base)
        try:
            renovacoes = estado.tokens_renovados
            r.sessao._token = "token-invalido"
            r.sessao._expira = time.time() + 3600    # ele *acha* que e valido
            itens = r.listar("/")
            checar(len(itens) > 0, "a listagem funcionou mesmo assim")
            checar(estado.tokens_renovados > renovacoes,
                   "porque o cliente renovou o token depois do 401")
        finally:
            r.fechar()


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
def _gdrive(base):
    from ftpzilla.remotes.gdrive import GDriveRemote
    r = GDriveRemote(_site("gdrive"))
    r.conectar()
    return r


def teste_gdrive_navegacao():
    _precisa()
    estado = Estado()
    estado.povoar(_arvore())
    with servidor_drive(estado) as base, _Apontar("drive", base, "google"):
        r = _gdrive(base)
        try:
            nomes = sorted(e.name for e in r.listar("/"))
            igual(nomes, ["dados.bin", "leiame.txt", "pasta"], "lista a raiz")
            filhos = sorted(e.name for e in r.listar("/pasta"))
            igual(filhos, ["dentro.txt", "outro.txt", "terceiro.txt"],
                  "a paginacao por pageToken foi seguida ate o fim")
            e = r.stat("/pasta/dentro.txt")
            checar(e is not None and e.size == 12,
                   "o caminho foi resolvido em id, componente por componente")
            igual(r.stat("/nao/existe"), None,
                  "caminho inexistente devolve None")
        finally:
            r.fechar()


def teste_gdrive_cache_de_id():
    """Resolver o caminho custa uma consulta por componente. O cache evita
    repetir isso a cada clique - e morre com a conexao, porque id guardado
    entre sessoes apontaria para o arquivo errado depois de alguem mover
    algo pelo navegador."""
    _precisa()
    estado = Estado()
    estado.povoar(_arvore())
    with servidor_drive(estado) as base, _Apontar("drive", base, "google"):
        r = _gdrive(base)
        try:
            ident = r.id_de("/pasta/dentro.txt")
            checar(bool(ident), "resolveu o id")
            igual(r.id_de("/pasta/dentro.txt"), ident,
                  "a segunda vez devolve o mesmo id")
            checar("/pasta/dentro.txt" in r._cache_id, "e ficou no cache")
            r.fechar()
            igual(len(r._cache_id), 0, "fechar a conexao esvazia o cache")
        finally:
            try:
                r.fechar()
            except Exception:
                pass


def teste_gdrive_manipulacao_e_transferencia():
    _precisa()
    estado = Estado()
    estado.povoar(_arvore())
    with servidor_drive(estado) as base, _Apontar("drive", base, "google"):
        r = _gdrive(base)
        try:
            r.criar_pasta("/nova")
            checar(r.existe("/nova"), "criar_pasta")
            r.renomear("/leiame.txt", "/renomeado.txt")
            checar(r.existe("/renomeado.txt"), "renomear")
            r.apagar_arquivo("/renomeado.txt")
            checar(not r.existe("/renomeado.txt"), "apagar_arquivo")

            buf = io.BytesIO()
            r.baixar("/dados.bin", buf)
            igual(len(buf.getvalue()), 10240, "baixou o arquivo inteiro")

            buf = io.BytesIO()
            r.baixar("/dados.bin", buf, offset=10000)
            igual(len(buf.getvalue()), 240, "Range faz a retomada do download")

            dados = b"subindo pelo envio retomavel" * 100
            r.enviar(io.BytesIO(dados), "/nova/enviado.txt", tamanho=len(dados))
            e = r.stat("/nova/enviado.txt")
            checar(e is not None and e.size == len(dados),
                   "o envio retomavel criou o arquivo")

            novo = b"substituido"
            r.enviar(io.BytesIO(novo), "/nova/enviado.txt", tamanho=len(novo))
            igual(r.stat("/nova/enviado.txt").size, len(novo),
                  "enviar de novo substitui em vez de duplicar")
        finally:
            r.fechar()


def teste_gdrive_hash_do_servidor():
    """O Drive publica o MD5: da para provar de graca que o download veio
    inteiro, em vez de so comparar tamanho."""
    _precisa()
    estado = Estado()
    estado.povoar(_arvore())
    with servidor_drive(estado) as base, _Apontar("drive", base, "google"):
        r = _gdrive(base)
        try:
            resposta = r.hash_remoto("/dados.bin")
            checar(resposta is not None, "o Drive informou um hash")
            algoritmo, valor = resposta
            igual(algoritmo, "md5", "e o hash e MD5")
            esperado = hashlib.md5(bytes(range(256)) * 40).hexdigest()
            igual(valor, esperado, "que bate com o conteudo do arquivo")

            with PastaTemp() as tmp:
                from ftpzilla.transfer import conferir_integridade
                bom = os.path.join(tmp, "bom.bin")
                with open(bom, "wb") as f:
                    f.write(bytes(range(256)) * 40)
                ok, detalhe = conferir_integridade(r, "/dados.bin", bom, 10240)
                checar(ok, "arquivo igual passa na verificacao (%s)" % detalhe)
        finally:
            r.fechar()


def teste_nuvem_nao_segmenta():
    """Em API de nuvem o gargalo e a cota de requisicoes, e nao a banda por
    conexao: paralelizar so aproxima o 429."""
    _precisa()
    from ftpzilla.remotes.gdrive import GDriveRemote
    from ftpzilla.remotes.onedrive import OneDriveRemote
    from ftpzilla.transfer import plano_segmentos

    for classe in (GDriveRemote, OneDriveRemote):
        r = classe(_site(classe.kind))
        checar(not r.segmentavel,
               "%s nao se declara segmentavel" % classe.rotulo)
        igual(plano_segmentos(500 * 1024 * 1024, r, livres=4), [],
              "e o planejador nao segmenta nem com arquivo enorme (%s)"
              % classe.rotulo)
        checar(r.resume_download and r.resume_upload,
               "mas retoma nos dois sentidos (%s)" % classe.rotulo)


def teste_campos_secretos_da_nuvem():
    from ftpzilla import remotes
    checar("refresh_token" in remotes.campos_secretos("gdrive"),
           "o refresh token do Drive e tratado como segredo")
    checar("client_secret" in remotes.campos_secretos("gdrive"),
           "o segredo do cliente tambem")
    checar("refresh_token" in remotes.campos_secretos("onedrive"),
           "e o do OneDrive igualmente - um refresh token vale mais que uma "
           "senha, porque da acesso continuo ate ser revogado")


TESTES = [teste_onedrive_navegacao, teste_onedrive_manipulacao,
          teste_onedrive_transferencia, teste_onedrive_retoma_envio,
          teste_onedrive_renova_token_no_401, teste_gdrive_navegacao,
          teste_gdrive_cache_de_id, teste_gdrive_manipulacao_e_transferencia,
          teste_gdrive_hash_do_servidor, teste_nuvem_nao_segmenta,
          teste_campos_secretos_da_nuvem]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Google Drive e OneDrive"))
