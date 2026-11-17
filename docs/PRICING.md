# Simulador de preços e margem

Em Preços e margem, selecione um produto, confira seus custos e compare cenários. A tela permite cadastrar os custos pelo botão Conferir custos e margem. Não existem custos presumidos: sem cadastro, a simulação é bloqueada.

## Cenários
- Preço atual, descontos de 5% e 10%, piso para margem mínima.
- Preço livre para teste, opcional.
- Preço concorrente informado manualmente, opcional, com desconto de 0 a 20%. O cenário ajusta essa referência e usa o maior valor entre o resultado e o piso da margem. Não há coleta automática nem validação de modelo, condição, frete ou reputação do concorrente.

## Cálculos
Resultado = preço × (1 − tarifa percentual − imposto percentual) − custo − frete unitário.
Margem = resultado / preço.
Piso da margem = (custo + frete) / (1 − tarifa − imposto − margem mínima).
Equilíbrio = (custo + frete) / (1 − tarifa − imposto).
O piso e o equilíbrio são arredondados para cima em centavos. O desconto máximo sobre o preço atual é arredondado para baixo em duas casas. Cenários com resultado negativo ou margem abaixo da meta aparecem com alerta. A verificação usa o cálculo integral, mesmo se a margem exibida arredondar para o valor da meta.

Valores em BRL por unidade. Publicidade, devoluções, juros, taxas fixas adicionais e custos não cadastrados não são considerados. O resultado não é uma promessa de lucro ou de vendas.

## Aprovações
Cenários que mantêm a meta podem gerar uma proposta detalhada com responsável, cálculo, benefícios, riscos e premissas. Cenários abaixo da meta e cenários sem mudança de preço não geram propostas por esta tela. Se os custos ou preço atual mudarem, é necessário simular novamente. O histórico preserva as premissas originais. Repetir o mesmo cenário com proposta pendente é bloqueado no uso sequencial.

A aprovação continua simulada: não existe publicação de preços no Mercado Livre. A proposta guarda as premissas do cálculo para revisão; uma execução futura deverá revalidá-las novamente no momento de publicar. As propostas manuais antigas do catálogo continuam disponíveis e mostram seus próprios riscos; não usam o bloqueio de margem deste simulador.

## Verificação
Testes de fronteira de centavos, comparação manual abaixo do piso, proteção por margem, custos alterados, validação de entradas e preservação de preço/estoque após aprovação. Nenhum cálculo utiliza OpenAI ou chamada externa.
