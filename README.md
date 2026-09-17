# FTPZilla

Cliente de transferência de arquivos para Windows — FTP, FTPS, SFTP, Google Drive e OneDrive — escrito em Python com interface Tkinter.

A proposta é simples: fazer o que o FileZilla faz, sem as três coisas que mais incomodam nele.

| Incômodo | O que o FTPZilla faz |
|---|---|
| Senhas em XML legível na pasta do usuário | Senha, passphrase e token cifrados pela **DPAPI**, amarrados à sua conta do Windows. O arquivo copiado para outra máquina não entrega nada. |
| Instalador com adware | Um `.exe` só, sem instalador, sem patrocinador, sem tela de oferta. |
| A janela congela enquanto a fila trabalha | A conexão de navegação é **separada** das conexões de transferência. Navegar nunca espera a fila. |

E mais três que são ganho direto:

- **Retomada de verdade.** A fila vive em SQLite e sobrevive a fechar o programa (ou a um desligamento). Ao voltar, a transferência continua do byte exato — depois de conferir que o parcial local e o arquivo remoto ainda batem.
- **Nuvem sem pagar.** Google Drive e OneDrive entram como qualquer outro servidor.
- **Comparar e sincronizar pastas** com pré-visualização do que vai acontecer antes de qualquer byte sair do lugar.

## Estado atual

Em construção, por marcos. Cada marco entrega algo que funciona de ponta a ponta e tem sua bateria de testes.

- [x] **M1** — Base do projeto, abstração de acesso a arquivos, disco local, dois painéis navegáveis, temas.
- [ ] **M2** — FTP e FTPS navegáveis, gerente de sites, credenciais protegidas.
- [ ] **M3** — Fila de transferências persistente.
- [ ] **M4** — Retomada e repetição automática.
- [ ] **M5** — SFTP.
- [ ] **M6** — Download segmentado.
- [ ] **M7** — Comparação e sincronização de pastas.
- [ ] **M8** — Abas, favoritos, busca remota, editor remoto.
- [ ] **M9** — Google Drive e OneDrive.
- [ ] **M10** — Executável e empacotamento.

## Como rodar

Precisa de Python 3.11 ou mais novo (testado no 3.13).

```
pip install -r requirements.txt
python app.py
```

Opções da linha de comando:

```
python app.py --tema Claro          # tema inicial
python app.py --pasta-dados .\dados # modo portátil: tudo numa pasta só
python app.py --verboso             # log em nível DEBUG
```

## Onde ficam os arquivos

Por padrão em `%APPDATA%\FTPZilla\`:

| Arquivo | O que guarda |
|---|---|
| `sites.json` | Servidores salvos. Host, usuário e pastas em texto; **senha e token cifrados**. |
| `fila.db` | Fila de transferências (SQLite). Guarda o `id` do site, **nunca a credencial**. |
| `known_hosts` | Chaves de host SSH aceitas, no formato do OpenSSH. |
| `logs\` | Log diário, com senha e token mascarados antes de gravar. |

A variável de ambiente `FTPZILLA_HOME` (ou `--pasta-dados`) muda essa pasta — é o que permite carregar o programa num pendrive.

## Testes

Sem pytest: cada suíte é um script que roda sozinho.

```
python tests\rodar_todos.py      # tudo
python tests\teste_local.py      # uma suíte só
```

Os testes de FTP e SFTP sobem o servidor dentro do próprio processo (`pyftpdlib` e `paramiko`), então não dependem de nenhum servidor externo no ar. Instale as dependências de teste com `pip install -r requirements-dev.txt`.

## Limites conhecidos

Prefiro dizer do que a pessoa descobrir usando:

- **Arrastar de dentro do FTPZilla para o Explorer não funciona.** Isso exigiria implementar COM (`IDataObject`/`CFSTR_FILEDESCRIPTOR`) e está fora do escopo. Use o menu de contexto → "Baixar para...". O caminho contrário (arrastar do Explorer para a janela) funciona com o pacote opcional `tkinterdnd2`.
- **Pastas com dezenas de milhares de arquivos ficam pesadas.** A lista do Tkinter não tem modo virtual. O painel insere em lotes para não travar, esconde os ícones acima de 5.000 itens e, acima de 20.000, pede que você use o filtro.
- **Só Windows, por enquanto.** Roda em Linux e macOS, mas a proteção de credenciais cai para texto puro (a DPAPI é do Windows) e o programa avisa isso na tela.

## Estrutura do projeto

```
app.py                ponto de entrada
ftpzilla/
  remotes/            acesso a arquivos: local, ftp, sftp, nuvem
  ui/                 Tkinter: janela, painéis, tema, diálogos
  browser.py          navegação (uma thread por servidor)
  fila.py fila_store.py transfer.py pool.py    o motor de transferência
  compare.py          comparação e sincronização
tests/                suítes executáveis
```

## Licença

MIT. Veja [LICENSE](LICENSE).
