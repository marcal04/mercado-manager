# Tarifa realizada em vendas com múltiplos pagamentos

A resposta da ordem pode informar marketplace_fee zero nos pagamentos e sale_fee positivo nos itens da venda. O Financeiro compara as duas representações em vez de interpretar isoladamente o zero como isenção.

A tarifa dos itens é por unidade e deve ser multiplicada pela quantidade, somando cada item de cada ordem uma única vez. Ela não é multiplicada pelo número de pagamentos. Quando pagamentos informam zero e os itens informam a tarifa completa, usa-se a tarifa dos itens. As duas fontes nunca são somadas. Se ambas são positivas e divergem, o resultado exige revisão e não conclui o líquido. Na ausência da tarifa dos itens, uma tarifa positiva completa dos pagamentos continua utilizável. Zero isolado nos pagamentos, sem tarifa dos itens verificável, fica pendente. Zero confirmado nos itens continua sendo zero.

A API de leitura financeira retorna fee_source, fee_sources e fee_notes para explicar a origem do valor. A tela mostra essa origem. O cálculo após tarifa e frete usa componentes realizados da ordem e do envio, sem substituir por uma cotação de anúncio nem comprovar repasse bancário. Packs, pagamentos e envios compartilhados continuam sujeitos às verificações de duplicidade e moeda existentes.

Referência oficial: https://developers.mercadolivre.com.br/pt_ar/provisoes
