# Recuperação local do acesso

A senha do operador é armazenada como hash com salt; a senha antiga não pode ser mostrada. Para definir uma nova senha, o proprietário da máquina pode abrir Recuperar-acesso.cmd na pasta da instalação com o Docker ligado, ou executar `docker compose exec api python -m app.operator_recovery` em um terminal nessa pasta.

A ferramenta exige terminal interativo. A senha é digitada duas vezes, sem eco na tela, e não deve ser passada por argumento, arquivo ou chat. Exige as mesmas regras de criação de senha. A operação troca apenas o hash do operador, encerra sessões antigas, limpa contadores de tentativas e registra auditoria sem a senha. Produtos, vendas, tokens OAuth e configurações não são modificados. A recuperação não é disponibilizada por HTTP nem pelo túnel público. Controle o acesso ao computador e ao Docker: eles já permitem administrar o banco local.
