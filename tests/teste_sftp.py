"""SFTP contra um servidor paramiko de verdade, em thread."""
from __future__ import annotations

import hashlib
import io
import os
import time

import ajuda
import servidores
from ajuda import PastaTemp, checar, escrever, igual, levanta, pular

from ftpzilla.remotes.base import (ErroAutenticacao, ErroChaveDesconhecida,
                                   ErroPermanente)
from ftpzilla.remotes.sftp import SftpRemote, impressao
from ftpzilla.sites import Site

TAM = 400 * 1024


def _precisa():
    if not servidores.tem_paramiko():
        pular("paramiko nao instalado")


def _site(host, porta, **extra) -> Site:
    dados = dict(nome="teste", kind="sftp", host=host, porta=porta,
                 usuario=servidores.USUARIO, senha=servidores.SENHA,
                 usar_agente=False)
    dados.update(extra)
    site = Site(**dados)
    # o dialogo de confianca ja teria respondido "so desta vez"; aqui isso e
    # dito na mao para o teste nao depender do known_hosts da maquina
    site.opcoes["aceitar_chave"] = "uma_vez"
    return site


def _montar(raiz):
    escrever(os.path.join(raiz, "leiame.txt"), b"conteudo\n")
    escrever(os.path.join(raiz, "dados.bin"), bytes(range(256)) * 1600)
    os.makedirs(os.path.join(raiz, "sub"), exist_ok=True)
    escrever(os.path.join(raiz, "sub", "dentro.txt"), b"x")


def _conectado(host, porta, **extra):
    r = SftpRemote(_site(host, porta, **extra))
    r.conectar()
    return r


# ---------------------------------------------------------------------------
def teste_navegacao():
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        os.makedirs(raiz, exist_ok=True)
        _montar(raiz)
        with servidores.servidor_sftp(raiz) as (host, porta, _):
            r = _conectado(host, porta)
            try:
                itens = {e.name: e for e in r.listar("/")}
                igual(sorted(itens), ["dados.bin", "leiame.txt", "sub"],
                      "lista a raiz")
                checar(itens["sub"].is_dir, "reconhece a pasta")
                igual(itens["leiame.txt"].size, 9, "tamanho do arquivo")
                checar(itens["leiame.txt"].mtime > 0, "data preenchida")
                checar(bool(itens["leiame.txt"].perms),
                       "permissoes no estilo Unix (%s)"
                       % itens["leiame.txt"].perms)
                igual([e.name for e in r.listar("/sub")], ["dentro.txt"],
                      "lista a subpasta")
                checar(r.stat("/leiame.txt") is not None, "stat do arquivo")
                igual(r.stat("/nao_existe"), None,
                      "stat de inexistente devolve None, nao excecao")
            finally:
                r.fechar()


def teste_manipulacao():
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        os.makedirs(raiz, exist_ok=True)
        _montar(raiz)
        with servidores.servidor_sftp(raiz) as (host, porta, _):
            r = _conectado(host, porta)
            try:
                r.criar_pasta("/nova")
                checar(r.existe("/nova"), "criar_pasta")
                r.criar_pastas("/nova/a/b")
                checar(r.existe("/nova/a/b"), "criar_pastas cria a arvore")
                r.renomear("/leiame.txt", "/renomeado.txt")
                checar(r.existe("/renomeado.txt"), "renomear")
                r.apagar_arquivo("/renomeado.txt")
                checar(not r.existe("/renomeado.txt"), "apagar_arquivo")
                r.apagar_arvore("/nova")
                checar(not r.existe("/nova"), "apagar_arvore")
                levanta(ErroPermanente, lambda: r.apagar_arquivo("/sumiu.txt"),
                        "apagar inexistente vira ErroPermanente")
            finally:
                r.fechar()


def teste_permissoes_e_data():
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        os.makedirs(raiz, exist_ok=True)
        _montar(raiz)
        with servidores.servidor_sftp(raiz) as (host, porta, _):
            r = _conectado(host, porta)
            try:
                checar(r.pode_chmod, "SFTP anuncia que sabe mudar permissoes")
                r.chmod("/leiame.txt", 0o640)
                e = r.stat("/leiame.txt")
                checar(e is not None, "o arquivo continua la depois do chmod")

                quando = time.time() - 86400 * 2
                r.definir_mtime("/leiame.txt", quando)
                obtido = r.stat("/leiame.txt").mtime
                checar(abs(obtido - quando) < 2.0,
                       "definir_mtime gravou a data")
                checar(r.preserva_mtime,
                       "e a capacidade continua ligada (o servidor aceitou)")
            finally:
                r.fechar()


def teste_transferencia_e_retomada():
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        os.makedirs(raiz, exist_ok=True)
        _montar(raiz)
        with servidores.servidor_sftp(raiz) as (host, porta, _):
            r = _conectado(host, porta)
            try:
                checar(r.resume_download and r.resume_upload,
                       "SFTP retoma nos dois sentidos (seek e nativo)")
                checar(r.segmentavel, "e aceita download por faixa")

                buf = io.BytesIO()
                pedacos = []
                n = r.baixar("/dados.bin", buf, cb=pedacos.append)
                igual(n, TAM, "baixou o arquivo inteiro")
                igual(sum(pedacos), TAM, "a soma dos deltas bate")
                checar(len(pedacos) > 1, "progresso reportado em blocos")

                buf = io.BytesIO()
                r.baixar("/dados.bin", buf, offset=TAM - 500)
                igual(len(buf.getvalue()), 500, "offset pula o comeco")

                buf = io.BytesIO()
                r.baixar("/dados.bin", buf, offset=1000, limite=256)
                igual(len(buf.getvalue()), 256,
                      "faixa limitada (base da segmentacao)")

                dados = b"a" * 1000 + b"b" * 1000
                r.enviar(io.BytesIO(dados[:1000]), "/enviado.bin", tamanho=1000)
                igual(r.stat("/enviado.bin").size, 1000, "primeira metade subiu")
                r.enviar(io.BytesIO(dados[1000:]), "/enviado.bin",
                         tamanho=1000, offset=1000)
                chegou = os.path.join(raiz, "enviado.bin")
                igual(os.path.getsize(chegou), 2000, "o arquivo ficou completo")
                with open(chegou, "rb") as f:
                    igual(hashlib.sha256(f.read()).hexdigest(),
                          hashlib.sha256(dados).hexdigest(),
                          "e o hash bate: a retomada colou no lugar certo")
            finally:
                r.fechar()


def teste_senha_errada():
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        os.makedirs(raiz, exist_ok=True)
        with servidores.servidor_sftp(raiz) as (host, porta, _):
            r = SftpRemote(_site(host, porta, senha="errada"))
            levanta(ErroAutenticacao, r.conectar,
                    "senha errada vira ErroAutenticacao (e nao se repete)")


def teste_chave_desconhecida_pergunta():
    """RejectPolicy: servidor novo NAO conecta sozinho. Vira uma pergunta,
    com a impressao no mesmo formato que o OpenSSH imprime."""
    _precisa()
    with PastaTemp() as tmp:          # PastaTemp isola o known_hosts
        raiz = os.path.join(tmp, "servidor")
        os.makedirs(raiz, exist_ok=True)
        with servidores.servidor_sftp(raiz) as (host, porta, chave_host):
            site = Site(nome="t", kind="sftp", host=host, porta=porta,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        usar_agente=False)
            site.opcoes["usar_known_hosts_do_ssh"] = False
            r = SftpRemote(site)
            try:
                r.conectar()
                ajuda.falha("conectou num servidor desconhecido sem perguntar")
            except ErroChaveDesconhecida as e:
                ajuda.ok("chave desconhecida vira pergunta, e nao conexao")
                checar(not e.mudou, "e a pergunta diz que e a primeira vez")
                checar(e.fingerprint.startswith("SHA256:"),
                       "com a impressao no formato do OpenSSH (%s)"
                       % e.fingerprint[:20])
                igual(e.fingerprint, impressao(chave_host),
                      "e e mesmo a impressao da chave do servidor")

            # o usuario responde "confio: salvar"
            site.opcoes["aceitar_chave"] = "salvar"
            r = SftpRemote(site)
            r.conectar()
            try:
                checar(r.conectado, "depois de aceitar, conecta")
            finally:
                r.fechar()

            # e nas proximas vezes nao pergunta mais
            site.opcoes.pop("aceitar_chave", None)
            r = SftpRemote(site)
            r.conectar()
            try:
                checar(r.conectado,
                       "a chave salva no known_hosts vale para as proximas")
            finally:
                r.fechar()


def teste_chave_trocada_e_recusada():
    """O caso que importa: a chave do servidor MUDOU. Ou reinstalaram o
    servidor, ou tem alguem no meio - e so o usuario sabe qual."""
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        os.makedirs(raiz, exist_ok=True)

        # primeira "maquina": aceita e salva a chave
        with servidores.servidor_sftp(raiz) as (host, porta, chave1):
            site = Site(nome="t", kind="sftp", host=host, porta=porta,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        usar_agente=False)
            site.opcoes["usar_known_hosts_do_ssh"] = False
            site.opcoes["aceitar_chave"] = "salvar"
            r = SftpRemote(site)
            r.conectar()
            r.fechar()
            ajuda.ok("a chave do servidor original foi salva")

        # o mesmo endereco, agora com outra chave de host
        with servidores.servidor_sftp(raiz) as (host2, porta2, chave2):
            site.opcoes.pop("aceitar_chave", None)
            # forca o mesmo nome de host do registro anterior
            from ftpzilla.remotes import sftp as mod
            conhecidas = mod.carregar_known_hosts(False)
            nome_antigo = mod._nome_host(host, porta)
            entrada = conhecidas.lookup(nome_antigo)
            checar(entrada is not None, "o known_hosts guardou a chave antiga")

            import paramiko
            novas = paramiko.HostKeys()
            novas.add(mod._nome_host(host2, porta2), chave1.get_name(), chave1)
            novas.save(mod.paths.arquivo_known_hosts())

            site.host, site.porta = host2, porta2
            r = SftpRemote(site)
            try:
                r.conectar()
                ajuda.falha("conectou mesmo com a chave do servidor trocada")
            except ErroChaveDesconhecida as e:
                checar(e.mudou,
                       "a troca de chave e detectada e sinalizada como TROCA")
                igual(e.fingerprint, impressao(chave2),
                      "e a impressao mostrada e a da chave NOVA, para comparar")


def teste_capacidades():
    r = SftpRemote(Site(kind="sftp"))
    checar(not r.is_local, "SFTP nao e local")
    checar(r.pode_chmod, "sabe mudar permissao")
    checar(r.preserva_mtime, "sabe gravar data")
    checar(r.segmentavel, "aceita segmentacao")


TESTES = [teste_navegacao, teste_manipulacao, teste_permissoes_e_data,
          teste_transferencia_e_retomada, teste_senha_errada,
          teste_chave_desconhecida_pergunta, teste_chave_trocada_e_recusada,
          teste_capacidades]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "SFTP"))
