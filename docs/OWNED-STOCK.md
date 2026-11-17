# Estoque próprio sem compra

Catálogo → Origem do estoque permite distinguir estoque próprio sem compra de revenda com reposição. O perfil é salvo separadamente dos custos de venda, com data e auditoria. O perfil de estoque próprio confirma custo de aquisição em zero; não inventa imposto, frete ou margem desejada. Não cria um cadastro completo de custos sem que esses componentes sejam informados.

A origem é configurada por produto e pode ser alterada se passar a haver aquisição para revenda. Novos produtos não herdam automaticamente o perfil dos anteriores.

Em Reposição, estoque próprio não exige fornecedor, prazo, demanda ou pedido mínimo e não gera proposta de compra. Políticas antigas são preservadas, mas ficam inativas enquanto a origem for própria. A quantidade anunciada e o estoque físico não são modificados. Estoque próprio não dispensa conferir disponibilidade antes de vender.

Em Custos e margem, aquisição confirmada e tarifa percentual estimada da API podem preencher o formulário. Demais valores desconhecidos ficam vazios, exigindo confirmação. Uma tarifa com componente fixo não é convertida silenciosamente em percentual. A margem só é calculada após cadastro completo de custos; é uma estimativa, não repasse bancário.

Mudar a origem atualiza somente o custo de aquisição nos custos completos existentes, preservando tarifa, frete, imposto, meta e estoque mínimo. Não é permitido informar aquisição positiva para o perfil sem compra. Para revenda com custos completos, o novo custo precisa ser informado. Custo zero explicitamente cadastrado também é válido para os cálculos de reposição de uma revenda; não há obrigação de custo estritamente positivo.

Testes cobrem zero confirmado sem completar despesas ausentes, bloqueio de compras para estoque próprio, preservação de despesas, mudança de origem, validações e cálculo de margem com aquisição zero.
