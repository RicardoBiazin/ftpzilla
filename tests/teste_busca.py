"""Busca remota, editor remoto com vigia, favoritos e arrastar."""
from __future__ import annotations

import os
import time

import ajuda
import servidores
from ajuda import PastaTemp, checar, escrever, igual, pular

from ftpzilla import editor as editormod
from ftpzilla.busca import BuscaRemota
from ftpzilla.editor import EditorRemoto, _assinatura
from ftpzilla.remotes.local import LocalRemote
from ftpzilla.sites import GerenteSites, Site


def _precisa():
    if not servidores.tem_pyftpdlib():
        pular("pyftpdlib nao instalado")


def _arvore(raiz):
    escrever(os.path.join(raiz, "config.php"), b"<?php")
    escrever(os.path.join(raiz, "leiame.txt"), b"a")
    escrever(os.path.join(raiz, "app", "config.php"), b"<?php app")
    escrever(os.path.join(raiz, "app", "views", "index.php"), b"<?php v")
    escrever(os.path.join(raiz, "app", "views", "estilo.css"), b"body{}")
    escrever(os.path.join(raiz, "backup", "config.php.bak"), b"velho")


# ---------------------------------------------------------------------------
# Busca
# ---------------------------------------------------------------------------
def teste_busca_local():
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "site")
        _arvore(raiz)
        r = LocalRemote()
        caminho = raiz.replace("\\", "/")

        b = BuscaRemota(r, caminho, "config.php")
        nomes = sorted(a.caminho.replace(caminho, "") for a in b.rodar())
        igual(nomes, ["/app/config.php", "/backup/config.php.bak",
                      "/config.php"],
              "achou os config.php em pastas diferentes - e tambem o .bak, "
              "porque texto sem curinga procura em qualquer parte do nome")

        b = BuscaRemota(r, caminho, "*/config.php")
        igual(len(b.rodar()), 0,
              "curinga explicito casa contra o NOME, nao contra o caminho")

        b = BuscaRemota(r, caminho, "*.php")
        igual(len(b.rodar()), 3, "glob explicito pega todos os .php")

        b = BuscaRemota(r, caminho, "config")
        achados = b.rodar()
        checar(any(a.nome == "config.php.bak" for a in achados),
               "texto sem curinga procura em qualquer parte do nome - quem "
               "digita 'config' quer achar 'config.php' tambem")

        b = BuscaRemota(r, caminho, "APP", so_arquivos=False)
        checar(any(a.entrada.is_dir for a in b.rodar()),
               "acha pasta, e sem diferenciar maiuscula")

        b = BuscaRemota(r, caminho, "APP", so_arquivos=True)
        igual(len(b.rodar()), 0, "'so arquivos' deixa a pasta de fora")


def teste_busca_entrega_em_lotes():
    """Esperar a varredura inteira para so entao mostrar algo faz a busca
    parecer travada - e numa hospedagem grande ela demora mesmo."""
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "site")
        for i in range(60):
            escrever(os.path.join(raiz, "p%d" % (i // 10), "arq%02d.txt" % i),
                     b"x")
        r = LocalRemote()
        lotes, pastas = [], []
        b = BuscaRemota(r, raiz.replace("\\", "/"), "*.txt")
        achados = b.rodar(ao_lote=lotes.append, ao_pasta=pastas.append)
        igual(len(achados), 60, "achou todos")
        checar(len(lotes) > 1, "os resultados vieram em %d lotes" % len(lotes))
        igual(sum(len(l) for l in lotes), 60,
              "a soma dos lotes e o total de achados")
        checar(len(pastas) >= 6, "avisou qual pasta estava lendo")
        checar("60 achado(s)" in b.resumo(), "o resumo conta certo: %s"
               % b.resumo())


def teste_busca_pode_ser_interrompida():
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "site")
        for i in range(200):
            escrever(os.path.join(raiz, "p%d" % i, "a.txt"), b"x")
        from ftpzilla.remotes.base import Cancelado
        b = BuscaRemota(LocalRemote(), raiz.replace("\\", "/"), "*.txt")

        def no_lote(_lote):
            b.parar()

        try:
            b.rodar(ao_lote=no_lote)
            ajuda.falha("a busca nao parou quando foi mandada parar")
        except Cancelado:
            ajuda.ok("parar() interrompe a varredura")
        checar(not b.terminada, "e a busca nao se declara concluida")


def teste_busca_respeita_profundidade():
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "site")
        fundo = raiz
        for i in range(8):
            fundo = os.path.join(fundo, "n%d" % i)
        escrever(os.path.join(fundo, "fundo.txt"), b"x")

        b = BuscaRemota(LocalRemote(), raiz.replace("\\", "/"), "fundo.txt",
                        max_profundidade=3)
        igual(len(b.rodar()), 0, "nao desce alem do limite pedido")
        b = BuscaRemota(LocalRemote(), raiz.replace("\\", "/"), "fundo.txt",
                        max_profundidade=12)
        igual(len(b.rodar()), 1, "com limite maior, acha")


def teste_busca_no_servidor():
    _precisa()
    from ftpzilla.remotes.ftp import FtpRemote
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        _arvore(raiz)
        with servidores.servidor_ftp(raiz) as (h, p):
            r = FtpRemote(Site(nome="t", kind="ftp", host=h, porta=p,
                               usuario=servidores.USUARIO,
                               senha=servidores.SENHA, tls_modo="nenhum"))
            r.conectar()
            try:
                b = BuscaRemota(r, "/", "*.php")
                achados = sorted(a.caminho for a in b.rodar())
                igual(achados, ["/app/config.php", "/app/views/index.php",
                                "/config.php"],
                      "a busca recursiva funciona pelo protocolo")
            finally:
                r.fechar()


# ---------------------------------------------------------------------------
# Editor remoto
# ---------------------------------------------------------------------------
def teste_assinatura_detecta_mudanca():
    with PastaTemp() as tmp:
        arq = os.path.join(tmp, "a.txt")
        escrever(arq, b"conteudo original")
        a1 = _assinatura(arq)
        checar(bool(a1), "a assinatura foi calculada")
        igual(_assinatura(arq), a1, "arquivo intocado tem a mesma assinatura")
        escrever(arq, b"conteudo alterado")
        checar(_assinatura(arq) != a1, "o conteudo mudou e a assinatura mudou")
        igual(_assinatura(os.path.join(tmp, "nao_existe")), (),
              "arquivo inexistente devolve assinatura vazia")


def teste_editor_so_envia_arquivo_estavel():
    """Um arquivo grande sendo gravado muda de tamanho varias vezes. Enviar
    na primeira mudanca subiria o arquivo pela metade."""
    _precisa()
    from ftpzilla.remotes.ftp import FtpRemote
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        escrever(os.path.join(raiz, "config.php"), b"<?php $x = 1;")
        with servidores.servidor_ftp(raiz) as (h, p):
            site = Site(nome="t", kind="ftp", host=h, porta=p,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        tls_modo="nenhum")
            r = FtpRemote(site)
            r.conectar()
            avisos = []
            ed = EditorRemoto(ao_modificar=avisos.append)
            # nao abrir editor de verdade durante o teste
            ed._abrir_no_editor = staticmethod(lambda *a, **k: None)
            try:
                local = ed.abrir(r, site, "/config.php")
                checar(os.path.exists(local), "o arquivo foi baixado")
                with open(local, "rb") as f:
                    igual(f.read(), b"<?php $x = 1;", "com o conteudo certo")

                aberto = ed.abertos[local]
                # primeira mudanca: ainda gravando
                escrever(local, b"<?php $x = 2;")
                ed._conferir(aberto)
                igual(len(avisos), 0,
                      "uma mudanca so nao dispara o envio (pode estar no meio "
                      "da gravacao)")
                # segunda leitura com o mesmo conteudo: agora esta estavel
                ed._conferir(aberto)
                igual(len(avisos), 1, "estavel por dois ciclos: envia")
                igual(avisos[0].caminho_remoto, "/config.php",
                      "e sabe para onde devolver")

                igual(len(ed.pendentes()), 1, "fica pendente ate confirmar")
                ed.confirmar_envio(local)
                igual(len(ed.pendentes()), 0, "confirmado, sai da lista")

                ed._conferir(aberto)
                igual(len(avisos), 1,
                      "arquivo que nao mudou de novo nao e reenviado")
            finally:
                ed.fechar()
                r.fechar()


def teste_editor_separa_arquivos_de_mesmo_nome():
    """Dois 'config.php' de pastas diferentes nao podem se sobrescrever na
    pasta temporaria - seria editar um e subir por cima do outro."""
    _precisa()
    from ftpzilla.remotes.ftp import FtpRemote
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        _arvore(raiz)
        with servidores.servidor_ftp(raiz) as (h, p):
            site = Site(nome="t", kind="ftp", host=h, porta=p,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        tls_modo="nenhum")
            r = FtpRemote(site)
            r.conectar()
            ed = EditorRemoto()
            ed._abrir_no_editor = staticmethod(lambda *a, **k: None)
            try:
                um = ed.abrir(r, site, "/config.php")
                dois = ed.abrir(r, site, "/app/config.php")
                checar(um != dois,
                       "os dois arquivos foram para pastas temporarias "
                       "diferentes")
                with open(um, "rb") as f:
                    igual(f.read(), b"<?php", "o da raiz tem o conteudo da raiz")
                with open(dois, "rb") as f:
                    igual(f.read(), b"<?php app", "e o de app, o de app")
                igual(len(ed.abertos), 2, "os dois estao sendo vigiados")

                ed.descartar(um)
                igual(len(ed.abertos), 1, "descartar tira da vigilancia")
                checar(not os.path.exists(um), "e apaga a copia temporaria")
            finally:
                ed.fechar()
                r.fechar()


# ---------------------------------------------------------------------------
# Favoritos e arrastar
# ---------------------------------------------------------------------------
def teste_favoritos_sobrevivem():
    with PastaTemp() as tmp:
        caminho = os.path.join(tmp, "sites.json")
        g = GerenteSites(caminho)
        site = g.adicionar(Site(nome="Servidor", kind="ftp", host="h"))
        site.opcoes["favoritos"] = ["/var/www/html"]
        g.extras["favoritos_locais"] = ["C:/DEV"]
        g.salvar()

        g2 = GerenteSites(caminho).carregar()
        igual(g2.extras.get("favoritos_locais"), ["C:/DEV"],
              "favorito de pasta local voltou do disco")
        igual(g2.sites[0].opcoes.get("favoritos"), ["/var/www/html"],
              "favorito do servidor voltou junto com o site")


def teste_separar_caminhos_do_arrastar():
    """O tkdnd entrega os caminhos numa lista do Tcl: com chaves quando ha
    espaco. Separar no espaco quebraria 'C:/Meus Documentos'."""
    from ftpzilla.ui.dnd import _separar
    igual(_separar("C:/a.txt"), ["C:/a.txt"], "um arquivo")
    igual(_separar("C:/a.txt C:/b.txt"), ["C:/a.txt", "C:/b.txt"],
          "dois arquivos sem espaco no nome")
    igual(_separar("{C:/Meus Documentos/a.txt}"),
          ["C:/Meus Documentos/a.txt"], "nome com espaco vem entre chaves")
    igual(_separar("{C:/Meus Documentos/a.txt} C:/b.txt"),
          ["C:/Meus Documentos/a.txt", "C:/b.txt"], "mistura dos dois casos")
    igual(_separar(""), [], "nada soltado")


TESTES = [teste_busca_local, teste_busca_entrega_em_lotes,
          teste_busca_pode_ser_interrompida, teste_busca_respeita_profundidade,
          teste_busca_no_servidor, teste_assinatura_detecta_mudanca,
          teste_editor_so_envia_arquivo_estavel,
          teste_editor_separa_arquivos_de_mesmo_nome,
          teste_favoritos_sobrevivem, teste_separar_caminhos_do_arrastar]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Busca, editor remoto e favoritos"))
