"""FTPZilla - cliente de transferencia de arquivos (FTP/FTPS/SFTP/nuvem).

O pacote esta dividido em tres camadas que nao se conhecem mais do que o
necessario:

  remotes/  - acesso a arquivos (local, FTP, SFTP, nuvem) atras da classe
              Remote; nao sabe nada de interface nem de fila.
  fila/transfer/pool/browser - o motor: quem transfere, quem guarda a fila,
              quem fala com o servidor. Nao toca em widget.
  ui/       - Tkinter. Unica camada que toca widget, e a unica que roda na
              thread principal.
"""
from __future__ import annotations

__version__ = "1.4.0"
__author__ = "Ricardo Biazin"
__app__ = "FTPZilla"
