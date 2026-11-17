# Backup e recuperação local

## Operação
O serviço backup inicia com docker compose up -d --build. Faz uma cópia inicial e outra a cada 24 horas enquanto o Docker estiver ligado. O agendamento considera a última cópia concluída em disco e continua após reiniciar. Falhas são tentadas novamente após 15 minutos. As cópias são mantidas em backups/data, fora da imagem e do Git, sem remoção automática nesta versão. Acompanhe o espaço em disco conforme a base crescer.

Em Ajustes → Backup e recuperação, o painel mostra a última cópia, a próxima execução, quantidade de tabelas e registros e o último teste de restauração. O painel distingue o teste da cópia mais recente do teste de uma cópia anterior. Uma cópia com mais de 26 horas é marcada como antiga. Não existe rota de download de arquivos, de chave ou de restauração do banco principal.

## Conteúdo e proteção
pg_dump 16 cria um arquivo custom comprimido de todas as tabelas da aplicação. Contagens e dump compartilham uma transação com snapshot exportado, evitando divergência quando uma sincronização acontece durante a cópia. O pacote contém o dump e um manifesto com contagens e SHA-256. O pacote inteiro é cifrado com AES-256-GCM; a chave é derivada da APP_ENCRYPTION_KEY por HKDF com contexto próprio. Tokens e snapshots da aplicação continuam cifrados dentro do dump. Nenhuma credencial é impressa em logs ou gravada no manifesto.

Arquivos só recebem o nome final após conclusão. Em restauração, a autenticação completa do GCM termina antes de abrir o conteúdo. Pacotes alterados, truncados e chaves erradas são rejeitados; saídas temporárias são removidas. A extração aceita somente os dois arquivos fixos do pacote, recusando caminhos arbitrários e links.

A cópia é local: não protege contra perda do computador inteiro. A APP_ENCRYPTION_KEY e as demais configurações do .env não estão incluídas no arquivo de backup; preserve-as separadamente em local seguro. Perder essa chave impede abrir o backup e os dados cifrados da aplicação. Não envie a chave pelo chat. Não há sincronização com nuvem nesta entrega.

## Testar uma recuperação
Na pasta da instalação:

```powershell
docker compose --profile maintenance up -d --wait restore-check
docker compose run --rm --no-deps backup verify
docker compose --profile maintenance rm -s -f restore-check
```

O serviço restore-check usa PostgreSQL 16 sem porta publicada e armazenamento temporário em memória. O comando verify usa exclusivamente esse destino e exige um banco vazio; não oferece argumento para apontar para o banco principal. Ele restaura numa transação, compara a contagem de cada tabela com o manifesto e confere a decifragem dos tokens e snapshots restaurados. A remoção do serviço de teste descarta somente esse banco temporário, sem tocar no volume postgres_data.

Um novo teste exige um restore-check novo e vazio. Se o banco temporário já tiver tabelas, a execução é recusada, sem apagá-las. O estado de falha fica visível no painel até um novo teste bem-sucedido.

Para criar uma cópia extra, use docker compose run --rm --no-deps backup once. Não modifique o .env nem regenere a chave durante a recuperação. Restaurar no banco principal exige um procedimento separado de manutenção e revisão do destino; esta entrega fornece o arquivo recuperável e a verificação isolada, sem substituir automaticamente dados ativos.

## Validação
Testes isolados verificam cifragem em blocos, chave incorreta, alteração, truncamento, preservação de arquivos existentes, bloqueio de extração insegura, destino de teste fixo e resumo de saúde sem segredos. A instalação conectada também passa por uma cópia e uma restauração reais no serviço temporário.

Referências oficiais:
- https://www.postgresql.org/docs/16/app-pgdump.html
- https://www.postgresql.org/docs/16/app-pgrestore.html
- https://cryptography.io/en/latest/hazmat/primitives/symmetric-encryption/

A verificação exige RESTORE_CHECK_PASSWORD no .env privado. O Compose fornece o mesmo valor ao banco temporário e ao worker; não existe senha fixa alternativa. tools/setup_local.py gera esse valor para instalações novas. Não recrie o .env de uma instalação existente.
