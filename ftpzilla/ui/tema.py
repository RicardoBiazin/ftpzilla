"""Temas de cor e o trabalho sujo de aplicar tema no Tkinter.

Duas coisas o ttk nao resolve sozinho e valem o comentario:

1. Widgets classicos (tk.Text, tk.Menu, tk.Listbox, tk.Canvas) ignoram o
   ttk.Style. Quem quiser tema escuro de verdade tem que pinta-los na mao -
   e por isso existe pintar_classico().

2. No Windows 11 a barra de titulo continua clara mesmo com a janela toda
   escura. Tres linhas de ctypes resolvem (DWMWA_USE_IMMERSIVE_DARK_MODE) e
   sao a diferenca entre "tema escuro" e "tema escuro pela metade".
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

#: bg=fundo, fg=texto, field=campos/listas, log_bg/log_fg=caixa de log,
#: linha=divisorias, fraco=texto secundario, tags=cores da comparacao
THEMES = {
    "Claro": {
        "bg": "#f2f2f2", "fg": "#1a1a1a", "field": "#ffffff",
        "log_bg": "#ffffff", "log_fg": "#222222",
        "linha": "#d0d0d0", "fraco": "#6b6b6b", "zebra": "#f7f9fb",
        "escuro": False,
        "tags": {
            "so_esquerda":  "#1b6e2a", "so_direita":   "#8a5a00",
            "esq_mais_nova": "#0b5fa5", "dir_mais_nova": "#6a2fa0",
            "tamanho_difere": "#a05a00", "conflito": "#b00020",
            "igual": "#7a7a7a", "pasta": "#1a1a1a",
        },
    },
    "Escuro": {
        "bg": "#1e1e1e", "fg": "#e6e6e6", "field": "#2b2b2b",
        "log_bg": "#111111", "log_fg": "#d6d6d6",
        "linha": "#3a3a3a", "fraco": "#9a9a9a", "zebra": "#252526",
        "escuro": True,
        "tags": {
            "so_esquerda":  "#6cc36c", "so_direita":   "#e0b25c",
            "esq_mais_nova": "#6bb6ff", "dir_mais_nova": "#c39cf0",
            "tamanho_difere": "#e0a05c", "conflito": "#ff6b6b",
            "igual": "#8a8a8a", "pasta": "#e6e6e6",
        },
    },
    "Azul": {
        "bg": "#e8f0fb", "fg": "#10243d", "field": "#ffffff",
        "log_bg": "#0e2233", "log_fg": "#cfe6ff",
        "linha": "#c3d4ea", "fraco": "#53698a", "zebra": "#f2f7fe",
        "escuro": False,
        "tags": {
            "so_esquerda":  "#1b6e2a", "so_direita":   "#8a5a00",
            "esq_mais_nova": "#0b5fa5", "dir_mais_nova": "#6a2fa0",
            "tamanho_difere": "#a05a00", "conflito": "#b00020",
            "igual": "#6a7f9a", "pasta": "#10243d",
        },
    },
    "Sepia": {
        "bg": "#f4ecd8", "fg": "#3b2f1c", "field": "#fffdf5",
        "log_bg": "#2a2317", "log_fg": "#e9dcc0",
        "linha": "#ded0b0", "fraco": "#7a6a4c", "zebra": "#faf4e6",
        "escuro": False,
        "tags": {
            "so_esquerda":  "#1b6e2a", "so_direita":   "#8a5a00",
            "esq_mais_nova": "#0b5fa5", "dir_mais_nova": "#6a2fa0",
            "tamanho_difere": "#a05a00", "conflito": "#b00020",
            "igual": "#8a7a5c", "pasta": "#3b2f1c",
        },
    },
}

PADRAO = "Escuro"
ACCENT_PADRAO = "#2f6fd0"

#: tema em vigor, para quem precisar consultar depois (paineis, dialogos)
atual = {"nome": PADRAO, "accent": ACCENT_PADRAO, "cores": THEMES[PADRAO]}


def cores() -> dict:
    return atual["cores"]


def center_over(win, master) -> None:
    """Posiciona 'win' centralizada sobre 'master', sem sair da tela."""
    try:
        win.update_idletasks()
        master.update_idletasks()
        mw, mh = master.winfo_width(), master.winfo_height()
        mx, my = master.winfo_rootx(), master.winfo_rooty()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = max(0, min(mx + (mw - w) // 2, win.winfo_screenwidth() - w))
        y = max(0, min(my + (mh - h) // 2, win.winfo_screenheight() - h))
        win.geometry("+%d+%d" % (x, y))
    except tk.TclError:
        pass


def barra_titulo_escura(win, escuro: bool = True) -> None:
    """Pinta a barra de titulo do Windows 11 de escuro."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        valor = ctypes.c_int(1 if escuro else 0)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE; 19 no Windows 10 antigo
        for attr in (20, 19):
            ok = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(valor), ctypes.sizeof(valor))
            if ok == 0:
                break
    except Exception:
        pass   # cosmetico: nunca pode derrubar a janela


def aplicar(root, nome: str, accent: str = ACCENT_PADRAO) -> dict:
    """Aplica o tema na janela inteira. Devolve o dicionario de cores."""
    t = THEMES.get(nome) or THEMES[PADRAO]
    atual.update({"nome": nome if nome in THEMES else PADRAO,
                  "accent": accent, "cores": t})

    style = ttk.Style(root)
    try:
        style.theme_use("clam")      # 'clam' respeita cores customizadas
    except tk.TclError:
        pass

    bg, fg, field = t["bg"], t["fg"], t["field"]
    linha, fraco = t["linha"], t["fraco"]
    root.configure(bg=bg)

    style.configure(".", background=bg, foreground=fg, fieldbackground=field,
                    bordercolor=linha, lightcolor=bg, darkcolor=bg)
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=fg)
    style.configure("Fraco.TLabel", background=bg, foreground=fraco)
    style.configure("Erro.TLabel", background=bg, foreground=t["tags"]["conflito"])
    style.configure("TLabelframe", background=bg, foreground=fg)
    style.configure("TLabelframe.Label", background=bg, foreground=fg)
    style.configure("TCheckbutton", background=bg, foreground=fg)
    style.configure("TRadiobutton", background=bg, foreground=fg)
    style.configure("TButton", background=field, foreground=fg, bordercolor=linha)
    style.map("TButton",
              background=[("active", accent), ("disabled", bg)],
              foreground=[("active", "#ffffff"), ("disabled", fraco)])
    style.configure("TEntry", fieldbackground=field, foreground=fg,
                    insertcolor=fg, bordercolor=linha)
    style.configure("TCombobox", fieldbackground=field, foreground=fg,
                    background=field, arrowcolor=fg)
    style.map("TCombobox", fieldbackground=[("readonly", field)],
              foreground=[("readonly", fg)])
    style.configure("TPanedwindow", background=bg)
    style.configure("Sash", background=linha, gripcount=0)

    # Notebook: sem isto as abas ficam brancas no tema escuro
    style.configure("TNotebook", background=bg, bordercolor=linha, tabmargins=(2, 4, 2, 0))
    style.configure("TNotebook.Tab", background=bg, foreground=fraco, padding=(12, 5))
    style.map("TNotebook.Tab",
              background=[("selected", field)],
              foreground=[("selected", fg)])

    style.configure("Treeview", background=field, foreground=fg,
                    fieldbackground=field, bordercolor=linha, rowheight=20)
    style.map("Treeview",
              background=[("selected", accent)],
              foreground=[("selected", "#ffffff")])
    style.configure("Treeview.Heading", background=bg, foreground=fg,
                    relief="flat", padding=(4, 3))
    style.map("Treeview.Heading", background=[("active", field)])

    style.configure("Horizontal.TProgressbar", troughcolor=field,
                    background=accent, bordercolor=linha, lightcolor=accent,
                    darkcolor=accent)
    style.configure("TScrollbar", background=field, troughcolor=bg,
                    bordercolor=linha, arrowcolor=fg)
    style.configure("Status.TLabel", background=bg, foreground=fraco)
    style.configure("Accent.TLabel", background=bg, foreground=accent)

    barra_titulo_escura(root, bool(t["escuro"]))
    pintar_classico(root, t)
    return t


def pintar_classico(widget, t: dict = None) -> None:
    """Percorre a arvore pintando os widgets que ignoram o ttk.Style."""
    t = t or cores()
    for w in _todos(widget):
        cls = w.winfo_class()
        try:
            if cls == "Text":
                w.configure(background=t["log_bg"], foreground=t["log_fg"],
                            insertbackground=t["log_fg"],
                            selectbackground=atual["accent"],
                            selectforeground="#ffffff",
                            highlightthickness=0, borderwidth=0)
            elif cls == "Menu":
                w.configure(background=t["field"], foreground=t["fg"],
                            activebackground=atual["accent"],
                            activeforeground="#ffffff",
                            borderwidth=0, relief="flat")
            elif cls == "Listbox":
                w.configure(background=t["field"], foreground=t["fg"],
                            selectbackground=atual["accent"],
                            selectforeground="#ffffff",
                            highlightthickness=0, borderwidth=0)
            elif cls in ("Canvas", "Frame", "Toplevel", "Tk"):
                w.configure(background=t["bg"])
            elif cls == "Label":
                w.configure(background=t["bg"], foreground=t["fg"])
        except tk.TclError:
            pass


def _todos(w):
    yield w
    for filho in w.winfo_children():
        for neto in _todos(filho):
            yield neto


def aplicar_tags(tree, t: dict = None) -> None:
    """Configura as tags de cor usadas na comparacao de diretorios."""
    t = t or cores()
    for nome, cor in t["tags"].items():
        try:
            tree.tag_configure(nome, foreground=cor)
        except tk.TclError:
            pass
    try:
        tree.tag_configure("zebra", background=t["zebra"])
    except tk.TclError:
        pass
