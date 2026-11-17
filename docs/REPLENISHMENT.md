# Planejamento local de reposição

Em Reposição, selecione um produto e cadastre fornecedor, prazo de entrega, cobertura adicional após a entrega, segurança em unidades, múltiplo do lote, pedido mínimo e quantidade em trânsito. A demanda diária é opcional: vazia usa pedidos pagos importados; preenchida (inclusive zero) usa sua previsão manual. Não existem compras, pagamentos, mensagens ao fornecedor ou movimentações automáticas de estoque.

## Fórmula
- Demanda histórica: unidades de pedidos pagos / dias da janela. Usa apenas a conta conectada e o SKU meli:ID do anúncio. Variações do anúncio são agregadas, sem distinguir estoque por variante.
- Histórico automático exige a última importação bem-sucedida, completa, de até 24 horas e com janela pelo menos igual à escolhida. O período termina na data dessa importação. Sem essas condições, a demanda fica indisponível, em vez de presumir zero.
- Disponível para o plano = estoque físico + unidades em trânsito − unidades de pedidos manuais pendentes. Quando um pedido manual é confirmado, ele deixa a reserva do plano, pois já baixou o estoque físico.
- Alvo = maior entre estoque mínimo cadastrado e teto(demanda diária × (prazo + cobertura adicional)) + segurança. A segurança se aplica ao termo de demanda antes da comparação com o mínimo.
- Necessidade = máximo(0, alvo − disponível). Se positiva, aplica pedido mínimo e arredonda para cima ao múltiplo do lote.
- Estimativa de mercadoria = quantidade sugerida × custo unitário. Não inclui frete de compra ou encargos extras. Cobertura disponível em dias é mostrada apenas quando a demanda é maior que zero.

## Dados e revisão
Sem regras, contagem física, custo positivo ou demanda utilizável, criar proposta fica bloqueado. Um estoque físico explicitamente contado como zero é diferente de estoque nunca conferido. O plano pode exibir quantidades informativas mesmo com custo pendente, mas não permitirá proposta.

Pedidos pagos do Mercado Livre servem apenas como demanda, não são novamente reservados ou descontados do estoque. A contagem física precisa refletir a operação real. Mercadoria em trânsito é um campo manual: atualize-o ao receber produtos para evitar dupla contagem. Não há gestão de compras recebidas nesta etapa.

A proposta registra o cálculo e exige revisão de riscos e premissas. Repetir uma proposta pendente para o produto é bloqueado no uso sequencial. Se custos, estoque ou regras mudarem antes de criar a proposta, a tela deve ser atualizada. Aprovar não faz compra nem entrada de estoque; uma futura execução precisará revalidar tudo.

Demanda manual e médias passadas não garantem vendas futuras. Reposição é uma projeção local por regras, sem IA paga.

## Validação
Testes de ausência de estoque/demanda, reserva manual sem dupla contagem, lote mínimo, mercadoria em trânsito, duplicidade, propostas sem compra, mudança de premissas, conta/período/status do histórico, variações agregadas e importação incompleta ou antiga.
