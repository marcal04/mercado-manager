# Sincronização do catálogo e arquivamento

A importação pelo Catálogo e a sincronização completa/agendada reutilizam o mesmo reconciliador. Ele percorre a busca de anúncios em modo scan com scroll_id, valida a conta proprietária, atualiza os anúncios recebidos e registra o estado no banco. A busca deve informar a contagem total; páginas repetidas, cursor ausente, mudança da contagem ou falhas ficam como consulta incompleta.

Anúncios com status closed, deleted ou sub_status deleted saem do catálogo operacional e vão para Anúncios arquivados. Anúncios pausados continuam no catálogo atual. A confirmação de um anúncio ativo novamente restaura o mesmo produto, sem duplicar o SKU. Não são removidos produtos, pedidos, custos, contagens físicas, propostas, auditoria ou snapshots históricos. Nomes dos produtos arquivados permanecem disponíveis nos pedidos e campanhas anteriores.

Ausência na busca, isoladamente, não é prova de exclusão. Depois de uma enumeração completa, anúncios anteriormente associados à conta e não retornados são consultados individualmente. Só uma resposta explícita de encerramento/exclusão, ou HTTP 404 de um anúncio já associado à conta, arquiva o registro. HTTP 403, erro de rede, resposta inválida ou busca incompleta preservam os produtos sem confirmação. Produtos locais e anúncios de outra conta não são arquivados pela ausência na busca.

A nova tabela marketplace_listing_states guarda estado, conta e data de conferência. O catálogo atual, seletores, estimativas de sobra, reposição, indicadores e contexto do assistente excluem arquivados. GET /api/v1/catalog/archived exibe o histórico com estado e data; é protegido pelo login existente. As exportações normais refletem o catálogo atual.

Testes cobrem exclusão/encerramento, ausência e 404 confirmado, pausa/reativação, preservação de histórico e estoque físico, páginas incompletas, mudanças de contagem, negação de acesso, falha de rede, conta diferente e uso do mesmo fluxo na importação manual. Todas as chamadas ao Mercado Livre continuam GET; o arquivamento é apenas local.
