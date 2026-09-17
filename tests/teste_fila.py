"""Fila persistente: SQLite, ordenacao, controle e transferencia de verdade."""
from __future__ import annotations

import os
import time

import ajuda
import servidores
from ajuda import PastaTemp, checar, escrever, igual, pular

from ftpzilla import fila_store
from ftpzilla.fila import GerenciadorFila, ItemFila
from ftpzilla.fila_store import (BAIXAR, CONCLUIDO, ENVIAR, ESPERANDO, FALHOU,
                                 PAUSADO, RODANDO, FilaStore)
from ftpzilla.sites import GerenteSites, Site


def _precisa():
    if not servidores.tem_pyftpdlib():
        pular("pyftpdlib nao instalado (pip install -r requirements-dev.txt)")


# ---------------------------------------------------------------------------
# O banco
# ---------------------------------------------------------------------------
def teste_store_basico():
    with PastaTemp() as tmp:
        store = FilaStore(os.path.join(tmp, "fila.db")).abrir()
        try:
            ids = store.inserir([
                {"sentido": BAIXAR, "origem": "/a.txt", "destino": "C:/a.txt",
                 "tamanho": 100, "prioridade": 1},
                {"sentido": BAIXAR, "origem": "/b.txt", "destino": "C:/b.txt",
                 "tamanho": 200, "prioridade": 2}])
            igual(len(ids), 2, "inserir devolve um id por item")
            checar(all(i > 0 for i in ids), "os ids vem do banco")

            linhas = store.carregar()
            igual(len(linhas), 2, "carregar traz os dois")
            igual(linhas[0]["estado"], ESPERANDO, "estado inicial")

            store.atualizar(ids[0], estado=CONCLUIDO, bytes_feitos=100)
            linhas = {l["id"]: l for l in store.carregar()}
            igual(linhas[ids[0]]["estado"], CONCLUIDO, "atualizar grava")
            igual(linhas[ids[0]]["bytes_feitos"], 100, "bytes_feitos grava")

            store.remover([ids[1]])
            igual(len(store.carregar()), 1, "remover apaga")
        finally:
            store.fechar()


def teste_rodando_vira_pausado_ao_abrir():
    """Ninguem esta transferindo nada quando o programa abre. Deixar
    'rodando' no banco faria a interface mostrar uma transferencia fantasma;
    o bytes_feitos, porem, tem que sobreviver - e dele que a retomada parte."""
    with PastaTemp() as tmp:
        caminho = os.path.join(tmp, "fila.db")
        store = FilaStore(caminho).abrir()
        ids = store.inserir([{"sentido": BAIXAR, "origem": "/a", "destino": "b",
                              "tamanho": 1000}])
        store.atualizar(ids[0], estado=RODANDO, bytes_feitos=512)
        store.fechar()

        store = FilaStore(caminho).abrir()
        try:
            linha = store.carregar()[0]
            igual(linha["estado"], PAUSADO, "rodando virou pausado")
            igual(linha["bytes_feitos"], 512, "o progresso foi preservado")
        finally:
            store.fechar()


def teste_progresso_e_gravado_em_lote():
    """Gravar a cada bloco transformaria o disco no gargalo com varias
    transferencias simultaneas. Em compensacao, o valor precisa estar la
    quando o item muda de estado - e o que a retomada le."""
    with PastaTemp() as tmp:
        store = FilaStore(os.path.join(tmp, "fila.db")).abrir()
        try:
            ids = store.inserir([{"sentido": BAIXAR, "origem": "/a",
                                  "destino": "b", "tamanho": 10 ** 9}])
            for n in range(1, 40):
                store.progresso(ids[0], n * 1024)
            store.atualizar(ids[0], esperar=True, estado=PAUSADO)
            time.sleep(0.3)
            linha = store.carregar()[0]
            checar(linha["bytes_feitos"] > 0,
                   "o progresso pendente foi junto com a mudanca de estado")

            store.progresso(ids[0], 50 * 1024 * 1024)
            time.sleep(0.5)
            linha = store.carregar()[0]
            igual(linha["bytes_feitos"], 50 * 1024 * 1024,
                  "volume grande forca a gravacao sem esperar o intervalo")
        finally:
            store.fechar()


def teste_segmentos():
    with PastaTemp() as tmp:
        store = FilaStore(os.path.join(tmp, "fila.db")).abrir()
        try:
            ids = store.inserir([{"sentido": BAIXAR, "origem": "/a",
                                  "destino": "b", "tamanho": 1000}])
            store.gravar_segmentos(ids[0], [(0, 499), (500, 999)])
            segs = store.segmentos(ids[0])
            igual(len(segs), 2, "gravou os dois segmentos")
            store.segmento_feito(ids[0], 0, 499)
            time.sleep(0.2)
            segs = store.segmentos(ids[0])
            igual(segs[0]["feito"], 499, "o progresso do segmento grava")
        finally:
            store.fechar()


# ---------------------------------------------------------------------------
# Ordenacao
# ---------------------------------------------------------------------------
def teste_prioridade_fracionaria():
    """Subir um item nao pode renumerar a fila inteira: com cinco mil itens,
    renumerar a cada arrastar trava a interface."""
    with PastaTemp() as tmp:
        g = _gerenciador(tmp)
        try:
            itens = g.enfileirar([
                ItemFila(sentido=BAIXAR, origem="/%d" % n, destino="d%d" % n)
                for n in range(5)])
            antes = {i.id: i.prioridade for i in g.itens}
            alvo = itens[3]
            g.mover([alvo.id], para_cima=True)
            igual([i.origem for i in g.itens],
                  ["/0", "/1", "/3", "/2", "/4"], "o item subiu uma posicao")
            mudaram = [i.id for i in g.itens if antes[i.id] != i.prioridade]
            igual(mudaram, [alvo.id],
                  "so a prioridade do item movido mudou (o resto da fila "
                  "nao foi renumerado)")

            g.mover([alvo.id], para_cima=False)
            igual([i.origem for i in g.itens],
                  ["/0", "/1", "/2", "/3", "/4"], "e desceu de volta")
        finally:
            g.fechar()


def _gerenciador(tmp, sites=None, max_global=4):
    store = FilaStore(os.path.join(tmp, "fila.db"))
    g = GerenciadorFila(sites or GerenteSites(os.path.join(tmp, "sites.json")),
                        store=store, max_global=max_global)
    return g.abrir()


# ---------------------------------------------------------------------------
# Transferencia de verdade
# ---------------------------------------------------------------------------
def _cenario(tmp):
    """Servidor com arquivos + um site apontando para ele."""
    raiz = os.path.join(tmp, "servidor")
    os.makedirs(raiz, exist_ok=True)
    escrever(os.path.join(raiz, "um.txt"), b"a" * 5000)
    escrever(os.path.join(raiz, "dois.bin"), bytes(range(256)) * 2048)
    os.makedirs(os.path.join(raiz, "pasta"), exist_ok=True)
    escrever(os.path.join(raiz, "pasta", "tres.txt"), b"c" * 300)
    return raiz


def teste_baixa_de_verdade():
    _precisa()
    with PastaTemp() as tmp:
        raiz = _cenario(tmp)
        destino = os.path.join(tmp, "baixados")
        os.makedirs(destino, exist_ok=True)
        with servidores.servidor_ftp(raiz) as (host, porta):
            sites = GerenteSites(os.path.join(tmp, "sites.json"))
            site = sites.adicionar(Site(
                nome="teste", kind="ftp", host=host, porta=porta,
                usuario=servidores.USUARIO, senha=servidores.SENHA,
                tls_modo="nenhum"))
            g = _gerenciador(tmp, sites)
            try:
                d = destino.replace("\\", "/")
                g.enfileirar(g.montar_itens(site, BAIXAR, [
                    ("/um.txt", d + "/um.txt", 5000, 0),
                    ("/dois.bin", d + "/dois.bin", 524288, 0)]))
                checar(g.esperar_vazia(60), "a fila esvaziou")
                estados = {i.origem: i.estado for i in g.itens}
                igual(estados.get("/um.txt"), CONCLUIDO, "um.txt concluiu")
                igual(estados.get("/dois.bin"), CONCLUIDO, "dois.bin concluiu")
                igual(os.path.getsize(os.path.join(destino, "um.txt")), 5000,
                      "o arquivo chegou inteiro")
                igual(os.path.getsize(os.path.join(destino, "dois.bin")),
                      524288, "o arquivo grande chegou inteiro")
                checar(not os.path.exists(os.path.join(destino,
                                                       "um.txt.ftzpart")),
                       "nao sobrou arquivo parcial")
                resumo = g.resumo()
                checar(resumo["feitos"] >= 529288,
                       "o medidor global contou os bytes")
            finally:
                g.fechar()


def teste_envia_de_verdade():
    _precisa()
    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        os.makedirs(raiz, exist_ok=True)
        origem = os.path.join(tmp, "locais")
        escrever(os.path.join(origem, "envio.bin"), b"z" * 40000)
        with servidores.servidor_ftp(raiz) as (host, porta):
            sites = GerenteSites(os.path.join(tmp, "sites.json"))
            site = sites.adicionar(Site(
                nome="teste", kind="ftp", host=host, porta=porta,
                usuario=servidores.USUARIO, senha=servidores.SENHA,
                tls_modo="nenhum"))
            g = _gerenciador(tmp, sites)
            try:
                o = origem.replace("\\", "/") + "/envio.bin"
                g.enfileirar(g.montar_itens(site, ENVIAR,
                                            [(o, "/sub/envio.bin", 40000, 0)]))
                checar(g.esperar_vazia(60), "a fila esvaziou")
                igual(g.itens[0].estado, CONCLUIDO, "o envio concluiu")
                chegou = os.path.join(raiz, "sub", "envio.bin")
                checar(os.path.exists(chegou),
                       "a pasta de destino foi criada no servidor")
                igual(os.path.getsize(chegou), 40000, "o arquivo subiu inteiro")
            finally:
                g.fechar()


def teste_expandir_pasta():
    _precisa()
    with PastaTemp() as tmp:
        raiz = _cenario(tmp)
        with servidores.servidor_ftp(raiz) as (host, porta):
            from ftpzilla.remotes.ftp import FtpRemote
            site = Site(nome="t", kind="ftp", host=host, porta=porta,
                        usuario=servidores.USUARIO, senha=servidores.SENHA,
                        tls_modo="nenhum")
            r = FtpRemote(site)
            r.conectar()
            try:
                g = GerenciadorFila(None)
                pares = g.expandir(r, BAIXAR, ["/pasta", "/um.txt"],
                                   "C:/destino")
                nomes = sorted(p[0] for p in pares)
                igual(nomes, ["/pasta/tres.txt", "/um.txt"],
                      "a pasta foi percorrida e o arquivo solto veio junto")
                destinos = {p[0]: p[1] for p in pares}
                checar(destinos["/pasta/tres.txt"].endswith("pasta/tres.txt"),
                       "a estrutura de pastas e mantida no destino")
                igual(destinos["/um.txt"], "C:/destino/um.txt",
                      "arquivo solto vai direto para a pasta escolhida")
            finally:
                r.fechar()


def teste_fila_sobrevive_a_fechar():
    """O teste que justifica o SQLite: fechar o programa no meio e reabrir."""
    _precisa()
    with PastaTemp() as tmp:
        raiz = _cenario(tmp)
        destino = os.path.join(tmp, "baixados").replace("\\", "/")
        with servidores.servidor_ftp(raiz) as (host, porta):
            sites = GerenteSites(os.path.join(tmp, "sites.json"))
            site = sites.adicionar(Site(
                nome="teste", kind="ftp", host=host, porta=porta,
                usuario=servidores.USUARIO, senha=servidores.SENHA,
                tls_modo="nenhum"))
            sites.salvar()

            g = _gerenciador(tmp, sites)
            g.pausar_tudo()         # nada roda: queremos a fila cheia e parada
            g.enfileirar(g.montar_itens(site, BAIXAR, [
                ("/um.txt", destino + "/um.txt", 5000, 0),
                ("/dois.bin", destino + "/dois.bin", 524288, 0)]))
            ids = [i.id for i in g.itens]
            g.fechar()

            # "reabrir o programa"
            sites2 = GerenteSites(os.path.join(tmp, "sites.json")).carregar()
            g2 = _gerenciador(tmp, sites2)
            try:
                igual(len(g2.itens), 2, "os itens voltaram do disco")
                igual([i.id for i in g2.itens], ids, "com os mesmos ids")
                igual(g2.itens[0].origem, "/um.txt", "com os mesmos caminhos")
                g2.retomar_tudo()
                checar(g2.esperar_vazia(60), "a fila retomada rodou ate o fim")
                igual(g2.itens[0].estado, CONCLUIDO,
                      "e concluiu depois de reabrir")
            finally:
                g2.fechar()


def teste_pausar_e_cancelar():
    _precisa()
    with PastaTemp() as tmp:
        raiz = _cenario(tmp)
        destino = os.path.join(tmp, "baixados").replace("\\", "/")
        with servidores.servidor_ftp(raiz) as (host, porta):
            sites = GerenteSites(os.path.join(tmp, "sites.json"))
            site = sites.adicionar(Site(
                nome="teste", kind="ftp", host=host, porta=porta,
                usuario=servidores.USUARIO, senha=servidores.SENHA,
                tls_modo="nenhum"))
            g = _gerenciador(tmp, sites)
            try:
                g.pausar_tudo()
                itens = g.enfileirar(g.montar_itens(site, BAIXAR, [
                    ("/um.txt", destino + "/um.txt", 5000, 0)]))
                time.sleep(0.3)
                igual(itens[0].estado, ESPERANDO,
                      "com a fila pausada o item fica esperando, nao rodando")

                g.pausar([itens[0].id])
                igual(itens[0].estado, PAUSADO, "pausar item a item")

                g.cancelar([itens[0].id])
                igual(itens[0].estado, "cancelado", "cancelar marca o item")

                g.remover([itens[0].id])
                igual(len(g.itens), 0, "remover tira da lista")
                igual(len(g.store.carregar()), 0, "e tira do banco")
            finally:
                g.fechar()


def teste_site_sumido_vira_falha():
    """Item apontando para um site que foi apagado nao pode ficar tentando
    para sempre - e nao pode guardar credencial para se virar sozinho."""
    with PastaTemp() as tmp:
        g = _gerenciador(tmp)
        try:
            itens = g.enfileirar([ItemFila(sentido=BAIXAR, site_id="sumiu",
                                           origem="/a", destino="C:/a")])
            fim = time.time() + 10
            while time.time() < fim and itens[0].estado != FALHOU:
                time.sleep(0.05)
            igual(itens[0].estado, FALHOU, "o item falhou")
            checar("nao existe mais" in itens[0].erro,
                   "com uma mensagem que explica o motivo")
        finally:
            g.fechar()


def teste_erro_transitorio_agenda_nova_tentativa():
    """Sem servidor no ar: a conexao falha, e o item tem que voltar para a
    fila com espera, e nao virar falha na primeira."""
    with PastaTemp() as tmp:
        sites = GerenteSites(os.path.join(tmp, "sites.json"))
        site = sites.adicionar(Site(nome="morto", kind="ftp",
                                    host="127.0.0.1", porta=1,
                                    usuario="x", senha="y", tls_modo="nenhum"))
        g = _gerenciador(tmp, sites)
        try:
            itens = g.enfileirar(g.montar_itens(site, BAIXAR,
                                                [("/a", "C:/a", 10, 0)]))
            fim = time.time() + 15
            while time.time() < fim and itens[0].tentativas == 0:
                time.sleep(0.05)
            checar(itens[0].tentativas >= 1, "contou a tentativa")
            igual(itens[0].estado, ESPERANDO,
                  "voltou para a fila em vez de falhar de cara")
            checar(itens[0].proxima_em > time.time(),
                   "com uma espera antes da proxima tentativa")
            checar(bool(itens[0].erro), "e guardou a mensagem do erro")
        finally:
            g.fechar()


def teste_backoff():
    from ftpzilla import transfer
    esperas = [transfer.espera(n) for n in range(1, 6)]
    checar(all(e > 0 for e in esperas), "a espera e sempre positiva")
    checar(esperas[-1] > esperas[0] / 2,
           "a espera cresce com o numero de tentativas")
    checar(all(e <= transfer.ESPERA_MAXIMA * 1.5 for e in esperas),
           "mas tem teto")
    amostras = {round(transfer.espera(3), 4) for _ in range(20)}
    checar(len(amostras) > 15,
           "tem jitter: oito transferencias que caem juntas nao voltam juntas")


TESTES = [teste_store_basico, teste_rodando_vira_pausado_ao_abrir,
          teste_progresso_e_gravado_em_lote, teste_segmentos,
          teste_prioridade_fracionaria, teste_baixa_de_verdade,
          teste_envia_de_verdade, teste_expandir_pasta,
          teste_fila_sobrevive_a_fechar, teste_pausar_e_cancelar,
          teste_site_sumido_vira_falha,
          teste_erro_transitorio_agenda_nova_tentativa, teste_backoff]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Fila de transferencias"))
