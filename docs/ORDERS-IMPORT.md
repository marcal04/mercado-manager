# Pedidos reais do Mercado Livre — somente leitura

## Como usar
1. Entre em Pedidos e mantenha selecionada a origem Mercado Livre.
2. Escolha 7, 30, 90 ou 365 dias e clique em Importar pedidos.
3. Filtre por status ou pelo número do pedido. Use a paginação para consultar a cópia local.
4. Exporte o relatório CSV ou abra o histórico de importações.
5. Para continuar registrando pedidos manuais, selecione Controle manual.

A importação consulta GET /orders/search com seller, período, limite e offset. Lê no máximo 1.000 resultados por tentativa; se atingir esse limite, a interface indica importação parcial e recomenda reduzir o período. Pedidos armazenados de outras janelas não são excluídos. O resumo mostra toda a base importada, não só a última janela. Novas importações atualizam o mesmo número de pedido, sem duplicá-lo, e respostas antigas não sobrescrevem versões mais recentes.

## Limites da informação
- Somente número, vendedor da integração, status, moeda, total bruto, datas e linhas de produtos são armazenados. Não persistimos nomes, documentos, endereços nem payloads completos de compradores.
- A base é uma cópia, não tempo real. Cancelamentos fora da janela consultada só serão atualizados quando incluídos em nova importação.
- Status pago não significa enviado ou entregue. Envio e rastreamento não são consultados nesta etapa.
- Totais são separados por moeda e incluem somente status paid. Não representam saldo recebido, lucro ou conciliação financeira.
- Não existe conversão automática para pedido manual nem baixa de estoque físico, evitando duplicidade.
- A importação manual não modifica o Mercado Livre; POST externo é usado apenas para renovação de credenciais OAuth quando o token expira.
- A renovação conserva o novo refresh token cifrado, verificando a conta retornada e bloqueando a linha durante a troca no PostgreSQL. Se a autorização foi revogada, será necessário reconectar em Ajustes.
- Permissão negada, limitação de consultas ou falha de leitura são exibidas na tela e no histórico, preservando os pedidos da importação anterior. Uma interrupção do serviço pode deixar uma tentativa como em andamento; uma nova importação pode ser iniciada após reiniciar.

## Validação
Testes isolados, com respostas simuladas da API: paginação, atualização de status, idempotência, preservação do estoque físico, bloqueio de conta divergente, descarte de dados pessoais, falha sem gravação parcial, renovação de tokens e mensagens de permissão. Nenhum teste chama o marketplace ou OpenAI.

## Referências oficiais
- https://developers.mercadolivre.com.br/pt_br/gerenciamento-de-vendas
- https://developers.mercadolivre.com.br/autenticacao-e-autorizacao
