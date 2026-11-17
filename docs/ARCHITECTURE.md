# Arquitetura técnica

## Visão geral

A aplicação reúne uma interface estática, API FastAPI, persistência SQLAlchemy/PostgreSQL, integração OAuth e um executor separado para alterações aprovadas. O processo automático de leitura compartilha o processo da API; a configuração atual usa um único worker.

| Componente | Responsabilidade | Controle principal |
|---|---|---|
| Dashboard HTML/CSS/JavaScript | Consulta, formulários, análise e revisão | Não recebe credenciais dos serviços. |
| FastAPI | Rotas, validação e coordenação | Login privado e validação de origem. |
| PostgreSQL / SQLAlchemy | Dados, estados, limites e auditoria | Conta restrita após a configuração local. |
| MercadoLivreClient | OAuth, renovação serializada e consultas | Tokens cifrados; chamadas do servidor. |
| Data Hub / Auto Sync | Coleta, histórico e fila de notificações | Avisos não são prova de dados; provocam consultas autenticadas. |
| Catalog Sync | Reconciliação de anúncios | Confirma encerramento/exclusão, sem apagar histórico. |
| Listing Manager | Propostas e execução de alterações | Tipos permitidos, revisão exata, revalidação e serialização por vendedor. |
| AIService / ApprovalService | Assistente e propostas simuladas | Não executam as alterações reais do Listing Manager. |
| Backup | Cópias cifradas e restauração de teste | Destino temporário isolado para verificação. |

## Persistência

- products e marketplace_listing_states: catálogo e ciclo de vida dos anúncios.
- marketplace_connections e api_snapshots: autorização cifrada e cópias privadas recebidas da integração.
- marketplace_orders e marketplace_sync_runs: histórico normalizado e qualidade da importação.
- product_economics, product_stock_profiles e seller_margin_goals: custos confirmados e regras de margem.
- marketplace_listing_changes: propostas cifradas, validade, estado e resultado da execução real.
- approvals: propostas simuladas e decisões do fluxo anterior.
- audit_events: trilha das operações e decisões, sem credenciais.
- operator_account, operator_sessions, operator_session_state e authentication_budgets: acesso privado.

As áreas de financeiro, reposição e relatórios usam esses registros, distinguindo histórico da plataforma e controles locais. A divisão por conta existe em diversos recursos; o isolamento completo de todas as áreas para múltiplos vendedores ainda precisa de evolução e validação.

## Fluxos de leitura e escrita

A sincronização consulta a conta autorizada, registra cópias cifradas e atualiza o catálogo. Anúncios encerrados ou excluídos são arquivados, preservando produtos e referências históricas.

Na gestão real de anúncios, o operador prepara uma proposta com antes/depois. O servidor guarda o conteúdo cifrado, associa conta e recurso e calcula o hash de revisão. A aprovação revalida a proposta e a situação do anúncio antes do envio. O estado é persistido antes da requisição comercial para impedir repetições automáticas. Conferir resultado usa leitura do provedor.

O assistente local continua em um fluxo separado de propostas simuladas. IA autônoma, otimização de publicidade e testes de criativos não estão implementados.

## Demonstração do portfólio

O script tools/demo_preview.py cria SQLite temporário com dados fictícios, desativa escrita, IA e agendamento, e substitui clientes HTTP externos por um provedor em memória. Ele serve somente na interface de loopback e não usa a configuração nem o banco operacional. As imagens do README foram geradas nesse ambiente.

## Limites da implantação

A solução atual é de operador único e um processo API. Migrações versionadas, observabilidade de produção, cópias fora da máquina, gestão de usuários e implantação contínua são evoluções. O executor real depende da autorização da conta, dos escopos e das regras específicas de cada recurso do Mercado Livre.
