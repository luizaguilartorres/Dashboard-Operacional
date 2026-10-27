from werkzeug.security import generate_password_hash
import sqlite3
from pathlib import Path
import os

CAMINHO = Path(__file__).parent
(CAMINHO / "db").mkdir(exist_ok=True)
CAMINHO_BANCO = CAMINHO / "db" / "usuarios.db"


def limpar_tela():
    input("Pressione enter para continuar    ")
    os.system("cls" if os.name == "nt" else "clear")


def criar_banco():
    conn = sqlite3.connect(CAMINHO_BANCO)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usuarios(
    id_usuario INTEGER PRIMARY KEY AUTOINCREMENT,
    nome_usuario TEXT NOT NULL UNIQUE,
    senha_hash TEXT NOT NULL,
    empresa TEXT NOT NULL,
    admin INTEGER NOT NULL DEFAULT 0
    )
""")
    conn.commit()
    conn.close()


def cadastrar_usuario(nome, senha, empresa, admin):
    # A senha chega em texto puro: o hash é gerado AQUI (não gere de novo em quem chama).
    nome = nome.strip()
    empresa = empresa.strip().lower()  # "Porto " e "porto" viram a mesma empresa

    conn = sqlite3.connect(CAMINHO_BANCO)
    cursor = conn.cursor()

    senha_hash = generate_password_hash(senha)

    try:
        cursor.execute("""
        INSERT INTO usuarios (nome_usuario, senha_hash, empresa, admin) VALUES (?, ?, ?, ?)
        """, (nome, senha_hash, empresa, admin))
        conn.commit()  # grava de verdade no arquivo
    finally:
        conn.close()   # fecha mesmo se der erro (ex.: nome repetido)


def buscar_usuario(nome):
    conn = sqlite3.connect(CAMINHO_BANCO)
    cursor = conn.cursor()

    cursor.execute("SELECT nome_usuario, senha_hash, empresa, admin FROM usuarios WHERE nome_usuario = ?", (nome,))

    usuario = cursor.fetchone()
    conn.close()
    return usuario


def deletar_usuario(nome):
    conn = sqlite3.connect(CAMINHO_BANCO)
    cursor = conn.cursor()

    cursor.execute("DELETE FROM usuarios WHERE nome_usuario = ?", (nome,))
    conn.commit()
    conn.close()


def alterar_usuario(nome_alterado, nome):
    nome_alterado = nome_alterado.strip()

    conn = sqlite3.connect(CAMINHO_BANCO)
    cursor = conn.cursor()

    try:
        cursor.execute("""
        UPDATE usuarios
        SET nome_usuario = ?
        WHERE nome_usuario = ?
        """, (nome_alterado, nome))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    criar_banco()
    while True:
        print("===== MENU =====")
        print(
            "[1] Cadastrar usuário: \n"
            "[2] Buscar usuário \n"
            "[3] Excluir usuário\n"
            "[4] Alterar informações\n"
            "[5] Sair")
        opcao = input("Selecione uma opçao: ")

        if opcao == "1":
            print("===== CADASTRO =====")
            nome = input("Nome de usuario: ")
            senha = input("Senha: ")
            empresa = input("Empresa que trabalha: ")
            resposta = input("Administrador? [1] SIM [2] NÃO: ")
            admin = 1 if resposta == "1" else 0

            try:
                cadastrar_usuario(nome, senha, empresa, admin)
                print("Usuario cadastrado com sucesso")
            except sqlite3.IntegrityError:
                print("Usuario já existe")
            limpar_tela()

        elif opcao == "2":
            print("===== BUSCAR USUARIOS =====")
            nome = input("Nome de usuario: ")
            usuario = buscar_usuario(nome)

            if usuario is None:
                print("Usuario não encontrado")
            else:
                print(f"Nome: {usuario[0]}")
                print(f"Empresa: {usuario[2]}")
                print("Admin: Sim" if usuario[3] else "Admin: Não")
            limpar_tela()

        elif opcao == "3":
            print("===== EXCLUIR USUARIOS =====")
            try:
                nome = input("Nome de usuario: ")
                if buscar_usuario(nome) is None:
                    print("Usuario não encontrado")
                else:
                    confirmacao = input("Tem certeza? [S/N] ").lower()
                    if confirmacao == "s":
                        deletar_usuario(nome)
                        print("Usuario excluido com sucesso!!")
            except Exception as e:
                print(e)
            limpar_tela()

        elif opcao == "4":
            print("===== ALTERAR USUARIOS =====")
            nome = input("Nome de usuario: ")
            if buscar_usuario(nome) is None:
                print("Usuario não encontrado")
            else:
                nome_alterado = input("Novo nome: ")
                confirmacao = input("Tem certeza [S/N] ").lower()
                if confirmacao == "s":
                    try:
                        alterar_usuario(nome_alterado, nome)
                        print("Usuario alterado com sucesso")
                    except sqlite3.IntegrityError:
                        print("Esse nome já está em uso")
                else:
                    print("Operação cancelada")
            limpar_tela()

        elif opcao == "5":
            break

        else:
            print("Opção inválida! Escolha entre 1 e 5.")