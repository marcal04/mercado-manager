# Acesso privado de operador único

## Primeiro acesso
Abra http://localhost:8000 no computador da instalação. O painel encaminha para a criação do acesso. Escolha e confirme uma senha com pelo menos 12 caracteres, contendo letras, números e símbolos; não compartilhe a senha pelo chat. A criação inicial só é permitida pelo endereço local. Existe uma única conta de operador; depois de criada, o cadastro inicial é fechado. A senha é armazenada como hash scrypt com salt próprio (N=32768, r=8, p=3), nunca como texto aberto.

O banco, as integrações, as aprovações e o estoque são preservados. A senha protege as rotas do painel e da API, incluindo os detalhes cifrados. A sincronização e o backup em segundo plano continuam funcionando sem manter o navegador aberto.

## Sessões
O navegador recebe um identificador aleatório em cookie HttpOnly e SameSite=Strict, válido por até 12 horas, com encerramento após 30 minutos de inatividade. Somente a sessão mais recente fica ativa. O contexto do navegador é conferido pelo hash de User-Agent, sem armazenar o valor original. Um cookie roubado e copiado com o mesmo contexto continua sendo um risco; essa verificação é defesa adicional. O servidor guarda somente o hash desse identificador e verifica a expiração em cada acesso. Sair revoga a sessão no banco; reutilizar o cookie antigo não permite entrar. Em HTTPS, o cookie também usa Secure.

Há até 5 tentativas de entrada em 5 minutos por conta, endereço de conexão e cookie de pré-login válido. Os contadores são atômicos e persistidos no PostgreSQL: reiniciar o serviço ou trocar cookies/endereço não limpa o limite da conta. O bloqueio é temporário para evitar perda definitiva de acesso. Requisições anônimas a rotas privadas têm limite adicional de 30 por minuto e endereço. A instalação Compose usa um único processo API. Senhas e dados dos formulários de entrada não aparecem em mensagens de erro.

## Rede e navegador
O serviço API é publicado somente em 127.0.0.1:8000. A porta do PostgreSQL não é publicada no computador. Hosts e origens não autorizados são recusados. O hostname de retorno OAuth configurado é permitido para manter o callback existente, mas não pode criar a senha inicial. Não abra túneis públicos antes de concluir o primeiro acesso.

Respostas possuem proteção contra enquadramento, no-sniff, política de origem, restrição de scripts e cache desabilitado. HTTPS acrescenta HSTS e cookie Secure. Scripts do login usam nonce. A documentação da API fica desligada por padrão; mesmo habilitada, passa pelo controle de acesso. HTTPS externo, múltiplas contas, MFA e recuperação automática de senha não fazem parte desta etapa local.

## Configurações
AUTH_REQUIRED=true é o padrão. AUTH_REQUIRED=false é uma opção exclusiva de desenvolvimento controlado; uma senha ou conta existente continua exigindo entrada mesmo nesse modo. ALLOWED_HOSTS lista endereços aceitos sem wildcard. API_DOCS_ENABLED=false mantém a documentação interna desligada.

O modo antigo APP_ACCESS_TOKEN continua compatível quando já configurado no servidor. Não é necessário definir essa variável para criar a conta pelo painel. Credenciais antigas não são exibidas nem migradas para o frontend. A chave de cifragem da integração e a autorização do Mercado Livre permanecem as mesmas.

Se esquecer a senha, preserve o banco e os backups e solicite um procedimento local de recuperação. Não remova tabelas ou regenere a chave de cifragem para tentar recuperar acesso. As sessões e o hash de senha entram nas próximas cópias cifradas do banco.

## Validação
Testes isolados verificam criação única, hash sem texto aberto, entrada e saída, reutilização de cookie revogado, expiração, alteração de cookie, limitação de tentativas, origens e hosts, documentação fechada, cabeçalhos e leitura de detalhes apenas após entrar. Testes usam banco e senhas descartáveis, sem chamadas externas.

Referências:
- https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- https://docs.python.org/3.12/library/hashlib.html


## Troca de senha e banco
Ajustes → Trocar minha senha exige a senha atual e confirmação de uma nova senha com letras, números, símbolos e 12 ou mais caracteres. A troca gera novo salt/hash, revoga os cookies anteriores e emite uma nova sessão. A atualização não redefine senhas existentes automaticamente. Login, falhas e trocas são registrados sem senhas, cookies ou hashes nos eventos de auditoria.

A conta de conexão da aplicação usa mercado_app, com senha aleatória somente no .env da instalação e permissões de SELECT/INSERT/UPDATE/DELETE e uso das sequências. Não é superusuária, não cria tabelas, bancos nem funções administrativas. A conta administrativa permanece no serviço local de banco/manutenção e backup; não é utilizada como conexão normal da API.

Para atualizações que acrescentem tabelas, após reconstruir a imagem, execute python tools/database_security.py schema na pasta da instalação antes de iniciar a API com as tabelas novas. Esse helper usa o PostgreSQL local do Docker para aplicar apenas as tabelas ainda ausentes, compiladas dos modelos SQLAlchemy. Ele não migra alterações de colunas existentes; essas mudanças exigem migrações versionadas específicas. O comando restrict também cria a conta de aplicação e preserva as demais chaves do .env, sem exibir credenciais. Não o utilize para bancos remotos ou outros projetos.

## Escopo dos testes de segurança
Foram preparados testes de hash/salt, política de senha, tentativa simultânea, troca de cookie/IP/cliente, expiração, sessão única, alteração de senha e controles de acesso. Os testes de SQLi usam entradas de tautologia, UNION, instruções empilhadas e referência a espera temporal no login e em filtros, cookies e identificadores. O objetivo é confirmar que os dados são tratados como parâmetros/literais, sem autenticação indevida, ampliação de resultados ou alteração das tabelas.

O roteiro tools/postgres_security_check.py recusa o banco principal e só executa contra restore-check/restore_check, com dados fictícios. Ele também testa as rotas usando uma conta PostgreSQL sem privilégios administrativos e confirma a recusa de CREATE TABLE. Essas verificações cobrem casos automatizados; não representam um pentest profissional completo ou garantia de ausência de vulnerabilidades.
