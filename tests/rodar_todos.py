"""Roda todas as suites e resume o resultado.

Cada suite roda em um processo proprio: um travamento de socket ou uma
janela Tk que nao fecha derruba so a sua suite, e nao a bateria inteira.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))

# as suites falam UTF-8; o console do Windows abre em cp1252 e derrubaria o
# resumo inteiro por causa de um acento na saida de uma delas
for _fluxo in (sys.stdout, sys.stderr):
    try:
        _fluxo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

SUITES = [
    ("teste_base.py", "Contratos da abstracao e do registro"),
    ("teste_local.py", "Disco local como Remote"),
    ("teste_seguranca.py", "Protecao de credenciais e mascaramento de log"),
    ("teste_ftp.py", "FTP e FTPS (pyftpdlib)"),
    ("teste_resume.py", "Retomada, validacao de parcial e backoff"),
    ("teste_fila.py", "Fila persistente em SQLite"),
    ("teste_segmentos.py", "Download segmentado"),
    ("teste_sftp.py", "SFTP (servidor paramiko em thread)"),
    ("teste_compare.py", "Comparacao e sincronizacao"),
    ("teste_conflitos.py", "Arquivo que ja existe no destino"),
    ("teste_busca.py", "Busca remota e editor remoto"),
    ("teste_nuvem.py", "Google Drive e OneDrive (APIs falsas)"),
    ("teste_gui.py", "Fumaca da interface"),
]


def rodar(arquivo: str) -> tuple:
    caminho = os.path.join(AQUI, arquivo)
    if not os.path.exists(caminho):
        return (0, 0, 0, "ausente")
    t0 = time.time()
    p = subprocess.run([sys.executable, caminho], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    saida = (p.stdout or "") + (p.stderr or "")
    print(saida.rstrip())
    ok = saida.count("\n  OK    ") + (1 if saida.startswith("  OK    ") else 0)
    falha = saida.count("\n  FALHA") + (1 if saida.startswith("  FALHA") else 0)
    pulado = saida.count("\n  PULADO") + (1 if saida.startswith("  PULADO") else 0)
    if p.returncode != 0 and falha == 0:
        falha = 1        # morreu sem imprimir nada: conta como uma falha
    return (ok, falha, pulado, "%.1fs" % (time.time() - t0))


def main() -> int:
    total_ok = total_falha = total_pulado = 0
    linhas = []
    for arquivo, descricao in SUITES:
        print("\n" + "#" * 68)
        print("# %s - %s" % (arquivo, descricao))
        print("#" * 68)
        ok, falha, pulado, tempo = rodar(arquivo)
        total_ok += ok
        total_falha += falha
        total_pulado += pulado
        if tempo == "ausente":
            linhas.append("  %-22s (ainda nao existe)" % arquivo)
        else:
            linhas.append("  %-22s %3d ok  %3d falha  %3d pulado  %s"
                          % (arquivo, ok, falha, pulado, tempo))

    print("\n" + "=" * 68)
    print("RESUMO")
    print("=" * 68)
    for l in linhas:
        print(l)
    print("-" * 68)
    print("  TOTAL: %d ok, %d falha(s), %d pulado(s)"
          % (total_ok, total_falha, total_pulado))
    return 1 if total_falha else 0


if __name__ == "__main__":
    sys.exit(main())
