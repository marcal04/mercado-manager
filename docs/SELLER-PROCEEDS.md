# Quanto sobra pela API

Em Preços e margem, a cotação usa o anúncio atual ou um preço hipotético, sem alterar o anúncio. O catálogo e a visão geral usam a cotação do preço atual. A meta é configurável e independente dos custos manuais.

Cálculo por unidade: preço menos sale_fee_amount (total, sem somar fixed_fee novamente), menos coverage.all_country.list_cost (cotação do frete do vendedor), menos aquisição confirmada. Os parâmetros de logística, modalidade e condição acompanham o anúncio. Campos ausentes, moeda incompatível e cotações com mais de 24 horas impedem exibir uma sobra confirmada. As cotações da sincronização guardam o contexto do anúncio e preço; simulações não substituem a cotação do preço atual. O cache de 30 segundos sempre confere antes a propriedade e o contexto atual do anúncio.

As consultas externas são GET pelo servidor; as credenciais continuam protegidas. Uma meta salva é apenas uma preferência local. Não há mudança de preço no marketplace. A rota de cotação exige login e limita consultas a 30 por minuto.

Financeiro mostra componentes efetivos de pedidos, deduplicando pagamentos e fretes compartilhados. O frete do vendedor usa senders[].cost. Não presumimos que order.taxes.amount seja imposto descontado do vendedor: esse valor pode compor o pagamento do comprador. Retenções, descontos e outros ajustes não informados pela cotação ficam sem confirmação; não são inventados. A sobra estimada não comprova crédito bancário.

Documentação oficial:
- https://developers.mercadolivre.com.br/pt_br/comissao-por-vender
- https://developers.mercadolivre.com.br/pt_br/produto-consulta-de-usuarios/custos-de-envio
- https://developers.mercadolivre.com.br/pt_br/imovel-gerenciamento-de-pacotes/mercado-envios-custos-e-cotacoes
- https://developers.mercadolivre.com.br/pt_br/gerenciamento-de-vendas
