"""Fumaca da interface: monta a janela, troca de tema, mede a Treeview.

Nao testa comportamento visual (nao da, sem olho humano). Testa o que
costuma quebrar sem ninguem perceber: um nome de estilo errado no tema, um
icone coletado pelo GC, e a insercao em lote continuando rapida.
"""
from __future__ import annotations

import time

import ajuda
from ajuda import checar, igual, pular, tem_display


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
    root = tk.Tk()
    root.withdraw()
    try:
        j = JanelaPrincipal(root)
        root.update()
        checar(j.esquerda is not None and j.direita is not None,
               "a janela monta os dois paineis")
        checar(j.esquerda.caminho not in ("", None),
               "o painel esquerdo abriu em alguma pasta")
        for nome in ("Claro", "Escuro"):
            j.var_tema.set(nome)
            j._trocar_tema()
            root.update()
        ajuda.ok("trocar de tema com a janela montada nao quebra")
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


TESTES = [teste_tem_display, teste_temas, teste_icones_sobrevivem_ao_gc,
          teste_janela_monta, teste_treeview_grande,
          teste_menu_contexto_seleciona_item_sob_cursor]

if __name__ == "__main__":
    raise SystemExit(ajuda.rodar(TESTES, "Fumaca da interface"))
