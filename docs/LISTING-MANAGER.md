# Gerenciar anúncios com aprovação real

A nova área Gerenciar anúncios prepara mudanças reais, separadas das aprovações simuladas antigas. Consultas automáticas continuam apenas lendo. Nenhuma IA ou trabalho agendado executa uma alteração nesta entrega.

## Operações

- Preço, com cotação da API e meta de margem conferidas na preparação e na execução.
- Quantidade anunciada, pausa e reativação.
- Título, nome da família, descrição e galeria ordenada, somente nos anúncios onde o tipo e o estado permitem.
- Publicação de novos produtos com categoria sugerida, atributos, preço, quantidade, fotos, condição, tipo de anúncio, garantia opcional e descrição. A validação remota /items/validate não publica. Para contas User Products, envia family_name e permite que a plataforma gere o título.

Catálogo mantém título, fotos e conteúdo da ficha sob controle da plataforma. User Products e vínculos podem propagar campos compartilhados para outras publicações. Multi-origem, variações e Full não usam o controle simples de quantidade. Preço com regra automática é bloqueado. Não há exclusão definitiva, encerramento, compra, reembolso ou alteração de orçamento de publicidade nesta área.

## Revisão e envio

A proposta é imutável, cifrada no banco, vinculada à conta e válida por 15 minutos. O operador vê antes/depois e confirma expressamente o envio pelo botão da revisão. O servidor exige o hash exato da proposta, confere propriedade do anúncio, permissões e dados atuais. Dados ou tarifas alterados exigem nova proposta. A execução por vendedor é serializada por índice único parcial no banco; o estado é persistido antes da requisição comercial. Repetir a aprovação não repete a alteração. Há auditoria de preparação, aprovação, rejeição e resultado, sem credenciais.

POST /items é feito uma única vez. A identidade criada é persistida antes de enviar a descrição separadamente por POST /items/{id}/description. Falha nessa etapa deixa resultado parcial e a identidade para corrigir só a descrição, sem republicar. Resposta perdida deixa resultado desconhecido. Conferir resultado usa apenas GET e nunca reenvia uma mutação. Se uma criação não devolver identificador, é necessário consultar os anúncios da conta; não existe republicação automática. Fotos novas recebem identificadores novos e podem precisar de conferência visual na plataforma.

As chamadas usam caminhos fixos tipados, sem proxy de URL ou JSON arbitrário. Fotos aceitam identificadores ou URLs HTTPS públicas; o servidor não baixa imagens externas. Rotas privadas e limites de frequência aplicam-se às operações. A flag MARKETPLACE_WRITE_ENABLED permite o executor local, mas não cria permissões no Mercado Livre.

## Permissões externas

Conferir permissões consulta GET /applications/{id} no servidor e guarda somente o diagnóstico, sem credenciais. A permissão geral write não equivale aos escopos funcionais: publicação/sincronização e publicidade podem continuar read-only. O executor confere novamente antes de enviar. Quando o acesso específico está somente leitura, não faz requisição comercial. É necessário editar os acessos no DevCenter, salvar e renovar OAuth em Ajustes. Permissão configurada não garante que o produto, anunciante ou operação estejam liberados para a conta.

Product Ads depende da disponibilidade de anunciante para a conta. A documentação pública consultada descreve leitura de métricas e campanhas; escrita paga não é presumida com base nessas rotas. Campanhas, testes de fotos/títulos e carrosséis e decisões autônomas permanecem próximos módulos; dependem de acesso, operações suportadas e limites de investimento aprovados. A integração de IA permanece desativada.

## Validação

Testes usam httpx.MockTransport e não alteram anúncios reais. Cobrem revisão exata, expiração, rejeição, acesso fechado, permissão granular, margem, dados desatualizados, propriedade, restrições de catálogo/UP, pausa, publicação e descrição, resposta perdida, não repetição e exclusão mútua da execução. A validação de escrita ponta a ponta no Mercado Livre continua pendente de liberação externa e aprovação de uma alteração concreta pelo operador.

Fontes oficiais consultadas:
- https://developers.mercadolivre.com.br/pt_br/produto-sincronizacao-de-publicacoes
- https://developers.mercadolivre.com.br/pt_br/preco-variacao
- https://developers.mercadolivre.com.br/pt_br/produto-consulta-de-usuarios/estoque-distribuido
- https://developers.mercadolivre.com.br/pt_br/pt_br/publicacao-de-produtos
- https://developers.mercadolivre.com.br/pt_br/api-docs-pt-br/validador-de-publicacoes
- https://developers.mercadolivre.com.br/pt_br/automatizacoes-de-precos
- https://developers.mercadolivre.com.br/pt_br/product-ads-para-catalogo-e-user-products-leitura
- https://developers.mercadolibre.com.ar/primeros-pasos-in-house
