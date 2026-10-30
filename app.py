
import os
import re
import secrets
import sqlite3
import unicodedata
from functools import wraps
from pathlib import Path

import pandas as pd
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from jinja2 import TemplateNotFound
from werkzeug.security import check_password_hash

from criar_usuarios import criar_banco, buscar_usuario, cadastrar_usuario

# APIs das empresas
from api_porto import porto
from api_sebastian import sebastian
from api_4log import api_4log

# Compatibilidade com os dois nomes possíveis do Blueprint da Flora.
from api_flora import flora as api_flora


# ==========================================================
# CONFIGURAÇÃO DO FLASK
# ==========================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "chave-temporaria-apenas-para-testes",
)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Em produção com HTTPS, defina COOKIE_SECURE=1.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
)

app.json.ensure_ascii = False


# ==========================================================
# REGISTRO DAS APIs
# ==========================================================

app.register_blueprint(porto)
app.register_blueprint(sebastian)
app.register_blueprint(api_4log)
app.register_blueprint(api_flora)


# Cria o banco de usuários se ainda não existir.
criar_banco()


# ==========================================================
# EMPRESAS
# ==========================================================

EMPRESAS = {
    "4log": "4Log",
    "porto": "Porto-Brasil",
    "sebastian": "Sebastian",
    "flora": "Flora",
    "lego": "Lego Education",
}


# ==========================================================
# CAMINHOS DOS ARQUIVOS
# ==========================================================

CAMINHO = Path(__file__).resolve().parent
PASTA_EXCEL = CAMINHO / "xlsx"
PASTA_DB = CAMINHO / "db"


# ==========================================================
# CONVERSÃO DE XLSX PARA SQLITE
# ==========================================================

def normalizar(nome):
    """Normaliza nomes de colunas e abas para uso no SQLite."""
    nome = unicodedata.normalize("NFKD", str(nome))
    nome = nome.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", nome.lower()).strip("_")


def remover_acentos(texto):
    """Remove acentos de um texto."""
    return "".join(
        caractere
        for caractere in unicodedata.normalize("NFD", texto)
        if unicodedata.category(caractere) != "Mn"
    )


def remover_acentos_df(df):
    """Remove acentos das células textuais do DataFrame."""
    for coluna in df.select_dtypes(include="object").columns:
        df[coluna] = df[coluna].apply(
            lambda valor: remover_acentos(valor)
            if isinstance(valor, str)
            else valor
        )
    return df


def converter_xlsx():
    """
    Converte arquivos XLSX da pasta xlsx para bancos SQLite
    na pasta db.

    Atenção: as tabelas dos bancos gerados são substituídas
    quando a planilha de origem é mais recente.
    """
    PASTA_DB.mkdir(parents=True, exist_ok=True)

    if not PASTA_EXCEL.exists():
        print(f"[xlsx] Pasta não encontrada: {PASTA_EXCEL}")
        return

    for xlsx in PASTA_EXCEL.glob("*.xlsx"):
        if xlsx.name.startswith("~$"):
            continue

        destino = PASTA_DB / f"dados_{xlsx.stem}.db"

        if (
            destino.exists()
            and destino.stat().st_mtime >= xlsx.stat().st_mtime
        ):
            continue

        conn = None

        try:
            abas = pd.read_excel(xlsx, sheet_name=None)
            conn = sqlite3.connect(destino)

            for aba, df in abas.items():
                df.columns = [
                    normalizar(coluna)
                    for coluna in df.columns
                ]

                df = remover_acentos_df(df)
                nome_tabela = normalizar(aba)

                df.to_sql(
                    nome_tabela,
                    conn,
                    if_exists="replace",
                    index=False,
                )

            print(
                f"[xlsx] {xlsx.name} convertido "
                f"para {destino.name}"
            )

        except Exception as erro:
            print(f"[xlsx] Falha ao converter {xlsx.name}: {erro}")

        finally:
            if conn is not None:
                conn.close()


converter_xlsx()


# ==========================================================
# AUTORIZAÇÃO DE ADMINISTRADORES
# ==========================================================

def admin_required(funcao):
    @wraps(funcao)
    def wrapper(*args, **kwargs):
        if "usuario" not in session:
            return redirect(url_for("login"))

        if not session.get("admin"):
            abort(403)

        return funcao(*args, **kwargs)

    return wrapper


def empresas_permitidas():
    """
    A 4Log pode administrar usuários de todas as empresas.
    As demais empresas só podem administrar usuários da própria empresa.
    """
    if session.get("empresa") == "4log":
        return EMPRESAS.copy()

    empresa = session.get("empresa")

    return {
        chave: nome
        for chave, nome in EMPRESAS.items()
        if chave == empresa
    }


# ==========================================================
# PROTEÇÃO CSRF
# ==========================================================

def gerar_csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(32)

    return session["csrf"]


def csrf_valido():
    enviado = request.form.get("csrf", "")
    esperado = session.get("csrf", "")

    if not enviado or not esperado:
        return False

    return secrets.compare_digest(enviado, esperado)


app.jinja_env.globals["csrf_token"] = gerar_csrf


# ==========================================================
# LOGIN
# ==========================================================

@app.route("/")
def index():
    if "usuario" in session:
        return redirect(
            url_for(
                "dashboard",
                empresa_nome=session["empresa"],
            )
        )

    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        nome = request.form.get("usuario", "").strip()
        senha = request.form.get("senha", "")
        empresa_selecionada = request.form.get("clientes", "")

        if empresa_selecionada not in EMPRESAS:
            flash("Empresa inválida")
            return render_template(
                "login.html",
                empresas=EMPRESAS,
                cliente="4log",
            )

        usuario = buscar_usuario(nome)

        if usuario is None or not check_password_hash(
            usuario[1],
            senha,
        ):
            flash("Usuário ou senha inválidos")
            return render_template(
                "login.html",
                empresas=EMPRESAS,
                cliente=empresa_selecionada,
            )

        empresa_do_usuario = usuario[2]

        pode_entrar = (
            empresa_do_usuario == "4log"
            or empresa_do_usuario == empresa_selecionada
        )

        if not pode_entrar:
            flash("Este usuário não tem acesso a esta empresa")
            return render_template(
                "login.html",
                empresas=EMPRESAS,
                cliente=empresa_selecionada,
            )

        # Limpa dados antigos antes de iniciar uma sessão autenticada.
        session.clear()

        session["usuario"] = usuario[0]
        session["empresa"] = empresa_do_usuario
        session["admin"] = usuario[3]
        session["csrf"] = secrets.token_hex(32)

        return redirect(
            url_for(
                "dashboard",
                empresa_nome=empresa_selecionada,
            )
        )

    return render_template(
        "login.html",
        empresas=EMPRESAS,
        cliente="4log",
    )


# ==========================================================
# LOGOUT
# ==========================================================

@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ==========================================================
# DASHBOARDS DAS EMPRESAS
# ==========================================================

@app.route("/dashboard/<empresa_nome>")
def dashboard(empresa_nome):
    if "usuario" not in session:
        return redirect(url_for("login"))

    if empresa_nome not in EMPRESAS:
        abort(404)

    pode_ver = (
        session.get("empresa") == "4log"
        or session.get("empresa") == empresa_nome
    )

    if not pode_ver:
        abort(403)

    nome_template = f"dashboard_{empresa_nome}.html"

    try:
        return render_template(
            nome_template,
            empresa_nome=empresa_nome,
            nome_empresa=EMPRESAS[empresa_nome],
            usuario_logado=session["usuario"],
        )

    except TemplateNotFound:
        return (
            f"Dashboard {EMPRESAS[empresa_nome]} "
            "ainda não possui um template configurado."
        ), 404


# ==========================================================
# LANÇAMENTOS
# ==========================================================

def _render_lancamentos(empresa_nome):
    if "usuario" not in session:
        return redirect(url_for("login"))

    empresas_com_lancamentos = (
        "porto",
        "sebastian",
        "flora",
    )

    if empresa_nome not in empresas_com_lancamentos:
        abort(404)

    if session.get("empresa") not in ("4log", empresa_nome):
        abort(403)

    return render_template(
        f"lancamentos_{empresa_nome}.html",
        nome_empresa=EMPRESAS[empresa_nome],
        usuario_logado=session["usuario"],
        empresa_nome=empresa_nome,
    )


@app.route("/lancamentos")
def lancamentos():
    # Mantém o endereço antigo da Porto-Brasil.
    return _render_lancamentos("porto")


@app.route("/lancamentos/<empresa_nome>")
def lancamentos_empresa(empresa_nome):
    return _render_lancamentos(empresa_nome)


# ==========================================================
# ADMINISTRAÇÃO DE USUÁRIOS
# ==========================================================

@app.route("/admin/usuarios", methods=["GET", "POST"])
@admin_required
def admin_usuarios():
    permitidas = empresas_permitidas()

    if request.method == "POST":
        if not csrf_valido():
            abort(400)

        nome = request.form.get("nome", "").strip()
        senha = request.form.get("senha", "")
        confirmar = request.form.get("confirmar", "")
        empresa = request.form.get("empresa", "")
        admin = 1 if request.form.get("admin") else 0

        if not nome or not senha:
            flash("Preencha o nome e a senha", "erro")

        elif senha != confirmar:
            flash("As senhas não conferem", "erro")

        elif len(senha) < 8:
            flash(
                "A senha precisa ter pelo menos 8 caracteres",
                "erro",
            )

        elif empresa not in permitidas:
            flash(
                "Você não pode criar usuários para essa empresa",
                "erro",
            )

        else:
            try:
                cadastrar_usuario(
                    nome,
                    senha,
                    empresa,
                    admin,
                )
                flash(f"Usuário {nome} criado", "ok")

            except sqlite3.IntegrityError:
                flash(
                    "Esse nome de usuário já existe",
                    "erro",
                )

        return redirect(url_for("admin_usuarios"))

    tema = (
        session["empresa"]
        if session["empresa"] != "4log"
        else ""
    )

    return render_template(
        "admin_usuarios.html",
        empresas=permitidas,
        tema=tema,
    )


# ==========================================================
# ERROS HTTP
# ==========================================================

@app.errorhandler(403)
def acesso_negado(_erro):
    return "Você não tem permissão para acessar esta página.", 403


@app.errorhandler(404)
def pagina_nao_encontrada(_erro):
    return "Página não encontrada.", 404

if __name__ == "__main__":
    app.run(debug=True)