# Revisão para publicação

Esta pasta é uma distribuição separada para portfólio. A instalação operacional não foi transformada em repositório público.

## Escopo revisado

Foi feito inventário recursivo dos arquivos da instalação original. A distribuição inclui somente código da aplicação, testes, ferramentas, configuração de exemplo e documentação. Foram excluídos configuração operacional, backups cifrados, estados de backup, caches e demais arquivos gerados. Não contém banco, exportação de dados, contas de operador, compradores, tokens, chave de proteção ou histórico Git operacional.

A revisão de conteúdo procurou padrões de tokens, chaves privadas, credenciais em URLs, caminhos pessoais, identificadores reais de anúncios e correspondência com os valores privados do .env operacional. Os valores nunca são exibidos pelo verificador nem copiados para o relatório. As ocorrências de endereços em testes são exemplos sintéticos.

## Ajustes aplicados

- `.env.example` com campos privados vazios.
- Senhas fixas removidas da configuração Compose desta distribuição.
- `tools/setup_local.py` gera credenciais novas por instalação, sem imprimi-las.
- Exclusões reforçadas para credenciais, bancos, backups, logs, capturas de tráfego e arquivos privados.
- `tools/check_publication.py` verifica arquivos ou o conteúdo efetivamente selecionado no Git; hook opcional em `.githooks/pre-commit`.
- README revisado para apresentar as funcionalidades e limitações reais do projeto.

A revisão trata da preparação para publicar código e da exposição de informações privadas. Não representa certificação de segurança da aplicação. Publique somente os arquivos desta distribuição. Configurações e dados adicionados depois exigem nova conferência.

## Imagens e apresentação

As cinco capturas operacionais fornecidas como referência não integram este pacote: continham identificadores reais de anúncios e pedido, estoques e valores da loja. A versão pública usa cinco novas capturas do mesmo dashboard, executado por tools/demo_preview.py com base temporária e provedor simulado. Todas mostram o aviso de demonstração, sem dados pessoais ou operacionais reais. A captura financeira usa a lógica corrigida de tarifa por item.

As imagens JPEG foram verificadas visualmente, conferidas sem metadados EXIF e registradas por SHA-256 em docs/images/manifest.json. O verificador bloqueia binários não revisados e imagens que não correspondam ao hash aprovado. Isso registra a revisão das imagens atuais; novas capturas precisam de nova revisão humana antes de atualizar o manifesto.

O README foi refeito com apresentação do projeto pessoal, cinco telas, funcionalidades, arquitetura, fluxo de execução, demonstração sem credenciais, instalação, segurança, testes e limitações atuais. A conta real, o domínio do túnel e a configuração privada permanecem fora da distribuição.
