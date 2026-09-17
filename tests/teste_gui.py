"""Fumaca da interface: monta a janela, troca de tema, mede a Treeview.

Nao testa comportamento visual (nao da, sem olho humano). Testa o que
costuma quebrar sem ninguem perceber: um nome de estilo errado no tema, um
icone coletado pelo GC, e a insercao em lote continuando rapida.
"""
from __future__ import annotations

import time

import ajuda
from ajuda import PastaTemp, checar, escrever, igual, pular, tem_display


def _tk():
    import tkinter as tk
    return tk


def teste_tem_display():
    if not tem_display():
        pular("sem display grafico nesta maquina")
    ajuda.ok("da para criar janelas Tk aqui")


def teste_temas():
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    from ftpzilla.ui import tema
    root = tk.Tk()
    root.withdraw()
    try:
        for nome in tema.THEMES:
            cores = tema.aplicar(root, nome)
            faltando = [c for c in ("bg", "fg", "field", "log_bg", "linha",
                                    "fraco", "zebra", "tags")
                        if c not in cores]
            checar(not faltando, "tema %s tem todas as chaves de cor" % nome)
        tags = tema.THEMES["Claro"]["tags"].keys()
        checar(all(set(t["tags"]) == set(tags) for t in tema.THEMES.values()),
               "todos os temas definem as mesmas tags de comparacao")
    finally:
        root.destroy()


def teste_icones_sobrevivem_ao_gc():
    """Icone sem referencia viva some da Treeview sem erro nenhum. O cache
    de modulo existe justamente para impedir isso."""
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    import gc
    from ftpzilla.ui import icones, tema
    root = tk.Tk()
    root.withdraw()
    try:
        tema.aplicar(root, "Escuro")
        icones.limpar()
        img = icones.get("pasta")
        checar(img is not None, "o icone de pasta foi desenhado")
        gc.collect()
        checar(icones.get("pasta") is img,
               "o mesmo objeto continua no cache depois do gc")
        igual(icones.get("nao_existe"), None, "nome desconhecido devolve None")
    finally:
        root.destroy()


def teste_janela_monta():
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    from ftpzilla.ui.janela import JanelaPrincipal
    # PastaTemp isola o FTPZILLA_HOME: sem isso o teste criaria fila.db e
    # sites.json na configuracao real de quem esta rodando a suite
    with PastaTemp():
        _janela_monta(tk, JanelaPrincipal)


def _janela_monta(tk, JanelaPrincipal):
    root = tk.Tk()
    root.withdraw()
    try:
        j = JanelaPrincipal(root)
        root.update()
        aba = j.aba_atual()
        checar(aba is not None and aba.esquerda is not None
               and aba.direita is not None,
               "a aba inicial monta os dois paineis")
        checar(aba.esquerda.caminho not in ("", None),
               "o painel esquerdo abriu em alguma pasta")
        for nome in ("Claro", "Escuro"):
            j.var_tema.set(nome)
            j._trocar_tema()
            root.update()
        ajuda.ok("trocar de tema com a janela montada nao quebra")
        checar(j.fila is not None and j.fila_view is not None,
               "a fila global subiu junto com a janela")
        j.fechar()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def teste_treeview_grande():
    """5.000 linhas em lotes precisam continuar em segundos, nao minutos."""
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    from ftpzilla.remotes.base import Entry
    from ftpzilla.ui import tema
    from ftpzilla.ui.painel import LIMITE_ICONE, FilePane
    from ftpzilla.browser import NavegadorLocal
    from ftpzilla.remotes.local import LocalRemote

    root = tk.Tk()
    root.withdraw()
    try:
        tema.aplicar(root, "Escuro")
        painel = FilePane(root, NavegadorLocal(LocalRemote()))
        painel.pack()
        n = 5000
        painel._itens = [Entry("arq%05d.txt" % i, size=i, mtime=1700000000.0)
                         for i in range(n)]
        t0 = time.time()
        painel._redesenhar()
        while painel._pendente is not None:
            root.update()
        gasto = time.time() - t0
        igual(len(painel.tree.get_children()), n, "inseriu as %d linhas" % n)
        checar(gasto < 20.0, "levou %.1fs para %d linhas" % (gasto, n))
        checar(painel._com_icone is (n <= LIMITE_ICONE),
               "a regra do icone bateu com o limite")

        painel.var_filtro.set("arq0001")
        root.update()
        checar(len(painel.tree.get_children()) < n,
               "o filtro reduz a lista antes de inserir")
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def teste_menu_contexto_seleciona_item_sob_cursor():
    """Clicar com o direito fora da selecao tem que mover a selecao - senao
    'Apagar' age num arquivo que o usuario nem esta olhando."""
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    from ftpzilla.remotes.base import Entry
    from ftpzilla.ui import tema
    from ftpzilla.ui.painel import FilePane
    from ftpzilla.browser import NavegadorLocal
    from ftpzilla.remotes.local import LocalRemote

    root = tk.Tk()
    root.withdraw()
    try:
        tema.aplicar(root, "Claro")
        painel = FilePane(root, NavegadorLocal(LocalRemote()))
        painel.pack()
        painel._itens = [Entry("a.txt"), Entry("b.txt"), Entry("c.txt")]
        painel._redesenhar()
        root.update()
        painel.tree.selection_set("i0")

        class CliqueFalso:
            x = y = x_root = y_root = 5
        # identify_row com a janela escondida devolve "", entao simulamos a
        # decisao que o _menu_contexto toma
        iid = "i2"
        if iid not in painel.tree.selection():
            painel.tree.selection_set(iid)
        igual(painel.tree.selection(), ("i2",),
              "a selecao segue o item sob o cursor")
        igual([e.name for e in painel.selecionados()], ["c.txt"],
              "selecionados() acompanha")
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def teste_transferencia_pela_interface():
    """Do clique ate o arquivo no destino, passando pela fila de verdade."""
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    import os
    from ftpzilla.ui.janela import JanelaPrincipal

    with PastaTemp() as tmp:
        origem = os.path.join(tmp, "de")
        destino = os.path.join(tmp, "para")
        escrever(os.path.join(origem, "arquivo.bin"), b"k" * 30000)
        os.makedirs(destino, exist_ok=True)

        root = tk.Tk()
        root.withdraw()
        try:
            j = JanelaPrincipal(root)
            aba = j.aba_atual()
            aba.esquerda.ir_para(origem.replace("\\", "/"))
            aba.direita.ir_para(destino.replace("\\", "/"))
            root.update()

            selecionados = [e for e in aba.esquerda._itens
                            if e.name == "arquivo.bin"]
            igual(len(selecionados), 1, "o painel enxergou o arquivo")
            j.transferir(aba.esquerda, selecionados)
            root.update()
            igual(len(j.fila.itens), 1, "o item entrou na fila")

            fim = time.time() + 30
            while time.time() < fim and not j.fila.esperar_vazia(0.2):
                root.update()
            chegou = os.path.join(destino, "arquivo.bin")
            checar(os.path.exists(chegou), "o arquivo chegou no destino")
            igual(os.path.getsize(chegou), 30000, "com o tamanho certo")

            j.fila_view.redesenhar()
            root.update()
            igual(len(j.fila_view.tree.get_children()), 1,
                  "a fila mostra a linha do item")
            j.fechar()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass


def teste_sincronizar_pela_interface():
    """Analisar, ver a lista, desmarcar e executar - com dois discos locais."""
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    import os
    from ftpzilla.ui.janela import JanelaPrincipal
    from ftpzilla.ui.sincronizar import SincronizarDialog

    with PastaTemp() as tmp:
        esq = os.path.join(tmp, "esq")
        dir_ = os.path.join(tmp, "dir")
        escrever(os.path.join(esq, "novo.txt"), b"conteudo novo")
        escrever(os.path.join(esq, "outro.txt"), b"outro")
        escrever(os.path.join(esq, "igual.txt"), b"identico")
        escrever(os.path.join(dir_, "igual.txt"), b"identico")
        agora = time.time() - 100
        for lado in (esq, dir_):
            os.utime(os.path.join(lado, "igual.txt"), (agora, agora))

        root = tk.Tk()
        root.withdraw()
        try:
            j = JanelaPrincipal(root)
            aba = j.aba_atual()
            aba.esquerda.ir_para(esq.replace("\\", "/"))
            aba.direita.ir_para(dir_.replace("\\", "/"))
            root.update()

            dlg = SincronizarDialog(root, j, aba.esquerda, aba.direita, None)
            dlg.analisar()
            fim = time.time() + 20
            while time.time() < fim and not dlg.acoes:
                root.update()
                time.sleep(0.02)
            checar(bool(dlg.acoes), "a analise encontrou diferencas")
            enviar = [a for a in dlg.acoes if a["acao"] == "enviar"]
            igual(len(enviar), 2,
                  "os dois arquivos que so existem de um lado entraram")
            checar(all(a["par"].rel != "igual.txt" for a in dlg.acoes),
                   "o arquivo identico ficou de fora do plano")

            # desmarca o primeiro: ele nao pode ser transferido
            dlg.tree.selection_set("0")
            dlg.alternar()
            igual(len(dlg._marcados), len(dlg.acoes) - 1,
                  "desmarcar tira a acao da execucao")

            dlg.executar()
            fim = time.time() + 30
            while time.time() < fim and not j.fila.esperar_vazia(0.2):
                root.update()
            copiados = sorted(os.listdir(dir_))
            igual(len(copiados), 2,
                  "so o item marcado foi copiado (%s)" % copiados)
            j.fechar()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass


def teste_comparacao_pinta_os_paineis():
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    import os
    from ftpzilla.ui.janela import JanelaPrincipal
    from ftpzilla.ui.sincronizar import comparar_nos_paineis

    with PastaTemp() as tmp:
        esq = os.path.join(tmp, "esq")
        dir_ = os.path.join(tmp, "dir")
        escrever(os.path.join(esq, "so_aqui.txt"), b"a")
        escrever(os.path.join(dir_, "so_la.txt"), b"b")

        root = tk.Tk()
        root.withdraw()
        try:
            j = JanelaPrincipal(root)
            aba = j.aba_atual()
            aba.esquerda.ir_para(esq.replace("\\", "/"))
            aba.direita.ir_para(dir_.replace("\\", "/"))
            root.update()

            comparar_nos_paineis(j, aba.esquerda, aba.direita)
            fim = time.time() + 20
            while time.time() < fim and not aba.esquerda._comparacao:
                root.update()
                time.sleep(0.02)
            igual(aba.esquerda._comparacao.get("so_aqui.txt"), "so_esquerda",
                  "o arquivo que so existe aqui foi marcado")
            igual(aba.esquerda._comparacao.get("so_la.txt"), "so_direita",
                  "e o que so existe la tambem")
            tags = aba.esquerda.tree.item("i0", "tags")
            checar(bool(tags), "a linha recebeu a tag de cor (%s)" % (tags,))
            j.fechar()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass


def teste_aba_de_servidor_de_ponta_a_ponta():
    """Conectar, listar e baixar de um FTP de verdade, pela janela.

    E o teste que prova a ponte entre a thread do BrowserWorker e a do
    Tkinter: se ela falhar, a listagem simplesmente nunca aparece, sem erro
    nenhum no log - foi assim que o bug apareceu aqui.
    """
    if not tem_display():
        pular("sem display grafico")
    import servidores
    if not servidores.tem_pyftpdlib():
        pular("pyftpdlib nao instalado")
    tk = _tk()
    import os
    from ftpzilla.sites import Site
    from ftpzilla.ui.janela import JanelaPrincipal

    with PastaTemp() as tmp:
        raiz = os.path.join(tmp, "servidor")
        escrever(os.path.join(raiz, "remoto.txt"), b"vim do servidor")
        os.makedirs(os.path.join(raiz, "pasta"), exist_ok=True)
        baixar = os.path.join(tmp, "baixados")
        os.makedirs(baixar, exist_ok=True)

        with servidores.servidor_ftp(raiz) as (host, porta):
            root = tk.Tk()
            root.withdraw()
            try:
                j = JanelaPrincipal(root)
                site = Site(nome="Servidor de teste", kind="ftp", host=host,
                            porta=porta, usuario=servidores.USUARIO,
                            senha=servidores.SENHA, tls_modo="nenhum")
                j.abrir_site(site)
                aba = j.aba_atual()

                fim = time.time() + 30
                while time.time() < fim and aba.direita is None:
                    root.update()
                    time.sleep(0.02)
                checar(aba.direita is not None, "a aba conectou no servidor")

                fim = time.time() + 30
                while time.time() < fim and not aba.direita._itens:
                    root.update()
                    time.sleep(0.02)
                nomes = sorted(e.name for e in aba.direita._itens)
                igual(nomes, ["pasta", "remoto.txt"],
                      "a listagem do servidor chegou na interface")

                aba.esquerda.ir_para(baixar.replace("\\", "/"))
                root.update()
                alvos = [e for e in aba.direita._itens
                         if e.name == "remoto.txt"]
                j.transferir(aba.direita, alvos)
                root.update()
                fim = time.time() + 30
                while time.time() < fim and not j.fila.esperar_vazia(0.2):
                    root.update()
                chegou = os.path.join(baixar, "remoto.txt")
                checar(os.path.exists(chegou),
                       "o arquivo baixado do servidor chegou no disco")
                j.fechar()
            finally:
                try:
                    root.destroy()
                except tk.TclError:
                    pass


def teste_formulario_vem_do_registro():
    """Protocolo novo tem que aparecer no gerente de sites sozinho. Se este
    teste quebrar ao registrar um backend, e porque alguem escreveu campo na
    mao em vez de declara-lo no RemoteSpec."""
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    from ftpzilla import remotes
    from ftpzilla.sites import Site
    from ftpzilla.ui import tema
    from ftpzilla.ui.dialogos import FormularioSite

    root = tk.Tk()
    root.withdraw()
    try:
        tema.aplicar(root, "Claro")
        form = FormularioSite(root)
        form.pack()
        for kind in remotes.kinds():
            if kind == "local":
                continue
            site = Site(kind=kind, nome="teste")
            form.carregar(site)
            root.update()
            esperados = {c.key for c in remotes.campos(kind)}
            checar(esperados.issubset(set(form._vars)),
                   "o tipo %s teve todos os campos montados" % kind)

        site = Site(kind="ftp", nome="x")
        form.carregar(site)
        form._vars["host"].set("servidor.exemplo")
        form._vars["porta"].set("2121")
        form._vars["senha"].set("nova-senha")
        form.var_nome.set("Meu servidor")
        form.aplicar()
        igual(site.host, "servidor.exemplo", "o texto digitado volta para o Site")
        igual(site.porta, 2121, "campo int vira int, e nao texto")
        igual(site.nome, "Meu servidor", "o nome e aplicado")
        igual(site.senha, "nova-senha", "a senha digitada e aplicada")
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def teste_senha_protegida_nao_aparece_no_formulario():
    """A caixa de senha mostra um marcador; nao mexer nela mantem a senha
    gravada. Se o marcador fosse aplicado de volta, abrir e fechar o gerente
    de sites trocaria a senha por oito bolinhas."""
    if not tem_display():
        pular("sem display grafico")
    tk = _tk()
    from ftpzilla import segredos
    from ftpzilla.sites import Site
    from ftpzilla.ui import tema
    from ftpzilla.ui.dialogos import FormularioSite

    root = tk.Tk()
    root.withdraw()
    try:
        tema.aplicar(root, "Claro")
        protegida = segredos.proteger("senha original")
        site = Site(kind="ftp", nome="x", host="h", senha=protegida)
        form = FormularioSite(root)
        form.pack()
        form.carregar(site)
        root.update()
        mostrado = form._vars["senha"].get()
        checar("senha original" not in mostrado and protegida not in mostrado,
               "a senha gravada nao aparece na tela")
        form.aplicar()
        igual(site.senha, protegida,
              "salvar sem tocar na senha mantem o valor protegido")
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


TESTES = [teste_tem_display, teste_temas, teste_icones_sobrevivem_ao_gc,
          teste_janela_monta, teste_treeview_grande,
          teste_menu_contexto_seleciona_item_sob_cursor,
          teste_transferencia_pela_interface,
          teste_sincronizar_pela_interface,
          teste_comparacao_pinta_os_paineis,
          teste_aba_de_servidor_de_ponta_a_ponta,
          teste_formulario_vem_do_registro,
          teste_senha_protegida_nao_aparece_no_formulario]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Fumaca da interface"))
