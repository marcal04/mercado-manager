# Complementação das integrações da API

## Descrições
Consulta a descrição do anúncio. Se houver 404 ou texto vazio, usa short_description da ficha oficial do catálogo vinculada ao anúncio. A interface identifica a origem como anúncio, catálogo ou ausente. O texto de catálogo não é apresentado como conteúdo personalizado do vendedor. HTML é convertido em texto; scripts e estilos não são exibidos.

## Reclamações
A busca verifica o vínculo dos participantes com a conta. A tela distingue reclamante e reclamado; um cancelamento de compra como comprador não é uma reclamação das vendas da loja. Os detalhes, histórico e resoluções esperadas são consultados apenas quando a API libera o recurso base. Respostas 401/403 são registradas como restrição; não existem tentativas de ampliar permissões ou contornar o bloqueio. O resumo permanece disponível quando autorizado.

## Publicidade
A central usa a versão 2 e os endpoints atuais de Product Ads, com métricas de campanhas, séries diárias, grupos de anúncios e anúncios dos grupos. Brand Ads e Display usam seus recursos de métricas atuais, inclusive palavras-chave de Brand Ads. A janela utilizada é dos últimos 30 dias completos, com datas no horário de São Paulo. Não são incluídos dados de hoje ainda não consolidados.

A área Dados Mercado Livre → Métricas das campanhas reais mostra impressões, cliques, custo, receita atribuída, ROAS e ACOS das séries recebidas por campanha. ROAS e ACOS não são calculados quando faltam dados ou o denominador é zero. Não se somam totais e séries diárias duplicadas. Métricas de grupos e palavras-chave ficam no explorador cifrado. A API só retorna anunciantes e métricas de produtos liberados para a conta; 404/403 não significa métricas iguais a zero. Nenhuma campanha é criada, ativada, alterada ou cobrada.

## Financeiro
A tela Financeiro agrupa ordens por pack e usa pagamentos aprovados identificados. Um mesmo pagamento dentro do pack é contado uma vez. Pagamentos ou envios compartilhados entre packs exigem rateio e não são duplicados. O subtotal só é calculado quando o bruto das ordens coincide com os pagamentos e os componentes obrigatórios estão presentes. Moedas divergentes e custos ausentes bloqueiam o cálculo. Cotações de tarifas não substituem comissões realizadas. Estornos/contestações impedem o status de componentes conferidos.

A conferência não comprova saldo recebido, repasse bancário ou lucro. Custo de aquisição, impostos próprios, publicidade e estornos não são abatidos do subtotal. Saldos e extratos completos do Mercado Pago exigem integração e autorização específicas; não são presumidos a partir do faturamento da plataforma.

O faturamento compara resumo e soma de detail_amount por detail_sub_type, tipo de cobrança/bonificação, grupo e período. IDs de detalhes repetidos não são somados novamente. Divergência maior que um centavo aparece para revisão. Se a sincronização de faturamento ficou incompleta ou um componente não possui dados suficientes, a tela mostra pendência. Resultado sem cobranças não é tratado como confirmação de repasse.

Também há leitura dos descontos das ordens e do recurso de pagamentos de documentos de faturamento, que representa pagamento de faturas da plataforma, não recebimentos de vendas. Esses detalhes ficam na central de registros. Recursos de faturamento continuam com cache diário e intervalo entre chamadas.

## Cobertura e validação
A central passa de 19 para 21 áreas: acrescenta métricas de publicidade e descontos/leitura financeira. Todos os conectores permanecem de leitura, exceto a renovação OAuth previamente autorizada. Testes usam respostas simuladas e banco isolado, incluindo descrição de catálogo, bloqueio de detalhes, rotas atuais de anúncios, duplicidade de pagamento/frete, valores ausentes, moedas distintas e conciliação de cobranças.

## Referências oficiais
- https://developers.mercadolivre.com.br/pt_br/product-ads-para-catalogo-e-user-products-leitura
- https://developers.mercadolivre.com.br/pt_br/ads-brand-ads
- https://developers.mercadolivre.com.br/pt_br/display-ads
- https://developers.mercadolivre.com.br/pt_br/gerenciar-reclamacoes
- https://developers.mercadolivre.com.br/pt_br/boas-praticas-para-o-consumo-das-apis-de-relatorios-de-faturamento
- https://developers.mercadolivre.com.br/pt_br/relatorios-de-faturamento/pagamentos
