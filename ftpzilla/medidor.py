"""Contagem de bytes e calculo de velocidade.

A velocidade e uma media EXPONENCIAL sobre uma janela curta, e nao a media
desde o inicio. A diferenca importa na pratica: com media acumulada, uma
transferencia que ficou cinco minutos parada e voltou continua mostrando
"120 KB/s" por muito tempo depois de ja estar a 8 MB/s, e o tempo restante
vira ficcao. Com janela curta, o numero na tela e o que esta acontecendo
agora.

O metodo bloco() e chamado a cada pedaco de arquivo, de dentro das threads
de transferencia. Ele precisa ser barato e nao pode tocar em widget: soma
sob um lock e pronto. Quem desenha e a thread do Tkinter, lendo snapshot()
a cada 250 ms.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

#: constante de tempo da media (segundos). Baixo demais faz o numero tremer;
#: alto demais faz ele demorar a reagir a uma queda de velocidade.
TAU = 3.0


class Medidor:
    """Progresso de um item (ou do conjunto todo)."""

    def __init__(self, total: int = 0, feitos: int = 0):
        self._lock = threading.Lock()
        self.total = int(total or 0)
        self.feitos = int(feitos or 0)
        self.inicio = 0.0
        self.fim = 0.0
        self._ultimo_t = 0.0
        self._ultimo_b = 0
        self._vel = 0.0

    # --- escrita (threads de transferencia) -------------------------------
    def comecar(self, total: int = None, feitos: int = None) -> None:
        with self._lock:
            if total is not None:
                self.total = int(total)
            if feitos is not None:
                self.feitos = int(feitos)
            agora = time.time()
            self.inicio = agora
            self.fim = 0.0
            self._ultimo_t = agora
            self._ultimo_b = self.feitos
            self._vel = 0.0

    def bloco(self, n: int) -> None:
        """Contabiliza n bytes NOVOS (delta, nunca acumulado)."""
        if n <= 0:
            return
        with self._lock:
            self.feitos += n
            agora = time.time()
            decorrido = agora - self._ultimo_t
            if decorrido >= 0.25:
                instantanea = (self.feitos - self._ultimo_b) / decorrido
                if self._vel <= 0:
                    self._vel = instantanea
                else:
                    # peso exponencial: quanto maior o intervalo, mais a
                    # amostra nova pesa - assim a conta nao depende de com
                    # que frequencia bloco() e chamado
                    peso = 1.0 - pow(2.718281828, -decorrido / TAU)
                    self._vel += (instantanea - self._vel) * peso
                self._ultimo_t = agora
                self._ultimo_b = self.feitos

    def terminar(self) -> None:
        with self._lock:
            self.fim = time.time()

    def ajustar_total(self, total: int) -> None:
        with self._lock:
            self.total = int(total or 0)

    # --- leitura (thread do Tkinter) --------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            feitos, total = self.feitos, self.total
            vel = self._vel
            inicio, fim = self.inicio, self.fim
            parado = time.time() - self._ultimo_t if self._ultimo_t else 0.0

        if fim and inicio:
            # terminou: a media do trecho todo e mais honesta que a ultima
            # janela, porque nao ha mais "agora"
            gasto = max(fim - inicio, 0.001)
            vel = feitos / gasto
        elif parado > 2 * TAU:
            # ninguem reportou bloco ha muito tempo: a velocidade caiu a zero
            vel = 0.0

        pct = (feitos / total * 100.0) if total > 0 else 0.0
        if total > 0 and vel > 0 and feitos < total:
            eta = (total - feitos) / vel
        else:
            eta = -1.0
        return {"feitos": feitos, "total": total, "velocidade": vel,
                "pct": min(pct, 100.0), "eta": eta,
                "total_conhecido": total > 0,
                "terminado": bool(fim)}


class MedidorGlobal:
    """Soma dos medidores ativos, para a barra de status.

    Somar snapshots de cada item a cada 250 ms seria O(n) na fila inteira -
    com 5.000 itens, isso pesa. Por isso os totais concluidos sao acumulados
    aqui quando cada item termina, e so os itens EM ANDAMENTO sao somados.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._ativos = {}          # id do item -> Medidor
        self.bytes_prontos = 0
        self.bytes_totais = 0
        self.itens_prontos = 0
        self.itens_totais = 0

    def registrar(self, item_id, medidor: Medidor) -> None:
        with self._lock:
            self._ativos[item_id] = medidor

    def concluir(self, item_id, bytes_feitos: int) -> None:
        with self._lock:
            self._ativos.pop(item_id, None)
            self.bytes_prontos += max(int(bytes_feitos or 0), 0)
            self.itens_prontos += 1

    def desistir(self, item_id) -> None:
        with self._lock:
            self._ativos.pop(item_id, None)

    def definir_totais(self, itens: int, bytes_: int) -> None:
        with self._lock:
            self.itens_totais = int(itens)
            self.bytes_totais = int(bytes_)

    def zerar(self) -> None:
        with self._lock:
            self._ativos.clear()
            self.bytes_prontos = self.bytes_totais = 0
            self.itens_prontos = self.itens_totais = 0

    def snapshot(self) -> dict:
        with self._lock:
            ativos = list(self._ativos.values())
            prontos = self.bytes_prontos
            totais = self.bytes_totais
            itens_prontos = self.itens_prontos
            itens_totais = self.itens_totais

        feitos = prontos
        vel = 0.0
        for m in ativos:
            s = m.snapshot()
            feitos += s["feitos"]
            vel += s["velocidade"]

        pct = (feitos / totais * 100.0) if totais > 0 else 0.0
        eta = (totais - feitos) / vel if (totais > 0 and vel > 0) else -1.0
        return {"feitos": feitos, "total": totais, "velocidade": vel,
                "pct": min(pct, 100.0), "eta": eta,
                "ativos": len(ativos),
                "itens_prontos": itens_prontos, "itens_totais": itens_totais}


class Limitador:
    """Limite de banda simples, por balde de tokens.

    Existe porque subir um arquivo grande no meio do expediente nao pode
    derrubar a chamada de video de ninguem. Dorme dentro da thread de
    transferencia, que e onde dormir nao atrapalha a interface.
    """

    def __init__(self, bytes_por_segundo: int = 0):
        self._lock = threading.Lock()
        self.taxa = int(bytes_por_segundo or 0)
        self._saldo = float(self.taxa)
        self._quando = time.time()

    def definir(self, bytes_por_segundo: int) -> None:
        with self._lock:
            self.taxa = int(bytes_por_segundo or 0)
            self._saldo = float(self.taxa)
            self._quando = time.time()

    def consumir(self, n: int) -> None:
        if self.taxa <= 0 or n <= 0:
            return
        while True:
            with self._lock:
                agora = time.time()
                self._saldo = min(float(self.taxa),
                                  self._saldo + (agora - self._quando) * self.taxa)
                self._quando = agora
                if self._saldo >= n:
                    self._saldo -= n
                    return
                falta = (n - self._saldo) / self.taxa
            time.sleep(min(falta, 0.25))
