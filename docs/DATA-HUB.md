# Central de dados do Mercado Livre

## Uso
Abra Dados Mercado Livre e clique em Sincronizar dados da conta. A execução ocorre em segundo plano e a tela acompanha cada área. Pode interromper entre consultas. Não publica, não responde mensagens, não altera pedidos, não entra em promoções e não investe em campanhas. Mensagens são lidas com mark_as_read=false.

## Cobertura implementada
1. Conta e reputação: /users/me.
2. Anúncios e variações: varredura scan/scroll dos IDs, seguida de /items/{id}; atualiza nome, marca, preço e quantidade anunciada do catálogo local.
3. Descrições de anúncios.
4. Preços e condições por anúncio.
5. Cotação de tarifas com categoria, preço, tipo de anúncio e logística. O componente fixo já integra o total e não é somado novamente.
6. Cotação de envio grátis com contexto do anúncio.
7. Visitas por anúncio e vendedor, janela de 90 dias.
8. Perguntas e respostas, com paginação.
9. Pedidos de até 365 dias: busca dividida em janelas menores quando há mais de 1.000 resultados, detalhes individuais, cópia operacional e projeção na tabela de pedidos importados. Taxas realizadas são preservadas no detalhe da ordem.
10. Envios e rastreamento dos pedidos encontrados.
11. Custos efetivos de cada envio encontrado.
12. Mensagens dos packs/pedidos encontrados, sem marcá-las como lidas.
13. Reclamações da conta e seus detalhes. A listagem distingue papéis da conta quando fornecidos; podem existir registros como comprador. Resultados sem vínculo com a conta são rejeitados.
14. Devoluções, cancelamentos e trocas derivados das reclamações retornadas; não é um conector de logística reversa independente.
15. Promoções disponíveis e itens elegíveis.
16. Estoque Full dos inventory_id presentes nos anúncios e variações.
17. Categorias dos anúncios, seus atributos e fichas de produtos do catálogo vinculadas.
18. Períodos de faturamento ML/MP, documentos, resumos e detalhes consultáveis em JSON. Não gera nem baixa arquivos por endpoints de escrita.
19. Anunciantes PADS/BADS/DISPLAY e listagens de campanhas acessíveis. Estruturas não habilitadas pela conta aparecem como indisponíveis ou sem permissão. Esta versão ainda não aprofunda todas as métricas de anúncios publicitários ou criativos.

Esta cobertura não equivale a toda a superfície de APIs do ecossistema Mercado Livre/Mercado Pago. Recursos variam por país, conta, produto contratado e permissões. Saldos e operações financeiras do Mercado Pago não são presumidos a partir de autorização do Mercado Livre. Mídias, PDFs, etiquetas e anexos são mantidos como referências quando vierem na resposta; não há download binário indiscriminado. Os status por área mostram o que efetivamente foi obtido, sem afirmar que um recurso negado foi importado.

## Armazenamento e origem
Respostas JSON são guardadas em snapshots cifrados com a mesma chave local já usada nos tokens OAuth. Os campos de credenciais e instrumentos de pagamento conhecidos são descartados recursivamente. Cada registro identifica conta, área, chave, rota de origem e data da coleta. Nenhum payload completo ou credencial é colocado nos logs da aplicação. Detalhes privados só podem ser abertos depois de configurar APP_ACCESS_TOKEN e entrar no painel. Não modifique APP_ENCRYPTION_KEY sem uma migração das cópias cifradas.

Snapshots anteriores são preservados se um recurso falhar ou deixar de aparecer. O contador em cache inclui essas cópias, e o contador da tentativa representa registros consultados ou reutilizados do cache naquela execução. Observe as datas: a central não exclui registros históricos que tenham sumido da busca. Não é tempo real.

## Limites explícitos
- Busca de pedidos: até 365 dias conforme disponibilidade do provedor; janelas divididas recursivamente até um segundo. Se houver mais de 1.000 ordens numa janela indivisível, marca parcial.
- Listagens: scan/scroll até 1.000 páginas de 100 IDs; inclui itens retornados pela busca, não restaura anúncios eliminados pelo provedor.
- Coleções: até 10.000 resultados por chamada lógica, ou 9.950 para reclamações. Se atingir o limite, marca parcial. IDs repetidos sem avanço interrompem a coleta com sinalização parcial.
- Faturamento: até 2.400 períodos por grupo/tipo. A disponibilidade real é definida pelo Mercado Livre.
- Orçamento técnico de 20.000 consultas por execução. Ao atingir, marca pendências; não finge que a execução foi completa. 429 tem duas novas tentativas com espera progressiva e respeito a Retry-After (até 30 segundos). Faturamento tem intervalo mínimo de 6 segundos entre chamadas; cópias com menos de 24 horas são reutilizadas. Detalhes de faturamento usam from_id/last_id; documentos usam paginação por offset.
- 400 é falha de consulta/formato, 401/403 é acesso negado, 404 é indisponível; uma área pode ficar parcial se apenas algumas consultas funcionarem. Nenhuma permissão é ampliada automaticamente.
- Uma execução por conta. Este Compose usa um worker de API. Reiniciar interrompe uma execução e libera nova tentativa, sem apagar snapshots anteriores. Não implantar múltiplos workers sem substituir a recuperação de jobs por fila externa.

## Uso no planejamento
Reposição pode usar Mercado Livre (API) como estoque-base, sem preencher estoque físico manual. Isso usa a última cópia do anúncio da conta conectada e não altera a contagem física. A interface permite selecionar a contagem física se preferir. Os custos de aquisição, impostos próprios e condições do fornecedor continuam locais quando não vierem da API. A tarifa percentual pode preencher o formulário de custos se não houver valor local salvo e a cotação não tiver componente fixo. Não cria custo de aquisição ou imposto presumido. Cotações e dados realizados são distinguidos; valores ausentes nunca são preenchidos como se fossem zero conhecido.

## Referências oficiais
- https://developers.mercadolivre.com.br/itens-e-buscas
- https://developers.mercadolivre.com.br/pt_br/gerenciamento-de-vendas
- https://developers.mercadolivre.com.br/pt_br/comissao-por-vender
- https://developers.mercadolivre.com.br/mensagens-post-venda
- https://developers.mercadolivre.com.br/pt_br/gerenciar-reclamacoes
- https://developers.mercadolivre.com.br/pt_br/gerenciar-ofertas
- https://developers.mercadolivre.com.br/envios-fulfillment
- https://developers.mercadolivre.com.br/pt_br/boas-praticas-para-o-consumo-das-apis-de-relatorios-de-faturamento
- https://developers.mercadolivre.com.br/pt_br/product-ads-para-catalogo-e-user-products-leitura
