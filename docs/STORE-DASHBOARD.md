# Visão geral da operação

A página inicial destaca pedidos importados do Mercado Livre. As datas da interface usam o horário de São Paulo; horários UTC sem indicação de fuso, como os de testes SQLite, são interpretados como UTC. A janela pode ser de 7, 30 ou 90 dias e termina na data da última importação exibida. O total bruto inclui apenas pedidos com status paid e mantém cada moeda separada. Cancelados não entram no bruto pago. Pagamento não informa se houve entrega, lucro ou repasse recebido.

Uma importação completa, recente e cobrindo o período selecionado pode confirmar que não houve pedidos. Ausência de histórico, consulta incompleta, falha posterior ou dados antigos não são apresentados como zero confirmado. Registros existentes continuam visíveis como observações da cópia local. Uma consulta é considerada recente por até 24 horas; o agendamento normal continua configurado separadamente na central de dados.

Os cinco pedidos mais recentes da janela possuem acesso à consulta no painel de Pedidos. O controle manual aparece em um bloco separado e não é somado aos números da API. Aprovações continuam locais e simuladas.

A lista de próximos passos reúne conta, histórico, agendamento, aquisição, despesas e margem, estoque anunciado, backup e acesso privado. A aquisição zero de estoque próprio conta como confirmada. A conferência física é um controle separado, não uma exigência para usar a quantidade anunciada da API. Despesas ausentes ficam pendentes e possuem atalho para o cadastro, sem presumir frete ou imposto zero. As restrições da última sincronização da API permanecem visíveis separadamente.

O botão Atualizar recarrega os dados locais da visão geral. Ele não muda anúncios nem aciona uma importação externa. A sincronização externa continua na Central de dados e no agendamento já existente. As atualizações periódicas da situação da central renovam também os indicadores da página inicial.

A rota /api/v1/store-dashboard é protegida pelo mesmo acesso do operador. A consulta de pedidos é restrita à conta conectada e usa parâmetros ORM. Não há campos de comprador, endereços, tokens, hashes de senha ou chaves na resposta.

Testes cobrem janela, ausência de dados, zero confirmado, histórico incompleto/antigo, falha posterior, moedas separadas, exclusão de outra conta, aquisição própria com custo zero e acesso protegido.
