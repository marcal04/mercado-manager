"""Local terminal-only operator password recovery. No HTTP recovery endpoint."""
import getpass
import sys
from .access import Operator,OperatorSession,hash_password,strong_password
from .auth_guard import SessionState,AuthBudget
from .db import SessionLocal
from .models import AuditEvent


def reset_operator(db,password):
    if not strong_password(password):
        raise ValueError("Use pelo menos 12 caracteres, com letras, números e símbolos.")
    account=db.query(Operator).filter_by(id=1).with_for_update().first()
    encoded=hash_password(password)
    if account:account.password_hash=encoded
    else:db.add(Operator(id=1,password_hash=encoded))
    db.query(OperatorSession).delete()
    db.query(SessionState).delete()
    # A recovery performed by the local machine owner starts a fresh attempt window.
    db.query(AuthBudget).delete()
    db.add(AuditEvent(event_type="security.password_recovered",entity_type="operator",entity_id="1",detail={"source":"local_terminal","sessions_revoked":True}))
    db.commit()


def main():
    if not sys.stdin.isatty():
        print("Abra esta recuperação em um terminal local interativo. Não envie senha por argumentos ou arquivos.",file=sys.stderr)
        return 1
    print("Redefinir o acesso do Mercado Manager neste computador.")
    print("Os produtos, vendas e credenciais do Mercado Livre serão preservados.")
    print("A senha não aparece enquanto você digita. Use 12 ou mais caracteres, letras, números e símbolos.")
    try:
        password=getpass.getpass("Nova senha: ")
        confirmation=getpass.getpass("Confirme a nova senha: ")
        if password!=confirmation:
            print("As senhas não coincidem. Nada foi alterado.")
            return 1
        with SessionLocal() as db:reset_operator(db,password)
    except ValueError as error:
        print(str(error));return 1
    except (EOFError,KeyboardInterrupt):
        print("Recuperação cancelada. Nenhuma redefinição foi concluída.");return 1
    except Exception:
        print("Não foi possível concluir. Confira se o Docker e o serviço do painel estão ligados. Nenhuma credencial foi exibida.",file=sys.stderr)
        return 1
    print("Nova senha salva. As sessões antigas foram encerradas. Entre no painel com a nova senha.")
    return 0

if __name__=="__main__":raise SystemExit(main())
