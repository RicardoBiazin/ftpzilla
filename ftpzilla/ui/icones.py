"""Icones 16x16 desenhados em codigo, sem arquivo e sem Pillow.

tk.PhotoImage le PNG nativamente, mas empacotar dezenas de PNGs so para
pintar uma pasta e um arquivo nao compensa: desenhar retangulos com put()
custa milissegundos, adapta a cor ao tema e some do PyInstaller.

ARMADILHA: Tkinter nao segura referencia de imagem. Uma PhotoImage sem
referencia viva em Python e coletada pelo GC e o icone simplesmente
desaparece da Treeview, sem erro nenhum. Por isso o cache _CACHE e de
modulo - ele existe para manter as imagens vivas, nao so por velocidade.
"""
from __future__ import annotations

import tkinter as tk
from typing import Dict, Optional

from . import tema

_CACHE: Dict[tuple, tk.PhotoImage] = {}    # mantem as imagens vivas
_ROOT = [None]                             # em qual interpretador Tk elas vivem


def _novo(tam: int = 16) -> tk.PhotoImage:
    return tk.PhotoImage(width=tam, height=tam)


def _conferir_root() -> None:
    """Uma PhotoImage pertence ao interpretador Tk que a criou.

    Se a janela for destruida e outra criada (acontece nos testes, e tambem
    quando o app reabre a janela sem sair), as imagens antigas viram
    'image "pyimage1" doesn't exist' no meio da Treeview. Trocou de root,
    descarta o cache.
    """
    root = getattr(tk, "_default_root", None)
    if _ROOT[0] is not root:
        _CACHE.clear()
        _ROOT[0] = root


def _ret(img: tk.PhotoImage, x1: int, y1: int, x2: int, y2: int, cor: str) -> None:
    """Retangulo cheio, coordenadas inclusivas."""
    if x2 < x1 or y2 < y1:
        return
    img.put(cor, to=(x1, y1, x2 + 1, y2 + 1))


def _pasta(cor: str, borda: str) -> tk.PhotoImage:
    img = _novo()
    _ret(img, 1, 3, 6, 5, borda)          # aba
    _ret(img, 2, 4, 6, 5, cor)
    _ret(img, 1, 5, 14, 13, borda)        # corpo
    _ret(img, 2, 6, 13, 12, cor)
    return img


def _arquivo(cor: str, borda: str) -> tk.PhotoImage:
    img = _novo()
    _ret(img, 3, 1, 12, 14, borda)
    _ret(img, 4, 2, 11, 13, cor)
    # canto dobrado
    _ret(img, 9, 1, 12, 4, borda)
    _ret(img, 10, 2, 11, 3, cor)
    return img


def _disco(cor: str, borda: str) -> tk.PhotoImage:
    img = _novo()
    _ret(img, 1, 4, 14, 11, borda)
    _ret(img, 2, 5, 13, 10, cor)
    _ret(img, 10, 7, 12, 8, borda)        # a luzinha
    return img


def _servidor(cor: str, borda: str) -> tk.PhotoImage:
    img = _novo()
    for topo in (2, 8):
        _ret(img, 2, topo, 13, topo + 4, borda)
        _ret(img, 3, topo + 1, 12, topo + 3, cor)
        _ret(img, 10, topo + 2, 11, topo + 2, borda)
    return img


def _acima(cor: str, borda: str) -> tk.PhotoImage:
    img = _novo()
    for i in range(6):                    # ponta da seta
        _ret(img, 7 - i, 4 + i, 8 + i, 4 + i, borda)
    _ret(img, 6, 9, 9, 14, borda)         # haste
    _ret(img, 7, 10, 8, 13, cor)
    return img


def _link(cor: str, borda: str) -> tk.PhotoImage:
    img = _arquivo(cor, borda)
    _ret(img, 1, 10, 6, 14, borda)        # a setinha do atalho
    _ret(img, 2, 11, 5, 13, cor)
    return img


_DESENHOS = {
    "pasta": _pasta,
    "arquivo": _arquivo,
    "disco": _disco,
    "servidor": _servidor,
    "acima": _acima,
    "link": _link,
}

#: cor de preenchimento de cada icone, por tema claro/escuro
_CORES = {
    "pasta":    ("#e8b23a", "#b07d12"),
    "arquivo":  ("#ffffff", "#8a8a8a"),
    "disco":    ("#9fb6cc", "#5d7085"),
    "servidor": ("#8fb98f", "#4d6b4d"),
    "acima":    ("#c8c8c8", "#6b6b6b"),
    "link":     ("#ffffff", "#8a8a8a"),
}

_CORES_ESCURO = {
    "pasta":    ("#d9a441", "#8a6212"),
    "arquivo":  ("#3f4448", "#8a8a8a"),
    "disco":    ("#5d7085", "#8ea6bd"),
    "servidor": ("#4d6b4d", "#8fb98f"),
    "acima":    ("#6b6b6b", "#c8c8c8"),
    "link":     ("#3f4448", "#8a8a8a"),
}


def get(nome: str) -> Optional[tk.PhotoImage]:
    """Icone pelo nome, no tema em vigor. None se nao existir o desenho."""
    _conferir_root()
    escuro = bool(tema.cores().get("escuro"))
    chave = (nome, escuro)
    if chave in _CACHE:
        return _CACHE[chave]
    desenho = _DESENHOS.get(nome)
    if desenho is None:
        return None
    paleta = _CORES_ESCURO if escuro else _CORES
    cor, borda = paleta.get(nome, ("#cccccc", "#777777"))
    try:
        img = desenho(cor, borda)
    except tk.TclError:
        return None       # ainda nao ha root: quem chamou tenta de novo depois
    _CACHE[chave] = img
    return img


def para_entry(e) -> Optional[tk.PhotoImage]:
    """Escolhe o icone de uma Entry de listagem."""
    if e.is_link:
        return get("link")
    if e.is_dir:
        return get("pasta")
    return get("arquivo")


def limpar() -> None:
    """Descarta o cache (ao trocar de tema)."""
    _CACHE.clear()
