# Login por HTTPS via proxy ou túnel

Quando o navegador acessa HTTPS e o proxy encaminha HTTP ao serviço local, a origem vista pelo navegador difere do transporte interno. Configure PUBLIC_BASE_URL no .env privado com a origem HTTPS pública exata, sem caminho ou parâmetros. Exemplo fictício: https://shop.example.test.

Somente esse hostname e essa porta configurados usam a origem HTTPS externa para a validação de Origin/Referer e para cookies Secure e HSTS. Cabeçalhos X-Forwarded-Proto e X-Forwarded-Host não são usados por esse reconhecimento. Hosts, esquemas e portas diferentes continuam bloqueados. As restrições de Fetch Metadata permanecem ativas.

Os cookies de tentativa e de sessão continuam HttpOnly e SameSite=Strict. A senha, o hash, os limites de tentativas e a expiração das sessões não mudam. O login local em http://127.0.0.1:8000 continua usando a origem local HTTP. Ao mudar o domínio do túnel, atualize a configuração privada e recrie o serviço.

PUBLIC_BASE_URL não substitui o callback OAuth do Mercado Livre nem configura o DevCenter. São configurações distintas e devem ser revistas se o domínio externo mudar. O pacote público contém somente esse campo vazio; nunca inclua o endereço operacional nem seu .env no repositório.
