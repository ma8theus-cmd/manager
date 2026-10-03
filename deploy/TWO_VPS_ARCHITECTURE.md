# Arquitetura MesLibertines — 2 VPS

## Objetivo
Separar completamente as operacoes em dois nos com funcoes distintas.

## VPS1 — CONTROL
Responsabilidades:
- criacao e ativacao de contas;
- painel principal e banco central;
- coleta publica de comentarios promissores;
- exportacao da pesquisa (TXT/CSV/JSON);
- controle de limites e auditoria de cadastro.

Nao deve executar:
- postagem de comentarios;
- Scout de comentarios.

Configuracao:
`MESLIB_NODE_ROLE=control`

## VPS2 — COMMENT_WORKER
Responsabilidades:
- postar comentarios autorizados;
- executar Scout dos comentarios;
- devolver resultados ao controle central;
- usar IP fixo/proprio para esse fluxo.

Nao deve executar:
- criacao de contas;
- coleta publica de comentarios promissores.
