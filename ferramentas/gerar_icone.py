"""Gera o ftpzilla.ico sem depender do Pillow.

Um .ico e um cabecalho simples seguido de imagens. Para 32 bits com canal
alfa da para escrever o BMP na mao: sao poucos bytes e evita arrastar uma
biblioteca de imagens so para desenhar duas setas.

Uso:  python ferramentas/gerar_icone.py
Saida: ftpzilla/recursos/ftpzilla.ico (16, 32, 48 e 64 pixels)
"""
from __future__ import annotations

import os
import struct
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAIDA = os.path.join(RAIZ, "ftpzilla", "recursos", "ftpzilla.ico")

# cores (R, G, B, A)
FUNDO = (24, 30, 38, 255)
SETA_SOBE = (108, 195, 108, 255)     # verde: enviar
SETA_DESCE = (107, 182, 255, 255)    # azul: baixar
TRANSPARENTE = (0, 0, 0, 0)


def desenhar(tam: int):
    """Duas setas em sentidos opostos, sobre um quadrado arredondado."""
    px = [[TRANSPARENTE] * tam for _ in range(tam)]
    borda = max(1, tam // 16)

    def dentro_do_quadrado(x, y):
        # cantos arredondados baratos: corta os quatro cantos na diagonal
        corte = tam // 5
        if x + y < corte or (tam - 1 - x) + y < corte:
            return False
        if x + (tam - 1 - y) < corte or (tam - 1 - x) + (tam - 1 - y) < corte:
            return False
        return borda <= x < tam - borda and borda <= y < tam - borda

    for y in range(tam):
        for x in range(tam):
            if dentro_do_quadrado(x, y):
                px[y][x] = FUNDO

    def seta(coluna, cor, para_cima):
        largura = max(2, tam // 8)
        topo = tam // 5
        base = tam - tam // 5
        haste_x = coluna
        for y in range(topo, base):
            for dx in range(-largura // 2, largura // 2 + 1):
                x = haste_x + dx
                if dentro_do_quadrado(x, y):
                    px[y][x] = cor
        # ponta
        altura = max(3, tam // 5)
        for i in range(altura):
            metade = altura - i
            y = (topo + i) if para_cima else (base - 1 - i)
            for dx in range(-metade, metade + 1):
                x = haste_x + dx
                if 0 <= x < tam and 0 <= y < tam and dentro_do_quadrado(x, y):
                    px[y][x] = cor

    seta(tam // 3, SETA_SOBE, True)
    seta(2 * tam // 3, SETA_DESCE, False)
    return px


def bmp_de(px, tam: int) -> bytes:
    """BITMAPINFOHEADER + pixels BGRA + mascara AND (exigida pelo formato)."""
    cabecalho = struct.pack("<IiiHHIIiiII", 40, tam, tam * 2, 1, 32, 0,
                            tam * tam * 4, 0, 0, 0, 0)
    corpo = bytearray()
    for y in range(tam - 1, -1, -1):     # o BMP e de baixo para cima
        for x in range(tam):
            r, g, b, a = px[y][x]
            corpo += bytes((b, g, r, a))
    # mascara AND: zerada, porque a transparencia ja vem do canal alfa
    linha = ((tam + 31) // 32) * 4
    mascara = bytes(linha * tam)
    return cabecalho + bytes(corpo) + mascara


def main() -> int:
    tamanhos = (16, 32, 48, 64)
    imagens = [(t, bmp_de(desenhar(t), t)) for t in tamanhos]

    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    with open(SAIDA, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(imagens)))
        deslocamento = 6 + 16 * len(imagens)
        for tam, dados in imagens:
            f.write(struct.pack("<BBBBHHII",
                                tam if tam < 256 else 0,
                                tam if tam < 256 else 0,
                                0, 0, 1, 32, len(dados), deslocamento))
            deslocamento += len(dados)
        for _tam, dados in imagens:
            f.write(dados)
    print("Icone gerado: %s (%d bytes)" % (SAIDA, os.path.getsize(SAIDA)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
