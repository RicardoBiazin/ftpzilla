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
- [x] **M2** — FTP e FTPS navegáveis, gerente de sites, credenciais protegidas.
- [x] **M3** — Fila de transferências persistente.
- [x] **M4** — Retomada e repetição automática.
- [x] **M5** — SFTP.
- [x] **M6** — Download segmentado.
- [x] **M7** — Comparação e sincronização de pastas.
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

## Sobre o download em várias conexões

Um arquivo grande pode ser baixado em até quatro conexões simultâneas, cada uma pegando uma faixa do arquivo. Isso ajuda **em um caso específico**: servidor que limita a banda *por conexão* — o que é comum em hospedagem compartilhada. Aí a diferença é entre usar 20% e 100% do seu link.

Fora desse caso, segmentar **não** ajuda, e pode atrapalhar. Por isso o FTPZilla só segmenta quando todas estas condições valem:

- é download (envio segmentado não é confiável em FTP, e o ganho em SFTP não paga o risco);
- o servidor aceita retomada por faixa (`REST STREAM` no FTP, `seek` no SFTP);
- o arquivo tem pelo menos 64 MB — abrir uma conexão FTPS custa de 0,3 a 1 segundo, o que engole o ganho em arquivo pequeno;
- há conexão sobrando no pool e **ninguém esperando na fila** — senão segmentar apenas rouba conexão de outro arquivo, e o total não melhora;
- o destino é um arquivo local comum;
- não é Google Drive nem OneDrive, onde o gargalo é a cota de requisições e mais paralelismo só aproxima o erro 429.

Medindo em rede local ou contra `localhost` você **não vai ver diferença** — não há o que contornar ali. O ganho aparece contra servidor remoto com limite por conexão.

O arquivo é pré-alocado no disco e cada conexão escreve na sua faixa; ao terminar, o resultado é conferido (hash quando o servidor sabe calcular, tamanho quando não sabe) antes de virar o arquivo final.

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
