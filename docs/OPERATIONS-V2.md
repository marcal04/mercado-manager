# Operação local sem IA paga

Atualizações posteriores: [Central da API](DATA-HUB.md), [complementação da API e financeiro](API-COMPLETION.md) e [sincronização automática](AUTO-SYNC.md).

## Recursos disponíveis
- Visão geral: catálogo, pedidos em aberto, receita bruta local e alertas por regras.
- Catálogo: importação manual dos anúncios existentes e cadastro de produtos locais.
- Custos: custo unitário, tarifa percentual, impostos percentuais, frete por unidade, meta de margem e estoque mínimo.
- Estoque físico: entradas e saídas auditadas, separado da quantidade importada do Mercado Livre. A primeira contagem começa em zero.
- Pedidos manuais: pendente → confirmado → enviado → concluído. Pode cancelar pendentes ou confirmados. Confirmar baixa estoque; cancelar confirmado devolve. Repetir a transição é bloqueado. Devoluções após envio ainda não estão implementadas.
- Campanhas: planejamento com produto, objetivo, orçamento e aprovação simulada. Não há criação ou cobrança no Mercado Livre.
- Propostas manuais de preço: exigem custos cadastrados; mostram margem estimada, risco e premissas. Aprovar não muda o preço.
- Relatório CSV do catálogo, eventos de auditoria e movimentações recentes.
- Assistente local por regras, sem chamadas à OpenAI quando AI_ENABLED=false.
- Senha compartilhada opcional com cookie de sessão de 12 horas.

## Atualizar a instalação existente
Na pasta original do projeto:

```powershell
docker compose up -d --build
```

Abra http://localhost:8000 e use Ctrl+F5. As tabelas novas são criadas no início, sem modificar as tabelas anteriores. O volume PostgreSQL e as credenciais existentes são preservados. Não execute `docker compose down -v`.

## Primeira operação
1. Importe seus anúncios em Catálogo e margem.
2. Cadastre custos conferidos de cada produto. O cálculo é uma estimativa, não uma consulta às tarifas oficiais.
3. Registre a contagem inicial do estoque físico com entrada positiva e seu nome.
4. Cadastre um pedido manual com referência única. Confirme para baixar estoque local.
5. Crie um rascunho de campanha ou uma proposta de preço. Revise em Aprovações.
6. Confira o Histórico e baixe o relatório.

## Cálculos
Resultado unitário = preço × (1 − tarifa% − imposto%) − custo − frete.
Margem = resultado / preço × 100.
Preço para meta = (custo + frete) / (1 − tarifa% − imposto% − margem desejada%).
São usados valores decimais para custos e pedidos. Preços importados conservam o modelo anterior. Devoluções, juros, descontos, publicidade e taxas fixas não fazem parte deste cálculo; inclua custos fixos por unidade no custo, quando aplicável. Receita local inclui pedidos confirmados, enviados e concluídos; não significa valor recebido ou conciliado.

## Acesso ao painel

O login de operador único é obrigatório na configuração padrão. Crie o acesso local pela página de entrada. Senhas usam scrypt com salt, e sessões e tentativas têm os controles descritos em PRIVATE-ACCESS.md. O campo legado APP_ACCESS_TOKEN não é necessário para criar o operador pelo painel. Não desative a autenticação em uma instalação operacional. O modo sem senha da demonstração usa somente uma base temporária e provedores simulados, em processo separado.

Para HTTPS por túnel ou proxy, configure a origem pública no .env privado conforme HTTPS-LOGIN.md. As rotas de callback OAuth e notificações têm tratamento próprio. Cada mudança real de anúncio exige a revisão da área Gerenciar anúncios; propostas antigas de campanhas continuam simuladas.

## IA por último
AI_ENABLED=false é o padrão e não depende de apagar a chave existente. Ativação paga, tratamento completo de falhas do provedor e integração contextual serão feitos após a etapa operacional.

## Escopo ainda pendente
Esta entrega amplia a operação local, mas não é uma versão completa de produção. Já foram adicionados pedidos reais, leitura financeira por componentes, sincronização agendada e fila de notificações. Restam: conciliação de repasses do Mercado Pago, pesquisa confiável de concorrentes, ativação e validação das notificações reais no DevCenter, contas e papéis individuais, migrações versionadas, backup/restauração automatizados, monitoramento e implantação de produção. Publicação de anúncios, atualização de preços, anúncios pagos e compras exigirão autorização explícita futura. A IA será a última integração.

## Validação
Testes em SQLite isolado verificam margem, estoque, transições e cancelamento de pedidos, campanhas simuladas, decisão única, CSV e acesso. Bloqueios concorrentes usam SELECT FOR UPDATE no PostgreSQL; concorrência real deve ser validada no PostgreSQL antes de uso por múltiplos operadores.
