# Atualização automática — leitura da API

## Uso
Em Dados Mercado Livre → Atualização automática, escolha a frequência (1, 3, 6, 12 ou 24 horas) ou pause. O padrão é ativado a cada 6 horas. As preferências, a próxima execução e a fila ficam no PostgreSQL. O serviço deve estar ligado; o navegador pode estar fechado. A primeira execução ocorre no início, respeitando 15 minutos desde a última consulta manual ou automática. Salvar uma frequência agenda a próxima consulta a partir desse momento.

O painel mostra as 10 últimas sincronizações, conclusão e número de áreas com pendências. A consulta automática usa as mesmas 21 áreas da consulta manual, incluindo seus limites e cache diário de faturamento. Não publica anúncios, não modifica preços, não movimenta estoque e não usa IA paga.

## Notificações
O receptor POST /api/v1/webhooks/mercadolivre persiste o aviso e responde HTTP 200 sem fazer consultas externas na requisição. São aceitos apenas application_id configurado e user_id conectado, tópicos conhecidos e formatos permitidos de recurso. Dados recebidos são avisos não autenticados, não são prova de origem e não atualizam dados da operação. O sistema consulta a conta pela API com OAuth; nunca busca a URL fornecida no corpo. Não é usada uma assinatura inventada nem se confia no IP de cabeçalhos encaminhados.

Avisos repetidos são deduplicados por identificação, tópico e recurso. O corpo bruto e o recurso não são guardados; só o hash, tópico e metadados da fila. Limite de corpo: 16 KiB; até 10.000 avisos não concluídos. Avisos são agrupados numa consulta da conta inteira, no máximo a cada 15 minutos. Eventos que chegam durante uma consulta ficam aguardando a próxima, evitando perda. “Com consulta concluída” indica consulta da conta, não garantia de que todo recurso ficou disponível. As pendências individuais permanecem visíveis.

Para receber eventos reais, configure no aplicativo do Mercado Livre uma URL HTTPS pública estável apontando para /api/v1/webhooks/mercadolivre, e os tópicos necessários já autorizados: items, orders_v2, shipments, questions, claims, messages e user-products. A configuração no DevCenter não é realizada por esta entrega. Sem isso, funciona a atualização periódica. O painel informa quando nenhum aviso foi recebido, sem presumir que o cadastro externo está ativo. O callback OAuth é outro endereço e não deve ser usado para notificações.

## Recuperação
Uma única consulta da central pode executar por conta. Uma consulta manual ativa impede a automática; a restrição única do banco cobre a corrida entre ambas. Ao iniciar o serviço, consultas interrompidas são marcadas e reavaliadas. Se uma automática falhar ou for interrompida, avisos voltam a pendentes; a espera para nova tentativa cresce de 15 para 30, 60, 120, 240 e até 360 minutos. Novos avisos não furam essa espera. Conclusão com pendências de permissão segue a frequência normal, sem insistência rápida em recursos negados. A pausa não cancela uma consulta ativa; use Interromper para isso.

Esta versão requer um único processo API (a configuração Compose atual). Não execute múltiplas réplicas: a recuperação inicial de trabalhos pressupõe um único processo. AUTO_SYNC_WORKER_ENABLED=false desliga o processo automático, sem desabilitar as rotas manuais. Preferências da conta não substituem esse controle operacional. Não há captura automática de missed_feeds nesta etapa; a consulta periódica é a recuperação de atualizações não recebidas.

## Validação
Testes isolados cobrem deduplicação, dados estranhos à conta, URLs malformadas, limites de corpo, não sobreposição, agrupamento, eventos durante execução, pausa, falhas e retomada com sessões independentes. Respostas do provedor são simuladas; testes não acessam serviços externos. A consulta automática também é conferida na instalação local conectada.

Referências oficiais:
- https://developers.mercadolivre.com.br/produto-receba-notificacoes
- https://developers.mercadolivre.com.br/en_us/visits-resource/application-security
