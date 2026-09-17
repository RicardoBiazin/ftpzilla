"""Persistencia da fila, em SQLite.

Por que SQLite e nao JSON: a fila e escrita a cada poucos segundos por
varias threads, precisa de atualizacao parcial (so o bytes_feitos de UM
item) e tem que sobreviver ao processo morrer no meio. Reescrever um JSON
de cinco mil itens a cada tique e inviavel, e deixa uma janela em que o
arquivo esta pela metade - justamente o momento em que o computador
desliga.

Todas as escritas passam por UMA thread. Assim nao ha contencao, nao ha
check_same_thread para contornar e a ordem das operacoes e a ordem em que
elas aconteceram.

O que NAO entra neste banco: senha, passphrase, token. So o id do site. Se
o site for apagado do sites.json, o item vira falha com uma mensagem clara,
em vez de guardar credencial em mais um lugar.
"""
from __future__ import annotations

import os
import queue
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from . import log, paths

logger = log.get()

# --- estados de um item ----------------------------------------------------
ESPERANDO = "esperando"
RODANDO = "rodando"
PAUSADO = "pausado"
CONCLUIDO = "concluido"
FALHOU = "falhou"
CANCELADO = "cancelado"
PULADO = "pulado"

ATIVOS = (ESPERANDO, RODANDO, PAUSADO)
ENCERRADOS = (CONCLUIDO, FALHOU, CANCELADO, PULADO)

# --- sentidos --------------------------------------------------------------
BAIXAR = "download"
ENVIAR = "upload"

#: grava o progresso no maximo a cada N segundos ou M bytes por item.
#: Sem isso, oito transferencias simultaneas transformam o disco no gargalo;
#: com isso, o pior caso de perda numa queda e de alguns segundos, que a
#: retomada recupera.
INTERVALO_PROGRESSO = 2.0
BYTES_PROGRESSO = 8 * 1024 * 1024

ESQUEMA = """
CREATE TABLE IF NOT EXISTS itens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    grupo         TEXT    NOT NULL DEFAULT '',
    site_id       TEXT    NOT NULL DEFAULT '',
    site_nome     TEXT    NOT NULL DEFAULT '',
    sentido       TEXT    NOT NULL,
    origem        TEXT    NOT NULL,
    destino       TEXT    NOT NULL,
    tamanho       INTEGER NOT NULL DEFAULT -1,
    mtime         REAL    NOT NULL DEFAULT 0,
    bytes_feitos  INTEGER NOT NULL DEFAULT 0,
    estado        TEXT    NOT NULL DEFAULT 'esperando',
    prioridade    REAL    NOT NULL DEFAULT 0,
    tentativas    INTEGER NOT NULL DEFAULT 0,
    proxima_em    REAL    NOT NULL DEFAULT 0,
    erro          TEXT    NOT NULL DEFAULT '',
    acao_existente TEXT   NOT NULL DEFAULT 'perguntar',
    upload_url    TEXT    NOT NULL DEFAULT '',
    criado_em     REAL    NOT NULL DEFAULT 0,
    atualizado_em REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_itens_estado ON itens(estado, prioridade);

CREATE TABLE IF NOT EXISTS segmentos (
    item_id INTEGER NOT NULL,
    indice  INTEGER NOT NULL,
    ini     INTEGER NOT NULL,
    fim     INTEGER NOT NULL,
    feito   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, indice)
);

CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
"""

COLUNAS = ("id", "grupo", "site_id", "site_nome", "sentido", "origem",
           "destino", "tamanho", "mtime", "bytes_feitos", "estado",
           "prioridade", "tentativas", "proxima_em", "erro", "acao_existente",
           "upload_url", "criado_em", "atualizado_em")


class _Tarefa:
    __slots__ = ("funcao", "evento", "resultado", "erro")

    def __init__(self, funcao):
        self.funcao = funcao
        self.evento = None
        self.resultado = None
        self.erro = None

    def esperar(self, timeout: float = 10.0):
        self.evento = self.evento or threading.Event()
        if not self.evento.wait(timeout):
            raise TimeoutError("a thread de gravacao da fila nao respondeu")
        if self.erro is not None:
            raise self.erro
        return self.resultado


class FilaStore:
    """Acesso ao fila.db. Uma thread escreve; qualquer thread pode pedir."""

    def __init__(self, caminho: str = ""):
        self.caminho = caminho or paths.arquivo_fila()
        self._fila: "queue.Queue[Optional[_Tarefa]]" = queue.Queue()
        self._con: Optional[sqlite3.Connection] = None
        self._thread: Optional[threading.Thread] = None
        self._parar = threading.Event()
        self._pendentes: Dict[int, int] = {}      # item_id -> bytes_feitos
        self._ultimo_flush = 0.0
        self._gravado: Dict[int, int] = {}        # item_id -> ultimo gravado

    # ------------------------------------------------------------------
    def abrir(self) -> "FilaStore":
        os.makedirs(os.path.dirname(self.caminho) or ".", exist_ok=True)
        self._thread = threading.Thread(target=self._laco, name="fila-store",
                                        daemon=True)
        self._thread.start()
        self._executar(self._criar_esquema).esperar()
        return self

    def _criar_esquema(self, con) -> None:
        con.executescript(ESQUEMA)
        con.commit()

    def _laco(self) -> None:
        self._con = sqlite3.connect(self.caminho, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        # WAL deixa leitura e escrita conviverem; synchronous=NORMAL troca
        # um fsync por operacao por um a cada checkpoint, que e o que torna
        # viavel gravar progresso de varias transferencias ao mesmo tempo
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        while not self._parar.is_set():
            try:
                tarefa = self._fila.get(timeout=0.5)
            except queue.Empty:
                self._flush_progresso()
                continue
            if tarefa is None:
                break
            try:
                tarefa.resultado = tarefa.funcao(self._con)
            except Exception as e:           # noqa: BLE001
                tarefa.erro = e
                logger.error("Erro ao gravar a fila: %s", e)
            finally:
                if tarefa.evento is not None:
                    tarefa.evento.set()
        self._flush_progresso()
        try:
            if self._con is not None:
                self._con.commit()
                self._con.close()
        except Exception:
            pass

    def _executar(self, funcao, esperar: bool = True) -> _Tarefa:
        tarefa = _Tarefa(funcao)
        if esperar:
            tarefa.evento = threading.Event()
        self._fila.put(tarefa)
        return tarefa

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------
    def carregar(self) -> List[dict]:
        """Le a fila do disco. Item que estava RODANDO vira PAUSADO.

        Ninguem esta transferindo nada agora - o programa acabou de abrir -
        entao deixar 'rodando' no banco so confundiria. O bytes_feitos e
        preservado: e dele que a retomada parte, depois de validar o parcial.
        """
        def tarefa(con):
            con.execute("UPDATE itens SET estado=? WHERE estado=?",
                        (PAUSADO, RODANDO))
            con.commit()
            cur = con.execute("SELECT * FROM itens ORDER BY prioridade, id")
            return [dict(l) for l in cur.fetchall()]
        return self._executar(tarefa).esperar(30.0)

    def segmentos(self, item_id: int) -> List[dict]:
        def tarefa(con):
            cur = con.execute("SELECT * FROM segmentos WHERE item_id=? "
                              "ORDER BY indice", (item_id,))
            return [dict(l) for l in cur.fetchall()]
        return self._executar(tarefa).esperar()

    def meta(self, chave: str, padrao: str = "") -> str:
        def tarefa(con):
            cur = con.execute("SELECT valor FROM meta WHERE chave=?", (chave,))
            linha = cur.fetchone()
            return linha["valor"] if linha else padrao
        return self._executar(tarefa).esperar()

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------
    def inserir(self, itens: List[dict]) -> List[int]:
        """Grava os itens novos e devolve os ids gerados."""
        def tarefa(con):
            ids = []
            agora = time.time()
            for d in itens:
                d = dict(d)
                d.setdefault("criado_em", agora)
                d["atualizado_em"] = agora
                campos = [c for c in COLUNAS if c != "id" and c in d]
                sql = ("INSERT INTO itens (%s) VALUES (%s)"
                       % (", ".join(campos),
                          ", ".join("?" for _ in campos)))
                cur = con.execute(sql, [d[c] for c in campos])
                ids.append(cur.lastrowid)
            con.commit()
            return ids
        return self._executar(tarefa).esperar(30.0)

    def atualizar(self, item_id: int, esperar: bool = False, **campos) -> None:
        """Atualiza campos de um item. Transicao de estado grava na hora."""
        if not campos:
            return
        campos = {k: v for k, v in campos.items() if k in COLUNAS}
        campos["atualizado_em"] = time.time()

        def tarefa(con):
            # o progresso pendente deste item precisa ir junto, senao uma
            # gravacao atrasada sobrescreveria o bytes_feitos recem-mudado
            pendente = self._pendentes.pop(item_id, None)
            dados = dict(campos)
            if pendente is not None and "bytes_feitos" not in dados:
                dados["bytes_feitos"] = pendente
            sql = ("UPDATE itens SET %s WHERE id=?"
                   % ", ".join("%s=?" % c for c in dados))
            con.execute(sql, list(dados.values()) + [item_id])
            con.commit()
            self._gravado[item_id] = dados.get("bytes_feitos",
                                               self._gravado.get(item_id, 0))
        self._executar(tarefa, esperar=esperar)
        if esperar:
            pass

    def progresso(self, item_id: int, bytes_feitos: int) -> None:
        """Anota o progresso; a gravacao sai em lote (ver _flush_progresso)."""
        self._pendentes[item_id] = int(bytes_feitos)
        anterior = self._gravado.get(item_id, 0)
        if bytes_feitos - anterior >= BYTES_PROGRESSO:
            self._executar(lambda con: self._flush_progresso(con),
                           esperar=False)

    def _flush_progresso(self, con=None) -> None:
        con = con or self._con
        if con is None or not self._pendentes:
            return
        agora = time.time()
        if agora - self._ultimo_flush < INTERVALO_PROGRESSO and self._pendentes:
            # so segura quando o intervalo ainda nao passou E nao ha muito
            # acumulado; quem chamou por volume ja forcou a passagem acima
            grande = any(b - self._gravado.get(i, 0) >= BYTES_PROGRESSO
                         for i, b in self._pendentes.items())
            if not grande:
                return
        pendentes, self._pendentes = self._pendentes, {}
        try:
            con.executemany(
                "UPDATE itens SET bytes_feitos=?, atualizado_em=? WHERE id=?",
                [(b, agora, i) for i, b in pendentes.items()])
            con.commit()
            self._gravado.update(pendentes)
            self._ultimo_flush = agora
        except Exception as e:      # noqa: BLE001
            logger.error("Erro ao gravar o progresso da fila: %s", e)

    def gravar_segmentos(self, item_id: int, faixas) -> None:
        def tarefa(con):
            con.execute("DELETE FROM segmentos WHERE item_id=?", (item_id,))
            con.executemany(
                "INSERT INTO segmentos (item_id, indice, ini, fim, feito) "
                "VALUES (?,?,?,?,0)",
                [(item_id, i, a, b) for i, (a, b) in enumerate(faixas)])
            con.commit()
        self._executar(tarefa).esperar()

    def segmento_feito(self, item_id: int, indice: int, feito: int) -> None:
        self._executar(
            lambda con: (con.execute(
                "UPDATE segmentos SET feito=? WHERE item_id=? AND indice=?",
                (feito, item_id, indice)), con.commit()), esperar=False)

    def remover(self, ids: List[int]) -> None:
        def tarefa(con):
            marcas = ",".join("?" for _ in ids)
            con.execute("DELETE FROM itens WHERE id IN (%s)" % marcas, ids)
            con.execute("DELETE FROM segmentos WHERE item_id IN (%s)" % marcas,
                        ids)
            con.commit()
        if ids:
            for i in ids:
                self._pendentes.pop(i, None)
                self._gravado.pop(i, None)
            self._executar(tarefa).esperar()

    def limpar(self, estados=ENCERRADOS) -> int:
        def tarefa(con):
            marcas = ",".join("?" for _ in estados)
            cur = con.execute("SELECT id FROM itens WHERE estado IN (%s)"
                              % marcas, list(estados))
            ids = [l["id"] for l in cur.fetchall()]
            if ids:
                m = ",".join("?" for _ in ids)
                con.execute("DELETE FROM itens WHERE id IN (%s)" % m, ids)
                con.execute("DELETE FROM segmentos WHERE item_id IN (%s)" % m,
                            ids)
                con.commit()
            return len(ids)
        return self._executar(tarefa).esperar(30.0)

    def definir_meta(self, chave: str, valor: str) -> None:
        self._executar(
            lambda con: (con.execute(
                "INSERT INTO meta (chave, valor) VALUES (?,?) "
                "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
                (chave, str(valor))), con.commit()), esperar=False)

    # ------------------------------------------------------------------
    def fechar(self) -> None:
        if self._thread is None:
            return
        self._executar(lambda con: self._flush_progresso(con)).esperar(10.0)
        self._parar.set()
        self._fila.put(None)
        self._thread.join(timeout=10.0)
        self._thread = None
