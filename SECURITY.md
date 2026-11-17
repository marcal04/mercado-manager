# Segurança e publicação

Esta distribuição contém somente código, testes e documentação. Não inclui a configuração da instalação operacional, tokens, contas de operador, bancos, backups, logs ou dados de vendedores/compradores. Os testes usam dados fictícios e não enviam alterações reais ao Mercado Livre.

## Antes de publicar

Execute `python tools/check_publication.py`. Depois de iniciar um repositório e selecionar os arquivos, execute `python tools/check_publication.py --staged`. Para usar a verificação antes de cada commit: `git config core.hooksPath .githooks`.

O verificador mostra somente nome do arquivo, linha e categoria; nunca mostra o valor encontrado. Ele cobre arquivos privados e padrões comuns de credenciais. A comparação opcional `--compare-env CAMINHO_PRIVADO` procura também os valores da instalação operacional sem copiá-los para o projeto. A verificação reduz o risco e não substitui revisão humana. Não é uma auditoria completa de vulnerabilidades.

O `.gitignore` não remove arquivos já incluídos em commits. Se uma credencial tiver sido publicada, revogue/rotacione a credencial no provedor e trate o histórico do Git; apagar o arquivo no último commit não é suficiente. Esta distribuição foi preparada sem histórico Git da instalação original.

## Execução local

`python tools/setup_local.py` gera credenciais novas em `.env`, sem mostrá-las e sem substituir um arquivo existente. Não reutilize chaves da sua conta real em demonstrações públicas. Variáveis de segredo ficam no servidor. O executor de escrita e a IA estão desativados no modelo público. O serviço é publicado somente em 127.0.0.1; não exponha banco, arquivos .env ou volumes de backup.

Para portfólio, use contas e dados fictícios. Revise capturas de tela antes de publicar: não inclua compradores, endereços, vendas reais, cookies, códigos OAuth, URLs privadas ou telas de credenciais.

## Capturas de tela do portfólio

Não copie capturas da operação real para a pasta pública. As imagens incluídas foram produzidas no modo de demonstração e usam somente dados fictícios. O manifesto em docs/images/manifest.json registra arquivos revisados e seus hashes; a ferramenta bloqueia imagens alteradas ou não registradas. Um hash não prova ausência de informação privada: revise visualmente toda captura nova antes de aprová-la.
